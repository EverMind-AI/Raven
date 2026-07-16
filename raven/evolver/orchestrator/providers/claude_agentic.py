"""Agentic analysis session: full-tool Claude Code, locked to read-only.

The map-reduce diagnosis reads every failing trajectory once, shallowly. The
``agentic`` analysis mode replaces that front half with one Claude Code session
that investigates the run the way an engineer would — ledger first, then
deep-reads of representative transcripts, then the harness source — and returns
one structured diagnosis (meta-harness style, but constrained to our taxonomy
so the failure_map/history/GSME machinery downstream is unchanged).

Safety model (the session must not be able to leave the safe zone):

- The tool whitelist is **hard-coded** to ``Read, Glob, Grep`` — no Bash, no
  Write/Edit, no Agent, and callers cannot widen it. In ``-p`` mode any tool
  outside ``--allowedTools`` is auto-denied, and ``--dangerously-skip-permissions``
  is never passed, so the session can inspect but cannot modify or execute.
- ``cwd`` is the assembled analysis workspace (digest + run data + a pinned
  harness worktree), not the live repo — no CLAUDE.md/project injection and
  nothing load-bearing to touch even if a write slipped through.
- Only Claude models may run this mode (enforced here): the analyst rides the
  local CLI's logged-in subscription, like :mod:`.claude_cli`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

AGENTIC_TOOLS = ("Read", "Glob", "Grep")


def claude_cli_available(claude_bin: str = "claude") -> bool:
    """True when the claude CLI is on PATH and answers ``--version``."""
    if shutil.which(claude_bin) is None:
        return False
    try:
        r = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=20
        )
    except Exception:  # noqa: BLE001 — any probe failure means "not usable"
        return False
    return r.returncode == 0


def require_claude_for_agentic(model: str, claude_bin: str = "claude") -> None:
    """Gate for analysis_mode="agentic": claude models + a working CLI only."""
    if not str(model).startswith("claude"):
        raise ValueError(
            f"analysis_mode='agentic' only runs on Claude models via the local "
            f"CLI (got model={model!r}); use analysis_mode='mapreduce' for other drivers"
        )
    if not claude_cli_available(claude_bin):
        raise RuntimeError(
            f"analysis_mode='agentic' requires the '{claude_bin}' CLI on PATH "
            "(logged-in Claude Code); it was not found or does not answer --version"
        )


def run_agentic_session(
    prompt: str,
    *,
    system_prompt: str,
    cwd: str | Path,
    model: str = "claude-opus-4-8",
    claude_bin: str = "claude",
    timeout: float = 1800.0,
    add_dirs: tuple = (),
    run: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
) -> str:
    """One read-only agentic Claude Code session; returns its final text.

    ``--append-system-prompt`` (not ``--system-prompt``) keeps Claude Code's
    native agentic system prompt so Read/Glob/Grep are actually driven well;
    our instructions ride on top.

    ``add_dirs`` grants READ access to trees outside ``cwd``: print-mode file
    access is confined to the cwd subtree by *resolved* path, so workspace
    symlinks pointing at run data are denied without it (observed live: the
    analyst reported the data 'absent' and degraded to signature analogy).
    The tool whitelist stays read-only, so the grant widens visibility, not
    write reach.
    """
    require_claude_for_agentic(model, claude_bin)
    _run = run or subprocess.run
    argv = [
        claude_bin, "-p",
        "--model", model,
        "--output-format", "json",
        "--allowedTools", ",".join(AGENTIC_TOOLS),
        "--disallowedTools", "Bash,Write,Edit,NotebookEdit,Agent,WebFetch,WebSearch",
        "--append-system-prompt", system_prompt,
    ]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    proc = _run(
        argv, input=prompt, capture_output=True, text=True,
        timeout=timeout, cwd=str(cwd),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agentic session exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[:400]}"
        )
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"agentic session is_error: {str(data.get('result'))[:400]}")
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError(f"agentic session empty result: {proc.stdout[:400]}")
    return result


__all__ = [
    "AGENTIC_TOOLS",
    "claude_cli_available",
    "require_claude_for_agentic",
    "run_agentic_session",
]
