"""Browser adapter for rendering HTML and SVG artifacts."""

from __future__ import annotations

import functools
import gzip
import http.server
import re
import shutil
import threading
import urllib.parse
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any, Self

from playwright.sync_api import Browser, BrowserContext, sync_playwright

from raven.rendering.browser_runtime import (
    INSTRUMENTATION,
    capture_probe,
    capture_timeline,
    classify_motion,
    open_page,
    replay_actions,
    runtime_metadata,
    timeline_appears_static,
)
from raven.rendering.bundle import BundlePaths
from raven.rendering.models import (
    AdapterResult,
    Detection,
    RenderConfig,
    RenderError,
    RenderRequest,
)
from raven.rendering.pdf import PdfBackend, PyMuPdfBackend
from raven.rendering.util import command_version

_CHROMIUM_ARGS = (
    "--no-sandbox",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
)
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})


@dataclass(frozen=True)
class _BrowserResult:
    runtime_snapshot: dict[str, Any]
    differences: list[dict[str, float]]
    classification: str
    confidence: str
    changed_pairs: int
    preview_candidates: list[dict[str, Any]]
    frame_count: int
    pdf_reference_path: str
    pdf_fidelity: str
    timeline_mode: str


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class _LocalAssetServer:
    def __init__(self, root: Path) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BrowserAdapter:
    def __init__(
        self,
        config: RenderConfig,
        pdf_backend: PdfBackend | None = None,
    ) -> None:
        self.config = config
        self.pdf = pdf_backend or PyMuPdfBackend()

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
    ) -> AdapterResult:
        if not self.config.chrome_path:
            raise RenderError(
                "renderer_unavailable",
                "No Chromium executable is available.",
            )
        browser_root = bundle_root / ".worker" / "browser-root"
        relative_source = _stage_assets(
            source,
            request.asset_root,
            browser_root,
            self.config.max_asset_bytes,
        )
        if source.suffix.lower() == ".svgz":
            relative_source = _expand_svgz(
                browser_root,
                relative_source,
                self.config.max_asset_bytes,
            )
        blocked: list[str] = []
        errors: list[str] = []
        warnings: list[dict[str, Any]] = []
        viewport = _effective_viewport(
            request,
            detection,
            self.config.max_side_pixels,
        )
        _validate_capture_budget(
            viewport,
            request,
            self.config,
        )
        screen_dir = bundle_root / "screen"
        screen_dir.mkdir(parents=True)
        probe_dir = bundle_root / ".worker" / "probes"
        with _LocalAssetServer(browser_root) as server:
            url = _local_url(server.port, relative_source)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=self.config.chrome_path,
                    args=list(_CHROMIUM_ARGS),
                )
                try:
                    result = self._render_in_browser(
                        browser,
                        server.port,
                        url,
                        viewport,
                        bundle_root,
                        screen_dir,
                        probe_dir,
                        detection,
                        request,
                        blocked,
                        errors,
                        warnings,
                    )
                finally:
                    browser.close()
        if errors:
            warnings.append(
                {
                    "code": "browser_page_errors",
                    "message": "The document emitted browser page errors.",
                    "details": errors[:5],
                }
            )
        if blocked:
            warnings.append(
                {
                    "code": "external_resource_blocked",
                    "message": "External browser resources were blocked.",
                    "details": {"count": len(blocked), "urls": blocked[:10]},
                }
            )
        return AdapterResult(
            renderer={
                "adapter": "browser",
                "backend": "playwright-chromium",
                "engine_version": command_version(self.config.chrome_path),
                "playwright_version": _playwright_version(),
                "isolated": False,
                "network_policy": ("configured-external" if self.config.allow_network else "loopback-only"),
                "timeline_mode": result.timeline_mode,
            },
            rendered={
                **result.runtime_snapshot,
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "blocked_resource_count": len(blocked),
                "page_error_count": len(errors),
                "frame_count": result.frame_count,
                "duration_seconds": (request.capture_duration_seconds if result.frame_count else 0),
                "pdf_reference_path": result.pdf_reference_path,
                "pdf_fidelity": result.pdf_fidelity,
            },
            motion={
                "classification": result.classification,
                "confidence": result.confidence,
                "observed_ms": round(request.capture_duration_seconds * 1000),
                "signals": detection.motion_signals,
                "changed_frame_pairs": result.changed_pairs,
                "probe_differences": result.differences,
            },
            artifacts={},
            preview_candidates=result.preview_candidates,
            warnings=warnings,
        )

    def _render_in_browser(
        self,
        browser: Browser,
        port: int,
        url: str,
        viewport: tuple[int, int],
        bundle_root: Path,
        screen_dir: Path,
        probe_dir: Path,
        detection: Detection,
        request: RenderRequest,
        blocked: list[str],
        errors: list[str],
        warnings: list[dict[str, Any]],
    ) -> _BrowserResult:
        context = self._new_context(browser, port, blocked, viewport)
        try:
            runtime_snapshot = self._capture_snapshot(
                context,
                url,
                viewport,
                bundle_root,
                screen_dir,
                request,
                errors,
            )
            if request.actions:
                action_records, runtime_probe = replay_actions(
                    context,
                    url,
                    bundle_root / "actions",
                    request.actions,
                    self.config.browser_ready_seconds,
                    errors,
                )
                return self._action_result(
                    request,
                    runtime_snapshot,
                    runtime_probe,
                    action_records,
                    warnings,
                )
            if request.motion_mode == "static":
                differences: list[dict[str, float]] = []
                runtime_probe = runtime_snapshot
            else:
                differences, runtime_probe = capture_probe(
                    context,
                    url,
                    probe_dir,
                    request.capture_duration_seconds,
                    self.config.browser_ready_seconds,
                    errors,
                )
            classification, confidence, changed_pairs = classify_motion(
                request.motion_mode,
                detection.motion_signals,
                differences,
                runtime_probe,
            )
            preview_candidates: list[dict[str, Any]] = [
                {
                    "path": "screen/viewport.png",
                    "kind": "viewport",
                    "page": 1,
                }
            ]
            frame_count = 0
            pdf_reference_path = "screen/viewport.png"
            pdf_fidelity = "vector_or_text_preserving"
            timeline_mode = "static"
            if classification == "autoplay_dynamic":
                deterministic = not (runtime_probe.get("video_count", 0) or runtime_probe.get("audio_count", 0))
                _validate_capture_budget(
                    viewport,
                    request,
                    self.config,
                    include_timeline=True,
                )
                timeline = capture_timeline(
                    context,
                    url,
                    bundle_root / "motion" / "frames",
                    request.capture_duration_seconds,
                    self.config.motion_fps,
                    self.config.browser_ready_seconds,
                    errors,
                    deterministic_clock=deterministic,
                )
                frame_count = len(timeline)
                preview_candidates, pdf_reference_path = _timeline_candidates(
                    timeline,
                    request.capture_at_seconds,
                )
                self.pdf.image_to_pdf(
                    bundle_root / pdf_reference_path,
                    BundlePaths(bundle_root).document,
                )
                pdf_fidelity = "raster_snapshot"
                timeline_mode = "deterministic" if deterministic else "real_time"
                frame_paths = [bundle_root / "motion" / "frames" / item["path"] for item in timeline]
                if timeline_appears_static(frame_paths):
                    warnings.append(
                        {
                            "code": "animation_not_running",
                            "message": ("Script or animation signals are present, but the page never visibly changed."),
                            "details": [
                                "the page declares script/animation but its pixels did not visibly "
                                f"change across the {request.capture_duration_seconds:.1f}s capture — "
                                "if the piece is meant to animate, the animation is not running"
                            ],
                        }
                    )
            if classification == "interaction_required" or (
                request.motion_mode != "static" and {"hover", "user_interaction"} & set(detection.motion_signals)
            ):
                warnings.append(
                    {
                        "code": "interaction_required",
                        "message": ("Interactive behavior was detected, but no user actions were supplied for replay."),
                    }
                )
            _append_suspicious_text_warning(warnings, runtime_probe)
            return _BrowserResult(
                runtime_snapshot=runtime_snapshot,
                differences=differences,
                classification=classification,
                confidence=confidence,
                changed_pairs=changed_pairs,
                preview_candidates=preview_candidates,
                frame_count=frame_count,
                pdf_reference_path=pdf_reference_path,
                pdf_fidelity=pdf_fidelity,
                timeline_mode=timeline_mode,
            )
        finally:
            context.close()

    @staticmethod
    def _action_result(
        request: RenderRequest,
        runtime_snapshot: dict[str, Any],
        runtime_probe: dict[str, Any],
        action_records: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> _BrowserResult:
        _append_suspicious_text_warning(warnings, runtime_probe)
        unresponsive = [
            record
            for record in action_records
            if record.get("status") == "ok"
            and record.get("action") not in (None, "before")
            and not str(record.get("action", "")).startswith("wait ")
            and not record.get("changed_pixel_ratio")
        ]
        if unresponsive:
            warnings.append(
                {
                    "code": "action_no_visible_response",
                    "message": "Some replayed actions produced no visible change.",
                    "details": [
                        f"{record['action']} succeeded but the page did not visibly change — "
                        "the control looks wired to nothing"
                        for record in unresponsive[:5]
                    ],
                }
            )
        changed_pairs = sum(1 for record in action_records if record.get("changed_pixel_ratio"))
        return _BrowserResult(
            runtime_snapshot=runtime_snapshot,
            differences=[],
            classification="interaction_replay",
            confidence="user_actions",
            changed_pairs=changed_pairs,
            preview_candidates=action_records,
            frame_count=0,
            pdf_reference_path="screen/viewport.png",
            pdf_fidelity="vector_or_text_preserving",
            timeline_mode="actions",
        )

    def _capture_snapshot(
        self,
        context: BrowserContext,
        url: str,
        viewport: tuple[int, int],
        bundle_root: Path,
        screen_dir: Path,
        request: RenderRequest,
        errors: list[str],
    ) -> dict[str, Any]:
        page = open_page(
            context,
            url,
            self.config.browser_ready_seconds,
            errors,
        )
        try:
            if request.capture_at_seconds:
                page.wait_for_timeout(round(request.capture_at_seconds * 1000))
            viewport_path = screen_dir / "viewport.png"
            page.screenshot(path=str(viewport_path), animations="allow")
            snapshot = runtime_metadata(page)
            page.emulate_media(media="screen")
            page.pdf(
                path=str(BundlePaths(bundle_root).document),
                width=f"{viewport[0]}px",
                height=f"{viewport[1]}px",
                print_background=True,
                page_ranges="1",
                margin={
                    "top": "0",
                    "right": "0",
                    "bottom": "0",
                    "left": "0",
                },
            )
            return snapshot
        finally:
            page.close()

    def _new_context(
        self,
        browser: Browser,
        port: int,
        blocked: list[str],
        viewport: tuple[int, int],
    ) -> BrowserContext:
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            color_scheme="light",
            reduced_motion="no-preference",
            service_workers="block",
            accept_downloads=False,
        )
        context.add_init_script(INSTRUMENTATION)

        def route_request(route: Any) -> None:
            parsed = urllib.parse.urlparse(route.request.url)
            allowed = (
                parsed.scheme in _LOCAL_SCHEMES
                or parsed.scheme in {"http", "ws"}
                and parsed.hostname == "127.0.0.1"
                and parsed.port == port
                or self._external_request_allowed(parsed)
            )
            if allowed:
                route.continue_()
            else:
                blocked.append(route.request.url[:500])
                route.abort("blockedbyclient")

        def route_websocket(websocket: Any) -> None:
            parsed = urllib.parse.urlparse(websocket.url)
            allowed = (
                parsed.scheme == "ws"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == port
                or self._external_request_allowed(parsed)
            )
            if allowed:
                websocket.connect_to_server()
            else:
                blocked.append(websocket.url[:500])

        context.route("**/*", route_request)
        context.route_web_socket("**/*", route_websocket)
        return context

    def _external_request_allowed(self, parsed: urllib.parse.ParseResult) -> bool:
        if not self.config.allow_network or parsed.scheme not in {
            "http",
            "https",
            "ws",
            "wss",
        }:
            return False
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if not hostname:
            return False
        allowlist = self.config.network_allowlist
        if not allowlist:
            return True
        return any(
            hostname == allowed.lower().rstrip(".") or hostname.endswith(f".{allowed.lower().rstrip('.')}")
            for allowed in allowlist
        )


def _append_suspicious_text_warning(
    warnings: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> None:
    suspicious = runtime.get("suspicious_text_tokens") or []
    if suspicious:
        warnings.append(
            {
                "code": "suspicious_page_text",
                "message": ("The rendered page text contains values that usually indicate a computation bug."),
                "details": [
                    f"page text contains '{token}' — a computed value is rendering as {token}" for token in suspicious
                ],
            }
        )


def _timeline_candidates(
    timeline: list[dict[str, Any]],
    capture_at_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    snapshot = min(
        timeline,
        key=lambda item: abs(item["timestamp_seconds"] - capture_at_seconds),
    )
    reference = f"motion/frames/{snapshot['path']}"
    candidates = [
        {
            "path": f"motion/frames/{item['path']}",
            "kind": "frame",
            "at_ms": round(item["timestamp_seconds"] * 1000),
            "duration_seconds": item["duration_seconds"],
            "primary": item["path"] == snapshot["path"],
            "_state": item["_state"],
        }
        for item in timeline
    ]
    return candidates, reference


def _stage_assets(
    source: Path,
    asset_root: Path | None,
    target: Path,
    max_bytes: int,
) -> Path:
    resolved_source = source.resolve()
    root = (asset_root or source.parent).resolve()
    if root == Path(root.anchor):
        raise RenderError("path_not_allowed", "The asset root is too broad.")
    try:
        source_relative = resolved_source.relative_to(root)
    except ValueError as exc:
        raise RenderError(
            "path_not_allowed",
            "The source file is outside the allowed asset root.",
        ) from exc
    total = 0
    target.mkdir(parents=True)
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        total += candidate.stat().st_size
        if total > max_bytes:
            raise RenderError(
                "resource_limit_exceeded",
                "Local browser assets exceed the configured byte limit.",
                details={"limit": max_bytes},
            )
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
    staged_source = target / source_relative
    if not staged_source.is_file():
        raise RenderError(
            "unsafe_input",
            "The source could not be staged safely.",
        )
    return source_relative


def _expand_svgz(
    browser_root: Path,
    relative_source: Path,
    max_bytes: int,
) -> Path:
    staged_source = browser_root / relative_source
    expanded_source = staged_source.with_suffix(".svg")
    with gzip.open(staged_source, "rb") as compressed:
        payload = compressed.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise RenderError(
            "resource_limit_exceeded",
            "The expanded SVG exceeds the configured asset limit.",
        )
    expanded_source.write_bytes(payload)
    return expanded_source.relative_to(browser_root)


def _local_url(port: int, relative_source: Path) -> str:
    encoded = "/".join(urllib.parse.quote(part) for part in relative_source.parts)
    return f"http://127.0.0.1:{port}/{encoded}"


def _playwright_version() -> str:
    import importlib.metadata

    return importlib.metadata.version("playwright")


def _effective_viewport(
    request: RenderRequest,
    detection: Detection,
    max_side_pixels: int,
) -> tuple[int, int]:
    if detection.format != "svg":
        return request.viewport_width, request.viewport_height

    def number(value: Any) -> float | None:
        if value is None:
            return None
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", str(value))
        return float(match.group(1)) if match else None

    width = number(detection.metadata.get("width"))
    height = number(detection.metadata.get("height"))
    if width is None or height is None:
        view_box = detection.metadata.get("view_box")
        if view_box:
            values = re.split(r"[\s,]+", str(view_box).strip())
            if len(values) == 4:
                try:
                    width, height = float(values[2]), float(values[3])
                except ValueError:
                    width = height = None
    if width is not None and height is not None and 1 <= width <= max_side_pixels and 1 <= height <= max_side_pixels:
        return round(width), round(height)
    return request.viewport_width, request.viewport_height


def _validate_capture_budget(
    viewport: tuple[int, int],
    request: RenderRequest,
    config: RenderConfig,
    *,
    include_timeline: bool = False,
) -> None:
    probe_frames = 1 if request.motion_mode == "static" else 8
    timeline_frames = max(2, ceil(request.capture_duration_seconds * config.motion_fps) + 1) if include_timeline else 0
    pixels = viewport[0] * viewport[1] * (probe_frames + timeline_frames)
    if pixels > config.max_total_pixels:
        raise RenderError(
            "resource_limit_exceeded",
            "Browser capture frames exceed the total pixel limit.",
            details={
                "pixels": pixels,
                "limit": config.max_total_pixels,
            },
        )
