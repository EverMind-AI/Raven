"""Completion-gate guardrail: empty-diff falsification, plus the shared switch helper.

Evidence base (2026-08, trajectory error analysis across two independent model rounds
on this harness; the full evidence chain lives in the guardrail design notes, not here):
both rounds occasionally finish a change-the-code task with an EMPTY diff while claiming
the repository already satisfies the requirement. One failing trajectory even ran a
plausible-looking falsification experiment -- against the wrong check -- so a weak
"are you sure?" reminder is not enough. The gate asks for the one piece of evidence that
actually discriminates: the task's own acceptance or reproduction steps executed against
the current, unmodified code.

Design constraints shared with the other guardrails (see guardrail-design.md and their
implementations in main.py): at most one injection per turn, the framework states
verifiable facts and leaves the ruling to the model, ambiguity means silence, and
nothing is injected on the final iteration (no room left to act). The gates target the
autonomous single-task coding mode; interactive sessions leave them off by default.
"""

from __future__ import annotations

import os

EMPTY_DIFF_ENV = "RAVEN_GATE_EMPTY_DIFF"

_OPT_OUT_VALUES = frozenset({"0", "false", "no", "off", "disabled"})

_EMPTY_DIFF_NUDGE = """You are finishing this turn without any observed repository modification \
(no successful write_file/edit_file on a non-documentation, non-scratch path).

Facts to resolve before finishing:
- If you changed files through shell commands instead of the editing tools, state which \
commands changed which files -- that fully resolves this reminder.
- If the task asks for a change, an empty diff almost certainly means the work is incomplete.
- If you believe the repository ALREADY satisfies the request, show the discriminating \
evidence: execute the task's own acceptance or reproduction steps against the current, \
unmodified code, and quote the command plus the output showing the requested behavior \
already holds. Verifying with an unrelated or pre-existing check does not count -- it \
passes on broken code too.

If you cannot produce that evidence, continue working instead of finishing."""


def gate_enabled(env_name: str, default: bool) -> bool:
    """Resolve a guardrail switch: explicit environment value, else ``default``.

    The call site owns the default, and stating it is mandatory: an implicit
    default let a newly added gate arm itself against the branch policy.
    On the swarm-integration line every gate
    is opt-in (the loop passes ``default=False``): an orchestrated worker also
    serves non-coding requests, where change-the-code gates only mis-fire.
    Coding-style runs arm gates explicitly through the environment. An
    explicit value overrides in either direction: "0", "false", "no", "off"
    or "disabled" turns a gate off, anything else turns it on.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    return raw.strip().lower() not in _OPT_OUT_VALUES


def empty_diff_nudge(
    *,
    enabled: bool,
    code_edits_observed: bool,
    already_nudged: bool,
    iteration: int,
    max_iterations: int,
) -> str | None:
    """Return the one-shot empty-diff falsification nudge, or None."""
    if not enabled or already_nudged or code_edits_observed:
        return None
    if iteration >= max_iterations:
        return None
    return _EMPTY_DIFF_NUDGE
