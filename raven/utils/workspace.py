"""Workspace template sync: the bundled ``templates/`` package data copied into
Agent home once, creating only what is missing.

``SOUL.md`` is the host's voice as well as a template, so it is not create-only.
The sync keeps the product text it last wrote beside the file
(``.soul.md.template``) and, when a release ships a new voice, merges the
product's changes into the user's copy. A product hunk that conflicts with a
user edit leaves every section it touches as the user has it, including a
heading rename, and is named in the notice; every other hunk follows the new
release. The operator's own lines may also live in ``soul.local.md``; the sync
records the block it appended, withdraws that block on the next sync while it is
still at the end of the file, and appends the file's current contents, so edits
and removal take effect.
"""

import hashlib
import re
import stat
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from loguru import logger

from raven.utils.atomic_io import atomic_replace, back_up_file

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
_SOUL_ADDENDUM = ".soul.md.addendum"
_SOUL_LOCAL = "soul.local.md"


def _stderr_line(message: str) -> None:
    """Where a report lands when no host supplied a renderer: one plain line on stderr."""
    print(f"  {message}", file=sys.stderr)


def _digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fence_open(line: str) -> tuple[str, int] | None:
    match = _FENCE.match(line)
    if match is None:
        return None
    marker = match.group(1)
    if marker[0] == "`" and "`" in match.group(2):
        return None
    return marker[0], len(marker)


def _fence_closes(line: str, fence: tuple[str, int]) -> bool:
    marker, length = fence
    match = re.match(r"^ {0,3}([`~]+)[ \t]*$", line)
    return match is not None and match.group(1)[0] == marker and len(match.group(1)) >= length


def _heading_lines(lines: list[str]) -> list[int]:
    """The line indices of section headings, fenced code and quotes excluded.

    Setext headings are deliberately not recognised: the shipped SOUL texts use
    ATX headings, and treating ``---`` / ``===`` as a heading would misread a
    horizontal rule in a body. A base that uses setext stays one section, which
    makes the merge keep too much rather than move a heading over a user's body.
    """
    headings: list[int] = []
    fence: tuple[str, int] | None = None
    for index, raw in enumerate(lines):
        line = raw.rstrip("\n")
        if fence is not None:
            if _fence_closes(line, fence):
                fence = None
            continue
        opened = _fence_open(line)
        if opened is not None:
            fence = opened
            continue
        if line.lstrip().startswith(">"):
            continue
        if _ATX_HEADING.match(line):
            headings.append(index)
    return headings


def _section_of_line(headings: list[int], index: int, line_count: int) -> int:
    """The section holding ``index``; ``-1`` is the text before the first heading."""
    if not headings:
        return -1
    if index >= line_count:
        return len(headings) - 1
    return bisect_right(headings, index) - 1


def _section_of_gap(headings: list[int], index: int) -> int:
    """The section an insertion at ``index`` belongs to.

    At a heading line the insertion goes before that heading, so it belongs to
    the section above; there is no section of its own to conflict with.
    """
    if not headings:
        return -1
    return bisect_left(headings, index) - 1


def _inserted_heading(lines: list[str], i1: int, i2: int) -> bool:
    for raw in lines[i1:i2]:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        return bool(_ATX_HEADING.match(line))
    return False


def _sections_touched(
    lines: list[str], headings: list[int], i1: int, i2: int, line_count: int, *, inserted_heading: bool
) -> tuple[int, ...]:
    """Every section a base range touches, so a conflict can skip all of them."""
    if i1 == i2:
        gap = _section_of_gap(headings, i1)
        if not inserted_heading:
            return (gap,)
        after = _section_of_line(headings, i1, line_count) if i1 < line_count else gap
        return (gap,) if after == gap else (gap, after)
    content = [index for index in range(i1, i2) if lines[index].strip()]
    if content:
        first = _section_of_line(headings, content[0], line_count)
        last = _section_of_line(headings, content[-1], line_count)
    else:
        first = _section_of_line(headings, i1, line_count)
        last = _section_of_line(headings, i2 - 1, line_count)
    return tuple(range(first, last + 1))


def _section_title(lines: list[str], headings: list[int], section: int) -> str:
    if section < 0:
        return "the opening lines"
    return lines[headings[section]].strip()


def _already_applied(
    user_ops: list[tuple],
    ours_lines: list[str],
    theirs_lines: list[str],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> bool:
    """Whether the user's replacement is already exactly the product's text."""
    for tag, a1, a2, b1, b2 in user_ops:
        if tag == "replace" and a1 == i1 and a2 == i2:
            return ours_lines[b1:b2] == theirs_lines[j1:j2]
    return False


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
    be applied because the user edited a section they touch. A product hunk is
    attributed to every base section it touches and is skipped whole, so a
    heading rename and its body move together and a new heading never sits above
    an old body. Both sides appending at end of file is not a conflict: the
    product's text goes first. A product-only append also lands at the end.
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
    headings = _heading_lines(base_lines)
    user_ops = SequenceMatcher(None, base_lines, ours_lines, autojunk=False).get_opcodes()
    product_ops = [
        op for op in SequenceMatcher(None, base_lines, theirs_lines, autojunk=False).get_opcodes() if op[0] != "equal"
    ]
    conflicts: list[str] = []
    conflict_sections: set[int] = set()
    ops: list[tuple[tuple, tuple[int, ...]]] = []
    for op in product_ops:
        tag, i1, i2, j1, j2 = op
        inserted_at_eof = i1 == i2 == len(base_lines) and _user_insert_at(user_ops, i1) is not None
        appended_heading = tag == "insert" and i1 == i2 == len(base_lines) and _inserted_heading(theirs_lines, j1, j2)
        if inserted_at_eof or appended_heading:
            sections: tuple[int, ...] = ()
        else:
            sections = _sections_touched(
                base_lines,
                headings,
                i1,
                i2,
                len(base_lines),
                inserted_heading=tag == "insert" and _inserted_heading(theirs_lines, j1, j2),
            )
        ops.append((op, sections))
        if _conflicts_with_user(user_ops, i1, i2) and not inserted_at_eof:
            if _already_applied(user_ops, ours_lines, theirs_lines, i1, i2, j1, j2):
                continue
            conflict_sections.update(sections)
            for section in sections:
                name = _section_title(base_lines, headings, section)
                if name not in conflicts:
                    conflicts.append(name)
    for (tag, i1, i2, j1, j2), sections in sorted(ops, key=lambda pair: (pair[0][1], pair[0][2]), reverse=True):
        if conflict_sections.intersection(sections):
            continue
        if i1 == i2 == len(base_lines):
            oj = _user_insert_at(user_ops, i1)
            if oj is not None:
                ours_lines[oj:oj] = theirs_lines[j1:j2]
            elif ours_lines:
                if not ours_lines[-1].endswith("\n"):
                    ours_lines[-1] += "\n"
                ours_lines.extend(theirs_lines[j1:j2])
            continue
        mapped = _mapped_range(user_ops, i1, i2)
        if mapped is None:
            continue
        oj1, oj2 = mapped
        if ours_lines[oj1:oj2] != base_lines[i1:i2]:
            continue
        ours_lines[oj1:oj2] = theirs_lines[j1:j2]
    return "".join(ours_lines), conflicts


def _with_addendum(text: str, addendum: str | None) -> str:
    if not addendum:
        return text
    block = addendum.strip("\n")
    if not block or block in text:
        return text
    return text.rstrip("\n") + "\n\n" + block + "\n"


def _strip_block(text: str, block: str | None) -> str:
    """Remove one managed addendum block from the file's tail, if it is there."""
    if not block:
        return text
    block = block.strip("\n")
    if not block:
        return text
    for suffix in (f"\n\n{block}\n", f"\n\n{block}"):
        if text.endswith(suffix):
            return text[: -len(suffix)] + "\n"
    return text


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_addendum_record(target: Path, addendum: str | None, mode: int) -> None:
    """Record the block appended this sync, so the next one can withdraw it."""
    record = target.with_name(_SOUL_ADDENDUM)
    block = (addendum or "").strip("\n")
    if not block:
        record.unlink(missing_ok=True)
        return
    wanted = block + "\n"
    if _read_optional(record) == wanted:
        return
    atomic_replace(record, wanted, mode=mode, newline="", like=target.with_name(_SOUL_LOCAL))


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


def _receipt_digests(target: Path) -> set[str]:
    """Every text digest a generated-file receipt beside ``target`` names.

    The owner is deliberately not filtered: each product launcher writes its own
    name (``raven-code``, ``raven-research-ng``, ...), and a receipt that names
    this file means another product wrote it.
    """
    receipt = target.with_name(f".{target.name}.generated.sha256")
    try:
        lines = receipt.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    return {parts[1] for parts in (line.split() for line in lines) if len(parts) == 2}


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
    receipt = _receipt_digests(target)
    if receipt:
        for digest, text in templates.items():
            if digest in receipt:
                return text
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
        # Text-mode writing translates the \n inside a decoded \r\n again on
        # Windows (CR CR LF); write the decoded bytes as they are.
        dest.write_bytes(text.encode("utf-8"))
        added.append(f"{dest.relative_to(workspace)} (migrated from {src.relative_to(workspace)})")

    def _back_up(target: Path) -> Path:
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
        try:
            addendum_mode = stat.S_IMODE(local.stat().st_mode)
        except OSError:
            addendum_mode = 0o600
        if target.exists():
            existed += 1
            try:
                current = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                if say is not None:
                    say(f"could not read {target}: it is not UTF-8 text; save it as UTF-8")
                return
        else:
            current = None
        if current is None:
            atomic_replace(target, _with_addendum(template, addendum), newline="")
            _write_base(target, template)
            _write_addendum_record(target, addendum, addendum_mode)
            added.append(str(target.relative_to(workspace)))
            return
        wanted = _with_addendum(template, addendum)
        if current.replace("\r\n", "\n") == wanted:
            _write_base(target, template)
            _write_addendum_record(target, addendum, addendum_mode)
            return
        previous = _read_optional(target.with_name(_SOUL_ADDENDUM))
        body = _strip_block(current, previous)
        if body == current and addendum:
            body = _strip_block(current, addendum)
        templates = _soul_templates(tpl)
        current_digest = _digest(body)
        # Another product's generated prompt is not this sync's to rewrite. A
        # receipt naming a shipped text is a base instead, which _soul_base reads.
        if current_digest not in templates and current_digest in _receipt_digests(target):
            return
        base = _soul_base(target, body, templates)
        if base is None:
            if say is not None:
                say(
                    f"left {target} as it is: nothing shows a Raven release wrote it. "
                    f"If you edited a Raven prompt, move those lines into {local} and delete it."
                )
            return
        merged, conflicts = _merge_soul(base, body, template)
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
        _write_addendum_record(target, addendum, addendum_mode)
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
    try:
        _refresh_soul()
    except OSError as exc:
        if say is not None:
            say(f"could not refresh {workspace / 'agent_memory' / 'profile' / _SOUL}: {exc}")
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
