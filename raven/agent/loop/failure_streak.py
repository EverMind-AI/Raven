"""When a repeated tool failure is a stuck loop, and what to say about it.

The agent loop injects a change-approach nudge after the same tool fails the
same way several turns running. Two judgements decide whether that helps: which
failures are deterministic enough to count at all (a 429 clears itself), and
what counts as "the same failure" (two different errors mean the model is still
adapting). Both live here rather than in the loop, which only does the counting.
"""

from __future__ import annotations

import re

# Failure markers a plain retry would likely clear — these must NOT count toward
# the tool-failure-loop streak (nudging on a 429 that self-heals is just noise).
_TRANSIENT_FAILURE_MARKERS = (
    "429",
    "rate limit",
    "timed out",
    "timeout",
    "no healthy upstream",
    "502",
    "503",
)
# Successful-but-empty results: the tool ran fine and just found nothing. A
# repeated empty search is legitimate exploration, not a stuck dead call, so it
# must NOT count toward the failure streak.
_EMPTY_SUCCESS_MARKERS = ("no matches found", "no files found")


def failure_class(model_text: str) -> str:
    """Which kind of failure this is, for streak accounting.

    Coarse on purpose: the streak asks "is the model repeating the same dead
    call", and two different errors from one tool mean it is still adapting.
    Counting them together fires the nudge at a model that is working through
    a problem, which is the opposite of what the nudge is for.
    """
    low = model_text[:200].lower()
    if "[truncated]" in low:
        return "truncated"
    if "[invalid arguments]" in low:
        # Its own class, not "schema": a model that sent malformed JSON and
        # then sent a well-formed call missing a field has changed what it is
        # doing, which is the distinction this key exists to make.
        return "invalid_arguments"
    if "invalid parameters" in low:
        return "schema"
    if "not found" in low:
        return "not_found"
    if "permission" in low or "denied" in low:
        return "denied"
    if "timed out" in low:
        return "timeout"
    return "other"


def is_hard_tool_failure(result: object) -> bool:
    """True for a deterministic tool failure (recurs on an identical retry).

    False for success or a transient/retryable error. Used to decide whether a
    repeated identical tool call is a stuck loop worth breaking.
    """
    s = str(result)
    low = s.lower()
    if any(m in low for m in _TRANSIENT_FAILURE_MARKERS):
        return False
    if s.strip().rstrip(".").lower() in _EMPTY_SUCCESS_MARKERS:
        return False
    m = re.search(r"Exit code:\s*(-?\d+)", s)
    if m:
        return m.group(1) != "0"
    # Real not-found failures (file / dir / path / old_text) all start with
    # "Error:" or carry a non-zero exit code, so those are already covered; a
    # bare "not found" scan would only risk flagging successful output that
    # merely mentions the phrase.
    return s.lstrip().startswith("Error") or "error:" in low[:80]


def loop_break_nudge(tool: str, n: int, failure: str = "other") -> str:
    """Injected when the same tool fails deterministically N times running, so
    the model stops repeating a dead approach instead of adapting.

    Keyed on the failure class as well as the tool, because "change approach"
    is not always somewhere to go. A repeated truncation is a payload that
    keeps outrunning the output limit, and the tool that produced it -- the one
    that writes files -- has no counterpart to switch to; the same turn's
    ``truncation_hint`` has already told the model to call it again in pieces.
    Sending it elsewhere contradicts that. Sending it back with less does not.
    """
    if failure == "truncated":
        return (
            f"[loop] `{tool}` has been cut off at the output limit {n} times in a row. "
            "Splitting it further is the way through, but the pieces are still too big -- "
            "make the next one substantially smaller rather than resending this one."
        )
    return (
        f"[loop] `{tool}` has failed {n} times in a row with the same kind of error. "
        "Stop repeating it. The error text above names the actual cause -- read it "
        "and change approach: a different tool, command, or strategy. Do not call it "
        "again unchanged."
    )
