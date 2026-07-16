"""Path guard for evolver-generated patches (spec §22 + §22.7).

The immutable set is **evolver-immutable**, not human-immutable.
Human-driven development (PR review) can still touch these paths
through normal workflow; only auto-evolver patches are blocked.

Two pattern types are supported in ``IMMUTABLE_PATTERNS``:

- **Exact file** — e.g. ``"raven/agent/loop/main.py"``. Matches
  exactly this repo-relative path.
- **Directory subtree** — trailing-slash, e.g. ``"raven/evolver/"``.
  Matches the directory itself and any descendant path.

``MUTABLE_OVERRIDES`` takes precedence: a path matching an override is
mutable even if it also matches an immutable pattern. Use this to carve
out mutable sub-trees from a broader immutable directory.

Why repo-relative-string matching instead of full glob:
This module's job is fast, predictable, easy-to-audit gating. Glob
patterns (``**``, ``*.py``) invite surprises (case-sensitivity, slash
behaviour). Exact paths + directory subtrees cover everything in
§22.2 without ambiguity.

Reference layers (spec §22.1, §22.2):
    L1 — Self-reference (evolver/**)
    L2 — Evaluation substrate (eval_engine + external grader)
    L3 — Capability contract (agent loop, tools framework, providers,
         skill loader, sandbox, config schema, ...)
    L4 — Audit / data integrity (tool_audit_hook)
    L5 — Tests, deps, CI
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Immutable pattern list (spec §22.2)
# ---------------------------------------------------------------------------


# All paths are repo-relative with forward-slash separators.
# A trailing slash means "this directory and everything inside it".
IMMUTABLE_PATTERNS: tuple[str, ...] = (
    # ── L1 — Self-reference ────────────────────────────────────────────────
    "raven/evolver/",
    # ── L2 — Evaluation substrate ──────────────────────────────────────────
    "raven/eval_engine/engine.py",
    "raven/eval_engine/adapter/",
    "raven/eval_engine/judge/",
    # The AppWorld evolve glue (adapter/eval/diagnose/editor/precheck, plus
    # grade.py — the /evaluate call, success/infra classification and result
    # write) scores candidates; it sits INSIDE the designer whitelist tree,
    # so the guard must carve it out (maps the upstream evaluation/ entry).
    "benchmarks/appworld/evolve/",
    # The batch scorer orchestrates trials and records runner-level infra;
    # a candidate that can edit it can reshape its own denominator. The
    # editable agent surface is agent_cli.py (loop/prompt) and tool.py.
    "benchmarks/appworld/batch.py",
    # ── L3 — Capability contract ───────────────────────────────────────────
    "raven/agent/loop/main.py",
    "raven/agent/context/",
    "raven/agent/tools/base.py",
    "raven/agent/tools/registry.py",
    "raven/agent/personalizer/",
    "raven/agent/subagent/",
    # Raven replaced the upstream agent/api with the spine: the runner is the
    # bridge every turn goes through, same contract level.
    "raven/agent/spine_runner.py",
    "raven/spine/",
    "raven/providers/",
    "raven/session/",
    "raven/memory_engine/",
    "raven/skill_hub/",
    "raven/context_engine/",
    "raven/sandbox/",
    "raven/security/",
    # config: schema/loader (.py) are immutable; values (.yaml/.json) mutable
    "raven/config/__init__.py",
    "raven/config/raven.py",
    "raven/config/loader.py",
    "raven/config/paths.py",
    "raven/config/schema.py",
    "raven/config/update.py",
    "raven/config/update_channels.py",
    "raven/config/update_providers.py",
    # ── L4 — Audit / data integrity ────────────────────────────────────────
    "raven/eval_engine/hooks/tool_audit_hook.py",
    # ── L5 — Tests, deps, CI ───────────────────────────────────────────────
    "tests/",
    "pyproject.toml",
    "uv.lock",
    ".github/",
    # ── Core version (spec §22.5) — only humans bump ───────────────────────
    "raven/__init__.py",
)


# Carve-outs for paths inside immutable subtrees that should remain
# mutable. Empty at the moment — no carve-outs needed under the current
# §22.2 layout. Add an entry like ``"raven/sandbox/policies.yaml"``
# here if a specific file inside an immutable directory needs to evolve.
MUTABLE_OVERRIDES: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ImmutablePathError(ValueError):
    """Raised when an evolver patch targets an immutable kernel path.

    See spec §22 for the evolver-immutable kernel definition. If a
    legitimate patch is being blocked, two options:

    1. Reframe the patch — split off the mutable part, drop the
       immutable part. This is what `path_guard` expects.
    2. Walk the human-driven development path: open a PR that
       updates ``MUTABLE_OVERRIDES`` (if the path was misclassified)
       or that modifies the kernel directly (then bump ``core_version``
       per spec §22.5).
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise(path: str) -> str:
    """Normalise a path to repo-relative, forward-slash form.

    - Windows-style backslashes are converted to forward slashes
    - A leading ``./`` is stripped (callers sometimes pass it)

    Callers are expected to pass repo-relative paths; absolute paths
    will not match any pattern (and therefore be reported as mutable).
    Validating that is the caller's responsibility.
    """
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _match(path: str, pattern: str) -> bool:
    """Match a normalised path against one pattern.

    Two pattern shapes:

    - Trailing slash → directory subtree match (the directory itself
      and any descendant).
    - Otherwise → exact-string match.
    """
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return path == prefix or path.startswith(pattern)
    return path == pattern


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def is_immutable(path: str) -> bool:
    """Return True iff ``path`` is in the evolver-immutable kernel.

    Algorithm:

    1. Normalise the path (handle Windows paths + leading ``./``).
    2. If any pattern in ``MUTABLE_OVERRIDES`` matches → return False
       (override wins).
    3. If any pattern in ``IMMUTABLE_PATTERNS`` matches → return True.
    4. Otherwise → return False (path is mutable by default).
    """
    norm = _normalise(path)
    for pat in MUTABLE_OVERRIDES:
        if _match(norm, pat):
            return False
    for pat in IMMUTABLE_PATTERNS:
        if _match(norm, pat):
            return True
    return False


def check_patch_paths(target_files: list[str]) -> list[str]:
    """Return the subset of ``target_files`` that hit immutable paths.

    Useful for evolver code that wants to inspect violations without
    raising — e.g., to route the patch to a TODO markdown
    (spec §21.4.2) instead of attempting to apply it.
    """
    return [p for p in target_files if is_immutable(p)]


def assert_patch_allowed(target_files: list[str]) -> None:
    """Raise :class:`ImmutablePathError` if any target is immutable.

    Use this as the first-line gate in the evolver applier:

    .. code-block:: python

        from raven.evolver.applier import assert_patch_allowed
        assert_patch_allowed([c.target_file for c in patch.components])
        # ... proceed to apply patch only if no error
    """
    offenders = check_patch_paths(target_files)
    if offenders:
        head = offenders[:5]
        more = f" ... and {len(offenders) - 5} more" if len(offenders) > 5 else ""
        raise ImmutablePathError(
            f"Patch targets {len(offenders)} evolver-immutable path(s): "
            f"{head}{more}. The gates, ledgers, and eval glue may never be "
            "edited by a candidate (see IMMUTABLE_PATTERNS in this module)."
        )


__all__ = [
    "IMMUTABLE_PATTERNS",
    "MUTABLE_OVERRIDES",
    "ImmutablePathError",
    "assert_patch_allowed",
    "check_patch_paths",
    "is_immutable",
]
