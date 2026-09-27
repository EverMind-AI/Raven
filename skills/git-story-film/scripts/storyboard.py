#!/usr/bin/env python3
"""Build the storyboard page: a storyline strip across the top, acts as bands and
every shot as wide as its duration, then the shots grouped by act, each with its still, card,
picture, camera, captions in every language side by side, sound and the commit it rests on.

Usage:
  python3 storyboard.py shots.json --out storyboard.html                     # text-only shot list
  python3 storyboard.py shots.json --frames stills --out storyboard.html     # storyboard with stills
  python3 storyboard.py shots.json --frames stills --embed --out send.html   # one sendable file
  python3 storyboard.py shots.json --js shots.js                             # also the shot data the studio reads

shots.json is a list of shots (the older beats.json format still works):
  {"time": "0:44", "dur": 16, "key": "0:52", "act": "Turning point - riso", "card": "Self-evolution · only winners stay",
   "picture": "five gates, three proof birds race", "camera": "wide, keeps drifting right",
   "lines": {"English": "I can swap out my own feathers."}, "sound": "press rhythm",
   "note": "#140 evolver", "shot": "44s.png"}
Only "time" (m:ss) and "picture" are needed. A shot lasts until the next one starts unless "dur"
(seconds) says otherwise; give the last shot a "dur" or pass --length. "key" (m:ss or seconds) is
the moment that best shows the shot; the studio draws its storyboard frame there (default: the
middle). Consecutive shots with the same "act" form one band. "shot" is a file name inside --frames
(frames.mjs shots writes names like 44s.png). Use " / " inside a caption to mark a line break.
Stills are linked relative to the page unless --embed inlines them as JPEG. --ui zh switches the
page labels to Chinese. --font links a .ttf; leave it out for a portable page. --js writes
window.SHOTS for engine/studio-ui.js; put it next to the film HTML. Runs on Python 3.8+.
"""

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import tempfile

UI = {
    "en": {
        "camera": "camera",
        "sound": "sound",
        "note": "commit",
        "nostill": "no still",
        "shots": "shots",
        "stats": "{n} shots · {t} · {k} acts",
        "line": "storyline",
    },
    "zh": {
        "camera": "镜头",
        "sound": "声音",
        "note": "提交",
        "nostill": "暂无画面",
        "shots": "个镜头",
        "stats": "{n} 个镜头 · {t} · {k} 幕",
        "line": "故事线",
    },
}
LANG = {"中文": "zh", "English": "en", "日本語": "ja", "한국어": "ko"}
TINTS = ["#e7cf8f", "#b9d3a8", "#a9c8e0", "#e2aaa2", "#c9b6df", "#eab784", "#9fd0c9", "#d6c3a0"]
TC = re.compile(r"(\d+):(\d\d(?:\.\d+)?)")


def embed(path, size=1280):
    """JPEG data URL for a still; uses macOS sips or ImageMagick when present, else the raw file."""
    tmp = tempfile.mktemp(suffix=".jpg")
    for cmd in (
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72", "-Z", str(size), path, "--out", tmp],
        ["magick", path, "-resize", f"{size}x", "-quality", "72", tmp],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            data = open(tmp, "rb").read()
            os.remove(tmp)
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except (OSError, subprocess.CalledProcessError):
            continue
    mime = "image/png" if path.endswith(".png") else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(open(path, "rb").read()).decode()


def stamps(s):
    """Seconds for each m:ss time code in s, in order ("0:44–1:00" gives [44, 60])."""
    return [int(m) * 60 + float(x) for m, x in TC.findall(s or "")]


def seconds(v):
    """A time given as seconds (3.5, "3.5") or as m:ss ("0:52"); None when missing or unreadable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return (stamps(str(v)) or [None])[0]


def clock(t):
    m, s = divmod(int(round(t)), 60)
    return f"{m}:{s:02d}"


def spans(shots, length):
    """(start, end) in seconds per shot, None where the JSON doesn't say."""
    starts = [(stamps(b.get("time")) or [None])[0] for b in shots]
    out = []
    for i, b in enumerate(shots):
        t0, tc = starts[i], stamps(b.get("time"))
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        if t0 is None:
            t1 = None
        elif b.get("dur"):
            t1 = t0 + float(b["dur"])
        elif len(tc) > 1:
            t1 = tc[1]
        elif nxt is not None:
            t1 = nxt
        elif length:
            t1 = length
        else:
            t1 = None
        out.append((t0, t1 if t1 is not None and t1 > t0 else None))
    return out


def main():
    ap = argparse.ArgumentParser(description="Build the storyboard page from shots.json.")
    ap.add_argument("shots")
    ap.add_argument("--frames", help="folder with the stills; leave out for a text-only shot list")
    ap.add_argument("--out", default="storyboard.html")
    ap.add_argument("--title", default="Storyboard")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--length", type=float, help="film length in seconds, sizes the last shot")
    ap.add_argument("--ui", choices=sorted(UI), default="en")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--font", default="", help="optional path to a .ttf used for the page text")
    ap.add_argument("--js", help="also write the shot data the studio reads (window.SHOTS) to this path")
    a = ap.parse_args()
    data = json.load(open(a.shots, encoding="utf-8"))
    shots, ui = (data["shots"] if isinstance(data, dict) else data), UI[a.ui]

    def rel(p):
        return os.path.relpath(p, os.path.dirname(os.path.abspath(a.out)))

    def esc(s):
        return html.escape(str(s or "")).replace(" / ", "<br>")

    # Timing: each shot's share of the strip is its duration; unknown durations get the median.
    sp = spans(shots, a.length)
    durs = [t1 - t0 if t1 is not None else None for t0, t1 in sp]
    known = sorted(d for d in durs if d)
    weight = [d or (known[len(known) // 2] if known else 1) for d in durs]
    left = [sum(weight[:i]) / sum(weight) * 100 for i in range(len(shots))]
    width = [w / sum(weight) * 100 for w in weight]

    def timed(i, j):
        return sp[i][0] is not None and sp[j][1] is not None

    def rng(ix):
        return f"{clock(sp[ix[0]][0])}–{clock(sp[ix[-1]][1])}" if timed(ix[0], ix[-1]) else ""

    runtime = clock(sp[-1][1] - sp[0][0]) if shots and timed(0, -1) else "?"

    groups, gi = [], []  # consecutive shots with the same act form one group
    for i, b in enumerate(shots):
        if not groups or groups[-1][0] != b.get("act", ""):
            groups.append([b.get("act", ""), []])
        groups[-1][1].append(i)
        gi.append(len(groups) - 1)

    def tint(g):
        return TINTS[g % len(TINTS)]

    stills, thumbs = [], []
    for i, b in enumerate(shots):
        src = thumb = None
        if a.frames and b.get("shot"):
            p = os.path.join(a.frames, b["shot"])
            if not os.path.exists(p):
                print(f"warning: shot {i + 1:02d}: no file {p}", file=sys.stderr)
            elif a.embed:
                src, thumb = embed(p), embed(p, 320)
            else:
                src = thumb = rel(p)
        stills.append(src)
        thumbs.append(thumb)

    bands = ""
    for g, (act, ix) in enumerate(groups):
        name = esc(act) or "&nbsp;"
        bands += (
            f'<a class="band" style="left:{left[ix[0]]:.3f}%;width:{sum(width[i] for i in ix):.3f}%;--c:{tint(g)}" '
            f'href="#act{g + 1}"><b>{name}</b><span>{rng(ix)}</span></a>'
        )
    cells = ""
    for i, b in enumerate(shots):
        img = f'<img src="{thumbs[i]}" alt="">' if thumbs[i] else ""
        tip = html.escape(" · ".join(x for x in (f"{i + 1:02d}", b.get("time", ""), b.get("card", "")) if x))
        cells += (
            f'<a class="cell" style="left:{left[i]:.3f}%;width:{width[i]:.3f}%;--c:{tint(gi[i])}" '
            f'href="#s{i + 1:02d}" title="{tip}">{img}<span>{i + 1:02d}</span></a>'
        )
    ticks = ""
    if shots and all(durs):
        t0, t1 = sp[0][0], sp[-1][1]
        step = 10 if t1 - t0 <= 180 else 30 if t1 - t0 <= 600 else 60
        t = (int(t0 // step) + 1) * step
        while t < t1:
            ticks += f'<span style="left:{(t - t0) / (t1 - t0) * 100:.3f}%">{clock(t)}</span>'
            t += step
    if ticks:
        ticks = f'<div class="ticks">{ticks}</div>'

    def row(i, b):
        tc = f"{clock(sp[i][0])}–{clock(sp[i][1])}" if timed(i, i) else b.get("time", "")
        dur = f'<span class="dur">{round(durs[i], 1):g} s</span>' if durs[i] else ""
        if not a.frames:
            still = ""
        elif stills[i]:
            still = f'<img src="{stills[i]}" alt="{i + 1:02d}" loading="lazy">'
        else:
            still = f'<div class="noimg">{ui["nostill"]}</div>'
        lines = ""
        for k, v in (b.get("lines") or {}).items():
            lang = f' lang="{LANG[k]}"' if k in LANG else ""
            lines += f"<div><h4>{html.escape(k)}</h4><p{lang}>{esc(v)}</p></div>"
        if lines:
            lines = f'<div class="lines">{lines}</div>'
        card = f'<p class="card">{esc(b["card"])}</p>' if b.get("card") else ""
        kv = "".join(f'<p class="kv"><b>{ui[f]}</b>{esc(b[f])}</p>' for f in ("camera", "sound", "note") if b.get(f))
        cls = "shot" if a.frames else "shot text"
        return (
            f'<article class="{cls}" id="s{i + 1:02d}">{still}<div class="meta">'
            f'<div class="head"><span class="n">{i + 1:02d}</span><span class="tc">{esc(tc)}</span>{dur}</div>'
            f'{card}<p class="pic">{esc(b.get("picture"))}</p>{lines}{kv}</div></article>'
        )

    sections = ""
    for g, (act, ix) in enumerate(groups):
        head = ""
        if act or len(groups) > 1:
            info = " · ".join(x for x in (rng(ix), f"{len(ix)} {ui['shots']}") if x)
            head = f'<h2><i style="--c:{tint(g)}"></i>{esc(act)}<span>{info}</span></h2>'
        sections += f'<section class="act" id="act{g + 1}">{head}{"".join(row(i, shots[i]) for i in ix)}</section>'

    face = f'@font-face{{font-family:"SB";src:url("{rel(a.font)}")}}' if a.font else ""
    sub = f'<p class="sub">{esc(a.subtitle)}</p>' if a.subtitle else ""
    stats = ui["stats"].format(n=len(shots), t=runtime, k=len(groups))
    page = f'''<!doctype html><html lang="{a.ui}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(a.title)}</title>
<style>{face}
:root{{--paper:#f4efe4;--ink:#2b2622;--lead:#7a7066;--gold:#b8861f;--red:#c4473a;--rule:#ddd4c6;--card:#fffaf0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 "SB",-apple-system,"PingFang SC","Hiragino Sans GB",sans-serif;font-variant-numeric:tabular-nums;padding:32px 16px 64px}}
header,nav,main{{max-width:1240px;margin:0 auto}}h1{{font-size:30px;margin:0 0 4px}}.sub,.stats{{margin:0;color:var(--lead)}}.stats{{font-size:13px;font-variant-numeric:tabular-nums}}
nav{{margin-top:20px;margin-bottom:30px;overflow-x:auto;padding-bottom:4px}}.track{{min-width:max(100%,calc({len(shots)} * 30px))}}
.bands,.cells,.ticks{{position:relative}}.bands{{height:42px}}.cells{{height:60px;margin-top:4px}}.ticks{{height:20px;margin-top:4px}}
.band,.cell{{position:absolute;top:0;bottom:0;text-decoration:none;color:var(--ink);overflow:hidden}}
.band{{display:flex;flex-direction:column;justify-content:flex-end;padding:0 6px 2px;border-top:5px solid var(--c);border-left:2px solid var(--paper);font-size:12px;line-height:1.35}}
.band b,.band span{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.band span{{color:var(--lead);font-variant-numeric:tabular-nums}}
.cell{{background:var(--c);border:1px solid var(--paper);border-radius:4px;display:grid;place-items:center;font-size:12px;font-weight:700}}
.cell img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}.cell span{{position:relative;background:#fffaf0d9;padding:0 4px;border-radius:3px}}
.cell:hover,.cell:focus-visible{{outline:2px solid var(--red);outline-offset:-2px}}
.ticks span{{position:absolute;transform:translateX(-50%);font-size:11px;color:var(--lead);font-variant-numeric:tabular-nums}}
.ticks span::before{{content:"";position:absolute;left:50%;top:-4px;height:4px;border-left:1px solid var(--lead)}}
main{{display:flex;flex-direction:column;gap:36px}}
.act h2{{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;font-size:20px;margin:0 0 4px;padding-bottom:6px;border-bottom:2px solid var(--ink)}}
.act h2 i{{width:14px;height:14px;border-radius:3px;background:var(--c);align-self:center}}.act h2 span{{font-size:13px;font-weight:400;color:var(--lead);font-variant-numeric:tabular-nums}}
.shot{{display:grid;grid-template-columns:minmax(0,520px) 1fr;gap:22px;align-items:start;padding:18px 0;border-bottom:1px solid var(--rule);scroll-margin-top:12px}}.shot.text{{grid-template-columns:1fr;padding:12px 0}}
.shot img,.noimg{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:4px;box-shadow:0 0 0 1px #0001}}.noimg{{display:grid;place-items:center;color:var(--lead);background:#0000000a}}
.shot:target .n{{color:var(--red)}}.head{{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline}}.n{{font-size:24px;font-weight:700}}
.tc{{color:var(--red);font-weight:700;font-variant-numeric:tabular-nums}}.dur{{color:var(--lead);font-size:13px}}
.card{{margin:6px 0;color:var(--gold);font-weight:700}}.pic{{margin:4px 0}}
.lines{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;background:var(--card);padding:10px 14px;border-radius:4px;margin:10px 0 8px}}
.lines h4{{margin:0 0 4px;font-size:12px;letter-spacing:.08em;color:var(--lead)}}.lines p{{margin:0}}
.kv{{margin:3px 0;font-size:14px}}.kv b{{display:inline-block;min-width:3.4em;margin-right:6px;font-size:12px;font-weight:600;letter-spacing:.06em;color:var(--lead)}}
@media (max-width:820px){{.shot{{grid-template-columns:1fr}}}}</style>
<header><h1>{html.escape(a.title)}</h1>{sub}<p class="stats">{stats}</p></header>
<nav aria-label="{ui["line"]}"><div class="track"><div class="bands">{bands}</div><div class="cells">{cells}</div>{ticks}</div></nav>
<main>{sections}</main></html>'''
    open(a.out, "w", encoding="utf-8").write(page)
    extra = f"{sum(1 for s in stills if s)} stills" if a.frames else "text only"
    print(f"{a.out}: {len(shots)} shots, {len(groups)} acts, {runtime}, {extra}")

    if a.js:
        rows = [
            {
                "n": i + 1,
                "start": sp[i][0],
                "end": sp[i][1],
                "key": seconds(b.get("key")),
                "act": b.get("act", ""),
                "card": b.get("card", ""),
                "lines": b.get("lines") or {},
            }
            for i, b in enumerate(shots)
        ]
        data = json.dumps({"title": a.title, "shots": rows}, ensure_ascii=False)
        open(a.js, "w", encoding="utf-8").write(
            f"// Written by storyboard.py from {os.path.basename(a.shots)}; engine/studio-ui.js reads it.\nwindow.SHOTS = {data};\n"
        )
        print(f"{a.js}: shot data for the studio")


if __name__ == "__main__":
    main()
