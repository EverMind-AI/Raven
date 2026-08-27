"""ANSI escape-sequence whitelist filter for ``cli.dispatch`` output.

Per ``docs/openspec/changes/tui-ipc-bridge/design.md §3 D7 implementation
decision 5`` and ``specs/tui-ipc.md §3.8``, the rich-rendered output returned
from ``cli.dispatch`` MAY contain SGR (color / style) sequences but MUST NOT
contain cursor movement, screen-clear, OSC 8 hyperlinks, or
DECSET/DECRST (alt-screen / paste-mode) — those would corrupt the TUI's Ink
reconciler.

S2 black-listing of interactive Rich widgets (``Live`` / ``Progress`` /
``Prompt``) at the whitelist layer is the first defense; this filter is the
second.

What we KEEP:
- ``\\x1b[...m`` — SGR (Set Graphic Rendition): colors, bold, italic, reset.
  This includes truecolor ``\\x1b[38;2;R;G;Bm`` and 256-color ``\\x1b[38;5;Nm``.

What we STRIP:
- ``\\x1b[<n>A/B/C/D/E/F/G`` — cursor movement (up/down/right/left/...)
- ``\\x1b[<r>;<c>H`` and ``\\x1b[<r>;<c>f`` — cursor position
- ``\\x1b[s`` / ``\\x1b[u`` — save / restore cursor
- ``\\x1b[<n>J`` / ``\\x1b[<n>K`` — clear screen / clear line
- ``\\x1b[?<...>l`` / ``\\x1b[?<...>h`` — DECSET / DECRST (alt screen, paste, ...)
- ``\\x1b]<...>(\\x07|\\x1b\\\\)`` — OSC sequences (including OSC 8 hyperlinks)
- ``\\x1b[<n>q`` — cursor style
- ``\\x1bP...\\x1b\\\\`` — DCS device control strings
- ``\\x1b_...\\x1b\\\\`` — APC application program command

The strategy is whitelist-by-substitution: we match every non-SGR escape we
recognize and replace it with the empty string. We do NOT match SGR explicitly
(no need); anything we don't strip survives.
"""

from __future__ import annotations

import re

# OSC sequences: ``\x1b]`` followed by any payload, terminated by either BEL
# (``\x07``) or ST (``\x1b\\``). The ``.*?`` is non-greedy so back-to-back
# OSC envelopes don't bleed together.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# DCS / APC: ``\x1bP`` or ``\x1b_`` payload terminated by ST.
_DCS_APC_RE = re.compile(r"\x1b[P_].*?\x1b\\", re.DOTALL)

# CSI sequences that are NOT SGR. CSI = ``\x1b[`` + parameter bytes
# ``[0-?]*`` + intermediate bytes ``[ -/]*`` + final byte ``[@-~]``.
# SGR is final byte ``m``; we want everything except ``m``.
# We also explicitly cover the DEC private CSI ``\x1b[?...l/h`` which is
# the alt-screen / paste-mode toggle family.
_CSI_NON_SGR_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-ln-~]")

# Single-character C1 controls (e.g. ``\x1b=``, ``\x1b>``, ``\x1bM`` reverse
# linefeed). Excludes ``\x1b[`` (CSI — handled above), ``\x1b]`` (OSC), ``\x1bP``
# (DCS), ``\x1b_`` (APC), ``\x1b\\`` (ST — appears only paired). Strip the rest.
_C1_RE = re.compile(r"\x1b[ -Z\\-~]")


def filter_ansi(text: str) -> str:
    """Strip non-SGR ANSI / OSC / DCS sequences, preserving color/style.

    Args:
        text: a string that may contain ANSI escape sequences.

    Returns:
        the input with disallowed escapes removed; SGR (color/style) preserved.
    """
    if "\x1b" not in text:
        return text
    # Strip in order: OSC envelopes first (longest match), then DCS/APC,
    # then non-SGR CSI, then leftover single-char C1 controls.
    text = _OSC_RE.sub("", text)
    text = _DCS_APC_RE.sub("", text)
    text = _CSI_NON_SGR_RE.sub("", text)
    text = _C1_RE.sub("", text)
    return text


__all__ = ["filter_ansi"]
