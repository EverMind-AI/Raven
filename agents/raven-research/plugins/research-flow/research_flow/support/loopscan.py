"""Online loop detection (density fingerprint) shared by the flow's gates.

Degenerate decode spins — the same fragment recycling with no forward
progress — are detected with a *density* criterion, not a bare
"fragment repeats N times" rule. Bare repeat counts flag legitimate
patterns as loops: repeated constraint quoting in multi-hop reasoning,
parallel tool-call XML scaffolding, markdown table rules. The density
criterion requires that a large share of the message's windows repeat,
which only degenerate spins exhibit.

Criterion (calibrated offline against real spin and clean trajectories):

- slide a ``SPIN_WIN``-char window at ``SPIN_STRIDE`` over the text;
- a window is eligible only if it carries >= ``_MIN_INFORMATIVE_CHARS``
  alphanumeric/CJK chars — symbol/whitespace windows (table rules,
  separators) never count;
- a window "repeats" if it occurs >= ``SPIN_MIN_REP`` times, counted
  with overlaps (spin periods shorter than the window are undercounted
  by non-overlapping counts; CJK spin units commonly are);
- spin <=> repeat-window share >= ``SPIN_SHARE_MIN`` AND absolute
  repeat windows >= ``SPIN_MIN_WINDOWS`` (small-sample guard).

Callers strip ``<tool_call>`` XML blocks (``_TOOLCALL_BLOCK_RE``) before
scanning: parallel tool-call scaffolding legitimately repeats within one
message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SPIN_WIN = 60
SPIN_STRIDE = 30
SPIN_MIN_REP = 4
SPIN_SHARE_MIN = 0.2
SPIN_MIN_WINDOWS = 8
_MIN_INFORMATIVE_CHARS = 15

_ALNUM_CJK = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_TOOLCALL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.S)


@dataclass
class SpinStats:
    share: float = 0.0
    n_repeat: int = 0
    n_eligible: int = 0
    frag: str = ""


def _count_overlap(text: str, window: str, need: int) -> int:
    n = 0
    start = text.find(window)
    while start >= 0:
        n += 1
        if n >= need:
            return n
        start = text.find(window, start + 1)
    return n


def spin_stats(text: str) -> SpinStats:
    """Density scan of one assistant text; see the module docstring."""
    if not text or len(text) < SPIN_WIN + 3 * SPIN_STRIDE:
        return SpinStats()
    seen: dict[str, str] = {}
    n_eligible = n_repeat = 0
    frag = ""
    for i in range(0, len(text) - SPIN_WIN + 1, SPIN_STRIDE):
        w = text[i : i + SPIN_WIN]
        tag = seen.get(w)
        if tag is None:
            if len(_ALNUM_CJK.findall(w)) < _MIN_INFORMATIVE_CHARS:
                seen[w] = "skip"
                continue
            rep = _count_overlap(text, w, SPIN_MIN_REP) >= SPIN_MIN_REP
            seen[w] = "rep" if rep else "norep"
            n_eligible += 1
            if rep:
                n_repeat += 1
                if not frag:
                    frag = w
        elif tag != "skip":
            n_eligible += 1
            if tag == "rep":
                n_repeat += 1
    share = round(n_repeat / n_eligible, 3) if n_eligible else 0.0
    return SpinStats(share=share, n_repeat=n_repeat, n_eligible=n_eligible, frag=frag)


def is_spin(stats: SpinStats) -> bool:
    return stats.share >= SPIN_SHARE_MIN and stats.n_repeat >= SPIN_MIN_WINDOWS


__all__ = [
    "SpinStats",
    "spin_stats",
    "is_spin",
    "SPIN_WIN",
    "SPIN_STRIDE",
    "SPIN_MIN_REP",
    "SPIN_SHARE_MIN",
    "SPIN_MIN_WINDOWS",
]
