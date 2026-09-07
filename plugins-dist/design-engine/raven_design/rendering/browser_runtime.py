"""Playwright capture runtime for browser-rendered artifacts."""

from __future__ import annotations

import time
from contextlib import suppress
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Error, Page

from raven_design.rendering.models import RenderError
from raven_design.rendering.pdf import image_difference

PROBE_TIMES_SECONDS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0)
_CLOCK_STEP_MS = 16
_READY_SETTLE_MS = 150
_MOTION_CHANGED_PIXEL_RATIO = 0.002
_MOTION_CHANNEL_DELTA = 0.001


INSTRUMENTATION = """
(() => {
  const state = { raf: 0, timers: 0, canvas: 0, mutations: 0 };
  Object.defineProperty(window, "__RAVEN_RENDER_PROBE__", { value: state });
  const originalRaf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = callback => {
    state.raf += 1;
    return originalRaf(callback);
  };
  for (const name of ["setTimeout", "setInterval"]) {
    const original = window[name].bind(window);
    window[name] = (...args) => {
      state.timers += 1;
      return original(...args);
    };
  }
  if (window.HTMLCanvasElement) {
    const originalGetContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(...args) {
      state.canvas += 1;
      return originalGetContext.apply(this, args);
    };
  }
  const installObserver = () => {
    if (!document.documentElement) return;
    new MutationObserver(entries => { state.mutations += entries.length; })
      .observe(document.documentElement, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true
      });
  };
  if (document.documentElement) installObserver();
  else document.addEventListener("DOMContentLoaded", installObserver, { once: true });
  let seed = 0x6d2b79f5;
  Math.random = () => {
    seed |= 0;
    seed = seed + 0x6d2b79f5 | 0;
    let value = Math.imul(seed ^ seed >>> 15, 1 | seed);
    value = value + Math.imul(value ^ value >>> 7, 61 | value) ^ value;
    return ((value ^ value >>> 14) >>> 0) / 4294967296;
  };
})();
"""

_CONTROL_ANIMATIONS = """
delta => {
  const control = window.__RAVEN_RENDER_TIMELINE__ ||= {
    animations: new WeakSet(),
    elapsed: 0
  };
  control.elapsed += delta;
  for (const animation of document.getAnimations({ subtree: true })) {
    if (!control.animations.has(animation)) {
      animation.pause();
      animation.currentTime = Math.max(0, Number(animation.currentTime) || 0);
      control.animations.add(animation);
    } else {
      animation.currentTime = Math.max(
        0,
        (Number(animation.currentTime) || 0) + delta
      );
      animation.pause();
    }
  }
  for (const svg of document.querySelectorAll("svg")) {
    if (typeof svg.pauseAnimations === "function") svg.pauseAnimations();
    if (typeof svg.setCurrentTime === "function") {
      svg.setCurrentTime(control.elapsed / 1000);
    }
  }
}
"""

_FRAME_STATE = """
() => {
  const visible = element => {
    if (!(element instanceof Element)) return false;
    if (typeof element.checkVisibility === "function" &&
        !element.checkVisibility({
          checkOpacity: true,
          checkVisibilityCSS: true
        })) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) <= 0.01) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      rect.right > 0 && rect.bottom > 0 &&
      rect.left < innerWidth && rect.top < innerHeight;
  };
  const elements = [...document.querySelectorAll("body *, svg *")]
    .filter(visible);
  const tokens = [];
  const walker = document.createTreeWalker(
    document.documentElement,
    NodeFilter.SHOW_TEXT
  );
  while (walker.nextNode()) {
    const text = walker.currentNode.textContent
      ?.replace(/\\s+/g, " ").trim();
    const parent = walker.currentNode.parentElement;
    if (!text || !parent || !visible(parent)) continue;
    const range = document.createRange();
    range.selectNodeContents(walker.currentNode);
    const rect = range.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    tokens.push(text.slice(0, 160));
    if (tokens.length >= 512) break;
  }
  const uniqueTokens = [...new Set(tokens)].sort();
  return {
    visible_element_count: elements.length,
    visible_text_chars: uniqueTokens.reduce(
      (total, token) => total + token.length,
      0
    ),
    visible_text_tokens: uniqueTokens
  };
}
"""

_RUNTIME_METADATA = """
async () => {
  const root = document.documentElement;
  const probe = window.__RAVEN_RENDER_PROBE__ || {};
  const animations = document.getAnimations({ subtree: true });
  const eventAttributes = [...document.querySelectorAll("*")]
    .reduce((count, node) => count + [...node.attributes]
      .filter(attr => /^on/i.test(attr.name)).length, 0);
  return {
    title: document.title || null,
    dom_node_count: document.querySelectorAll("*").length,
    scroll_width: Math.max(root?.scrollWidth || 0, document.body?.scrollWidth || 0),
    scroll_height: Math.max(root?.scrollHeight || 0, document.body?.scrollHeight || 0),
    animation_count: animations.length,
    playing_animation_count: animations
      .filter(item => item.playState === "running" || item.playState === "pending").length,
    video_count: document.querySelectorAll("video").length,
    audio_count: document.querySelectorAll("audio").length,
    canvas_count: document.querySelectorAll("canvas").length,
    event_attribute_count: eventAttributes,
    suspicious_text_tokens: (() => {
      const text = document.body ? document.body.innerText : "";
      const tokens = [];
      if (/\\bNaN\\b/.test(text)) tokens.push("NaN");
      if (/\\bundefined\\b/.test(text)) tokens.push("undefined");
      if (/\\bInfinity\\b/.test(text)) tokens.push("Infinity");
      if (text.includes("[object Object]")) tokens.push("[object Object]");
      return tokens;
    })(),
    opening_visual: await (async () => {
      const vw = window.innerWidth, vh = window.innerHeight;
      const clip = r => {
        const x1 = Math.max(0, r.left), y1 = Math.max(0, r.top);
        const x2 = Math.min(vw, r.right), y2 = Math.min(vh, r.bottom);
        return x2 > x1 && y2 > y1 ? { x1, y1, x2, y2, area: (x2 - x1) * (y2 - y1) } : null;
      };
      const visuals = [];
      for (const el of document.querySelectorAll("img, video, canvas, svg, picture, *")) {
        const tag = el.tagName.toLowerCase();
        let src = null;
        if (tag === "img") src = el.currentSrc || el.getAttribute("src");
        else if (tag === "video") src = el.currentSrc || el.getAttribute("poster") || "video";
        else if (tag === "canvas" || tag === "svg") src = tag;
        else {
          const bg = getComputedStyle(el).backgroundImage;
          const m = bg && bg !== "none" ? bg.match(/url\\(["']?([^"')]+)/) : null;
          if (m) src = m[1];
        }
        if (!src) continue;
        const box = clip(el.getBoundingClientRect());
        if (box) visuals.push({ el, url: String(src), src: String(src).split("/").pop().split("?")[0], box });
      }
      if (!visuals.length) return null;
      visuals.sort((a, b) => b.box.area - a.box.area);
      const top = visuals[0];
      const headline = [...document.querySelectorAll("h1, h2, [role=heading]")]
        .map(h => ({ h, box: clip(h.getBoundingClientRect()) }))
        .filter(x => x.box)
        .sort((a, b) => parseFloat(getComputedStyle(b.h).fontSize) - parseFloat(getComputedStyle(a.h).fontSize))[0];
      let overlap = null, region = null;
      if (headline) {
        const a = headline.box, b = top.box;
        const ix = Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1));
        const iy = Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1));
        overlap = a.area ? Math.round(100 * ix * iy / a.area) : 0;
        const frac = v => v === "center" ? 0.5 : (v === "left" || v === "top") ? 0
          : (v === "right" || v === "bottom") ? 1 : /%$/.test(v) ? parseFloat(v) / 100 : null;
        const bitmap = async () => {
          const tag = top.el.tagName.toLowerCase();
          const cs = getComputedStyle(top.el);
          if (tag === "img") {
            if (!top.el.naturalWidth || cs.objectFit !== "cover") return null;
            const p = cs.objectPosition.trim().split(/\\s+/);
            return { im: top.el, fx: frac(p[0]), fy: frac(p[1] || p[0]) };
          }
          if (tag === "video" || tag === "canvas" || tag === "svg") return null;
          if (cs.backgroundSize !== "cover") return null;
          const p = cs.backgroundPosition.trim().split(/\\s+/);
          const im = await new Promise(resolve => {
            const i = new Image();
            i.onload = () => resolve(i.naturalWidth ? i : null); i.onerror = () => resolve(null);
            setTimeout(() => resolve(null), 2000);
            i.src = top.url;
          });
          return im ? { im, fx: frac(p[0]), fy: frac(p[1] || p[0]) } : null;
        };
        const hit = overlap > 0 ? await bitmap() : null;
        if (hit && hit.fx !== null && hit.fy !== null) {
          try {
            const img = hit.im, r = top.el.getBoundingClientRect();
            const scale = Math.max(r.width / img.naturalWidth, r.height / img.naturalHeight);
            const offX = (img.naturalWidth * scale - r.width) * hit.fx, offY = (img.naturalHeight * scale - r.height) * hit.fy;
            const c = document.createElement("canvas");
            const w = 32, hgt = 32; c.width = w; c.height = hgt;
            const sx = (Math.max(a.x1, b.x1) - r.left + offX) / scale;
            const sy = (Math.max(a.y1, b.y1) - r.top + offY) / scale;
            const sw = ix / scale, sh = iy / scale;
            const ctx = c.getContext("2d");
            ctx.drawImage(img, sx, sy, Math.max(1, sw), Math.max(1, sh), 0, 0, w, hgt);
            const d = ctx.getImageData(0, 0, w, hgt).data;
            let sum = 0, sq = 0, n = 0;
            for (let i = 0; i < d.length; i += 4) { const l = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; sum += l; sq += l * l; n++; }
            const mean = sum / n, std = Math.sqrt(Math.max(0, sq / n - mean * mean));
            region = std < 6 ? "uniform" : "textured";
          } catch (e) { region = null; }
        }
      }
      return {
        src: top.src,
        viewport_fraction: Math.round(100 * top.box.area / (vw * vh)),
        headline_overlap: overlap,
        region_under_headline: region
      };
    })(),
    image_crops: [...document.querySelectorAll("img")].flatMap(img => {
      const box = img.getBoundingClientRect();
      const fit = getComputedStyle(img).objectFit;
      if (!img.naturalWidth || !img.naturalHeight || !box.width || !box.height) return [];
      if (fit !== "cover" && fit !== "none") return [];
      if (box.width * box.height >= 0.8 * window.innerWidth * window.innerHeight) return [];
      const scale = fit === "cover"
        ? Math.max(box.width / img.naturalWidth, box.height / img.naturalHeight)
        : 1;
      const shown = Math.min(box.width, img.naturalWidth * scale) * Math.min(box.height, img.naturalHeight * scale);
      const full = img.naturalWidth * scale * img.naturalHeight * scale;
      const cropped = full ? 1 - shown / full : 0;
      if (cropped < 0.1) return [];
      return [{
        src: (img.currentSrc || img.getAttribute("src") || "").split("/").pop(),
        natural: [img.naturalWidth, img.naturalHeight],
        box: [Math.round(box.width), Math.round(box.height)],
        object_fit: fit,
        cropped_fraction: Math.round(cropped * 100) / 100
      }];
    }),
    probe
  };
}
"""


def runtime_metadata(
    page: Page,
    *,
    ignore_instrumented_timers: bool = False,
) -> dict[str, Any]:
    metadata = page.evaluate(_RUNTIME_METADATA)
    if ignore_instrumented_timers:
        metadata.get("probe", {})["timers"] = 0
    return metadata


def open_page(
    context: BrowserContext,
    url: str,
    timeout_seconds: float,
    errors: list[str],
    *,
    deterministic_clock: bool = False,
) -> Page:
    page = context.new_page()
    page.set_default_timeout(round(timeout_seconds * 1000))
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Under the deterministic clock, exceptions raised inside
    # clock-driven rAF callbacks surface as console errors instead of
    # pageerror events, so both channels must be captured.
    page.on(
        "console",
        lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None,
    )
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.on("popup", lambda popup: popup.close())
    try:
        if deterministic_clock:
            page.clock.install(time=0)
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=round(timeout_seconds * 1000),
        )
        if response is not None and response.status >= 400:
            raise RenderError(
                "conversion_failed",
                "The local browser server returned an error.",
                details={"status": response.status},
            )
        _wait_ready(page, timeout_seconds, deterministic_clock)
        return page
    except RenderError:
        page.close()
        raise
    except Exception as exc:
        page.close()
        raise RenderError(
            "conversion_failed",
            "Chromium could not load the staged document.",
        ) from exc


def _wait_ready(
    page: Page,
    timeout_seconds: float,
    deterministic_clock: bool,
) -> None:
    page.wait_for_load_state(
        "domcontentloaded",
        timeout=round(timeout_seconds * 1000),
    )
    with suppress(Error):
        page.evaluate("() => document.fonts ? document.fonts.ready : true")
    if deterministic_clock:
        page.evaluate(_CONTROL_ANIMATIONS, 0)
    else:
        page.wait_for_timeout(_READY_SETTLE_MS)


def _advance_controlled_time(page: Page, delta_seconds: float) -> None:
    remaining = max(0, round(delta_seconds * 1000))
    while remaining:
        step = min(_CLOCK_STEP_MS, remaining)
        page.clock.run_for(step)
        page.evaluate(_CONTROL_ANIMATIONS, step)
        remaining -= step


def capture_probe(
    context: BrowserContext,
    url: str,
    probe_dir: Path,
    duration_seconds: float,
    timeout_seconds: float,
    errors: list[str],
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    times = sorted({min(value, duration_seconds) for value in PROBE_TIMES_SECONDS})
    page = open_page(
        context,
        url,
        timeout_seconds,
        errors,
        deterministic_clock=True,
    )
    probe_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    elapsed = 0.0
    try:
        for index, target_time in enumerate(times):
            wait = max(0.0, target_time - elapsed)
            if wait:
                _advance_controlled_time(page, wait)
            elapsed = target_time
            target = probe_dir / f"probe-{index:03d}.png"
            page.screenshot(path=str(target), animations="allow")
            paths.append(target)
        runtime = runtime_metadata(
            page,
            ignore_instrumented_timers=True,
        )
    finally:
        page.close()
    differences = [image_difference(first, second) for first, second in pairwise(paths)]
    return differences, runtime


_AUTOPLAY_SOURCE_SIGNALS = frozenset(
    {
        "smil",
        "css_animation",
        "request_animation_frame",
        "timer",
        "media",
        "web_animations",
        "canvas",
        "script",
    }
)


def classify_motion(
    mode: str,
    source_signals: list[str],
    differences: list[dict[str, float]],
    runtime: dict[str, Any],
) -> tuple[str, str, int]:
    changed = sum(
        item["changed_pixel_ratio"] >= _MOTION_CHANGED_PIXEL_RATIO
        or item["mean_absolute_channel_delta"] >= _MOTION_CHANNEL_DELTA
        for item in differences
    )
    if mode == "static":
        return "static_forced", "user_override", changed
    if mode == "dynamic":
        return "autoplay_dynamic", "user_override", changed
    if changed:
        return "autoplay_dynamic", "observed_pixels", changed
    probe = runtime.get("probe", {})
    if (
        runtime.get("playing_animation_count", 0)
        or runtime.get("video_count", 0)
        or runtime.get("audio_count", 0)
        or probe.get("raf", 0)
    ):
        return "autoplay_dynamic", "runtime_activity", changed
    source_signal_set = set(source_signals)
    # Any page that can run code or declares animation gets the full
    # timeline: probes miss slow or small motion (and the deterministic
    # clock defeats the rAF counter), and a missed animation costs a
    # whole review while extra frames cost seconds. Only a provably
    # inert source may collapse to a single frame.
    if _AUTOPLAY_SOURCE_SIGNALS & source_signal_set or runtime.get("animation_count", 0) or probe.get("timers", 0):
        return "autoplay_dynamic", "source_signals", changed
    if {"hover", "user_interaction"} & source_signal_set:
        return "interaction_required", "source_and_runtime_signals", changed
    return "static_observed", "observed", changed


_ACTION_SETTLE_MS = 250
_ACTION_LOCATOR_TIMEOUT_MS = 2000


def _apply_action(page: Page, item: dict[str, Any]) -> tuple[str, str]:
    if "click" in item:
        description = f"click {item['click']}"
        page.locator(item["click"]).first.click(timeout=_ACTION_LOCATOR_TIMEOUT_MS)
    elif "hover" in item:
        description = f"hover {item['hover']}"
        page.locator(item["hover"]).first.hover(timeout=_ACTION_LOCATOR_TIMEOUT_MS)
    elif "fill" in item:
        spec = item["fill"]
        description = f"fill {spec['selector']}"
        page.locator(spec["selector"]).first.fill(str(spec["value"]), timeout=_ACTION_LOCATOR_TIMEOUT_MS)
    elif "wait_ms" in item:
        description = f"wait {item['wait_ms']}ms"
        page.wait_for_timeout(item["wait_ms"])
    else:
        return str(item), "invalid"
    return description, "ok"


def replay_actions(
    context: BrowserContext,
    url: str,
    actions_dir: Path,
    actions: tuple[dict[str, Any], ...],
    timeout_seconds: float,
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay user actions on a real-clock page, one screenshot per step.

    Returns preview candidate records (a "before" frame plus one frame per
    action, each carrying the action description, its status, and the pixel
    change against the previous frame) and the closing runtime metadata so
    interaction-triggered defects (NaN readouts, errors) are still caught.
    """
    page = open_page(context, url, timeout_seconds, errors)
    actions_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    try:
        page.wait_for_timeout(_READY_SETTLE_MS)
        previous = actions_dir / "action-000.png"
        page.screenshot(path=str(previous), animations="allow")
        records.append(
            {
                "path": f"actions/{previous.name}",
                "kind": "action",
                "action": "before",
                "status": "ok",
            }
        )
        for index, item in enumerate(actions, start=1):
            try:
                description, status = _apply_action(page, item)
            except Exception as exc:  # noqa: BLE001 — locator timeouts and detached nodes must not abort the replay
                description = next(
                    (f"{key} {value}" for key, value in item.items() if key != "fill"),
                    str(item),
                )
                if "fill" in item:
                    description = f"fill {item['fill'].get('selector', '')}"
                status = f"failed: {type(exc).__name__}"
            page.wait_for_timeout(_ACTION_SETTLE_MS)
            shot = actions_dir / f"action-{index:03d}.png"
            page.screenshot(path=str(shot), animations="allow")
            difference = image_difference(previous, shot)
            records.append(
                {
                    "path": f"actions/{shot.name}",
                    "kind": "action",
                    "action": description,
                    "status": status,
                    "changed_pixel_ratio": round(difference["changed_pixel_ratio"], 5),
                }
            )
            previous = shot
        runtime = runtime_metadata(page)
    finally:
        page.close()
    return records, runtime


def timeline_appears_static(frame_paths: list[Path], samples: int = 5) -> bool:
    """True when evenly-sampled timeline frames show no visible pixel change.

    Sampling keeps the check cheap and, unlike adjacent-frame diffs, wide
    intervals still catch slow motion (small bodies drifting across a large
    canvas move too few pixels per frame to clear the thresholds).
    """
    if len(frame_paths) < 2:
        return False
    count = min(samples, len(frame_paths))
    step = (len(frame_paths) - 1) / (count - 1)
    picked = [frame_paths[round(index * step)] for index in range(count)]
    return all(
        diff["changed_pixel_ratio"] < _MOTION_CHANGED_PIXEL_RATIO
        and diff["mean_absolute_channel_delta"] < _MOTION_CHANNEL_DELTA
        for diff in (image_difference(first, second) for first, second in pairwise(picked))
    )


def capture_timeline(
    context: BrowserContext,
    url: str,
    frames_dir: Path,
    duration_seconds: float,
    fps: int,
    timeout_seconds: float,
    errors: list[str],
    *,
    deterministic_clock: bool,
) -> list[dict[str, Any]]:
    page = open_page(
        context,
        url,
        timeout_seconds,
        errors,
        deterministic_clock=deterministic_clock,
    )
    frames_dir.mkdir(parents=True, exist_ok=True)
    times = timeline_frame_times(duration_seconds, fps)
    started = time.monotonic()
    elapsed = 0.0
    timeline: list[dict[str, Any]] = []
    try:
        for index, target_time in enumerate(times):
            if deterministic_clock:
                _advance_controlled_time(page, target_time - elapsed)
                elapsed = target_time
            else:
                remaining = target_time - (time.monotonic() - started)
                if remaining > 0:
                    page.wait_for_timeout(round(remaining * 1000))
            path = frames_dir / f"frame-{index + 1:06d}.png"
            page.screenshot(path=str(path), animations="allow")
            timeline.append(
                {
                    "index": index + 1,
                    "timestamp_seconds": round(target_time, 6),
                    "duration_seconds": round(
                        times[index + 1] - target_time if index + 1 < len(times) else 0,
                        6,
                    ),
                    "path": path.name,
                    "_state": page.evaluate(_FRAME_STATE),
                }
            )
    finally:
        page.close()
    return timeline


def timeline_frame_times(duration_seconds: float, fps: int) -> list[float]:
    count = max(2, ceil(duration_seconds * fps) + 1)
    return [min(index / fps, duration_seconds) for index in range(count)]
