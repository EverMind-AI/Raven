"""`ppt_build`: run the author's program, then show it every page it made.

The tool is thin on purpose -- schema, argument adaptation, the reply -- because
everything it does is somewhere else: the backend runs the program, the gates
measure the file, the design pass improves it, publication delivers it or refuses.
What lives here is the one thing a tool owns, which is what the model sees coming
back.

That turns out to matter more than it sounds. The predecessor returned twelve
unlabelled page renders and a JSON blob, and the model could not reliably say
which page it was talking about; it voiced one problem when two stood, and a run
spent seven rebuilds re-checking numbers because the fact ask was the only
instruction it got while seventeen colour bars stood untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool, ToolResult
from raven.ppt.contracts import (
    Audience,
    Finding,
    Profile,
    Project,
    Severity,
    brief_path,
    intake_path,
    load_brief,
    load_outline,
    load_plan,
    outline_path,
)
from raven.ppt.stages.build import BuildStage
from raven.ppt.tools import _return
from raven.ppt.tools._args import ArgumentError, as_ints
from raven.utils.helpers import image_block, text_block

# How many page renders come back in one reply. Keeping this to one makes each
# author-model call inspect one page instead of skimming a batch.
BATCH_VIEWS = 1


class PptBuildTool(Tool):
    name = "ppt_build"
    description = (
        "Build the deck by running the python-pptx program you write, then look at every page it made. "
        "Write the program to ppt_projects/<project>/build/build.py with write_file -- that whole path, "
        "relative to the workspace, because a bare build/build.py lands somewhere the build does not look "
        "-- revise it with edit_file, then call this with just the project. Read the recorded outline before writing each page: "
        "each page must carry its claim, supporting points, intended visual, and listed material needs. You may rewrite the wording, "
        "but do not silently drop the planned argument or replace a planned visual with empty boxes. With a template, keep the measured title row, body area, type ladder and background language from ppt_template; start every content page from the nearest example prototype and adapt its text, pictures, repeated units and spare furniture to the actual content. Free composition inside the house style is only for a page whose outline explicitly says that no example can carry its information shape after those edits. Define one shared content-page header helper and call it everywhere; page blocks must not invent title coordinates. Keep the kicker, title and explanatory line as one compact group with the larger gap below it. Never use a native PowerPoint table, `add_table`, or `ppt_layout.table`; preserve tabular rows and cells by drawing tables or comparison matrices from Box, write, rule and optional quiet planes so the model controls row height and type size. Table headers are the same size as body rows or larger and bold, never smaller. On a figure-and-text page, keep the figure dominant and put two to four labelled supporting points on restrained theme-coloured surfaces; retain its source caption or inspected visual_caption. Use the provided ppt_layout and ppt_icons modules instead of inventing a second coordinate or icon system. Keep source notes to one short line, at most two lines. It returns the rendered pages, anything the script printed, and everything "
        "measured on the deck. Iterate: read the render, edit the file, run again."
    )
    timeout_seconds = 900.0

    def __init__(self, workspace: Path, stage: BuildStage, views: Any, profile: Profile) -> None:
        self.workspace = workspace
        self.stage = stage
        self.views = views
        self.profile = profile

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_ingest"},
                "script": {
                    "type": "string",
                    "description": (
                        "Optional: a complete python-pptx program, written to the deck's build.py before it "
                        "runs. Omit it -- or leave it empty -- to run the build.py you wrote with the file "
                        "tools, which is the better path for a deck-sized script, since inlining one means "
                        "regenerating every line to change any line. The program runs with python-pptx and "
                        "Pillow available and reads its paths from the environment: PPT_FIGURES_DIR, the "
                        "directory of figures ppt_ingest extracted, and PPT_OUTPUT, the path to save the deck "
                        "to. Save there and nowhere else. A deck with a template gets two more: PPT_TEMPLATE, "
                        "the template with its example pages removed -- open it with "
                        "`Presentation(os.environ['PPT_TEMPLATE'])` and every page you add inherits its master, "
                        "theme and canvas, and its palette and type replace the ones below -- and "
                        "PPT_TEMPLATE_SOURCE, the original with those pages still in it, to clone one out of "
                        "with `from ppt_template import clone_page, replace_text, replace_picture, drop_shape`. "
                        "Take the palette and font from one of the reviewed themes "
                        "(`from ppt_theme import THEMES, rgb`) rather than inventing colours, and draw icons "
                        "with `from ppt_icons import add_icon`. Build your own helpers on top -- page "
                        "furniture, a bullet routine, a table routine -- and use them across every slide; "
                        "that consistency is what makes a deck look designed rather than generated. Draw the "
                        "pages in build.py itself, one block of it per page: a build.py that runs another "
                        "file, or a loop that draws every page from one call, is refused, because the build "
                        "matches each page's render back to the code that drew it by that block."
                    ),
                },
                "slides": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": BATCH_VIEWS,
                    "description": (
                        "which pages to render back. Omit to walk the deck in order -- look at every page "
                        "before deciding it is done, and name the ones you want again after an edit"
                    ),
                },
                "design_pages": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "description": (
                        "pages to replay through the design pass after a prior full design run. Omit on the "
                        "first finished build. A targeted replay keeps the shared setup and rechecks only "
                        "these rendered pages"
                    ),
                },
                "design_setup": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "with design_pages, also let the global design stage rewrite the shared setup once. "
                        "Use when the defect is cross-page, such as inconsistent header geometry"
                    ),
                },
                "page_from": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"which page the renders start at; omit for the first {BATCH_VIEWS} and pass the "
                        "number the previous reply named to see the rest"
                    ),
                },
                "draft": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "true only while the program is still being written: it builds what exists, measures it and "
                        "hands back the renders, without holding a part-written deck to the length that was "
                        "agreed and without publishing. Write the setup, append two or three pages, build a draft, look, append the next few. Once the requested pages are written, omit draft: only the finished build runs the design pass and attempts delivery"
                    ),
                },
            },
            "required": ["project"],
        }

    async def execute(
        self,
        project: str,
        script: str | None = None,
        slides: list[int] | None = None,
        design_pages: list[int] | None = None,
        design_setup: bool = False,
        page_from: int = 1,
        draft: bool = False,
        **kwargs: Any,
    ) -> str | ToolResult:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        # Refused rather than defaulted. Three things about this deck are the
        # user's to decide -- its language, its audience, its length -- and all
        # three are checked against the finished file, so guessing them here would
        # mean measuring the deck against a brief nobody agreed to.
        if load_brief(brief_path(deck)) is None:
            return _return.failed(
                "no brief recorded for this deck, so there is nothing to build it against",
                hint=(
                    "record the language, the audience and the page budget with ppt_brief -- ask the user "
                    "with ask_user when there is a user to ask, and read them out of the task when there "
                    "is not"
                ),
            )

        if load_plan(intake_path(deck)) is None:
            return _return.failed(
                "this deck's task has not been read, so nothing knows what it is for or what it stands on",
                hint=(
                    "call ppt_prepare with the user's request first -- it locates and ingests the materials, "
                    "binds a template and records what the request already states"
                ),
            )

        if load_outline(outline_path(deck)) is None:
            return _return.failed(
                "no outline recorded, so nothing has decided what this deck argues or what each page says",
                hint=(
                    "plan it with ppt_outline first -- it checks the figures against the catalogue and the "
                    "length against the brief, which is a cheap edit there and an expensive one here"
                ),
            )

        try:
            wanted = as_ints(slides, "slides") or None
            replay = as_ints(design_pages, "design_pages") or None
        except ArgumentError as exc:
            return _return.failed(str(exc), hint="slides: [1, 2, 3], design_pages: [4, 9]")

        # `polish` is not a parameter of this tool. It was, and it was the one switch that
        # turned off the only stage that owns layout: a live run passed `polish=false` on
        # eight consecutive finished builds. A draft is not polished and a finished deck
        # always is (design doc D8), so `draft` is the whole choice.
        run_options: dict[str, Any] = {
            "polish": True,
            "slides": wanted or [page_from],
            "page_from": page_from,
            "draft": draft,
        }
        if replay is not None:
            run_options["design_pages"] = replay
            run_options["design_setup"] = design_setup
        result = await self.stage.run(deck, script, **run_options)
        outcome = result.data.get("outcome")

        if outcome is None or not getattr(outcome, "ok", False):
            return _return.failed(
                "the build script did not produce a deck",
                stderr=getattr(outcome, "stderr", "") or None,
                stdout=getattr(outcome, "stdout", "") or None,
                hint="fix the script and run ppt_build again",
            )

        findings = list(result.findings)
        payload: dict[str, Any] = {"project": project, "slides": outcome.pages}
        if outcome.note:
            payload["note"] = outcome.note
        if outcome.stdout:
            payload["stdout"] = outcome.stdout
        if findings:
            shown_findings, folded = _folded(findings)
            payload["measured"] = _return.grouped(shown_findings)
            if folded:
                payload["consequences_folded"] = folded
        if "design_pass" in result.data:
            payload["design_pass"] = _summary(result.data["design_pass"])
        if "pptx_path" in result.data:
            payload["pptx_path"] = result.data["pptx_path"]

        shown = list(result.data.get("showing") or [])
        outline = load_outline(outline_path(deck))
        if outline is not None:
            payload["planned_pages"] = [
                page.as_dict() for page in outline.pages if page.page in set(shown)
            ]
        remaining = (
            max(outcome.pages - len(set(shown)), 0) if wanted else max(outcome.pages - (shown[-1] if shown else 0), 0)
        )
        # Blocking is decided on everything measured, before any folding: what refuses
        # publication cannot depend on how the reply is arranged.
        blocking = _return.blocking_of(findings, self.profile.blocking_kinds)
        asks = _asks(findings, blocking)
        # When the pass did not run, the bucket named after it is misleading: those
        # findings are the author's this round. A live model read
        # `for_the_design_pass` as "somebody else has this" and left 25 pairs of
        # overlapping words alone through eight builds.
        skipped = (result.data.get("design_pass") or {}).get("skipped")
        if skipped and any(finding.audience is Audience.DESIGNER for finding in findings):
            asks.insert(
                0,
                "the design pass did not run this time, so everything under "
                "for_the_design_pass is yours to answer as well -- it is the same program either way",
            )
        if remaining:
            payload["pages_shown"] = ", ".join(str(number) for number in shown) if shown else "none"
            payload["pages_not_shown"] = remaining
            more = (
                f"name them in slides, or omit slides and pass page_from={shown[-1] + 1}"
                if wanted
                else f"call ppt_build again with page_from={shown[-1] + 1}"
            )
            asks.insert(
                0,
                f"look at the {len(shown)} page(s) below; {remaining} of this deck's {outcome.pages} are not "
                f"here -- {more} -- because a page you have not looked at is a page you have not checked",
            )
        elif not blocking:
            asks.insert(0, "look at every page below")
        if not blocking:
            # The reply used to end at "look at every page", which is not a next step for a
            # model that has already looked: one run rebuilt the same finished deck eight
            # times, each reply identical, chasing a warning it had already decided to
            # accept. What refuses publication and what merely reports are different
            # questions, and only the first one has to be answered before publishing.
            asks.insert(
                0,
                # Naming the wrong call is worse than naming none: there is no publish
                # tool on this route -- a finished build *is* the delivery -- and a model
                # told to publish went looking for one, said it was not in the toolset,
                # and built again.
                f"this deck is delivered at {payload.get('pptx_path', 'the export path above')} and nothing "
                "refuses it: the findings below are reports, and the renders are the judge. Look at the "
                "pages first and trust what you see -- a finding you look at and disagree with is a finding "
                "to leave alone, and a page that reads wrong is worth fixing whether or not anything "
                "measured it. Answer the ones you agree with by editing the program and building again, "
                "or stop here"
                if not draft
                else "nothing refuses this draft. The findings are reports and the renders are the judge: "
                "read the pages, fix what looks wrong to you whether or not it was measured, and leave a "
                "finding you disagree with alone. When the pages read right, build again without `draft` -- "
                "that is the call that runs the design pass and delivers the deck",
            )
        body = _return.done(blocking=blocking, asks=asks, **payload)
        return await self._with_views(deck, outcome, body, findings, shown)

    async def _with_views(
        self, deck: Project, outcome: Any, body: str, findings: list[Finding], wanted: list[int]
    ) -> str | ToolResult:
        """Every page render, each preceded by the line that identifies it.

        The label is a separate text part rather than a caption inside the JSON:
        only that way does it stay next to its picture when a transport cannot
        carry an image in a tool result and the images follow in a user message.
        """
        renders = await self.views.pages(outcome.pptx_path, deck.review_dir, wanted)
        if not renders:
            return body
        by_page: dict[int, list[Finding]] = {}
        for finding in findings:
            if finding.page is not None:
                by_page.setdefault(finding.page, []).append(finding)
        outline = load_outline(outline_path(deck))
        planned = {page.page: page for page in outline.pages} if outline is not None else {}
        blocks: list[Any] = []
        for number in sorted(renders):
            said = [f"Page {number} of {outcome.pages}"]
            plan = planned.get(number)
            if plan is not None:
                said.append(f"Planned claim: {plan.claim}")
                if plan.carries:
                    said.append(f"Planned visual/layout: {plan.carries}")
                if plan.table_plan:
                    said.append(f"Planned table information shape: {plan.table_plan}")
                if plan.says:
                    said.append("Planned supporting points:\n" + "\n".join(f"  - {point}" for point in plan.says))
                if plan.figures:
                    said.append("Planned figures: " + ", ".join(plan.figures))
                if plan.needs:
                    said.append(f"Planned needs: {plan.needs}")
            said += [f"  - {f.message}" for f in by_page.get(number, [])]
            blocks.append(text_block("\n".join(said)))
            blocks.append(image_block(self.views.data_uri(renders[number])))
        return _return.with_images(body, blocks)


def _asks(findings: list[Finding], blocking: list[Finding]) -> list[str]:
    """Every applicable instruction, in the order they have to be answered.

    All of them, not the first: voicing one problem while two stand reads as the
    only thing wrong with the deck. Blocking first, because nothing publishes
    until those clear, and the author's warnings before the designer's, because
    the design pass has already been given its own.
    """
    asks: list[str] = []
    for kind, count in sorted(_counts(blocking).items()):
        asks.append(_ASK.get(kind, f"resolve {count} {kind} finding(s)").format(count=count))
    mine = [f for f in findings if f.audience is Audience.AUTHOR and f.severity is Severity.WARNING]
    for kind, count in sorted(_counts(mine).items()):
        asks.append(_ASK.get(kind, f"consider {count} {kind} finding(s)").format(count=count))
    return asks


_ASK = {
    "citation": "fix {count} page(s) citing one figure while showing another",
    "band": "remove {count} filled colour bar(s)",
    "unmapped_page": "give each slide its own block in build.py",
    "unseen_page": "look at the {count} page(s) you have not been shown, by running the build again",
    "density": (
        "cut {count} page(s) back to what a slide holds -- the design pass may rearrange a page but never "
        "cut it, so an overloaded page comes back compartmented however often it is redesigned"
    ),
    "evidence": "put something on the pages that are all prose: a figure, a table, a diagram",
    "wide_table": "narrow {count} table(s) or split them",
}


def _counts(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return counts


# A page that did not come from the prototype it named, or that was cloned and
# overlaid, is wrong at the root. Everything below is what that looks like once it is
# rendered.
# A page can be wrong at the root, and then its geometry findings are symptoms of that
# one thing. `spilled_copy` joined the list from a delivered deck: two page titles set
# in a `wrap=False` box painted off the canvas, and each of them also collided with the
# corner mark it ran into -- one defect, reported twice, with the collision reading as
# the more concrete of the two and sending the author to move the corner mark.
_ROOT_CAUSES = frozenset({"template_underlay", "prototype_kept", "spilled_copy"})
_CONSEQUENCES = frozenset(
    {
        "word_collision",
        "card_overflow",
        "crowded_panel",
        "overset_copy",
        "wrapped_label",
        "orphan_line",
        "off_page",
        "over_layout_art",
    }
)


def _folded(findings: list[Finding]) -> tuple[list[Finding], dict[str, Any]]:
    """Geometry findings folded into a count on pages that are wrong at the root.

    A live deck came back with 55 blocking findings: 29 unreplaced placeholders, 25
    pairs of overlapping words, and one saying the pages had been cloned for their
    background with new text boxes laid over them. The model read the 25 and moved
    boxes around, three `edit_file` calls at a time, because 25 concrete coordinate
    problems look far more actionable than one architectural one. They were all the
    same problem: the copy was on a second layer over the template's own.

    So a page whose root cause is named keeps that finding and reports its geometry as
    a count. The count is not a dismissal -- the numbers are real, and they go when the
    page is built the way it said it would be. Nothing is dropped from the blocking
    decision, which is taken before this runs.
    """
    roots = {finding.page for finding in findings if finding.kind in _ROOT_CAUSES and finding.page}
    if not roots:
        return findings, {}
    kept: list[Finding] = []
    counted: dict[int, dict[str, int]] = {}
    for finding in findings:
        if finding.page in roots and finding.kind in _CONSEQUENCES:
            counted.setdefault(finding.page, {})
            counted[finding.page][finding.kind] = counted[finding.page].get(finding.kind, 0) + 1
            continue
        kept.append(finding)
    if not counted:
        return findings, {}
    return kept, {
        "note": (
            "these pages are wrong at the root -- they did not come from the prototype they named -- and the "
            "geometry below is what that looks like rendered. Build the page from its prototype and they go. "
            "They are counted rather than listed so the root cause is not buried under its own symptoms"
        ),
        "pages": {str(page): kinds for page, kinds in sorted(counted.items())},
    }


def _summary(design: dict[str, Any]) -> dict[str, Any]:
    """What the design pass did, without the machinery it did it with.

    Or why it did not run. Three live runs never once reached it and the replies said
    nothing about that, so the absence read as "the layout was fine".
    """
    if "skipped" in design:
        return {"did_not_run": design["skipped"]}
    rounds = design.get("rounds") or []
    compact_rounds: list[dict[str, Any]] = []
    for entry in rounds:
        pages = entry.get("pages") or {}
        verdicts: dict[str, int] = {}
        decisions: dict[str, Any] = {}
        for page, raw in pages.items():
            decision = raw if isinstance(raw, dict) else {}
            verdict = str(decision.get("verdict") or "unknown")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            visible = {
                key: decision[key]
                for key in ("verdict", "notes", "error")
                if decision.get(key)
            }
            if visible and (visible.get("verdict") != "ok" or len(visible) > 1):
                decisions[str(page)] = visible
        compact_rounds.append(
            {
                "round": entry.get("round"),
                "deck": entry.get("deck") or {},
                "pages_judged": len(pages),
                "page_verdicts": verdicts,
                "page_decisions": decisions,
                "refused": {str(k): v for k, v in (entry.get("refused") or {}).items()},
                "rewrote": entry.get("rewrote") or [],
                "rewrote_setup": bool(entry.get("rewrote_setup")),
            }
        )
    return {
        "status": design.get("status", "unknown"),
        "changed": bool(design.get("changed")),
        "rounds": compact_rounds,
        "vocabulary": design.get("vocabulary") or [],
        "artifacts": design.get("artifacts") or {},
    }
