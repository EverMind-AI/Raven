"""Workspace template sync: the bundled ``templates/`` package data copied into
Agent home once, creating only what is missing.

``SOUL.md`` is the host's voice as well as a template, so it is not create-only.
The sync keeps the product text it last wrote beside the file
(``.soul.md.template``) and, when a release ships a new voice, merges the
product's changes into the user's copy. A product change that conflicts with a
user edit keeps that section as the user has it and is named in the notice;
every other change follows the new release. The operator's own lines may also
live in ``soul.local.md``, which is appended after the product text and never
rewritten.
"""

import hashlib
import sys
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from loguru import logger

from raven.utils.atomic_io import atomic_replace

# Workspace sync runs before the CLI decides logger.enable/disable("raven"),
# so an unscoped debug in this module would spam stderr through loguru's
# default sink on every first run. A later logger.enable("raven") still
# lifts this rule (loguru drops descendant rules whenever a parent rule is
# set), but the CLI flips logging only after its startup sync -- so the
# per-file detail below reaches callers that enable logging before syncing
# (tests, embedders), not the first sync of a `--logs` run.
logger.disable(__name__)

_SOUL = "soul.md"
_SOUL_BASE = ".soul.md.template"
_SOUL_LOCAL = "soul.local.md"


def _stderr_line(message: str) -> None:
    """Where a report lands when no host supplied a renderer: one plain line on stderr."""
    print(f"  {message}", file=sys.stderr)


def _digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _heading(lines: list[str], i1: int, i2: int) -> str:
    """The heading a product hunk belongs to, for a conflict notice."""
    for line in lines[i1:i2]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped
    for line in reversed(lines[: min(i1, len(lines) - 1) + 1]):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped
    return "the opening lines"


def _conflicts_with_user(user_ops: list[tuple], i1: int, i2: int) -> bool:
    """Whether the user changed any base line the product hunk also touches."""
    for tag, a1, a2, _b1, _b2 in user_ops:
        if tag == "equal":
            continue
        if i1 == i2:
            if a1 == a2 == i1 or a1 < i1 < a2:
                return True
        elif a1 == a2:
            if i1 <= a1 < i2:
                return True
        elif a1 < i2 and i1 < a2:
            return True
    return False


def _mapped_range(user_ops: list[tuple], i1: int, i2: int) -> tuple[int, int] | None:
    """The ``ours`` line range for a base range the user did not change."""
    for tag, a1, a2, b1, _b2 in user_ops:
        if tag == "equal" and a1 <= i1 and i2 <= a2:
            return b1 + (i1 - a1), b1 + (i2 - a1)
    if i1 == i2:
        for tag, a1, a2, b1, b2 in user_ops:
            if tag != "equal" and a2 == i1:
                return b2, b2
            if tag != "equal" and a1 == i1:
                return b1, b1
    return None


def _user_insert_at(user_ops: list[tuple], position: int) -> int | None:
    for tag, a1, a2, b1, _b2 in user_ops:
        if tag != "equal" and a1 == a2 == position:
            return b1
    return None


def _merge_soul(base: str, ours: str, theirs: str) -> tuple[str, list[str]]:
    """Apply the base-to-theirs changes to ours, keeping the regions ours changed.

    Returns the merged text and the headings of product changes that could not
    be applied because the user edited the same region. A section a conflict
    lands in is left wholly as the user has it, so a new heading never sits
    above an old body. Both sides appending at end of file is not a conflict:
    the product's text goes first.
    """
    base = base.replace("\r\n", "\n")
    ours = ours.replace("\r\n", "\n")
    theirs = theirs.replace("\r\n", "\n")
    if ours == base:
        return theirs, []
    if theirs == base:
        return ours, []
    base_lines = base.splitlines(keepends=True)
    ours_lines = ours.splitlines(keepends=True)
    theirs_lines = theirs.splitlines(keepends=True)
    user_ops = SequenceMatcher(None, base_lines, ours_lines, autojunk=False).get_opcodes()
    product_ops = [
        op for op in SequenceMatcher(None, base_lines, theirs_lines, autojunk=False).get_opcodes() if op[0] != "equal"
    ]
    conflicts: list[str] = []
    for _tag, i1, i2, _j1, _j2 in product_ops:
        inserted_at_eof = i1 == i2 == len(base_lines) and _user_insert_at(user_ops, i1) is not None
        if _conflicts_with_user(user_ops, i1, i2) and not inserted_at_eof:
            name = _heading(base_lines, i1, i2)
            if name not in conflicts:
                conflicts.append(name)
    for _tag, i1, i2, j1, j2 in sorted(product_ops, key=lambda op: (op[1], op[2]), reverse=True):
        if _heading(base_lines, i1, i2) in conflicts:
            continue
        if i1 == i2 == len(base_lines):
            oj = _user_insert_at(user_ops, i1)
            if oj is not None:
                ours_lines[oj:oj] = theirs_lines[j1:j2]
            continue
        mapped = _mapped_range(user_ops, i1, i2)
        if mapped is None:
            continue
        oj1, oj2 = mapped
        if ours_lines[oj1:oj2] != base_lines[i1:i2]:
            continue
        ours_lines[oj1:oj2] = theirs_lines[j1:j2]
    unique: list[str] = []
    for name in reversed(conflicts):
        if name not in unique:
            unique.append(name)
    return "".join(ours_lines), unique


def _with_addendum(text: str, addendum: str | None) -> str:
    if not addendum:
        return text
    block = addendum.strip("\n")
    if not block or block in text:
        return text
    return text.rstrip("\n") + "\n\n" + block + "\n"


def _soul_templates(tpl) -> dict[str, str]:
    """Every shipped SOUL.md text this release can merge from, by digest.

    The current template covers a copy this release already wrote; the historical
    versions under ``legacy/`` cover an install that has not run this code yet.
    A release that changes SOUL.md copies the outgoing text into ``legacy/`` so
    the next install still has a base.
    """
    out: dict[str, str] = {}
    current = (tpl / "SOUL.md").read_text(encoding="utf-8")
    out[_digest(current)] = current
    legacy = tpl / "legacy"
    if legacy.is_dir():
        for entry in legacy.iterdir():
            if entry.is_file() and entry.name.endswith(".md"):
                text = entry.read_text(encoding="utf-8")
                out[_digest(text)] = text
    return out


def _receipt_digest(target: Path) -> str | None:
    """The product-text digest a previous release's generated-file receipt recorded."""
    receipt = target.with_name(f".{target.name}.generated.sha256")
    try:
        for line in receipt.read_text(encoding="ascii").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "raven":
                return parts[1]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _soul_base(target: Path, current: str, templates: dict[str, str]) -> str | None:
    """The product text the current copy was seeded from, or ``None`` if unknown."""
    base_path = target.with_name(_SOUL_BASE)
    try:
        return base_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    known = templates.get(_digest(current))
    if known is not None:
        return known
    receipt = _receipt_digest(target)
    if receipt is not None:
        known = templates.get(receipt)
        if known is not None:
            return known
    prefix = [text for text in templates.values() if current.startswith(text)]
    if prefix:
        return max(prefix, key=len)
    scored = [(SequenceMatcher(None, text, current, autojunk=False).ratio(), text) for text in templates.values()]
    ratio, closest = max(scored, key=lambda pair: pair[0])
    return closest if ratio >= 0.6 else None


def sync_workspace_templates(
    workspace: Path, silent: bool = False, *, notify: "Callable[[str], None] | None" = None
) -> list[str]:
    """Sync bundled templates to Agent home, creating what is missing.

    ``SOUL.md`` is the exception: it is the host's voice, so a copy a previous
    release wrote is merged forward, keeping the regions the user edited.
    ``notify`` receives one plain sentence per file written or kept
    ("Initialized workspace (3 files)"); the host decides how to show it.
    Nothing here owns a terminal.
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
    say = notify if notify is not None else (None if silent else _stderr_line)

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

    def _back_up(target: Path) -> Path:
        from raven.config.product_render import back_up_file

        return back_up_file(target, target.read_bytes())

    def _write_base(target: Path, template: str) -> None:
        base_path = target.with_name(_SOUL_BASE)
        try:
            if base_path.read_text(encoding="utf-8") == template:
                return
        except (OSError, UnicodeDecodeError):
            pass
        atomic_replace(base_path, template, newline="")

    def _refresh_soul():
        nonlocal existed
        target = workspace / "agent_memory" / "profile" / _SOUL
        template = (tpl / "SOUL.md").read_text(encoding="utf-8")
        local = target.with_name(_SOUL_LOCAL)
        addendum = None
        if local.is_file():
            try:
                addendum = local.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                if say is not None:
                    say(f"could not read {local}: it is not UTF-8 text; save it as UTF-8")
        if target.exists():
            existed += 1
            current = target.read_text(encoding="utf-8", errors="replace")
        else:
            current = None
        if current is None:
            atomic_replace(target, _with_addendum(template, addendum), newline="")
            _write_base(target, template)
            added.append(str(target.relative_to(workspace)))
            return
        wanted = _with_addendum(template, addendum)
        if current.replace("\r\n", "\n") == wanted:
            _write_base(target, template)
            return
        base = _soul_base(target, current, _soul_templates(tpl))
        if base is None:
            if say is not None:
                say(
                    f"left {target} as it is: nothing shows a Raven release wrote it. "
                    f"If you edited a Raven prompt, move those lines into {local} and delete it."
                )
            return
        merged, conflicts = _merge_soul(base, current, template)
        merged = _with_addendum(merged, addendum)
        if merged != current:
            backup = _back_up(target)
            atomic_replace(target, merged, newline="")
            name = str(target.relative_to(workspace))
            added[:] = [item for item in added if item != name and not item.startswith(f"{name} (")]
            added.append(name)
            if say is not None and current != base:
                say(f"{target} was edited; backed up to {backup}. Your lines stay in place.")
        _write_base(target, template)
        if conflicts and say is not None:
            say(
                f"{target} kept your edits where the new voice changed the same lines; not updated: {', '.join(conflicts)}"
            )

    # Step 1 — migrate legacy workspace files into the L4 layout. Each
    # rule fires only when the legacy file exists and the L4 target is
    # still missing, so user edits made directly to L4 paths win.
    _migrate(workspace / "memory" / "MEMORY.md", workspace / "user_memory" / "profile" / "user.md")
    _migrate(workspace / "memory" / "HISTORY.md", workspace / "user_memory" / "episodic" / "episodes.md")
    _migrate(workspace / "SOUL.md", workspace / "agent_memory" / "profile" / _SOUL)
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
    _refresh_soul()
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
