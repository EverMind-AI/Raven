"""Turning the sentence a deck task arrived as into a project ready to build in.

The front of this route used to be four separate asks -- record the brief, bind
the template, fetch the sources, ingest them -- each described in its own tool and
sequenced by whoever was reading them. Asked for in prose, they did not happen in
order. And the sequencing is unnecessary here, because once the task has been read
every one of those steps is mechanical. Reading the task is the only judgement, so it is the only model call,
and the rest of this runs from code off its answer.

Two details of the order are load-bearing.

Everything that arrives becomes part of the deck's own source set, and the ingest
reads that set. Three arrival paths meet here -- a directory the user already has, a
file they attached, and the request text when it carries the substance -- and a
fourth, the fetch, meets them later. They used to be handled one at a time against a
single directory, with rules about which one won, and the rules were wrong: twice in
live runs the source the *user supplied* left the deck's evidence while sitting on
disk, because something else was ingested afterwards.

The user's directory is found by what is in it rather than by what it is called. A
name convention was the first answer -- ingest `materials/` -- and it misses
`papers/`, `素材/` and every other name a user picks. What identifies the materials
is that they are documents, so that is what is looked for, and only when exactly one
directory holds any: two is the case the model is needed for.

It is mirrored rather than copied once, so their directory stays authoritative for
what came from it, removals included. An attachment is not mirrored: it arrives under
a media cache outside the workspace, where nothing that walks the workspace can find
it and where the containment rule that refuses a model's invented path would refuse
it too -- and it did not come from a model, it came through the harness's own gate
because a person sent it. A `.pptx` among them is the house style rather than
material.

The ingest runs *before* the model is asked, because the call's real question is what
this deck still needs. Asked blind, it produced an errand reading "extract the text
and figures from the document in papers/" -- work already done by the time the errand
was recorded.

And the three fields the brief binds -- language, audience, length -- are turned
into questions *by this code* when the task does not state them, rather than left
to the call to remember. They gate the build, so a call that forgot one would
strand the deck behind a refusal with nothing telling anyone what to ask.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from raven.utils.images import text_block
from raven_ppt.contracts import (
    DeckBrief,
    Errand,
    IntakePlan,
    PageBudget,
    Project,
    Question,
    StageResult,
    StatedBrief,
    brief_path,
    intake_path,
    load_brief,
    load_plan,
    write_brief,
    write_plan,
)
from raven_ppt.services import citations
from raven_ppt.services import state as deck_state
from raven_ppt.services.ingest import documents, sources
from raven_ppt.services.template import (
    bind,
    default_template_prompt,
    fallback_default_template,
    find_default_template,
)
from raven_ppt.stages._briefs import intake_brief
from raven_ppt.stages._reply import json_defect, loads_maybe_fenced

# What the request is called when it is itself the material.
TASK_SOURCE = "from-the-request.md"

# How many of the plan's notes reach the brief. The brief's one-line summary is
# what carries it into the deck's state summary -- the intake prompt and every
# ppt_prepare reply -- so a long one crowds out the three fields it exists to
# carry.
MAX_BRIEF_NOTES = 3

# How many directories the listing names. Past this it stops being read, and a
# workspace with more than sixty is not one a task is pointing into.
MAX_LISTED = 60

# The questions this code asks when the task does not answer them. Canonical
# rather than composed, so the same missing field always reads the same way -- and
# so the intake call can be told not to ask them, which is what stops the user
# seeing two versions of the same question.
BRIEF_QUESTIONS: dict[str, Question] = {
    "language": Question(
        question="What language should the deck be written in?",
        why="The materials' language is not necessarily the audience's, and the built deck is checked against this.",
        options=("中文", "English", "the same language as the materials"),
    ),
    "audience": Question(
        question="Who is this for, and on what occasion?",
        why="It decides how much the deck may assume, and it is recorded in the brief the deck is written against.",
        options=("an internal review", "a conference talk", "a non-technical exec update"),
    ),
    "pages": Question(
        question="How many slides does the talk have room for?",
        why="A page budget comes from the room rather than from the materials, and the built deck is checked against it.",
        options=("10-12", "16-20", "as few as it takes"),
    ),
}


@dataclass
class PrepareStage:
    """One call: the task in, a prepared project out."""

    composer: Any | None = None
    ingest: Callable[[Path, Path], Any] | None = None
    max_tokens: int = 4000

    async def run(self, project: Project, task: str, files: Sequence[str] = ()) -> StageResult:
        taken = self._take(project, files)
        before = deck_state.read(project)
        recorded = load_plan(intake_path(project))
        unchanged = recorded is not None and recorded.digest == before.digest()
        # The digest covers the deck's inputs, not the words that were read. A revised
        # request with the same materials has the same digest and a different answer,
        # so the request has to be part of the condition or the reading is skipped for
        # a question nobody asked yet.
        if not taken and unchanged and recorded.request.strip() == task.strip():
            # Nothing the reading depended on has changed, so reading the task again
            # would produce the same plan. The questions are recomputed rather than
            # replayed: the brief is not in the digest, so it may have been recorded
            # since, and a reply that asked for it again would send the author round
            # a loop it had already finished.
            return StageResult(
                ok=True,
                data={
                    "plan": replace(recorded, questions=_completed(recorded, before)),
                    "state": before,
                    "reused": True,
                },
            )

        done: list[str] = list(taken)
        found = _sole_source_directory(project.workspace)
        if found is not None:
            done += self._mirror(project, found)
        done += await self._ingest(project, bool(done) or not before.ingested)
        state = deck_state.read(project) if done else before

        plan, defect = await self._read_task(task, state, project)
        if plan is None:
            return StageResult(ok=False, note=defect or "the task could not be read", data={"state": state})

        if plan.template and deck_state.read(project).template is None:
            # Skipped when an attachment already bound one. The task text usually
            # names the attachment too, and the path in it is absolute -- so the
            # request path's containment check fired and printed "no template was
            # bound" directly under "bound it", which reads as a failure and is not.
            default = find_default_template(Path(plan.template).name)
            done += (
                self._bind_default_template(project, default)
                if default is not None
                else self._bind_template(project, plan.template)
            )
        elif deck_state.read(project).template is None:
            default = fallback_default_template()
            if default is not None:
                done += self._bind_default_template(project, default)
        added = False
        if plan.task_is_material:
            done += self._take_task(project, task)
            added = True
        named = (project.workspace / plan.materials_dir).resolve() if plan.materials_dir else None
        if named is not None and named != found and _inside(project.workspace, named) and named.is_dir():
            # Added to the set rather than replacing it. A request naming a directory
            # says "these are also sources", not "forget the others".
            mirrored = self._mirror(project, named)
            done += mirrored
            added = added or bool(mirrored)
        done += await self._ingest(project, added)
        done += self._record_brief(project, plan)

        after = deck_state.read(project)
        plan = replace(
            plan,
            questions=_completed(plan, after),
            errands=_with_pictures(plan, after),
            # Always the deck's own source set, whatever the request said: that is
            # where a later fetch has to land, and where the ingest reads.
            materials_dir=_named(project.workspace, project.sources_dir),
            # Recorded unconditionally so a later call can tell a repeat from a
            # revision. The copy taken as a source is a separate decision.
            request=task,
            digest=after.digest(),
        )
        write_plan(plan, intake_path(project))
        return StageResult(ok=True, data={"plan": plan, "state": after, "done": done})

    async def _read_task(
        self, task: str, state: deck_state.DeckState, project: Project
    ) -> tuple[IntakePlan | None, str]:
        """The task as a plan, or a plan with nothing read when there is no model.

        The degraded plan is not a failure: the three brief questions are added by
        the caller either way, so a run with no provider configured still ends with
        the user being asked what only they can answer.
        """
        if self.composer is None:
            return IntakePlan(topic="", notes=("no model was configured to read the task",)), ""
        parts = [
            text_block(f"The task, in the user's own words:\n\n{task.strip() or '(nothing was said)'}"),
            text_block(f"What this deck already has:\n{state.summary()}"),
            text_block(f"The workspace:\n{_listing(project.workspace)}"),
        ]
        if state.template is None:
            parts.append(text_block(default_template_prompt()))
        if state.excerpt:
            parts.append(text_block(f"The first of the ingested materials:\n\n{state.excerpt}"))
        reply = await self.composer.ask(intake_brief(), parts, max_tokens=self.max_tokens)
        payload = loads_maybe_fenced(reply)
        if payload is None:
            # The transport's own reason first when there is one. Without this a
            # gateway answering 503 was reported as "the reply did not parse",
            # which sends the next actor to fix a prompt that was never read.
            failed = getattr(self.composer, "failure", "")
            return None, (
                f"the task could not be read: {failed}"
                if failed
                else f"the intake reply did not parse: {json_defect(reply)}"
            )
        return _plan_of(payload), ""

    def _take_task(self, project: Project, task: str) -> list[str]:
        """Write the request itself into the deck as a source, and read it.

        A user who pastes their notes into the request has supplied material, and
        before this it went nowhere: nothing was ingested, so every number they had
        just given us came back unanchored and the deck could state anything.

        The whole request rather than a part of it the model echoed back. An echo
        of a long passage is a paraphrase sooner or later, and a paraphrased source
        is worse than a slightly wide one -- the cost of the whole is that an
        instruction's numbers ("15 页") are authorised too, which is permissive
        where the alternative was blind.
        """
        sources.write_source(
            project, TASK_SOURCE, f"# What the user asked for, in their words\n\n{task}\n", sources.REQUEST
        )
        return ["took the request itself as a source"]

    def _take(self, project: Project, files: Sequence[str]) -> list[str]:
        """Copy what the user attached into the deck's sources, and say what each is.

        A `.pptx` is bound as the template instead: that is what attaching one means,
        and it is not something the ingest could read anyway.
        """
        said: list[str] = []
        for raw in files:
            source = Path(raw).expanduser()
            if not source.is_absolute():
                source = (project.workspace / source).resolve()
            if not source.is_file():
                said.append(f"{raw} is not a file, so it was not taken")
            elif source.suffix.lower() == ".pptx":
                said += self._bind_file(project, source)
            elif sources.take(project, source, sources.ATTACHED) is None:
                said.append(f"{source.name} is not something this can read, so it was not taken")
            else:
                said.append(f"took {source.name} as material")
        return said

    def _mirror(self, project: Project, directory: Path) -> list[str]:
        """Bring a user's directory into the deck's sources and keep it in step."""
        mirrored = sources.mirror(project, directory)
        if not any((mirrored.taken, mirrored.dropped, mirrored.left_behind, mirrored.templates)):
            return []
        where = _named(project.workspace, directory)
        said = [
            f"took {mirrored.taken} source(s) from {where}"
            + (f", dropped {mirrored.dropped} they had removed" if mirrored.dropped else "")
        ]
        if mirrored.templates:
            said.append(
                f"{', '.join(sorted(mirrored.templates))} in {where} is a .pptx, so it is a template rather than "
                "material -- bind it with ppt_template if it is the house style this deck is to be built in"
            )
        if mirrored.left_behind:
            said.append(
                f"left {len(mirrored.left_behind)} file(s) in {where} that nothing here can read: "
                f"{', '.join(sorted(mirrored.left_behind))} -- say so rather than writing pages as if they "
                "had been read"
            )
        return said

    async def _ingest(self, project: Project, changed: bool) -> list[str]:
        """Read the deck's whole source set, when something in it moved.

        One directory, always the same one, so there is no question of which arrival
        wins -- which is what went wrong when there were several. Skipped when
        nothing changed: reading is idempotent but not free.
        """
        if self.ingest is None or not changed:
            return []
        folder = project.sources_dir
        if not folder.is_dir() or not _holds_documents(folder):
            return []
        try:
            outcome = await asyncio.to_thread(self.ingest, folder, project.ingest_dir)
        except (OSError, ValueError, FileNotFoundError) as exc:
            return [f"the deck's sources could not be read: {exc}"]
        return [f"read {len(outcome.source_files)} source(s), {len(outcome.assets)} figure(s)"]

    def _bind_template(self, project: Project, relative: str) -> list[str]:
        """Bind a `.pptx` the *request* named, which has to be inside the workspace.

        The containment check is the point of the separate path: this one is a
        string a model produced from the task text, so it is checked like every
        other path a model produces.
        """
        source = (project.workspace / relative).resolve()
        if not _inside(project.workspace, source) or not source.is_file():
            return [f"{relative} is not a file in the workspace, so no template was bound"]
        return self._bind_file(project, source)

    def _bind_file(self, project: Project, source: Path) -> list[str]:
        template = bind(source, project)
        if template is None:
            return [f"{source.name} does not open as a presentation, so no template was bound"]
        return [f"bound {template.source.name} as the template, {template.example_pages} example page(s) to read"]

    def _bind_default_template(self, project: Project, template: Any | None) -> list[str]:
        if template is None:
            return []
        bound = bind(template.path, project)
        if bound is None:
            return [f"bundled default template {template.filename} could not be opened"]
        return [
            f"selected bundled default template {template.filename} ({', '.join(template.tags)})",
            f"bound {bound.source.name} as the template, {bound.example_pages} example page(s) to read",
        ]

    def _record_brief(self, project: Project, plan: IntakePlan) -> list[str]:
        """Write the brief when the task stated all three of it, and only then.

        Partial is not written at all. A brief exists to be checked against, so
        half of one filled out with defaults would have the deck measured against
        a decision nobody made -- which is worse than the refusal that stands while
        it is missing, because it looks like agreement.
        """
        if load_brief(brief_path(project)) is not None or plan.stated.missing:
            return []
        stated = plan.stated
        brief = DeckBrief(
            language=str(stated.language),
            audience=str(stated.audience),
            pages=PageBudget(low=int(stated.pages_low or 1), high=int(stated.pages_high or 1)),
            # Capped, because `summary()` is formatted into prompts as one line and
            # a live run produced three paragraph-length notes -- two of them
            # restating fields the brief already carries. The plan keeps all of
            # them; the brief carries the few that a page has to honour.
            notes=plan.notes[:MAX_BRIEF_NOTES],
            # Not capped, unlike the notes: dropping the fourth thing a user
            # forbade is the one failure this field exists to prevent, and a page
            # that uses it is not a slightly longer prompt.
            forbidden=plan.stated.forbidden,
        )
        write_brief(brief, brief_path(project))
        return [f"recorded the brief the task stated: {brief.summary()}"]


def _completed(plan: IntakePlan, state: deck_state.DeckState) -> tuple[Question, ...]:
    """The plan's own questions, plus one for every brief field still unanswered."""
    if state.brief is not None:
        return plan.questions
    missing = plan.stated.missing or tuple(BRIEF_QUESTIONS)
    return (*plan.questions, *(BRIEF_QUESTIONS[field] for field in missing if field in BRIEF_QUESTIONS))


def _with_pictures(plan: IntakePlan, state: deck_state.DeckState) -> tuple[Errand, ...]:
    """The plan's errands, plus one to look for evidence when ingest found none.

    Added by code for the same reason the brief questions are: it is the one
    threshold here that needs no judgement. Zero figures means every page will be
    prose or something the author draws, which the evidence gate reports after the
    build -- one round too late to do anything but rebuild. Any number above zero
    is a judgement about whether these particular figures carry this particular
    deck, and that is the model's to make, not a ratio invented here.

    What it asks for is a sweep of what the sources cite, not a picture search.
    Nothing here knows what the deck argues yet -- there are no pages -- so
    "find some pictures" is a question with no target, and it answers itself:
    one live run took two logos and a marketing banner, which made `state.figures`
    non-empty and the errand satisfied, while the material cited a paper whose
    figures carry their own captions and two benchmark posts nobody swept. Which
    picture a page needs is `ppt_outline`'s question, asked per page against a
    plan; this one only establishes what there is to choose from.

    Suppressed by the figures and by nothing else now. It used to stand down when any
    of the model's own errands had "image" anywhere in its `how`, which is a substring
    of `web_search(kind="images")` -- so an author that mentioned a picture search in
    passing, for one page, deleted the errand asking it to sweep the sources. The
    errand it deleted is the one thing said about the sweep before `ppt_outline`
    refuses over it, and two errands about pictures cost a reader a sentence.
    """
    if state.figures:
        return plan.errands
    urls = citations.cited(state.project)
    return (
        *plan.errands,
        Errand(
            what="the figures the sources themselves point at",
            why=(
                f"ingest extracted nothing visual from the files and the materials cite {len(urls)} URL(s), "
                "so every picture this deck could have is still on the other end of a link"
                if urls
                else "ingest extracted nothing visual from the files, so every page will be prose or a drawing"
            ),
            how=(
                "web_fetch on the URLs the material cites, for the pages themselves before their "
                "pictures -- a page's own words are material, and on a deck about whoever wrote it they "
                'are the most direct material there is. Then web_fetch(extractMode="images") on the '
                "same URLs, which returns each picture with the caption its author wrote; a listing "
                "that carries no caption is telling you the page gave its pictures no words, so those "
                "want ppt_figure_inspect before they are chosen. A paper cited as an abstract keeps its "
                "figures in the PDF, so ppt_fetch that and ingest extracts them, and a source the "
                "material names without linking is what web_search is for. ppt_fetch what you will "
                "use. ppt_outline records no outline while a cited URL is still unopened -- one "
                "that comes back with nothing goes in its `swept` argument, with what came back"
            ),
        ),
    )


def _plan_of(payload: dict) -> IntakePlan:
    return IntakePlan(
        topic=str(payload.get("topic") or ""),
        stated=StatedBrief.of(payload.get("stated")),
        materials_dir=str(payload.get("materials_dir") or ""),
        task_is_material=bool(payload.get("task_is_material")),
        template=str(payload.get("template") or ""),
        questions=tuple(
            Question(
                question=str(entry.get("question") or ""),
                why=str(entry.get("why") or ""),
                options=tuple(str(option) for option in entry.get("options") or ()),
            )
            for entry in payload.get("questions") or ()
            if isinstance(entry, dict) and str(entry.get("question") or "").strip()
        ),
        errands=tuple(
            Errand(
                what=str(entry.get("what") or ""),
                why=str(entry.get("why") or ""),
                how=str(entry.get("how") or ""),
            )
            for entry in payload.get("errands") or ()
            if isinstance(entry, dict) and str(entry.get("what") or "").strip()
        ),
        notes=tuple(str(note) for note in payload.get("notes") or () if str(note).strip()),
    )


def _sole_source_directory(workspace: Path) -> Path | None:
    """The one directory in the workspace holding documents, if there is one.

    Returns None when there are none and when there are several. Several is not a
    failure -- it is the case the model is there for, and it sees the same listing
    this walks.
    """
    holding = [directory for directory in _directories(workspace) if _counts(directory).get("document")]
    return holding[0].resolve() if len(holding) == 1 else None


def _holds_documents(directory: Path) -> bool:
    return any(path.suffix.lower() in documents.SUPPORTED_SUFFIXES for path in directory.rglob("*"))


def _named(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


def _listing(workspace: Path) -> str:
    """Every directory a task could be pointing at, and what is in it.

    Two levels deep and counted by kind, because the question this answers is
    "which of these is the materials" -- and a full walk of a workspace with a
    checkout in it would be the expensive part of an otherwise cheap call.
    """
    lines: list[str] = []
    for directory in _directories(workspace):
        counts = _counts(directory)
        if not counts:
            continue
        said = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
        lines.append(f"  {directory.relative_to(workspace)}/ — {said}")
        if len(lines) >= MAX_LISTED:
            break
    return "\n".join(lines) or "  (nothing but this deck's own project directory)"


def _directories(workspace: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in ("*", "*/*"):
        for path in sorted(workspace.glob(pattern)):
            if path.is_dir() and "deck" not in path.parts and "out" not in path.parts:
                found.append(path)
    return found


def _counts(directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in documents.IMAGE_SUFFIXES:
            counts["image"] = counts.get("image", 0) + 1
        elif suffix == ".pptx":
            counts["pptx"] = counts.get("pptx", 0) + 1
        elif suffix in documents.SUPPORTED_SUFFIXES:
            counts["document"] = counts.get("document", 0) + 1
    return counts


def _inside(workspace: Path, path: Path) -> bool:
    root = workspace.resolve()
    return path == root or root in path.parents
