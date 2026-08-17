"""Online loop detection (density fingerprint) for the agent loop.

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

``<tool_call>`` XML blocks are stripped before scanning: parallel
tool-call scaffolding legitimately repeats within one message.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

SPIN_WIN = 60
SPIN_STRIDE = 30
SPIN_MIN_REP = 4
SPIN_SHARE_MIN = 0.2
SPIN_MIN_WINDOWS = 8
_MIN_INFORMATIVE_CHARS = 15

_ALNUM_CJK = re.compile(r"[0-9A-Za-z一-鿿]")
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


def _response_text(response: object) -> str:
    """All text the model produced this iteration: reasoning + content.

    A spin can live on either side; both are scanned as one blob.
    Tolerates ``LLMResponse``-shaped objects and plain dicts.
    """
    if response is None:
        return ""
    parts = []
    for attr in ("reasoning_content", "content"):
        value = getattr(response, attr, None)
        if value is None and isinstance(response, dict):
            value = response.get(attr)
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


class LoopscanObserver(AgentHook):
    """Detect a degenerate spin in the current response and intervene.

    First ``max_rollbacks`` hits: rollback (pop + re-sample) with an
    escalating temperature override. Past the budget: pass through with
    a note, leaving termination to the loop's own bounds — fabricating
    a reply out of a spin would be worse than delivering it labelled.

    Fires at ``before_execute_tools`` for tool-call iterations (the
    spin is discarded before the tools execute) and at
    ``after_iteration`` for tool-free iterations (the candidate final
    answer). Cross-iteration state lives in ``ctx.metadata["loopscan"]``.
    """

    def __init__(
        self,
        max_rollbacks: int = 2,
        retry_temperatures: tuple[float, ...] = (1.0, 1.2),
    ) -> None:
        self._max_rollbacks = max_rollbacks
        self._retry_temperatures = retry_temperatures

    @property
    def name(self) -> str:
        return "LoopscanObserver"

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return self._scan(ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        return self._scan(ctx)

    def _scan(self, ctx: AgentHookContext) -> HookDecision:
        text = _response_text(ctx.response)
        if not text:
            return HookDecision()
        stats = spin_stats(_TOOLCALL_BLOCK_RE.sub(" ", text))
        if not is_spin(stats):
            return HookDecision()

        state = ctx.metadata.setdefault("loopscan", {"rollbacks": 0, "hits": []})
        state["hits"].append(
            {
                "iteration": ctx.iteration,
                "share": stats.share,
                "windows": stats.n_repeat,
                "frag": stats.frag[:80],
            }
        )
        if state["rollbacks"] >= self._max_rollbacks:
            state["exhausted"] = True
            logger.warning(
                "loopscan: spin persists after %d re-roll(s) (share=%.3f windows=%d); passing through",
                state["rollbacks"],
                stats.share,
                stats.n_repeat,
            )
            return HookDecision(
                notes=[f"loopscan_exhausted share={stats.share} windows={stats.n_repeat}"],
            )

        temperature = self._retry_temperatures[min(state["rollbacks"], len(self._retry_temperatures) - 1)]
        state["rollbacks"] += 1
        logger.warning(
            "loopscan: spin detected (share=%.3f windows=%d frag=%r); rollback %d/%d at temperature=%s",
            stats.share,
            stats.n_repeat,
            stats.frag[:40],
            state["rollbacks"],
            self._max_rollbacks,
            temperature,
        )
        return HookDecision(
            rollback=True,
            rollback_overrides={"temperature": temperature},
            notes=[f"loopscan_spin share={stats.share} windows={stats.n_repeat}"],
        )


__all__ = [
    "LoopscanObserver",
    "SpinStats",
    "spin_stats",
    "is_spin",
    "SPIN_WIN",
    "SPIN_STRIDE",
    "SPIN_MIN_REP",
    "SPIN_SHARE_MIN",
    "SPIN_MIN_WINDOWS",
]
