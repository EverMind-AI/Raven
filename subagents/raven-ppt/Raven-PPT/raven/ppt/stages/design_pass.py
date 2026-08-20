"""Looking at the pages that were built, and improving them.

Driven from code rather than asked for in prose. Asked for, it does not happen:
the author has been looking at these pages while getting them to build, so by the
time an instruction says "now review the layout" the review it would run is the
one it just ran, with the intent it already holds. These calls carry one page's
render and one block of code, and nothing else -- the isolation that matters is
the empty context, not a different model.

Two stages, because the two problems are not visible from the same distance.
Across pages you can see what one page cannot show you: a role set at different
sizes on different pages, a scale with no step in it, or the opposite and easier
to miss -- one construction used for everything, so that every page is the same
grid of panels and none of them argues anything. Within a page you can see what
the contact sheet is too small to show: type under the floor, a word past the edge
of its card, a rule struck through a row of text.

And it is a loop, which is what makes the result trustworthy. One pass can only
claim to have improved a page; the round after it renders that page again and
hands it to someone who did not write it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from raven.ppt.backends.script import (
    applied_lines,
    apply_verified,
    block_rejection,
    blocks_rejection,
    broken_page,
    page_blocks,
    prelude_rejection,
    script_path,
    with_banner,
)
from raven.ppt.contracts import (
    Audience,
    BuildOutcome,
    Finding,
    Project,
    Severity,
    StageResult,
    brief_path,
    load_brief,
)
from raven.ppt.services.template import bound
from raven.ppt.stages._briefs import deck_brief, page_brief
from raven.ppt.stages._reply import dedent_fence, json_defect, loads_maybe_fenced
from raven.utils.helpers import image_block, text_block


@dataclass(frozen=True)
class TypeFloors:
    """The two sizes the brief quotes and the measurement enforces.

    Passed in so they cannot disagree. Every PowerPoint canvas is 7.5 inches
    tall, which is why these are constants rather than a function of the page.
    """

    body_pt: float = 14.0
    min_pt: float = 10.8


class PageRenderer(Protocol):
    async def pages(self, pptx: Path, out_dir: Path, numbers: Sequence[int]) -> dict[int, Path]: ...

    def contact_sheet(self, pngs: Sequence[Path], out: Path, columns: int = 3) -> Path: ...

    def data_uri(self, png: Path) -> str: ...


class Composer(Protocol):
    """One question, one answer. No history: each call starts from nothing."""

    async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str: ...


@dataclass
class DesignPass:
    renderer: PageRenderer
    composer: Composer
    build: Callable[[Project], Any]
    # Awaited, and given the project: measuring means rendering the deck.
    measure: Callable[..., Any]
    floors: TypeFloors = field(default_factory=TypeFloors)
    rounds: int = 2
    concurrency: int = 6
    max_tokens: int = 16_000

    async def run(self, project: Project, outcome: BuildOutcome) -> StageResult:
        if not outcome.ok or outcome.pptx_path is None:
            return StageResult(ok=False, note="no deck to look at")
        source = script_path(project)
        history: list[dict[str, Any]] = []
        vocabulary: list[str] = []
        current = outcome

        for index in range(self.rounds):
            before = source.read_text(encoding="utf-8")
            record, current, vocabulary = await self._round(project, current, before, vocabulary)
            record["round"] = index + 1
            history.append(record)
            if not current.ok:
                # The round left the program broken. Put back the text that built,
                # and say which page it died in -- the next actor needs a deck to
                # work from more than it needs this round's changes.
                source.write_text(before, encoding="utf-8")
                rebuilt = await self.build(project)
                record["reverted"] = True
                return StageResult(
                    ok=False,
                    findings=(_broken(record.get("broken_page"), record.get("stderr", "")),),
                    data={"rounds": history, "outcome": rebuilt},
                    note="the round was reverted; the deck that built is back in place",
                )
            if not record.get("edited"):
                record["stopped"] = "nothing left to change"
                break

        return StageResult(ok=True, data={"rounds": history, "outcome": current, "vocabulary": vocabulary})

    async def _round(
        self, project: Project, outcome: BuildOutcome, script: str, vocabulary: list[str]
    ) -> tuple[dict[str, Any], BuildOutcome, list[str]]:
        record: dict[str, Any] = {"deck": None, "pages": {}, "refused": {}}
        lines = script.splitlines(keepends=True)
        blocks, derived = _blocks(outcome, lines)
        rejection = blocks_rejection(lines, blocks, outcome.pages, derived) if blocks else None
        if not blocks or rejection:
            record["refused"]["blocks"] = rejection or "no page blocks could be found in the program"
            return record, outcome, vocabulary

        renders = await self.renderer.pages(outcome.pptx_path, project.review_dir, sorted(blocks))
        findings = await self.measure(project, outcome.pptx_path, outcome)
        prelude_end = min(span[0] for span in blocks.values())
        prelude = "".join(lines[:prelude_end])

        template = bound(project)
        house_style = template is not None
        house = _house_numbers(project, template) if template is not None else ""
        deck_note, new_prelude, vocabulary = await self._deck_stage(
            project, renders, prelude, lines, blocks, house_style, house
        )
        record["deck"] = deck_note
        if new_prelude is not None:
            lines = new_prelude.splitlines(keepends=True) + lines[prelude_end:]
            blocks, derived = page_blocks(lines), "comments"
            prelude = new_prelude

        prototypes = await self._prototypes(project, sorted(blocks))
        replacements, notes, refused = await self._page_stage(
            lines, blocks, renders, findings, prelude, vocabulary, house_style, prototypes, house
        )
        record["pages"] = notes
        record["refused"].update(refused)
        if not replacements:
            record["edited"] = new_prelude is not None
            if new_prelude is not None:
                script_path(project).write_text("".join(lines), encoding="utf-8")
                return record, await self.build(project), vocabulary
            return record, outcome, vocabulary

        kept, dropped = apply_verified(lines, blocks, replacements)
        record["refused"].update(dropped)
        written = applied_lines(lines, blocks, kept)
        script_path(project).write_text("".join(written), encoding="utf-8")
        record["edited"] = bool(kept) or new_prelude is not None
        # Which pages were actually rewritten, beside what the pass says it did. Its
        # notes are decisions, not outcomes: one round reported "unified all six number
        # chips to a single accent fill" on a page whose six chips take their fill from
        # the prototype, where no edit to that page's code could have touched them. The
        # notes were read as results -- by a reader who then had to open the file to find
        # out otherwise -- so the fact travels with them.
        record["rewrote"] = sorted(kept)
        if new_prelude is not None:
            record["rewrote_setup"] = True

        rebuilt = await self.build(project)
        if not rebuilt.ok:
            record["stderr"] = rebuilt.stderr
            record["broken_page"] = broken_page("".join(written), rebuilt.stderr)
        return record, rebuilt, vocabulary

    async def _deck_stage(
        self,
        project: Project,
        renders: dict[int, Path],
        prelude: str,
        lines: list[str],
        blocks: dict[int, tuple[int, int]],
        house_style: bool = False,
        house: str = "",
    ) -> tuple[dict[str, Any], str | None, list[str]]:
        """Every page at once, and only the shared setup to change."""
        if not renders:
            return {"skipped": "no renders"}, None, []
        sheet = self.renderer.contact_sheet([renders[n] for n in sorted(renders)], project.review_dir / "contact.png")
        agreed = load_brief(brief_path(project))
        parts = [
            # What the deck is for, first, because it decides how much the pages
            # may assume -- and because a deck judged for the wrong room reads as
            # a design problem when it is not one.
            *([text_block(f"What this deck is for: {agreed.summary()}")] if agreed else []),
            text_block(f"The deck, all {len(renders)} pages:"),
            image_block(self.renderer.data_uri(sheet)),
            text_block(f"The shared setup every page is drawn from:\n\n{prelude}"),
        ]
        reply = await self.composer.ask(
            deck_brief(body_pt=self.floors.body_pt, min_pt=self.floors.min_pt, house_style=house_style, house=house),
            parts,
            max_tokens=self.max_tokens,
        )
        payload = loads_maybe_fenced(reply)
        if payload is None:
            return {"error": json_defect(reply)}, None, []
        note = {"verdict": payload.get("verdict"), "notes": payload.get("notes") or []}
        vocabulary = [str(line) for line in (payload.get("vocabulary") or []) if str(line).strip()]
        candidate = payload.get("prelude")
        if not isinstance(candidate, str) or not candidate.strip():
            return note, None, vocabulary
        candidate = dedent_fence(candidate)
        body = "".join("".join(lines[start:end]) for start, end in blocks.values())
        refusal = prelude_rejection(candidate, prelude, body)
        if refusal:
            note["refused"] = refusal
            return note, None, vocabulary
        return note, candidate, vocabulary

    async def _prototypes(self, project: Project, pages: Sequence[int]) -> dict[int, tuple[int, Path | None]]:
        """Per page: which template page the outline said it adapts, and its render.

        The outline is where this was decided, so this pass reads it rather than
        guessing from the built page -- and it hands the page's own author the same
        thing they chose from: the template page itself, beside their page. Without
        it a pass told to improve the design of a page that is deliberately somebody
        else's design has no way to know that, and redesigning it is exactly the
        wrong move.
        """
        from raven.ppt.contracts.outline import load_outline, outline_path
        from raven.ppt.services.template import template_path

        outline = load_outline(outline_path(project))
        if outline is None:
            return {}
        named = {page.page: page.prototype for page in outline.pages if page.prototype is not None}
        wanted = {number: named[number] for number in pages if number in named}
        if not wanted:
            return {}
        source = template_path(project)
        renders: dict[int, Path] = {}
        if source is not None and source.is_file():
            renders = await self.renderer.pages(source, project.review_dir / "template", sorted(set(wanted.values())))
        return {number: (prototype, renders.get(prototype)) for number, prototype in wanted.items()}

    async def _page_stage(
        self,
        lines: list[str],
        blocks: dict[int, tuple[int, int]],
        renders: dict[int, Path],
        findings: Sequence[Finding],
        prelude: str,
        vocabulary: Sequence[str],
        house_style: bool = False,
        prototypes: dict[int, tuple[int, Path | None]] | None = None,
        house: str = "",
    ) -> tuple[dict[int, str], dict[int, Any], dict[int, str]]:
        """One call per page, concurrently. Each sees its page and nothing else."""
        gate = asyncio.Semaphore(self.concurrency)
        brief = page_brief(body_pt=self.floors.body_pt, min_pt=self.floors.min_pt, house_style=house_style, house=house)
        for_page = _by_page(findings)

        async def one(number: int) -> tuple[int, str | None, Any, str | None]:
            async with gate:
                start, end = blocks[number]
                original = "".join(lines[start:end])
                parts: list[dict[str, Any]] = [text_block(f"Slide {number} as it renders:")]
                if number in renders:
                    parts.append(image_block(self.renderer.data_uri(renders[number])))
                if for_page.get(number):
                    measured = "\n".join(f"- {f.message}" for f in for_page[number])
                    parts.append(text_block(f"Measured on this page:\n{measured}"))
                prototype = (prototypes or {}).get(number)
                if prototype is not None:
                    source_page, render = prototype
                    parts.append(
                        text_block(
                            f"This page adapts the template's page {source_page}. Its design is the "
                            "template's, not yours to replace: keep its structure, its type roles and its "
                            "furniture, and fix this page against it -- what belongs to you is what the "
                            "page says and how the content sits in the slots the template drew."
                        )
                    )
                    if render is not None:
                        parts.append(text_block(f"The template's page {source_page}, as it renders:"))
                        parts.append(image_block(self.renderer.data_uri(render)))
                if vocabulary:
                    said = "\n".join(f"- {line}" for line in vocabulary)
                    parts.append(text_block(f"What this deck's pages are to do:\n{said}"))
                parts.append(text_block(f"The shared setup, read-only:\n\n{prelude}"))
                parts.append(text_block(f"This page's block, to replace:\n\n{original}"))
                reply = await self.composer.ask(brief, parts, max_tokens=self.max_tokens)
                return (number, *_page_reply(number, reply, original))

        # Only pages there is a render for. Without one, the call was handed the
        # literal text "Slide 7 as it renders:" and no image, under a brief that
        # says to judge the page as a reader will see it projected -- and its
        # rewrite was applied to the script and shipped. A page nobody can look at
        # is a page this pass has nothing to say about.
        judged = [number for number in sorted(blocks) if number in renders]
        skipped = {number: "no render to judge it by" for number in sorted(blocks) if number not in renders}
        results = await asyncio.gather(*(one(number) for number in judged), return_exceptions=True)
        replacements: dict[int, str] = {}
        notes: dict[int, Any] = dict.fromkeys(skipped, {"skipped": "no render"})
        refused: dict[int, str] = dict(skipped)
        for result in results:
            if isinstance(result, BaseException):
                continue
            number, block, note, refusal = result
            notes[number] = note
            if refusal:
                refused[number] = refusal
            elif block is not None:
                replacements[number] = block
        return replacements, notes, refused


def _page_reply(number: int, reply: str, original: str) -> tuple[str | None, Any, str | None]:
    payload = loads_maybe_fenced(reply)
    if payload is None:
        return None, {"error": json_defect(reply)}, None
    note = {"verdict": payload.get("verdict"), "notes": payload.get("notes") or []}
    candidate = payload.get("block")
    if not isinstance(candidate, str) or not candidate.strip():
        return None, note, None
    # The banner is restored mechanically before the block is judged: the caller
    # knows which page this is, and a dropped banner is not worth a model call.
    block = with_banner(number, dedent_fence(candidate))
    refusal = block_rejection(block, original)
    return (None, note, refusal) if refusal else (block, note, None)


def _house_numbers(project: Project, template: Any) -> str:
    """The template's measured style, as lines a brief can carry.

    Reuses the render `ppt_template` already made when it bound the file, because the
    numbers that matter are only right off a render: this template's title placeholder
    declares no size at all, and reading the file gives 24pt for a row its own pages set
    at 28. Without the render the master's ladder still comes through, which is the half
    that binds every page.
    """
    from raven.ppt.services.template.house import house_style as measure

    source = getattr(template, "source", None)
    if source is None or not Path(source).is_file():
        return ""
    rendered = project.review_dir / "template" / f"{Path(source).stem}.pdf"
    house = measure(Path(source), None, rendered if rendered.is_file() else None)
    if house is None:
        return ""
    lines: list[str] = []
    if house.title is not None:
        lines.append(f"- the page title goes {house.title.line()} -- on {house.title.pages} of its own pages")
    if house.scale:
        lines.append("- its type ladder: " + ", ".join(f"{role} {size:g}pt" for role, size in house.scale.items()))
    if house.faces.get("text"):
        lines.append(f"- the face its pages actually set: {house.faces['text']}")
    body = house.body_area
    if body:
        lines.append(
            f"- content stays inside ({body[0]:g}, {body[1]:g}) {body[2]:g}x{body[3]:g}in, under the title row"
        )
    return "\n".join(lines)


def _blocks(outcome: BuildOutcome, lines: list[str]) -> tuple[dict[int, tuple[int, int]], str]:
    """Page spans from execution when they exist, from the banners otherwise."""
    if outcome.sources:
        return {s.page: (s.first_line, s.last_line) for s in outcome.sources}, "execution"
    return page_blocks(lines), "comments"


def _by_page(findings: Sequence[Finding]) -> dict[int, list[Finding]]:
    """Only what a design pass is allowed to act on, keyed by page.

    Content findings are withheld on purpose. This pass may rearrange a page but
    never change what it says, so handing it "this page carries three slides'
    worth of copy" leaves it holding a problem it is forbidden to solve -- and
    the page comes back arranged into compartments however often it is
    redesigned.
    """
    out: dict[int, list[Finding]] = {}
    for finding in findings:
        if finding.audience is Audience.DESIGNER and finding.page is not None:
            out.setdefault(finding.page, []).append(finding)
    return out


def _broken(page: int | None, stderr: str) -> Finding:
    where = f" in the block that draws page {page}" if page else ""
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else "no error output"
    return Finding(
        kind="design_pass_broke_the_build",
        severity=Severity.BLOCKING,
        message=(
            f"the design pass left the program unable to run{where} ({tail}). Its changes were reverted and "
            "the deck that built is back in place"
        ),
        page=page,
        audience=Audience.AUTHOR,
    )
