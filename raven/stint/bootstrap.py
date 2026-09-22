"""Turning a project into one a stint can work in.

Two phases, deliberately not one. ``init`` reads the **file tree** and lays out
the scaffolding: the ``.stint/`` directory, a link to whatever the project already
calls its specification, the facts this machine can be asked for, and a guard
file per role. ``plan`` reads the **prose** and produces the backlog.

They are separated for three reasons. Their inputs differ, as above. The place a
person has to stand differs: a wrong directory shows up next round, a wrong plan
takes ten rounds to surface, so the confirmation gate belongs on the plan. And
they re-run at different times -- a revised specification needs a re-plan, not a
re-scaffold.

This module is ``init``. It calls no model: everything here is either a fact
about the machine, a template with its slots filled, or a link. What it cannot
know -- what the work actually is -- is exactly what ``plan`` is for.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from raven.i18n.zh_lexicon import SAYS_WHAT_IS_REQUIRED as SAYS_WHAT_IS_REQUIRED_ZH
from raven.stint.backlog import STINT_DIR
from raven.stint.roster import DEFAULT_ROLES
from raven.stint.verify import DEFAULT_TIMEOUT_SEC, CheckSpec, resolve_display

SPEC_LINK = "SPEC.md"
DECISIONS_FILE = "HUMAN_DECISIONS.md"
FIXLOG_FILE = "FIXLOG.md"
PLAYBOOK_FILE = "PLAYBOOK.md"
AGENT_DECISIONS_FILE = "AGENT_DECISIONS.md"
SOURCES_FILE = "SOURCES.md"
REPORTS_DIR = "reports"

#: A document worth linking as the specification, most likely first. A project
#: that keeps several gets an index at ``SPEC.md`` instead, written by a person.
SPEC_HINTS = ("prd", "spec", "requirement", "design", "brief")

#: Words a document uses when it is saying what has to be true, in either
#: language the roles read. A specification is the thing that makes claims of
#: this shape; a changelog, a meeting note and an API reference do not.
_SAYS_WHAT_IS_REQUIRED = (
    "must",
    "should",
    "shall",
    "required",
    "requirement",
    "acceptance",
    "criteria",
    "goal",
    "scope",
    *SAYS_WHAT_IS_REQUIRED_ZH,
)

#: Below this a document has a name that promises a specification and a body
#: that does not carry one -- an empty `SPEC.md` stub is the case that matters,
#: because the name alone would send every round to plan from nothing.
_ENOUGH_TO_PLAN_FROM = 200

#: Directories that hold the thing being built, where a project has one of them.
#: Detected rather than assumed: a guard that hands the Builder ``src/**`` in a
#: project whose code is in ``project/`` gives it ownership of nothing it writes,
#: and every write it makes is then undone as a violation.
#:
#: The test directories are here for the same reason and not as an afterthought:
#: what the Builder is asked for is a check that passes, and on most projects the
#: check runs the tests. A grant that stops at the source has it write the code
#: and then have the tests it wrote for that code undone -- measured on a
#: greenfield run of this playbook, where the brief asked for tests by name and
#: the round's own check compiled `src tests`, so the layout was refusing the
#: write its own check depended on.
SOURCE_DIRS = ("src", "project", "lib", "app", "pkg", "cmd", "tools", "scripts", "assets", "tests", "test", "spec")

#: Where a round's work lands rather than where the source lives. Not owned by
#: anyone: a build directory and a program's own log are written by whichever role
#: last ran the thing, and the specification here has the build write
#: `demo_outputs/feel_log.jsonl` every time it runs in test mode. Granting them
#: to the Builder meant Verifier's measurements were reverted for having been made,
#: so they are declared as artifacts and left ungraded.
OUTPUT_DIRS = ("build", "builds", "dist", "out", "demo_outputs", "replays")

#: The same answer for what a *runtime* leaves behind rather than a build: a
#: role that runs the tests has its interpreter write caches beside the files it
#: read, and those are nobody's work. Graded as writes they are a violation the
#: role cannot avoid and cannot undo, reported every round it runs anything.
BYPRODUCT_GLOBS = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**")


class InitError(RuntimeError):
    """The project cannot be set up, and why."""


@dataclass
class Init:
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    spec: str = ""
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = []
        if self.created:
            parts.append("wrote " + ", ".join(self.created))
        if self.kept:
            parts.append("kept " + ", ".join(self.kept))
        return "; ".join(parts) or "nothing to do"


def says_what_is_required(text: str) -> int:
    """How much of this document is saying what has to be true.

    The second sieve. The name is the first and it is only a promise: a project
    with an empty ``SPEC.md`` stub has a document called the specification and
    nothing to plan from, and taking the name's word for it would send thirty
    rounds off a blank page. Counted rather than judged -- a count is cheap,
    testable, and does not need a model turn of its own to pick a file.

    Zero is a document that never says anything has to be true, whatever it is
    called. Nothing here is conclusive on its own, which is why the choice is
    shown to a person rather than acted on silently.
    """
    lowered = text.lower()
    return sum(lowered.count(word) for word in _SAYS_WHAT_IS_REQUIRED)


def find_spec(project: Path) -> list[Path]:
    """Documents that look like the specification, most likely first.

    Ranked rather than chosen: naming the project's requirements is a judgement,
    and getting it wrong silently would point every role at the wrong document
    for the life of the run.

    Two sieves, name then content. The name orders the candidates and the
    content decides whether a candidate is one at all: a file is dropped when it
    is too short to plan from or never says that anything has to be true, so
    "this project has no specification" is an answer about the documents rather
    than about their filenames.
    """
    found: list[tuple[int, int, Path]] = []
    # `.stint/` is searched because that is where a person is told to write one
    # when the project had none: a request to put the specification somewhere
    # nothing reads would be a loop with no way out of it.
    for folder in (project, project / "docs", project / STINT_DIR):
        if not folder.is_dir():
            continue
        # In `.stint/` only the specification itself. The rest of what lives
        # there is the layout's own -- standing orders full of "must" and
        # "should", which read to the content sieve exactly like requirements
        # and are requirements about the roles rather than about the project.
        inside = (
            [folder / SPEC_LINK] if folder.name == STINT_DIR else sorted(folder.iterdir(), key=lambda i: i.name.lower())
        )
        for path in inside:
            # A link is `.stint/SPEC.md` pointing at a document already in this
            # list; following it would offer one document twice and say two
            # others matched when none did.
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in (".md", ".markdown"):
                continue
            lowered = path.name.lower()
            if lowered in ("readme.md", "changelog.md", "claude.md", "agents.md"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text.strip()) < _ENOUGH_TO_PLAN_FROM or not (weight := says_what_is_required(text)):
                continue
            rank = next((index for index, hint in enumerate(SPEC_HINTS) if hint in lowered), len(SPEC_HINTS))
            found.append((rank, -weight, path))
    return [path for _, _, path in sorted(found)]


#: Root files that are about the repository rather than the project, and so
#: carry nothing a run plans from.
_NOT_DOCUMENTS = ("readme.md", "changelog.md", "license.md")


def project_documents(project: Path, *, spec: Path | None = None) -> list[str]:
    """Every Markdown document the project carried before H*, as paths relative to it.

    The root's own files and everything under `docs/`, at any depth -- the
    role folders a project grew up with (`docs/planner/`, `docs/verifier/`) are where
    a half-built project keeps what it learned, and a plan that never opened
    them re-derives it wrong. `.stint/` and `reports/` are H*'s own and are not
    documents to absorb; the specification is linked, not absorbed.
    """
    project = Path(project)
    skip = {str(spec.resolve())} if spec else set()
    found: list[str] = []
    candidates = [path for path in project.iterdir() if path.is_file()] if project.is_dir() else []
    docs = project / "docs"
    if docs.is_dir():
        candidates += [path for path in docs.rglob("*") if path.is_file()]
    # Real files before their aliases, so `AGENTS.md -> CLAUDE.md` lists CLAUDE.md.
    for path in sorted(candidates, key=lambda item: (item.is_symlink(), item)):
        if path.suffix.lower() not in (".md", ".markdown") or path.name.startswith("."):
            continue
        if path.parent == project and path.name.lower() in _NOT_DOCUMENTS:
            continue
        real = str(path.resolve())
        if real in skip:
            continue
        skip.add(real)
        found.append(path.relative_to(project).as_posix())
    return sorted(found)


#: Granted when a project has no source tree yet. A greenfield handover is the
#: normal way a stint starts, and its first round is the one that creates the
#: source -- so a guard listing only what exists today would have the Builder's
#: very first write undone as a violation.
GREENFIELD_DIRS = ("src/**", "project/**", "tools/**", "tests/**")

#: Directories that are somebody's work but not the thing being built, so having
#: one says nothing about where the source lives. A repository holding only
#: `tests/` is still a greenfield handover and gets the conventional set.
_NOT_SOURCE_BY_ITSELF = ("tools", "scripts", "tests", "test", "spec")


def source_dirs(project: Path) -> list[str]:
    """The directories this project keeps its work in, or the ones it is about to.

    Detected rather than assumed, because a guard that hands the Builder
    ``src/**`` in a project whose code lives in ``project/`` grants it nothing it
    writes. Where nothing is there yet, the conventional set is granted and the
    note asks a person to check it -- an empty grant is the one answer that
    cannot be right.
    """
    project = Path(project)
    found = [f"{name}/**" for name in SOURCE_DIRS if (project / name).is_dir()]
    has_source = any(f"{name}/**" in found for name in SOURCE_DIRS if name not in _NOT_SOURCE_BY_ITSELF)
    return found if has_source else sorted(set(found) | set(GREENFIELD_DIRS))


def _godot_bin() -> str:
    return os.environ.get("GODOT_BIN") or shutil.which("godot") or "godot"


def _project_root(workspace: Path) -> Path | None:
    for candidate in (workspace / "project", workspace):
        if (candidate / "project.godot").is_file():
            return candidate
    for path in sorted(workspace.glob("*/project.godot")):
        return path.parent
    return None


def detect_checks(workspace: Path, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> list[CheckSpec]:
    """The checks this tree affords, without being told.

    Godot first, because it is the one build this layout can recognise with no
    manifest to read; then whatever assertion and capture scripts the project ships
    under ``tools/``, which are the only things that measure timing or a frame.
    """
    checks: list[CheckSpec] = []
    project = _project_root(workspace)
    if project is not None:
        rel = project.relative_to(workspace).as_posix() or "."
        godot = _godot_bin()
        checks.append(
            CheckSpec(
                name="import",
                command=f"{godot} --path {rel} --headless --import",
                timeout_sec=timeout_sec,
            )
        )
        checks.append(
            CheckSpec(
                name="clean_boot",
                command=f"{godot} --path {rel} -- --clean-boot-test",
                timeout_sec=min(timeout_sec, 600.0),
                needs_display=True,
                seedable=True,
            )
        )
    for script, name, needs_display in (
        ("tools/assert_feel.py", "feel_assert", False),
        ("tools/capture.py", "capture", True),
        ("tools/image_thresholds.py", "image_thresholds", False),
    ):
        if (workspace / script).is_file():
            argument = {
                "feel_assert": " demo_outputs/feel_log.jsonl",
                "capture": " --all",
                "image_thresholds": " demo_outputs/screens/",
            }[name]
            checks.append(
                CheckSpec(
                    name=name,
                    command=f"python3 {script}{argument}",
                    timeout_sec=timeout_sec,
                    needs_display=needs_display,
                    seedable=name == "capture",
                )
            )
    if not checks:
        for marker, command in (
            ("package.json", "npm run build --if-present"),
            ("Cargo.toml", "cargo build"),
            ("pyproject.toml", "python -m compileall -q ."),
        ):
            if (workspace / marker).is_file():
                checks.append(CheckSpec(name="build", command=command, timeout_sec=timeout_sec))
                break
    return checks


def machine_facts() -> list[str]:
    """What this machine is, as lines for the facts section a person then checks.

    Drafted rather than asserted: a person confirms them, because "there is no
    GPU here" is the kind of thing a run plans around for twenty rounds.
    """
    display = resolve_display(None, start_xvfb=False)
    return [
        f"- Machine: {platform.system()} {platform.machine()}, Python {platform.python_version()}",
        f"- Display: {display.provider}" + (f" at `{display.name}`" if display.name else ""),
        f"- Blender: {shutil.which('blender') or 'not on this machine'}",
        f"- Node: {shutil.which('node') or 'not on this machine'}",
    ]


#: What the guard's how-it-runs section says when `raven playbook stint init` found nothing
#: to run. A new project never has a runnable check -- the checks are what its first
#: rounds build -- so this sentence outlives its truth, and a project that grows one
#: is expected to write it in here.
NO_CHECK_DETECTED = (
    "No runnable check was detected in this project. Say here how the build is run\n"
    "and how a claim about it is proved, or the roles will each invent their own way."
)


def _detected_commands(project: Path) -> str:
    detected = list(detect_checks(project, 1800.0))
    if not detected:
        return NO_CHECK_DETECTED
    lines = ["Detected in this project -- correct them if they are wrong:", ""]
    lines += [f"    {spec.name}: {spec.command}" for spec in detected]
    return "\n".join(lines)


def _slots(project: Path, role: str, *, enforce_read: str, enforce_write: str, spec_name: str) -> dict[str, str]:
    reads = {
        "planner": [".stint/SPEC.md", ".stint/HUMAN_DECISIONS.md", ".stint/FIXLOG.md", "reports/verify_{NN-1}.md"],
        "builder": [
            "reports/brief_{NN}.md",
            ".stint/SPEC.md",
            ".stint/HUMAN_DECISIONS.md",
            ".stint/PLAYBOOK.md",
            ".stint/FIXLOG.md",
        ],
        "verifier": ["reports/round_{NN}.md", "reports/brief_{NN}.md", ".stint/SPEC.md", ".stint/HUMAN_DECISIONS.md"],
    }[role]
    detected = source_dirs(project)
    owns = {
        "planner": [],
        "builder": [f"  - {item}" for item in detected],
        "verifier": ["  - reports/evidence/round_{NN}/**"],
    }[role]
    # Declared on every guard rather than one: the paths are the project's, and
    # a set one role could write and another could not is the same trap again.
    # Quoted, unlike the directory entries: a YAML scalar that opens with `*` is
    # an alias reference, so an unquoted `**/__pycache__/**` is a parse error in
    # the guard file this writes rather than the glob it reads as here.
    artifacts = [f"  - {name}/**" for name in OUTPUT_DIRS] + [f'  - "{glob}"' for glob in BYPRODUCT_GLOBS]
    session = {"planner": "fresh", "builder": "continue", "verifier": "fresh"}[role]
    del session  # named above for the reader; the note it chose now lives in the template
    return {
        # Data, not prose. What used to be here was a paragraph of English that a
        # zh guard could not translate: the template was rendered per language
        # and then filled with these, so a Chinese role read Chinese headings
        # over English instruction. Every value below is a name, a path or a
        # list; the sentences around them belong to the template that is
        # written in the language the role reads.
        "spec_name": spec_name,
        "reads": "\n".join(f"  - {item}" for item in reads),
        "commands": _detected_commands(project),
        "owns_project_paths": "\n".join(owns),
        "artifacts": "\n".join(artifacts),
        "enforce_read": enforce_read,
        "enforce_write": enforce_write,
    }


def _render_guard(project: Path, role: str, *, enforce_read: str, enforce_write: str, spec_name: str) -> str:
    from raven.i18n import prompt

    text = prompt(f"stint_orders_{role}")
    slots = _slots(project, role, enforce_read=enforce_read, enforce_write=enforce_write, spec_name=spec_name)
    for name, value in slots.items():
        text = text.replace("{{" + name + "}}", value)
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        raise InitError(f"the {role} template asks for slot(s) this build cannot fill: {', '.join(sorted(set(left)))}")
    return text


def _write(path: Path, body: str, result: Init, *, force: bool) -> None:
    label = str(path.relative_to(path.parents[1])) if len(path.parents) > 1 else path.name
    if path.exists() and not force:
        result.kept.append(label)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    result.created.append(label)


def init(
    project: Path,
    *,
    spec: Path | None = None,
    roles: Sequence[str] = DEFAULT_ROLES,
    enforce_read: str = "soft",
    enforce_write: str = "hard",
    force: bool = False,
) -> Init:
    """Lay out ``.stint/`` in a project, and return what changed."""
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise InitError(f"no project directory at {project}")
    home = project / STINT_DIR
    home.mkdir(parents=True, exist_ok=True)
    result = Init()

    candidates = find_spec(project)
    chosen = Path(spec).expanduser().resolve() if spec else (candidates[0] if candidates else None)
    if spec and not chosen.is_file():
        raise InitError(f"no specification at {chosen}")
    link = home / SPEC_LINK
    if chosen is None:
        result.notes.append(
            f"no specification document found: write one and link it as {link}, or the roles have nothing to plan from"
        )
    elif link.exists() or link.is_symlink():
        result.kept.append(f"{STINT_DIR}/{SPEC_LINK}")
        result.spec = str(os.readlink(link)) if link.is_symlink() else str(link)
    else:
        # A link, not a copy: the project keeps its own filename and version, and
        # a revision is a re-pointed link rather than an edit to three guards.
        link.symlink_to(os.path.relpath(chosen, home))
        result.created.append(f"{STINT_DIR}/{SPEC_LINK} -> {os.path.relpath(chosen, home)}")
        result.spec = str(chosen)
    if len(candidates) > 1:
        result.notes.append(
            "more than one document could be the specification ("
            + ", ".join(path.name for path in candidates[:4])
            + f"): {link} points at one of them, and a project with several wants an index there instead"
        )

    spec_name = Path(result.spec).name or "the project's specification"
    _write(
        home / DECISIONS_FILE,
        "# HUMAN_DECISIONS\n\n"
        "The one file no agent writes. What a person has settled, and what is still\n"
        "waiting on one.\n\n"
        "## Facts about this project\n\n"
        "Drafted by `raven playbook stint init`. Check each line: a run plans around these for\n"
        "the whole of its life.\n\n" + "\n".join(machine_facts()) + "\n\n"
        "## Settled\n\n"
        "_What has been decided, and may not be reopened by an agent._\n\n"
        "## Awaiting a person\n\n"
        "_Added by the roles as they hit something only a person can settle. Answer one\n"
        "from the page, or by ticking it here and writing the ruling under it._\n",
        result,
        force=force,
    )
    _write(
        home / AGENT_DECISIONS_FILE,
        "# AGENT_DECISIONS\n\nThe Builder's. Anything the specification and HUMAN_DECISIONS both leave\n"
        "open, decided here the same round it is decided in practice.\n",
        result,
        force=force,
    )
    _write(
        home / FIXLOG_FILE,
        "# FIXLOG\n\nDefects the automated checks caught and the Builder fixed. Verifier appends with\n"
        "`Verifier found`. The root cause column is the one the Planner reads.\n\n"
        "| # | round | caught by | symptom | root cause | fix | commit |\n"
        "|---|-------|-----------|---------|-----------|-----|--------|\n",
        result,
        force=force,
    )
    _write(
        home / PLAYBOOK_FILE,
        "# PLAYBOOK\n\nWhat this project learned the hard way. It outranks any general skill a role\n"
        "was given: where the two disagree, follow this.\n\n"
        "## Verifier proposals\n\n_QA appends here; the Builder promotes an entry into the body above._\n",
        result,
        force=force,
    )
    # The reading map the standing orders send every role to, written here rather
    # than by the planning step that fills it in. The orders say "read the map
    # before opening any of them", and the planning step is not built -- so every
    # role was told to open a file that does not exist, and the honest ones said
    # so in their reports instead of doing the work. Written from what the
    # project actually carries, so on a project that carries nothing the map says
    # that, which is the one thing a first round most needs to know.
    carried = project_documents(project, spec=chosen)
    _write(
        home / SOURCES_FILE,
        "# SOURCES\n\n"
        "The documents this project had before the run, and what each is for. A role\n"
        "reads this before opening any of them.\n\n"
        + (
            # Counted, not listed. `review` reads this file to say which documents
            # nothing has absorbed yet, and it asks whether a path is named here --
            # so listing them all would report every one as absorbed on the day the
            # project was laid out, which is the opposite of true.
            f"Nothing here has been described yet. The project carries {len(carried)} document(s);\n"
            "describing them is the planning step's, and a role that needs one before then\n"
            "opens it and says in its report that the map was silent.\n"
            if carried
            else "The project carried no documents of its own. "
            f"`{SPEC_LINK}` is the whole of what there is to plan from.\n"
        ),
        result,
        force=force,
    )
    for role in roles:
        _write(
            home / f"{role}.md",
            _render_guard(project, role, enforce_read=enforce_read, enforce_write=enforce_write, spec_name=spec_name),
            result,
            force=force,
        )
    (project / REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    granted = source_dirs(project)
    existing = [item for item in granted if (project / item.split("/")[0]).is_dir()]
    if len(existing) < len(granted):
        result.notes.append(
            "the Builder is granted "
            + ", ".join(granted)
            + f", some of which do not exist yet -- check them against where this project's work "
            f"will actually go, in {home / 'builder.md'}"
        )
    return result


# ------------------------------------------------------------------------ plan

PLAN_SENTINEL = "ROUNDS_PLAN_DONE"

PLAN_PROMPT = """You are setting up a stint on this project, and your one job is the plan.

Read the specification and what a person has already settled:

- `{spec}`
- `{decisions}`

## First, carry over what the project already knows

A project may arrive half-built, with its rules, lessons and open questions
spread over the documents it grew up with. From the first round on, the roles
read `.stint/` first and open those documents only where `.stint/` points them --
so before you plan, every decision, lesson and defect they hold has to be in
`.stint/`, and everything else has to be findable from it. The documents are:

{documents}

Read every one of them, and move what they hold by kind:

- a rule or decision that is settled -> `{decisions}`, under `## Settled`, one
  line each, ending in `(source: <the document>)`;
- a question nobody has answered -> `raven playbook stint ask "<the question>" --project
  {project}`, one call each; carry the leaning the document records as `--decide "..."`
  when it has one;
- a lesson, a pitfall, a convention, how the thing is run and proved -> the body
  of `{playbook}`;
- a defect that was found and fixed -> a row of `{fixlog}`;
- a choice an agent made where the specification was silent -> `{agent_decisions}`;
- reference material -- an asset plan, a scene brief, an event contract, a
  tuning table, anything a role consults rather than obeys -> is **not**
  copied. It gets a row in the reading map below, and the role opens the
  document itself when the map says to. Copying it made `.stint/` a second copy
  of the same pages, which is not what a reference is for;
- a reference image -- a scene reference, a concept sheet, anything under
  `refs/` -- is a document too, and the one a brief about it can never stand
  in for. It gets its own row, naming the tasks whose Builder and Verifier must
  open the image itself before building or judging, and what to look for in
  it. Put the same instruction in those tasks' notes.

Do not copy documents whole, and do not edit or delete them: they stay as they
were, read-only. When you are done, write `{sources}`: **the reading map**, a
table with one row per document above -- what the document is, in a line; what
was carried out of it and where; and when a role should still open it, naming
the sections that matter (or `nothing to carry` with the reason: a template, a
report superseded by a later one, a duplicate of the specification). A document
missing from that map is a document the roles will never see again.

A project that has been thought about already says somewhere what has to happen
before what, and that ordering is the most valuable thing in this whole exercise:
it was paid for in someone's experience, and re-deriving it from the acceptance
table alone gets it wrong. Carry it into the backlog below as `depends_on`.

## Then, look at the tree

The documents say what was meant; the tree says what is. Before you write a
task, find out which of the work already exists. This repository is laid out to
its own convention, not to anyone's template, so look rather than assume:

- list the top level and find where the code lives -- `src/`, `project/`,
  `app/`, `lib/` are common names, but the answer is whatever holds the thing
  being built;
- find how it is built and proved -- a `Makefile`, `package.json`,
  `pyproject.toml`, `project.godot`, a CI file, a `tools/` or `scripts/`
  directory of checks -- and run what runs, keeping the output;
- find any record of earlier work -- a changelog, notes, evidence folders, and
  the briefs and Verifier reports of earlier rounds if this project ran under H*
  before (by default under `reports/`) -- and read it: it says what was tried,
  what was verified, and what came back.

A quick scan of this tree, as a starting point and not the map:

{tree}

A task for a capability that is already in the tree is not `open`: see `state`
below.

## Then, the backlog

Write `{backlog}`: every piece of work this project needs, in the order the
dependencies allow. This is the pool the Planner picks each round from, so it is
the only thing standing between the loop and thirty rounds of drift.

## The shape

```json
{{
  "meta": {{"planned_at": "", "confirmed_by_human": false}},
  "tasks": [
    {{"id": 1, "source": "spec:7.1", "name": "two to five words",
      "title": "one sentence: what done looks like", "gates": ["7.1"],
      "depends_on": [], "state": "open",
      "note": "only when the plan would not obviously have it"}}
  ]
}}
```

- **`id`** counts from 1 and is never reused.
- **`name`** is what a board card and a log line call the task, two to five
  words; **`title`** is the sentence that says what done looks like.
- **`source`** is where the task came from: `spec:<section or gate>` for
  everything you derive here.
- **`gates`** are the specification's own acceptance ids, when it declares any.
  A task can carry none -- the tooling that measures the others is itself work,
  and it has no gate of its own.
- **`depends_on`** is the plan's skeleton: what must settle before this can
  start. Write the real dependency, never a round number. A schedule goes stale
  the first time something takes two rounds; a dependency does not.
- **`state`** is `open` for work that is not in the tree. Work that already is
  goes in as `in_review`, with `"implements": [{{"round": 0, "commit": "<the sha
  it lives at>", "verdict": null, "evidence": "<the evidence path, if any>"}}]`,
  so the first round's Verifier verifies what the project inherited instead of the
  loop trusting it. Nothing is `done`: only Verifier, holding evidence, says that --
  the person confirming this plan may overrule by hand.

## What makes this plan good rather than a list

1. **Order by dependency, and say why.** If the specification states an order
   outright -- "no tuning before the measuring tool exists" -- that is a
   `depends_on`, not a sentence you drop.
2. **Cover every gate.** A gate no task moves is a requirement nobody planned
   for. Check at the end.
3. **Infrastructure first, and it is real work.** Whatever measures the build --
   the harness, the log, the determinism check -- is a task like any other, and
   almost everything depends on it.
4. **One task is one round's worth of work at most.** A task nobody can finish
   in a round will be attempted three times and reopened twice.
5. **Do not invent requirements.** Where the specification is silent on
   something that matters, do not decide it: say so in your reply, and a person
   settles it before the first round.

Write the file, then end your reply with `{sentinel}` on a line of its own,
and above it list: how many tasks, which gates each covers, and anything the
specification left open that a person has to settle first.
"""


NO_DOCUMENTS = "(none: this project starts from the specification alone, and the table below has one line saying so)"


def _tree_facts(project: Path) -> str:
    """What a scan sees of the tree without a model -- hints for the plan's own look, not the map."""
    project = Path(project)
    dirs = [name.split("/")[0] for name in source_dirs(project) if (project / name.split("/")[0]).is_dir()]
    lines = [
        "- directories that look like source: "
        + (", ".join(f"`{name}/`" for name in dirs) or "none found -- this may be a greenfield project")
    ]
    detected = list(detect_checks(project, 1800.0))
    lines.append("- checks detected: " + ("; ".join(f"`{spec.name}`: `{spec.command}`" for spec in detected) or "none"))
    reports = project / REPORTS_DIR
    earlier = sorted(path.name for path in reports.glob("*.md")) if reports.is_dir() else []
    lines.append(f"- Markdown under `{REPORTS_DIR}/`: " + (", ".join(f"`{name}`" for name in earlier) or "none"))
    return "\n".join(lines)


def plan_prompt(project: Path) -> str:
    home = Path(project) / STINT_DIR
    spec = home / SPEC_LINK
    documents = project_documents(project, spec=spec if spec.exists() else None)
    return PLAN_PROMPT.format(
        project=project,
        spec=spec,
        decisions=home / DECISIONS_FILE,
        playbook=home / PLAYBOOK_FILE,
        fixlog=home / FIXLOG_FILE,
        agent_decisions=home / AGENT_DECISIONS_FILE,
        sources=home / SOURCES_FILE,
        documents="\n".join(f"- `{doc}`" for doc in documents) or NO_DOCUMENTS,
        tree=_tree_facts(project),
        backlog=home / "backlog.json",
        sentinel=PLAN_SENTINEL,
    )


@dataclass
class Plan:
    tasks: int = 0
    gates: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()
    #: Documents the project carried that `.stint/SOURCES.md` does not account for.
    unabsorbed: tuple[str, ...] = ()
    reply: str = ""

    def render(self) -> str:
        parts = [f"{self.tasks} task(s)", f"{len(self.gates)} gate(s) covered"]
        if self.uncovered:
            parts.append("not covered: " + ", ".join(self.uncovered))
        if self.unabsorbed:
            parts.append(f"not carried into .stint/ ({SOURCES_FILE} does not name them): " + ", ".join(self.unabsorbed))
        return "; ".join(parts)


_GATE = re.compile(r"^\s*\|?\s*\**(?P<id>\d+\.\d+)\**\s*[|\s]", re.MULTILINE)


def gates_in(text: str) -> tuple[str, ...]:
    """Acceptance ids the specification declares, as `7.4` and friends.

    Used only to tell a person which of them no task covers. Deliberately not
    used to *build* the plan: what the work is cannot be read off a table.
    """
    return tuple(dict.fromkeys(match.group("id") for match in _GATE.finditer(text)))


def review(project: Path) -> Plan:
    """What the plan came out as, and which gates it left uncovered."""
    from raven.stint import backlog as backlog_mod

    backlog = backlog_mod.load(project)
    covered = {gate for task in backlog.tasks for gate in task.gates}
    spec = Path(project) / STINT_DIR / SPEC_LINK
    declared = gates_in(spec.read_text(encoding="utf-8", errors="replace")) if spec.exists() else ()
    sources = Path(project) / STINT_DIR / SOURCES_FILE
    named = sources.read_text(encoding="utf-8", errors="replace") if sources.is_file() else ""
    documents = project_documents(project, spec=spec if spec.exists() else None)
    return Plan(
        tasks=len(backlog.tasks),
        gates=tuple(sorted(covered)),
        uncovered=tuple(gate for gate in declared if gate not in covered),
        unabsorbed=tuple(doc for doc in documents if doc not in named),
    )
