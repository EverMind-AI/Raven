"""activation_spec — a node's machine-checkable "when do I take effect".

Kinds (one per mechanism class, design section 3):

- ``trajectory_regex``    code class. Counts trajectories containing a line
                          matching ``pattern`` within ``scope`` records.
                          (Delta-3's "pure cd" predicate is this kind.)
- ``consecutive_repeat``  hook class, repetition-trigger family. Counts
                          trajectories with >= ``threshold`` consecutive
                          identical ``scope`` contents. Empty/whitespace
                          contents are skipped by default (``ignore_empty: false``
                          to count them). Other hook trigger families express as
                          trajectory_regex or get a new kind + evaluator here.
- ``short_content_run``   hook class, reasoning-visibility family. Counts
                          trajectories with >= ``threshold`` consecutive
                          tool-call iterations whose visible assistant content
                          is shorter than ``max_chars`` (default 80). Uses the
                          shared predicate ``is_short_toolcall_iteration`` from
                          raven.evolver.activation.predicates, the same function the
                          ReasoningVisibilityHook calls at runtime: only records
                          carrying tool_calls count; a record WITHOUT tool_calls
                          (or a long-content one) resets the run. Content is
                          measured after stripping ``<think>...</think>`` blocks
                          and collapsing whitespace. Approximation: the hook
                          reads the live ``response.content``; over recorded
                          sessions we read the ``content`` field of the assistant
                          record (Qwen's chain-of-thought lands in a separate
                          ``reasoning_content`` field the hook does not read).
- ``empty_run``           hook class, response-quality family. Counts
                          trajectories with >= ``threshold`` consecutive empty
                          iterations. Uses the shared predicate
                          ``is_empty_response`` (raven.evolver.activation.predicates),
                          the same the EmptyRunBreakerHook calls: an iteration is
                          empty iff its content is blank AND it carries no
                          tool_calls. A blank-content record that still issues a
                          tool call is NOT empty (round-1 incident C1: the old
                          content-only predicate predicted 64.8% reachable while
                          the tool_calls-respecting hook fired 0). Any non-empty
                          iteration resets the run.
- ``repeated_failure_run`` hook class, robustness family. Counts trajectories
                          with >= ``threshold`` consecutive failures of the same
                          command (same head token). Walks raw records: assistant
                          content sets a pending head token (first whitespace-split
                          token); tool record resolves it (failed iff exit code
                          is nonzero); same head + fail grows the run; success
                          or head-change resets it. Tool records without an
                          "Exit code:" line count as success (conservative:
                          under-counts reachability, never over-counts).
                          ``scope`` is ignored (the kind inherently walks
                          assistant and tool records).
- ``min_iterations``      hook class, budget family. Counts trajectories with
                          >= ``threshold`` assistant iterations — the wrap-up
                          nudge trigger is an iteration-count crossing,
                          structurally drift-free (no semantic predicate).
- ``skill_routing``       skill class. Reachability is answered by a routing
                          dry-query, not by corpus replay — the chamber
                          delegates; evaluate_spec rejects it.
- ``presence``            always-on class. Answered by offline render/config
                          assert in the preflight CLI; evaluate_spec rejects
                          it likewise.

``evaluate_spec(spec, corpus) -> int`` returns HOW MANY trajectories the
spec is reachable in. 0 = the mechanism would never run = block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from raven.evolver.activation.predicates import (
    command_head,
    is_empty_response,
    is_short_toolcall_iteration,
)

_CORPUS_KINDS = {
    "trajectory_regex",
    "consecutive_repeat",
    "short_content_run",
    "empty_run",
    "repeated_failure_run",
    "min_iterations",
}
_KNOWN_KINDS = _CORPUS_KINDS | {"skill_routing", "presence"}

_DEFAULT_MAX_CHARS = 80


def _normalize_record(r: dict) -> dict:
    """Coerce a logged session record into the predicate record shape
    (content as str, tool_calls as list) so the shared predicate functions
    see the same view the hooks build via normalize_response()."""
    return {"content": str(r.get("content") or ""), "tool_calls": r.get("tool_calls") or []}


@dataclass
class ActivationSpec:
    kind: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ActivationSpec":
        kind = d.get("kind")
        if not kind:
            raise ValueError("activation_spec requires a 'kind' field")
        if kind not in _KNOWN_KINDS:
            raise ValueError(f"unknown activation_spec kind: {kind!r}")
        if kind == "trajectory_regex" and "pattern" not in d:
            raise ValueError("trajectory_regex requires 'pattern'")
        if kind == "trajectory_regex":
            try:
                re.compile(d["pattern"])
            except re.error as exc:
                raise ValueError(f"trajectory_regex pattern is not valid regex: {exc}") from exc
        if kind == "consecutive_repeat" and "threshold" not in d:
            raise ValueError("consecutive_repeat requires 'threshold'")
        if kind == "consecutive_repeat":
            try:
                int(d["threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"consecutive_repeat threshold must be an int: {d['threshold']!r}") from exc
        if kind == "short_content_run" and "threshold" not in d:
            raise ValueError("short_content_run requires 'threshold'")
        if kind == "short_content_run":
            try:
                int(d["threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"short_content_run threshold must be an int: {d['threshold']!r}") from exc
            if "max_chars" in d:
                try:
                    int(d["max_chars"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"short_content_run max_chars must be an int: {d['max_chars']!r}") from exc
        if kind == "empty_run" and "threshold" not in d:
            raise ValueError("empty_run requires 'threshold'")
        if kind == "empty_run":
            try:
                int(d["threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"empty_run threshold must be an int: {d['threshold']!r}") from exc
        if kind == "repeated_failure_run" and "threshold" not in d:
            raise ValueError("repeated_failure_run requires 'threshold'")
        if kind == "repeated_failure_run":
            try:
                int(d["threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"repeated_failure_run threshold must be an int: {d['threshold']!r}") from exc
        if kind == "min_iterations" and "threshold" not in d:
            raise ValueError("min_iterations requires 'threshold'")
        if kind == "min_iterations":
            try:
                int(d["threshold"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"min_iterations threshold must be an int: {d['threshold']!r}") from exc
        if kind == "skill_routing" and "skill_name" not in d:
            raise ValueError("skill_routing requires 'skill_name'")
        if kind == "presence" and "needle" not in d:
            raise ValueError("presence requires 'needle'")
        raw = {k: v for k, v in d.items() if k != "kind"}
        return cls(kind=kind, raw=raw)

    @property
    def pattern(self) -> str:
        return self.raw["pattern"]

    @property
    def threshold(self) -> int:
        return int(self.raw["threshold"])

    @property
    def max_chars(self) -> int:
        return int(self.raw.get("max_chars", _DEFAULT_MAX_CHARS))


def _scope_contents(traj: list[dict], scope: str) -> list[str]:
    return [str(r.get("content") or "") for r in traj if scope in ("any", r.get("role"))]


def evaluate_spec(spec: ActivationSpec, corpus: list[list[dict]]) -> int:
    if spec.kind not in _CORPUS_KINDS:
        raise ValueError(f"{spec.kind} is not corpus-evaluable; use the preflight CLI path")
    scope = spec.raw.get("scope", "assistant")
    hits = 0
    if spec.kind == "trajectory_regex":
        pat = re.compile(spec.pattern, re.M)
        for traj in corpus:
            if any(pat.search(c) for c in _scope_contents(traj, scope)):
                hits += 1
    elif spec.kind == "consecutive_repeat":
        threshold = spec.threshold
        ignore_empty = spec.raw.get("ignore_empty", True)
        for traj in corpus:
            run, prev = 0, object()
            for c in _scope_contents(traj, scope):
                if ignore_empty and not c.strip():
                    # Empty responses carry no command; a repetition
                    # trigger comparing commands never sees them.
                    continue
                run = run + 1 if c == prev else 1
                prev = c
                if run >= threshold:
                    hits += 1
                    break
    elif spec.kind == "short_content_run":
        threshold = spec.threshold
        max_chars = spec.max_chars
        for traj in corpus:
            run = 0
            fired = False
            for r in traj:
                if scope not in ("any", r.get("role")):
                    continue
                rec = _normalize_record(r)
                run = run + 1 if is_short_toolcall_iteration(rec, max_chars) else 0
                if run >= threshold:
                    fired = True
                    break
            if fired:
                hits += 1
    elif spec.kind == "empty_run":
        threshold = spec.threshold
        for traj in corpus:
            run = 0
            for r in traj:
                if scope not in ("any", r.get("role")):
                    continue
                rec = _normalize_record(r)
                run = run + 1 if is_empty_response(rec) else 0
                if run >= threshold:
                    hits += 1
                    break
    elif spec.kind == "repeated_failure_run":
        threshold = spec.threshold
        exit_re = re.compile(r"Exit code: (\d+)")
        for traj in corpus:
            run, prev_head = 0, None
            pending_head = None
            for r in traj:
                role, c = r.get("role"), str(r.get("content") or "")
                if role == "assistant" and c.strip():
                    pending_head = command_head(_normalize_record(r))
                elif role == "tool" and pending_head is not None:
                    m = exit_re.search(c)
                    failed = bool(m and m.group(1) != "0")
                    if failed and pending_head == (prev_head or pending_head):
                        run += 1
                        prev_head = pending_head
                    else:
                        run = 1 if failed else 0
                        prev_head = pending_head if failed else None
                    pending_head = None
                    if run >= threshold:
                        hits += 1
                        break
    elif spec.kind == "min_iterations":
        threshold = spec.threshold
        for traj in corpus:
            count = sum(1 for r in traj if r.get("role") == "assistant")
            if count >= threshold:
                hits += 1
    return hits
