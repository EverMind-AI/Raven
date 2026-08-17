"""Light/dark theme detection and palette for the interactive CLI.

Dark values are the CLI's existing literals, so dark rendering is unchanged.
Light values are chosen for legibility on white (body text must clear WCAG AA
4.5:1); most are drawn from the TUI source of truth ``ui-tui/src/theme.ts`` (TS
and Python cannot share code), but the mapping is not 1:1:
  - accent / selected light use ``#935F00`` (theme.ts yellow ramp .700), not the
    brand accent ``#B87900`` (only 3.64:1 on white) -- the CLI uses accent as
    inline body text, not large glyphs.
  - border stays a gold accent (matching the CLI's gold panel borders), whereas
    theme.ts ``border`` is a neutral grey; separator reuses that grey ``#d0d7de``.

Detection is kept out of import time and runs on the first themed render (see
``onboard_commands._ThemedConsole``). Only truecolor values are provided;
rich/prompt_toolkit downgrade to 256/16 automatically.
"""

from __future__ import annotations

import os
import sys
from typing import Literal

Scheme = Literal["light", "dark"]

_LUMA_LIGHT_THRESHOLD = 0.6

PALETTE: dict[Scheme, dict[str, str]] = {
    "dark": {
        "accent": "#fbe23f",
        "text": "#FFF5EA",
        "heading": "white",
        "selected": "#c8a900",
        "border": "#c8a900",
        "muted": "#6c6c6c",
        "separator": "#444444",
        "disabled": "#585858",
        "error": "#ff5f5f",
    },
    "light": {
        "accent": "#935F00",
        "text": "#24201a",
        "heading": "#24201a",
        "selected": "#935F00",
        "border": "#935F00",
        "muted": "#57606a",
        "separator": "#d0d7de",
        "disabled": "#6e7681",
        "error": "#cf222e",
    },
}

_cache: Scheme | None = None


def _parse_hex(value: str) -> tuple[int, int, int] | None:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return None
    try:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except ValueError:
        return None


def _is_light_rgb(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return luma >= _LUMA_LIGHT_THRESHOLD


def _osc_reply_to_rgb(data: str) -> tuple[int, int, int] | None:
    # Terminal answers OSC 11 as e.g. "rgb:ffff/f5f5/eaea" (16-bit per channel)
    # or "#rrggbb"; tolerate a trailing BEL/ST.
    marker = "rgb:"
    idx = data.find(marker)
    if idx != -1:
        parts = data[idx + len(marker) :].strip().strip("\a\033\\").split("/")
        if len(parts) >= 3:
            try:
                return tuple(int(p[:2], 16) for p in parts[:3])  # type: ignore[return-value]
            except ValueError:
                return None
    hidx = data.find("#")
    if hidx != -1:
        return _parse_hex(data[hidx : hidx + 7])
    return None


def _can_probe() -> bool:
    if os.name != "posix":
        return False
    if os.environ.get("CI"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    # GNU screen neither answers OSC 11 nor swallows the query, so the probe
    # bytes echo as visible garbage; tmux defaults to TERM=screen-256color and
    # can't answer a bare query either. Both fall back to COLORFGBG/dark.
    if os.environ.get("TERM", "").startswith("screen"):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, OSError):
        return False


def _probe_osc11() -> tuple[int, int, int] | None:
    try:
        import select
        import termios
        import tty
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return None
    try:
        tty.setraw(fd)
        sys.stdout.write("\033]11;?\033\\")
        sys.stdout.flush()
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            return None
        raw = os.read(fd, 64).decode("ascii", "replace")
    except (OSError, ValueError):
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except termios.error:
            pass
    return _osc_reply_to_rgb(raw)


def _detect() -> tuple[Scheme, bool]:
    """Return (scheme, definitive). Non-definitive results (the dark fallback)
    are not cached, so a later call from an interactive path can re-detect."""
    override = os.environ.get("RAVEN_THEME", "").strip().lower()
    if override in ("light", "dark"):
        return override, True  # type: ignore[return-value]

    hint = os.environ.get("RAVEN_TERM_BACKGROUND", "")
    rgb = _parse_hex(hint) if hint else None
    if rgb is not None:
        return ("light" if _is_light_rgb(rgb) else "dark"), True

    if _can_probe():
        rgb = _probe_osc11()
        if rgb is not None:
            return ("light" if _is_light_rgb(rgb) else "dark"), True

    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        last = fgbg.split(";")[-1].strip()
        if last in ("7", "15"):
            return "light", True
        if last.isdigit():
            return "dark", True

    return "dark", False


def detect_scheme() -> Scheme:
    global _cache
    if _cache is not None:
        return _cache
    scheme, definitive = _detect()
    if definitive:
        _cache = scheme
    return scheme


def _style_str(color: str, *attrs: str) -> str:
    return " ".join([color, *attrs]) if attrs else color


def build_rich_theme(scheme: Scheme):
    from rich.theme import Theme

    p = PALETTE[scheme]
    return Theme(
        {
            "accent": p["accent"],
            "text": p["text"],
            "heading": _style_str(p["heading"], "bold"),
            "selected": p["selected"],
            "border": p["border"],
            "muted": p["muted"],
            "separator": p["separator"],
            "disabled": p["disabled"],
            "error": p["error"],
        }
    )


def build_questionary_style(scheme: Scheme):
    from questionary import Style

    p = PALETTE[scheme]
    return Style(
        [
            ("qmark", f"fg:{p['accent']} bold"),
            ("question", "bold"),
            ("answer", f"fg:{p['accent']} bold"),
            ("pointer", f"fg:{p['accent']} bold"),
            ("highlighted", f"fg:{p['text']} bold noreverse"),
            ("selected", f"fg:{p['selected']} noreverse"),
            ("separator", f"fg:{p['separator']}"),
            ("instruction", f"fg:{p['muted']} italic"),
            ("disabled", f"fg:{p['disabled']} italic"),
            ("validation-toolbar", f"fg:{p['error']} bold"),
            ("text", f"fg:{p['text']}"),
        ]
    )
