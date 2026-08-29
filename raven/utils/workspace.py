"""Workspace template sync: the bundled ``templates/`` package data copied into a
workspace once, creating only what is missing.
"""

import sys
from collections.abc import Callable
from pathlib import Path

from loguru import logger

# Workspace sync runs before the CLI decides logger.enable/disable("raven"),
# so an unscoped debug in this module would spam stderr through loguru's
# default sink on every first run. A later logger.enable("raven") still
# lifts this rule (loguru drops descendant rules whenever a parent rule is
# set), but the CLI flips logging only after its startup sync -- so the
# per-file detail below reaches callers that enable logging before syncing
# (tests, embedders), not the first sync of a `--logs` run.
logger.disable(__name__)


def _stderr_line(message: str) -> None:
    """Where a report lands when no host supplied a renderer: one plain line on stderr."""
    print(f"  {message}", file=sys.stderr)


def sync_workspace_templates(
    workspace: Path, silent: bool = False, *, notify: "Callable[[str], None] | None" = None
) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files.

    ``notify`` receives one plain sentence when files were created ("Initialized
    workspace (3 files)"); the host decides how to show it. Nothing here owns a
    terminal.
    """
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("raven") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []
    existed = 0

    def _write(src, dest: Path):
        nonlocal existed
        if dest.exists():
            existed += 1
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    def _migrate(src: Path, dest: Path):
        """One-shot copy of legacy content to the L4 path. No-op when the
        source is missing or the destination already exists — safe to
        re-run on every workspace sync.  Reads as binary then decodes
        with UTF-8 (replace) so legacy files written under a non-UTF-8
        Windows code page still migrate without crashing."""
        if not src.is_file() or dest.exists():
            return
        try:
            raw = src.read_bytes()
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        added.append(f"{dest.relative_to(workspace)} (migrated from {src.relative_to(workspace)})")

    # Step 1 — migrate legacy workspace files into the L4 layout. Each
    # rule fires only when the legacy file exists and the L4 target is
    # still missing, so user edits made directly to L4 paths win.
    _migrate(workspace / "memory" / "MEMORY.md", workspace / "user_memory" / "profile" / "user.md")
    _migrate(workspace / "memory" / "HISTORY.md", workspace / "user_memory" / "episodic" / "episodes.md")
    _migrate(workspace / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
    _migrate(workspace / "AGENTS.md", workspace / "agent_memory" / "profile" / "agent.md")
    _migrate(workspace / "USER.md", workspace / "user_memory" / "profile" / "user.md")
    # feat/auto attention + behaviors content lived at workspace root.
    # Sentinel rewrites attention.md from its own producers each tick, so
    # the migrated file mostly serves as a head-start for the next refresh.
    _migrate(workspace / "ATTENTION.md", workspace / "user_memory" / "attention.md")
    _migrate(workspace / "BEHAVIORS.md", workspace / "user_memory" / "behaviors.md")
    _migrate(workspace / "BEHAVIOR.md", workspace / "user_memory" / "behaviors.md")

    # Step 2 — fall back to bundled templates for anything still missing.
    # L4 pillar files first; root-level files (TOOLS / HEARTBEAT) stay put.
    _write(tpl / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
    _write(tpl / "AGENTS.md", workspace / "agent_memory" / "profile" / "agent.md")
    _write(tpl / "USER.md", workspace / "user_memory" / "profile" / "user.md")
    _write(None, workspace / "user_memory" / "episodic" / "episodes.md")
    # Files L4 specifies but the legacy layout had no source for —
    # empty stubs; populated later by Sentinel / eval engine.
    _write(None, workspace / "agent_memory" / "procedural" / "skills.md")
    _write(None, workspace / "agent_memory" / "procedural" / "case.md")
    _write(None, workspace / "user_memory" / "attention.md")
    _write(None, workspace / "user_memory" / "behaviors.md")
    _write(tpl / "TOOLS.md", workspace / "TOOLS.md")
    _write(tpl / "HEARTBEAT.md", workspace / "HEARTBEAT.md")
    (workspace / "skills").mkdir(exist_ok=True)

    if added:
        for name in added:
            logger.debug("workspace sync: created {}", name)
    if added and not silent:
        label = "Initialized workspace" if existed == 0 else "Updated workspace templates"
        (notify or _stderr_line)(f"{label} ({len(added)} file{'s' if len(added) != 1 else ''})")
    return added


__all__ = ["sync_workspace_templates"]
