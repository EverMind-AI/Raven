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

import asyncio
import os
import re
from pathlib import Path

from loguru import logger

EMPTY_DIFF_ENV = "RAVEN_GATE_EMPTY_DIFF"

_GIT_PROBE_TIMEOUT_SECONDS = 10.0
# Paths whose presence does not constitute doing the task: prose and Raven's own
# runtime state. Mirrors the loop's edit-tracking filters; scratch dirs (/tmp,
# /dev/shm) need no entry because git reports workspace-relative paths only.
_NON_DELIVERABLE_RE = re.compile(r"(\.(md|rst|txt)$|(^|/)docs?(/|$)|(^|/)\.raven(/|$))", re.IGNORECASE)

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


GATE_MAX_ROUNDS_ENV = "RAVEN_GATE_MAX_ROUNDS"
_GATE_MAX_ROUNDS_DEFAULT = 2


def gate_round_budget() -> int:
    """Total completion-gate rounds one turn may spend, across ALL gates.

    Before this budget existed the gates could chain across successive finish
    attempts (measured on a gates-on 492-task run: mean 1.44 extra rounds per
    task, max 4, and the unconditional re-check nudge fired on 97% of tasks).
    A malformed or negative value falls back to the default rather than
    disabling the gates silently.
    """
    raw = os.environ.get(GATE_MAX_ROUNDS_ENV)
    if raw is None or not raw.strip():
        return _GATE_MAX_ROUNDS_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        return _GATE_MAX_ROUNDS_DEFAULT
    return max(0, value)


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

    Stating the default is mandatory: a gate added later as
    ``gate_enabled(NEW_ENV)`` would silently arm itself on every line that
    ships this helper, including the ones whose policy is opt-in.
    """
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    return raw.strip().lower() not in _OPT_OUT_VALUES


def gate_variant(env_name: str) -> str:
    """The switch's value as a lowercase variant name, "" when unset.

    A switch answers on/off through :func:`gate_enabled`; a gate whose wording
    has more than one form reads the same value here to pick which one. Keeping
    both readings in this module means callers never re-derive the env
    conventions themselves.
    """
    return (os.environ.get(env_name) or "").strip().lower()


async def workspace_has_changes(workspace: Path) -> bool | None:
    """Does the workspace hold uncommitted changes? ``None`` when git cannot say.

    Read-only by construction: ``git status`` neither stages nor writes objects,
    so this can run on every completion attempt without touching the repository
    under evaluation. ``None`` (not a git checkout, git missing, timeout) leaves
    the caller on its own evidence rather than inventing an answer.

    Changes confined to prose or Raven's own runtime state do not count -- the
    gate asks whether the *task* was done, and a session that only wrote notes
    has not done it.

    Known blind spot: an agent that commits its own work leaves a clean tree and
    reads as untouched here. Both benchmark verifiers we run diff the *working*
    tree, so committing already forfeits the score independently of this gate.
    """
    workspace = Path(workspace)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        logger.debug("empty-diff gate: git probe unavailable: {}", exc)
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.debug("empty-diff gate: git probe timed out after {}s", _GIT_PROBE_TIMEOUT_SECONDS)
        return None
    if proc.returncode != 0:
        return None
    return any(not _NON_DELIVERABLE_RE.search(path) for path in _porcelain_paths(out.decode(errors="replace")))


def _porcelain_paths(payload: str) -> list[str]:
    """Extract paths from ``git status --porcelain -z`` output.

    NUL-separated records avoid git's quoting of non-ASCII paths. A rename or
    copy spends a second record on its source path; that record carries no
    status prefix, so it must be consumed rather than parsed as a path.
    """
    records = [r for r in payload.split("\0") if r]
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if status[0] in ("R", "C"):
            index += 1
        if path:
            paths.append(path)
    return paths


def empty_diff_nudge(
    *,
    enabled: bool,
    code_edits_observed: bool,
    workspace_dirty: bool | None,
    already_nudged: bool,
    iteration: int,
    max_iterations: int,
) -> str | None:
    """Return the one-shot empty-diff falsification nudge, or None.

    Two independent signals must BOTH report an untouched repository before the
    nudge fires. ``code_edits_observed`` only covers edits made through the tools
    in THIS turn, and a harness may split one task across turns (a second turn
    just to collect a completion token), leaving the edits behind in an earlier
    turn -- trusting it alone made the gate assert an empty repository over a
    workspace full of changes. ``workspace_dirty`` is git's verdict on the whole
    workspace, which covers earlier turns and shell-driven edits alike, but reads
    clean when an edit was reverted or landed on an ignored path. Disagreement is
    ambiguity, and an ambiguous gate stays quiet. ``None`` means git could not
    answer at all, leaving the turn-local flag as the only evidence.
    """
    if not enabled or already_nudged:
        return None
    if code_edits_observed or workspace_dirty:
        return None
    if iteration >= max_iterations:
        return None
    return _EMPTY_DIFF_NUDGE
