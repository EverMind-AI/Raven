"""Workspace template sync: the bundled ``templates/`` package data copied into
Agent home once, creating only what is missing.

One exception: ``SOUL.md`` carries the host's voice rules, so a copy that still
holds a previous release's text is replaced on sync. A file the operator edited
is kept and named, and their own lines belong in ``soul.local.md`` beside it.
A receipt records the text this package last wrote, which is what lets a later
release recognise its own copy and refresh it.
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

#: Every ``SOUL.md`` this package shipped before the file became product-owned.
#: A workspace copy matching one of these is the product's to replace; anything
#: else is the operator's and is left alone.
_SOUL_LEGACY_DIGESTS = frozenset(
    {
        "5555e9bf0abd1fd43b6214cc785a3df4c7bb45504c6e3fbd58312c96a13f66d1",
        "d1dcf24ed3a81d791f892c71936a0375f00bb3c4a5dbb4b998d44b2ca50586bb",
    }
)


def _stderr_line(message: str) -> None:
    """Where a report lands when no host supplied a renderer: one plain line on stderr."""
    print(f"  {message}", file=sys.stderr)


def sync_workspace_templates(
    workspace: Path, silent: bool = False, *, notify: "Callable[[str], None] | None" = None
) -> list[str]:
    """Sync bundled templates to Agent home, creating what is missing.

    ``SOUL.md`` is the exception: it is the host's voice, so a copy a previous
    release wrote is refreshed in place. ``notify`` receives one plain sentence
    per file written or kept ("Initialized workspace (3 files)"); the host
    decides how to show it. Nothing here owns a terminal.
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

    say = notify if notify is not None else (None if silent else _stderr_line)

    def _refresh(src, dest: Path):
        """Replace a product-owned prompt that still holds a release's text."""
        nonlocal existed
        if not src.is_file():
            return
        if dest.exists():
            existed += 1
        from raven.config.product_render import write_generated

        action = write_generated(
            dest,
            src.read_text(encoding="utf-8"),
            owner="raven",
            legacy_digests=_SOUL_LEGACY_DIGESTS,
            adopt=False,
            notify=say,
        )
        if action == "written":
            name = str(dest.relative_to(workspace))
            # A legacy root copy was just migrated to this same file; the
            # refresh is the write worth reporting, not the copy before it.
            added[:] = [item for item in added if item != name and not item.startswith(f"{name} (")]
            added.append(name)

    # Step 1 — migrate legacy workspace files into the L4 layout. Each
    # rule fires only when the legacy file exists and the L4 target is
    # still missing, so user edits made directly to L4 paths win.
    _migrate(workspace / "memory" / "MEMORY.md", workspace / "user_memory" / "profile" / "user.md")
    _migrate(workspace / "memory" / "HISTORY.md", workspace / "user_memory" / "episodic" / "episodes.md")
    _migrate(workspace / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
    _migrate(workspace / "AGENTS.md", workspace / "agent_memory" / "profile" / "agent.md")
    _migrate(workspace / "USER.md", workspace / "user_memory" / "profile" / "user.md")
    # attention.md and behaviors.md are written under user_memory/; a copy left
    # at the workspace root by an older install is migrated in. The Sentinel
    # rewrites attention.md from its producers each tick, so the migrated file
    # is a head start for the next refresh rather than the record.
    _migrate(workspace / "ATTENTION.md", workspace / "user_memory" / "attention.md")
    _migrate(workspace / "BEHAVIORS.md", workspace / "user_memory" / "behaviors.md")
    _migrate(workspace / "BEHAVIOR.md", workspace / "user_memory" / "behaviors.md")

    # Step 2 — fall back to bundled templates for anything still missing.
    # L4 pillar files first; root-level files (TOOLS / HEARTBEAT) stay put.
    _refresh(tpl / "SOUL.md", workspace / "agent_memory" / "profile" / "soul.md")
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
            logger.debug("workspace sync: wrote {}", name)
    if added and not silent:
        label = "Initialized workspace" if existed == 0 else "Updated workspace templates"
        (notify or _stderr_line)(f"{label} ({len(added)} file{'s' if len(added) != 1 else ''})")
    return added


__all__ = ["sync_workspace_templates"]
