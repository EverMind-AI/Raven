# -*- coding: utf-8 -*-
"""Skills named on a node, delivered to an agent whose skill menu raven does not control.

A node's ``skills`` is a list of names on this machine's skill catalog. For a
built-in raven loop the list narrows the menu that loop is shown, which is the
cheapest possible delivery and needs nothing here. Every other agent -- an acp
peer, raven's own coding agent among them, a cli agent -- opens its own session
with its own catalog, and a name raven passes it resolves to nothing there. For
years the answer was a notice saying the list was ignored, which is a playbook
that declares a capability the run then quietly does not have.

The delivery that works for every agent that reads a prompt is the prompt, and
what goes into it is the same thing a built-in loop's menu holds: each skill's
name, one-line description and where its SKILL.md is, with the instruction to
read the one that applies. Not the body -- a skill is discovered progressively,
and ten named skills pasted whole would be ten documents the step has to read
before it starts. That is what the playbook specification promised ("folded
into this step's promptTemplate before dispatch").

Where the file is, is inside the step's own working directory: the skill's
directory is copied to :data:`SKILLS_DIR` there and the menu names the relative
path. An agent confined to its working directory -- a raven peer with
``restrictToWorkspace``, a cli agent's sandbox -- can open that where it could
not open the host's skills tree, and the host does not have to know which
agents are confined. The body is quoted only for an agent declared unable to
read local files at all (``reads_local_files`` false), because for it no path
is any good; then one skill is cut at :data:`SKILL_BODY_CAP` and the section at
:data:`SECTION_CAP`, and a cut says so. A name the catalog does not have is a
notice rather than a refusal, on the terms every capability gap has: a playbook
written on a better-equipped machine still runs here with the parts that work,
and the caller is told which part did not.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.subagent.role import WITHHELD_FROM_SUBAGENT
from raven.memory_engine import filter_by_required_tools

__all__ = ["SECTION_CAP", "SKILLS_DIR", "SKILL_BODY_CAP", "fold_skills", "skills_section"]

#: Where a step's skills are placed inside its working directory, so the agent
#: running the step can open them whatever its sandbox allows outside that
#: directory. ``.raven/`` is the host's own corner of a project -- the shadow
#: repository lives there too -- which a stint's commits leave out and its
#: boundary pass does not grade, so a copy here dirties nothing and is nobody's
#: stray write.
SKILLS_DIR = ".raven/skills"

#: Characters of one SKILL.md body that reach a prompt before it is cut.
SKILL_BODY_CAP = 6000
#: Characters the whole section may take, across every skill a node names.
SECTION_CAP = 16000

HEADING = "## Skills for this step"
LEAD = (
    "These are the skills for this step, and the only ones: do not search your own catalog for others. "
    "Read the SKILL.md of the one that fits with your file tool before you start; its directory holds "
    "whatever else it refers to. If you cannot open the file, say so in your output rather than guessing "
    "at what it says."
)
LEAD_QUOTED = (
    "These are the skills for this step, and the only ones: do not search your own catalog for others. "
    "They are quoted whole because this session cannot read the files they live in. Follow the one that fits."
)
NONE = f"{HEADING}\n\nThis step uses no skills. Do not reach for your own catalog."


class SkillLike(Protocol):
    name: str
    description: str
    path: Any
    content: str
    source: str


class CatalogLike(Protocol):
    """A ``LocalSkillCatalog`` (whose entries hang off ``registry``), or anything with ``list_all``."""


def _entries(catalog: Any) -> list[Any]:
    registry = getattr(catalog, "registry", None)
    source = registry if registry is not None and hasattr(registry, "list_all") else catalog
    return list(source.list_all())


def _match(names: Iterable[str], catalog: CatalogLike) -> tuple[list[SkillLike], list[str]]:
    """The catalog entries the names pick, in the order named, and the names that picked nothing.

    A name is the directory name (``game-testing``) or the qualified id a skill
    tool answers with (``local/game-testing``); both are how a person refers to
    a skill, and refusing one spelling would send them to look up the other.
    """
    entries = _entries(catalog)
    found: list[SkillLike] = []
    missing: list[str] = []
    for name in names:
        wanted = name.strip()
        bare = wanted.rsplit("/", 1)[-1]
        hit = next((entry for entry in entries if entry.name == wanted), None) or next(
            (entry for entry in entries if entry.name == bare), None
        )
        if hit is None:
            missing.append(wanted)
        elif hit not in found:
            found.append(hit)
    return found, missing


def _gated(found: Sequence[SkillLike]) -> tuple[list[SkillLike], list[tuple[str, str]]]:
    """The skills this step may actually be shown, and the ones the tool gate holds back.

    This is the third surface that renders skills into a prompt, and the gate's
    own docstring says why it has to be the same one: a per-surface copy is how
    "the DAG guide must never reach a sub-agent" ends up enforced by a hardcoded
    list of names on one side and by the declaration on the other.

    The gate normally takes what a reader *has*, which this surface cannot know
    -- an acp or cli peer opens its own session with its own tools. What it can
    know is what no sub-agent is ever given: ``WITHHELD_FROM_SUBAGENT`` is a
    specification, not a per-backend fact, and the DAG guide declares exactly
    one of those. So the gate is asked the question this surface can answer.

    Held back is said rather than done quietly: a name the caller wrote is
    already reported when the catalog lacks it, and a name dropped here without
    a word would read as a skill that silently did nothing.
    """
    kept = filter_by_required_tools(list(found), None, denied=WITHHELD_FROM_SUBAGENT)
    keep = {id(entry) for entry in kept}
    withheld = [
        (entry.name, ", ".join(t for t in _wanted_tools(entry) if t in WITHHELD_FROM_SUBAGENT))
        for entry in found
        if id(entry) not in keep
    ]
    return kept, withheld


def _wanted_tools(entry: SkillLike) -> list[str]:
    """The tools this skill declares, for the notice that names them.

    Read here only to say *which* tool held a skill back. What is held back is
    the gate's answer, never this: a second reading of ``requires`` that decided
    anything would be the per-surface copy the gate exists to prevent.
    """
    raw = (getattr(entry, "requires", None) or {}).get("tools")
    if isinstance(raw, str):
        return [raw]
    return [str(tool) for tool in (raw or [])]


def _folder_for(entry: SkillLike, source: Path) -> str:
    """The one directory name this skill is copied under.

    The skill's own directory, never ``entry.name``. A catalog entry's name is
    what the skill's SKILL.md frontmatter declares -- the registry falls back to
    the directory only when that field is absent -- so building the copy target
    from it let the skill file choose where it was written: ``../../..`` walks
    out of the working directory, and an absolute name discards the base
    entirely, since ``Path(base) / "/etc/x"`` is ``/etc/x``. A skill is a
    distribution unit and the name it advertises is the name a generated
    playbook carries, so the string was never the host's to trust.

    ``source`` is the SKILL.md the catalog resolved, so its parent is a real
    directory on this machine and a single path component by construction.
    """
    folder = source.parent.name
    return folder or Path(str(entry.name)).name


def _inside(target: Path, root: Path) -> bool:
    """Whether the copy lands under the step's own skills directory.

    Belt to :func:`_folder_for`'s braces, and the thing that makes a refusal
    sayable: what is refused here goes into the notices the caller already
    reads, rather than landing somewhere nobody looks.
    """
    try:
        return target.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _place(found: Sequence[SkillLike], workdir: Path | None) -> tuple[list[tuple[SkillLike, str]], list[str]]:
    """Each skill with the location the menu should name, and what could not be placed.

    With a working directory, the skill's whole directory is copied under
    :data:`SKILLS_DIR` there and the location is the relative path, so the file
    and everything beside it are inside whatever the agent may read. Copied
    over an earlier copy rather than around it, so a skill edited between two
    rounds reaches the next one. Without a working directory, or when the copy
    fails, the location is the catalog's own absolute path -- true, if not
    always readable -- and the failure is said.
    """
    placed: list[tuple[SkillLike, str]] = []
    problems: list[str] = []
    for entry in found:
        source = Path(entry.path)
        if workdir is None or not source.is_file():
            placed.append((entry, str(entry.path)))
            continue
        folder = _folder_for(entry, source)
        target = Path(workdir) / SKILLS_DIR / folder
        if not _inside(target, Path(workdir) / SKILLS_DIR):
            problems.append(
                f"skill '{entry.name}' names a directory outside the step's skills directory and was not copied"
            )
            placed.append((entry, str(entry.path)))
            continue
        try:
            shutil.copytree(
                source.parent,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc"),
            )
        except OSError as exc:
            problems.append(f"skill '{entry.name}' could not be copied into the working directory ({exc})")
            placed.append((entry, str(entry.path)))
            continue
        placed.append((entry, f"{SKILLS_DIR}/{folder}/{source.name}"))
    return placed, problems


def _menu(placed: Sequence[tuple[SkillLike, str]]) -> str:
    """The same directory a built-in loop is shown: name, description, location.

    Progressive on purpose: the body is not here. A skill is read when the step
    decides it applies, which is how the menu works for a built-in agent and
    what keeps ten named skills from being ten pasted documents.
    """
    lines = ["<skills>"]
    for entry, location in placed:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape(entry.name)}</name>")
        lines.append(f"    <description>{_escape(entry.description or entry.name)}</description>")
        lines.append(f"    <location>{location}</location>")
        lines.append("  </skill>")
    lines.append("</skills>")
    return "\n".join(lines)


def _quoted(found: Sequence[SkillLike]) -> str:
    """Every skill's body, for a session with no way to open the file itself."""
    parts: list[str] = []
    spent = 0
    for entry in found:
        body = (entry.content or "").strip()
        cut = ""
        if len(body) > SKILL_BODY_CAP:
            body = body[:SKILL_BODY_CAP].rstrip()
            cut = f"\n\n(cut here; the rest is in {entry.path})"
        block = f"### {entry.name}\n\n{entry.description}\n\nFile: {entry.path}\n\n{body}{cut}"
        if spent + len(block) > SECTION_CAP:
            parts.append(
                f"### {entry.name}\n\n{entry.description}\n\nFile: {entry.path}\n\n"
                f"(not quoted: this step's skills already fill their budget)"
            )
            continue
        parts.append(block)
        spent += len(block)
    return "\n\n".join(parts)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def skills_section(
    names: Iterable[str],
    catalog: CatalogLike,
    *,
    quote_bodies: bool = False,
    workdir: str | Path | None = None,
) -> tuple[str, list[str]]:
    """The prompt section for ``names``, and what went wrong: names the catalog lacks, copies that failed.

    The menu by default -- what a built-in loop sees -- with each skill placed
    under ``workdir`` when one is given (see :func:`_place`); the bodies only
    for ``quote_bodies``, which a caller passes for an agent that cannot read
    local files at all and so has no way to open any file the menu points at.

    Empty text for an empty list: ``skills: []`` means "no skills", and a
    heading over nothing would read as a list somebody forgot to fill.
    """
    found, missing = _match(names, catalog)
    problems = [f"skill '{name}' is not on this machine's catalog" for name in missing]
    found, withheld = _gated(found)
    problems.extend(
        f"skill '{name}' needs a tool no sub-agent is given ({tools}) and was left out of this step" for name, tools in withheld
    )
    if not found:
        return "", problems
    if quote_bodies:
        return f"{HEADING}\n\n{LEAD_QUOTED}\n\n{_quoted(found)}", problems
    placed, failed = _place(found, Path(workdir) if workdir is not None else None)
    return f"{HEADING}\n\n{LEAD}\n\n{_menu(placed)}", [*problems, *failed]


def fold_skills(
    spec: SubAgentDagSpec,
    capabilities: Mapping[str, Any],
    catalog: CatalogLike,
    workdir: str | Path | None = None,
) -> tuple[SubAgentDagSpec, list[str]]:
    """The graph with every menu-less node's skills folded into its prompt, and the notices.

    ``workdir`` is the nodes' working directory; given, each skill is copied
    under it so the menu can point inside it.

    A node whose agent takes an injected menu (``injectable_skills``) is left
    alone: its list reaches the loop as ``skills_allow`` and the menu says the
    same thing this section would. An agent absent from ``capabilities`` is not
    touched either, for the reason the other capability checks skip it -- there
    is nothing known to decide by, and ``run_dag`` will refuse the name itself.

    The section reads as the step's whole skill menu, not an addition to the
    agent's own -- "these and no others" -- which is as close to the narrowing a
    built-in loop gets as text can come: the agent's own catalog is its own, and
    a session the host does not build cannot be made to forget it. An empty
    list says so too, as ``skills: []`` does for a built-in loop by hiding the
    menu.
    """
    notices: list[str] = []
    nodes: list[DagNodeSpec] = []
    changed = False
    for node in spec.nodes:
        caps = capabilities.get(node.subagent)
        if node.skills is None or caps is None or getattr(caps, "injectable_skills", True):
            nodes.append(node)
            continue
        if not node.skills:
            nodes.append(node.model_copy(update={"prompt_template": f"{node.prompt_template.rstrip()}\n\n{NONE}\n"}))
            changed = True
            continue
        section, problems = skills_section(
            node.skills,
            catalog,
            quote_bodies=not getattr(caps, "reads_local_files", True),
            workdir=workdir,
        )
        for problem in problems:
            notices.append(f"node '{node.id}' (agent '{node.subagent}'): {problem}")
        if not section:
            nodes.append(node)
            continue
        nodes.append(node.model_copy(update={"prompt_template": f"{node.prompt_template.rstrip()}\n\n{section}\n"}))
        changed = True
    if not changed:
        return spec, notices
    return spec.model_copy(update={"nodes": nodes}), notices
