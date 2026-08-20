"""Build the deck, measure it, improve it, deliver it.

The order is the argument. Build, then measure the *file* rather than the
submission -- what an audience sees includes whatever a helper composed on the
way. Then the design pass, but only when nothing blocking stands, because
redesigning a page whose numbers are wrong spends a round on a page that has to
change anyway. Then measure again, since the pass rewrote the pages it just
looked at. Then publish, or refuse.

One refusal is not about the deck's content at all: a page whose code has never
been rendered back to the author. Every tool description here says to look at every
page and none of it was enforceable -- publication happens below, before the renders
are even fetched, so `slides=[1]` on an eighteen-page deck shipped seventeen pages
nobody had seen. What is recorded is that the tool handed a page over, which is the
closest thing to attention that exists, and it is keyed on the code that drew the
page so that editing one makes it unseen again. Skipped when the machine cannot
render at all: refusing a deck for a missing LibreOffice would be refusing it for the
machine rather than for itself.

Warnings ride along at every step and never stop the deck. Type size, overflow
and overlap are all satisfiable by shrinking the copy, so a gate that refused
publication until they cleared could be answered by making the page worse -- and
the loop then oscillates instead of converging, ending with no deck at all. They
are fed back instead: to the author when only the author may act, to the design
pass when it can.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven.ppt.backends.script import blocks_rejection, page_blocks, script_path
from raven.ppt.contracts import (
    Audience,
    BuildOutcome,
    Finding,
    Profile,
    Project,
    Severity,
    StageResult,
)
from raven.ppt.services import seen
from raven.ppt.services.publish import PublishRefusedError, publish, stage

# How many page renders one reply carries. Here rather than in the tool because the
# unseen check has to agree with what the tool will actually show.
BATCH_VIEWS = 12


@dataclass
class BuildStage:
    """One call: everything between a submitted program and a delivered deck."""

    backend: Any
    # Awaited: measuring means rendering the deck, which is slow and blocking.
    measure: Callable[..., Any]
    profile: Profile
    design_pass: Any | None = None
    destination: Callable[[Project], Path] = field(default=lambda p: p.exports_dir / "deck.pptx")

    async def run(
        self,
        project: Project,
        script: str | None = None,
        *,
        polish: bool = True,
        slides: Sequence[int] | None = None,
        page_from: int = 1,
        draft: bool = False,
    ) -> StageResult:
        outcome = await self.backend(project, script)
        if not outcome.ok:
            return StageResult(ok=False, data={"outcome": outcome}, note=outcome.note)

        findings = list(await self.measure(project, outcome.pptx_path, outcome))
        findings.extend(_mapping_findings(project, outcome))
        if draft:
            # A program still being written is an accepted intermediate state, and the
            # previous engine learned this the same way: requiring a whole deck in one
            # submission meant "tens of thousands of tokens that took minutes and that
            # the transport truncated". Holding a three-page draft to a twelve-page
            # brief pushes the author straight back into that one giant call, so the
            # length and what was agreed are not asked of a draft. Everything else is
            # measured, and nothing is published.
            findings = [finding for finding in findings if finding.kind not in _DRAFT_EXEMPT]
        design: dict[str, Any] | None = None

        skipped = self._not_polished(polish, draft)
        if skipped is None:
            result = await self.design_pass.run(project, outcome)
            design = dict(result.data)
            if not result.ok:
                # The pass reverted itself and the deck that built is back. Its
                # finding is the author's to answer, and the measurements below
                # describe the restored deck.
                outcome = design.get("outcome") or outcome
                findings = list(await self.measure(project, outcome.pptx_path, outcome)) + list(result.findings)
            else:
                outcome = design.get("outcome") or outcome
                findings = list(await self.measure(project, outcome.pptx_path, outcome))
                findings.extend(_mapping_findings(project, outcome))

        if skipped is not None:
            design = {"skipped": skipped}

        if draft:
            showing = _showing(outcome.pages, slides, page_from)
            data: dict[str, Any] = {"outcome": outcome, "findings": findings, "showing": showing, "draft": True}
            if design is not None:
                data["design_pass"] = design
            return StageResult(ok=True, findings=tuple(findings), data=data, note="draft: not published")

        showing = _showing(outcome.pages, slides, page_from)
        findings.extend(_unseen_findings(project, outcome, showing, findings))

        blocking = self._blocking(findings)
        data: dict[str, Any] = {"outcome": outcome, "findings": findings, "showing": showing}
        if design is not None:
            data["design_pass"] = design
        if blocking:
            return StageResult(ok=False, findings=tuple(findings), data=data)

        try:
            staged = stage(project, outcome.pptx_path, pages=outcome.pages)
            delivered = publish(
                project,
                staged,
                self.destination(project),
                findings=findings,
                blocking_kinds=self.profile.blocking_kinds,
            )
        except PublishRefusedError as exc:
            return StageResult(ok=False, findings=tuple(findings), data=data, note=str(exc))
        data["pptx_path"] = str(delivered)
        return StageResult(ok=True, findings=tuple(findings), data=data)

    def _blocking(self, findings: Sequence[Finding]) -> list[Finding]:
        kinds = self.profile.blocking_kinds
        return [f for f in findings if f.severity is Severity.BLOCKING or f.kind in kinds]

    def _not_polished(self, polish: bool, draft: bool) -> str | None:
        """Why the design pass will not run, or None when it will.

        It gated on findings twice and both were wrong. First on any blocking finding at
        all, which suppressed the one stage whose job is layout exactly when the layout
        was wrong: two live runs refused on a band gate that was itself mistaken, and the
        pass never ran once in either, for every build of the run. Then on any blocking
        finding not addressed to the designer, which is the same mistake one step back --
        a deck with 29 unreplaced placeholders (the author's to fix) also had 25 pairs of
        overlapping words (the pass's), and the second list waited on the first. Across
        three complete live runs the pass ran zero times, and no reply said so.

        It does not gate on findings at all now, because the division of labour is not
        between two lists of problems. The author owns the program and can change
        anything in it -- what a page says, where its boxes are, whether it exists -- so
        every measurement goes back to the author while the deck is still a draft, and
        the author iterates until the deck is roughly right. The pass is the last look
        over a finished deck, and it can only rearrange; asking it to fix what a draft
        got wrong asks the weaker tool to do the stronger one's work.

        So: a draft is not polished, and a finished build always is. When it does not
        run, the reply says which of these it was -- a stage that quietly does nothing
        reads exactly like a stage that ran and found nothing to do.
        """
        if not polish:
            return "the call asked for no design pass"
        if self.design_pass is None:
            return "the design pass is off in this install -- tools.ppt.designer.enabled turns it on"
        if draft:
            return "a draft is not polished -- every measurement below is yours to answer, and the design pass runs on the finished deck"
        return None


# What a draft is not held to: the length the brief agreed, and the pages the
# outline mapped. Both are statements about a finished deck, and a draft is not one.
_DRAFT_EXEMPT = frozenset({"page_budget", "page_mapping", "unseen_page"})


def _showing(pages: int, slides: Sequence[int] | None, page_from: int) -> list[int]:
    """The pages this call will render back: the ones asked for, or the next batch."""
    if slides:
        return sorted(set(slides))[:BATCH_VIEWS]
    first = max(1, min(page_from, pages or 1))
    return list(range(first, min(pages, first + BATCH_VIEWS - 1) + 1))


def _unseen_findings(
    project: Project, outcome: BuildOutcome, showing: Sequence[int], measured: Sequence[Finding]
) -> list[Finding]:
    """Refuse a deck carrying a page whose current code nobody has been shown.

    Recorded here rather than in the tool because this is where publication happens:
    a check the tool ran afterwards would be a check on a file already delivered.
    """
    if any(finding.kind == "unrendered" for finding in measured):
        return []
    try:
        script = script_path(project).read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = seen.blocks_of(script, outcome.sources)
    if not blocks:
        return []
    seen.record(project, {page: blocks[page] for page in showing if page in blocks})
    absent = seen.unseen(project, blocks)
    if not absent:
        return []
    listed = ", ".join(str(page) for page in absent[:12]) + ("…" if len(absent) > 12 else "")
    return [
        Finding(
            kind="unseen_page",
            severity=Severity.BLOCKING,
            audience=Audience.AUTHOR,
            message=(
                f"{len(absent)} page(s) have never been rendered back to you since the code that draws them "
                f"was written: {listed}. Run ppt_build again -- omit `slides` and pass `page_from` to walk the "
                "rest -- and look at them. A page you have not looked at is a page you have not checked, and "
                "this is the one finding you cannot answer by editing the deck"
            ),
            detail={"pages": list(absent)},
        )
    ]


def _mapping_findings(project: Project, outcome: BuildOutcome) -> list[Finding]:
    """Whether this deck's pages can be told apart in the program that drew them.

    Blocking, and it is the one blocking finding that is not about the deck's
    content: without a page-to-code mapping the design pass cannot run, a build
    failure cannot be attributed, and a render cannot be matched to what drew it.
    A pipeline that cannot reason about a page at all should say so rather than
    proceed and guess.

    The message carries a skeleton rather than a complaint. Refused without one,
    an author rewrote the same shim three times and then left the tool.
    """
    if outcome.sources:
        return []
    try:
        lines = script_path(project).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return []
    blocks = page_blocks(lines)
    reason = blocks_rejection(lines, blocks, outcome.pages, "comments") if blocks else None
    if blocks and not reason:
        return []
    return [
        Finding(
            kind="unmapped_page",
            severity=Severity.BLOCKING,
            audience=Audience.AUTHOR,
            message=(
                (reason or "build.py has no per-page blocks, so no render can be matched to the code that drew it")
                + ". Draw the pages in build.py itself, one block of it per page:\n"
                "    def new_slide():\n"
                "        return prs.slides.add_slide(prs.slide_layouts[6])\n"
                "\n"
                "    # SLIDE 1\n"
                "    sl = new_slide()\n"
                "    title(sl, 'Unified video segmentation')\n"
                "\n"
                "    # SLIDE 2\n"
                "    sl = new_slide()\n"
                "    ..."
            ),
        )
    ]
