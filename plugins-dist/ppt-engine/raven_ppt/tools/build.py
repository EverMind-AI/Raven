"""`ppt_build`: run the author's program, then show it every page it made.

The tool is thin on purpose -- schema, argument adaptation, the reply -- because
everything it does is somewhere else: the backend runs the program, the gates
measure the file, publication delivers it or refuses. What lives here is the one
thing a tool owns, which is what the model sees coming back -- and on this route
that reply *is* the review: the renders come back to whoever wrote the program,
which is the only actor that can act on all of what they show.

That turns out to matter more than it sounds. The predecessor returned twelve
unlabelled page renders and a JSON blob, and the model could not reliably say
which page it was talking about; it voiced one problem when two stood, and a run
spent seven rebuilds re-checking numbers because the fact ask was the only
instruction it got while seventeen colour bars stood untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven.contracts.tool import Tool, ToolResult
from raven.utils.images import image_block, text_block
from raven_ppt.backends.script import read_script
from raven_ppt.contracts import (
    DeckBrief,
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
from raven_ppt.services import regress, review_ledger, tier
from raven_ppt.services.publish import DestinationError, as_destination, delivered_decks, write_destination
from raven_ppt.services.regress import Regression
from raven_ppt.stages.build import BuildStage
from raven_ppt.tools import _return
from raven_ppt.tools._args import ArgumentError, as_ints
from raven_ppt.tools.review import READING_DECK_BUDGET_S, reading_seconds_spent, readings_taken
from raven_ppt.tools.review import _already_read as _pages_read

# How many unread pages fire the second reading on a draft. What it trades is findings
# that can still be acted on against a draft build that sometimes costs minutes instead
# of seconds: a reading of a finished deck arrives when acting on it means redoing every
# page, and one taken five pages in changes the pattern the rest of the deck repeats.
UNREAD_PAGES_BEFORE_READING = 5
# How many readings a draft may have before the author is on its own with the gates.
#
# The loop had no end. A page counts as unread once the code that drew it changes, a
# revision batch changes five to seven pages, and the floor above is five -- so acting
# on a reading re-armed it, every time. Measured on a fifteen-page run: two readings,
# about thirty reader calls (one per page, each carrying that page's render), opinions
# on eleven of fifteen pages, fifty edits after the first build, and 72 percent of a
# 63-minute run spent with the model writing. Two is what the reading is worth: the
# first sets the deck's pattern, which is the argument for reading a draft at all, and
# the second checks the pattern took. The delivered build always reads what is left.
READINGS_IN_DRAFT = 2
# And each one costs more than the last. A re-reading is worth less than a first
# reading -- the pattern is already set -- so the nth wants n times the pages unread.
# On the run above the second reading fired on seven unread pages; at this floor it
# would have wanted ten and stayed quiet, which is the whole difference.
REREADING_COSTS = 2


@dataclass(frozen=True)
class Reading:
    """What the first automatic review contributes to a build reply."""

    payload: dict[str, Any]
    asks: list[str]
    blocks: list[Any]


def _read_payload(read: dict[str, Any]) -> dict[str, Any]:
    """The reading's own fields, without the envelope this reply already has."""
    return {key: value for key, value in read.items() if key not in ("ok", "project", "next_step")}


class PptBuildTool(Tool):
    name = "ppt_build"
    description = (
        "Build the deck by running the python-pptx program you write, then look at every page it made. "
        "Write the program to deck/build/build.py with write_file -- that whole path, "
        "relative to the workspace -- revise it with edit_file, then call this with just the project. "
        "It returns a batch of rendered pages, whatever the script printed to stdout, and everything "
        "measured on the deck; the reply names how many pages it did not show and which page to pass to "
        "page_from to see the next of them. "
        "It refuses until ppt_prepare has read the task, ppt_brief holds what the user decides and "
        "ppt_outline has recorded what each page argues; and it refuses to publish a deck holding a page "
        "whose current code has never been rendered back to you, which is answered by building again and "
        "looking rather than by editing. "
        "Iterate: read the render, edit the file, run again -- and put the edits and this call in one "
        "reply. A reply's tool calls run in the order they are listed, so edit_file (or the write that "
        "appends a block) followed by ppt_build in the same reply is one turn, where a reply per call is "
        "two; a measured run spent half its iterations alternating one edit and one build."
    )
    timeout_seconds = 900.0

    def __init__(
        self, workspace: Path, stage: BuildStage, views: Any, profile: Profile, review: Any | None = None
    ) -> None:
        self.workspace = workspace
        self.stage = stage
        self.views = views
        self.profile = profile
        # The second reading, run by this tool rather than waited for. Asking the
        # author to call it left it to the author: one run made twenty builds and
        # reached it at iteration 61 on its own, and a run that never leaves draft
        # never meets the sentence that names it at all. When it is worth having is
        # `_first_reading`.
        self.review = review

    @property
    def views_per_call(self) -> int:
        """How many page renders one reply carries, which is also the `slides` cap.

        Read off the stage rather than held here. Two constants carried this number and
        drifted to 1 and 3: the stage writes the unseen-page record from the pages it
        selected, so a cap of its own on this side either forbids pages the reply would
        have carried or lets a call name pages the reply drops without saying so.
        """
        return self.stage.views_per_call

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "script": {
                    "type": "string",
                    "description": (
                        "Optional: a complete python-pptx program, written to the deck's build.py before it "
                        "runs. Omit it -- or leave it empty -- to run the build.py you wrote with the file "
                        "tools, which is the better path for a deck-sized script, since inlining one means "
                        "regenerating every line to change any line."
                    ),
                },
                "slides": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": self.views_per_call,
                    "description": (
                        f"up to {self.views_per_call} page(s) to render back, when you want those pages "
                        "rather than the ones you have not been shown -- the pages you just edited, usually. "
                        "Omit it and a build shows the pages whose code changed since you last saw them, "
                        f"up to {self.stage.catch_up_views} at a time, so a build without slides is how you "
                        "look at what you wrote"
                    ),
                },
                "page_from": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "where the batch that comes back as renders starts. Without slides, a build shows "
                        f"the pages you have not been shown since their code was written, up to "
                        f"{self.stage.catch_up_views} of them, and once every page has been shown it walks "
                        f"the deck {self.views_per_call} at a time from here; omit it to start at page 1"
                    ),
                },
                "deliver_to": {
                    "type": "string",
                    "description": (
                        "where the user asked for the file: an absolute path to the .pptx to write, or a "
                        "directory ending in / to keep the deck's own name. State it once -- on any build, a "
                        "draft included -- and it is kept for this deck: every build that publishes writes the "
                        "deck under out/ and copies the same bytes there, first delivery and every revision "
                        "after, and the reply names that path with the slide count. The deck's PDF preview is "
                        "written beside it under the same name as a second file, but only when nothing is at "
                        "that .pdf name already: a file there is the user's and is left alone, and the reply "
                        "says the preview stayed under out/ -- tell them that when it does. A copy you make "
                        "yourself with exec is not recorded and not the deliverable"
                    ),
                },
                "draft": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "true while the program is still being written: it builds what exists, measures it "
                        "and hands back the render, without holding a part-written deck to the agreed length "
                        "or to having shown you every page, and without publishing. Omit it once the requested "
                        "pages are written -- only a finished build runs every gate and delivers the deck"
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
        page_from: int = 1,
        draft: bool = False,
        deliver_to: str | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        # Before the order checks, so a destination stated on a build that is refused
        # for a missing outline is kept all the same: the author fixes the outline and
        # builds again without repeating it, and the deck still lands where it was asked.
        stated: dict[str, Any] = {}
        if deliver_to is not None:
            try:
                kept = write_destination(
                    deck, as_destination(deliver_to, deck, default_name=self.stage.destination(deck).name)
                )
            except DestinationError as exc:
                return _return.failed(str(exc), hint='deliver_to: "/home/user/decks/intro.pptx" or "/home/user/decks/"')
            stated["deliver_to"] = str(kept)

        agreed = load_brief(brief_path(deck))
        if unready := _unready(deck, agreed, stated):
            return unready

        try:
            wanted = as_ints(slides, "slides") or None
        except ArgumentError as exc:
            return _return.failed(str(exc), hint="slides: [7] -- page numbers, one per page you want back")

        # `wanted` and not `wanted or [page_from]`: filling it in made the stage's
        # walk branch unreachable, so omitting `slides` returned a single page whatever
        # the budget was, and a nineteen-page deck took nineteen builds to look at once.
        # The tier's caps (services/tier): a whole-deck build past the cap releases the
        # deck as it stands, and a reading past its cap is not taken.
        #
        # A draft counts as a whole build, and the cap releases a draft. Before that it
        # counted nothing and released nothing, which made it inert on every run that
        # never left draft: one_door made 13 draft builds and never wrote a builds.json
        # at all, a medium-tier run made 7 and the same, and across four runs the author
        # passed `draft: true` on 7/7, 13/13 and 5/5 of its calls. `draft: false` is a
        # road it does not take on its own, so a cap that only counts that road is a cap
        # on nothing. A draft already costs a whole build -- `measure`, which renders,
        # runs before the stage's draft branch and `_DRAFT_EXEMPT` waives three finding
        # kinds -- so one count is the honest count, and one number is one the author can
        # be told.
        caps = tier.read_caps(deck.workspace)
        whole = reached = tier.whole_builds_taken(deck) + 1
        release = caps.build_cap is not None and whole >= caps.build_cap
        result = await self.stage.run(
            deck,
            script,
            slides=wanted,
            page_from=page_from,
            draft=draft,
            release=release,
        )
        outcome = result.data.get("outcome")

        if outcome is None or not getattr(outcome, "ok", False):
            # The note travels on a failure too. A helper the author edited is
            # restored by provisioning, which happens before the script runs, so
            # the build that then dies is exactly the case that needs telling --
            # nine failed builds in a row is what the silence cost.
            return _return.failed(
                "the build script did not produce a deck",
                stderr=getattr(outcome, "stderr", "") or None,
                stdout=getattr(outcome, "stdout", "") or None,
                note=getattr(outcome, "note", "") or None,
                hint="fix the script and run ppt_build again",
            )
        findings = list(result.findings)
        # Pages the runner stood in for because their block raised. Read before the
        # count, because it is what the count turns on.
        lost = sorted({f.page for f in findings if f.kind == "page_failed" and f.page is not None})
        reprieve = None
        # Counted only now: a script that produced no deck was not a build of it,
        # and a budget it spent would release the first deck that exists past its
        # blocking findings after a single real build. A build that lost a page to a
        # raised block is the same thing for that page, so it goes against its own
        # allowance instead (services/tier) until the allowance is spent.
        reprieve = tier.count_lost_build(deck) if lost else None
        whole = tier.whole_builds_taken(deck) if reprieve else tier.count_whole_build(deck)
        # `reached` and not `whole`: the release above was decided on the count this
        # build would have made, and it is what the author is being told about --
        # while `whole` is what the build was actually charged, which the lost-pages
        # line below reports. A spared build that says "build 2 of 3" beside "the cap
        # is reached" is two true numbers reading as a contradiction.

        payload: dict[str, Any] = {"project": project, "slides": outcome.pages}
        # Every build, first thing, because the measured failure is a budget the author
        # cannot see: it answered a reading page by page knowing another one would come,
        # and spent 6 more whole builds after the findings stopped falling. A number it
        # is told is a number it can plan the next call against.
        payload["build_budget"] = (
            f"whole-deck build {whole} of the {caps.mode or 'session'} tier's {caps.build_cap} -- a draft "
            "counts as one, and the build that reaches the cap delivers the deck as it stands"
            if caps.build_cap is not None
            else f"whole-deck build {whole}; this {caps.mode or 'session'} tier sets no cap"
        )
        if outcome.note:
            payload["note"] = outcome.note
        # Two notes reach here and only one used to be read. `outcome.note` is the
        # script's; `result.note` is the stage's, and the stage puts a publish refusal
        # there -- an empty deck, a staged file that vanished, a deck that changed
        # after it was checked. None of those is a blocking finding, so the reply came
        # back `"ok": true` with no pptx_path and told the author the deck was
        # delivered "at the export path above" and that nothing refused it. Nothing
        # had been written at all.
        refused = None if result.ok else (result.note or "the deck was not published")
        if refused:
            payload["not_delivered"] = refused
        if outcome.stdout:
            payload["stdout"] = outcome.stdout
        # What the projected modules warned about while the script ran -- a picture
        # cropped past what its frame can hold, and whatever else stopped being a crash.
        # A warning nobody reads is a crash with worse manners, so it comes back beside
        # the findings rather than staying in a stderr only a failure used to show.
        stderr = (getattr(outcome, "stderr", "") or "").strip()
        if stderr:
            payload["warnings"] = stderr[-3000:]
        if findings:
            shown_findings, folded = _folded(findings)
            payload["measured"] = _return.grouped(shown_findings)
            if folded:
                payload["consequences_folded"] = folded
        if "pptx_path" in result.data:
            payload["pptx_path"] = result.data["pptx_path"]
        if "pdf_path" in result.data:
            payload["pdf_path"] = result.data["pdf_path"]
        if result.data.get("republished"):
            payload["republished"] = True
        if changed := result.data.get("delivery_changed"):
            payload["delivery_changed"] = changed
        payload.update(
            _released_at_cap(caps, reached=reached, released=result.data.get("released"), published=result.ok)
        )
        payload.update(_lost_pages(caps, whole=whole, lost=lost, reprieve=reprieve, draft=draft))
        payload.update(_delivered(result.data, stated))
        if draft and "pptx_path" not in payload and (not wanted or max(wanted) <= outcome.pages):
            # `pptx_path not in payload`, because a draft that reached the cap has just
            # delivered, and telling that reply a draft does not deliver contradicts the
            # path above it.
            #
            # Its own sentence rather than a clause in a next step, and otherwise
            # unconditional. The next step below says it too, but only when nothing is blocking
            # -- and a draft whose pages are drawn is exactly the state that collects
            # findings, so the reply that most needs this line is the one that would not
            # carry it. Four measured runs' builds:
            # stack5 took 17 finished builds and published; stack6 took 5 drafts and 0
            # finished, one_door 13 drafts and 0 finished, medium 1 finished, and none of
            # those three published. Every run that never left draft never delivered, and
            # the party choosing whether to be judged is the party the gates are for.
            payload["draft_does_not_deliver"] = (
                "these pages are drawn, and a draft is not what delivers them -- the deck reaches the user "
                "only from a finished build (`draft: false`), which is also the call that runs every gate "
                "and takes the second reading"
            )
        if dropped := result.data.get("dropped_pages"):
            # Said whether or not the cap released anything else, because it is the one
            # difference between the deck measured and the deck delivered: the file is
            # short and its page numbers past the gap no longer match the ones in the
            # findings above. Unsaid, that reads as a deck that was always this long.
            payload["pages_left_out"] = (
                f"page(s) {', '.join(str(page) for page in dropped)} did not draw, so the delivered deck has "
                f"{outcome.pages - len(dropped)} of {outcome.pages} pages and the pages after the gap are "
                "numbered one lower there than in the findings above. Fix those blocks and build again to "
                "deliver the whole deck"
            )

        shown = list(result.data.get("showing") or [])
        outline = load_outline(outline_path(deck))
        if outline is not None:
            payload["planned_pages"] = [page.as_dict() for page in outline.pages if page.page in set(shown)]
        # Blocking is decided on everything measured, before any folding: what refuses
        # publication cannot depend on how the reply is arranged.
        blocking = _return.blocking_of(findings, self.profile.blocking_kinds)
        if result.data.get("released") and result.ok:
            # The stage published past these at the tier's cap. They stay in the reply
            # as reports; deriving a refusal from them here answered a delivered deck
            # with "not published" and a next step to build again.
            #
            # `result.ok`, because the cap does not release everything: a page the
            # runner stood in for holds the deck back at any build count (see
            # stages/build.HELD_AT_THE_CAP), and clearing this on the cap alone
            # answered a refused deck as delivered -- the one reading the author
            # cannot recover from, since nothing then says the page is still missing.
            blocking = []
        asks = _asks(findings, blocking)
        # Every build, not once at the brief. What the user ruled out was formatted
        # into each design call and nowhere else, so with those calls gone this is the
        # only place it can reach the model that draws the pages -- and a prohibition
        # recorded thirty turns ago, before a line of the program was written, is one
        # nothing is holding.
        if agreed is not None and agreed.forbidden:
            payload["forbidden"] = list(agreed.forbidden)
            asks.append(
                "nothing in this deck uses " + "; ".join(agreed.forbidden) + " -- the user ruled these out, "
                "and smaller, once, or as decoration is still using one. A page that brings one back makes "
                "its point another way"
            )
        unseen = result.data.get("unseen_after")
        if look := _pages_ask(payload, unseen, shown=shown, wanted=wanted, pages=outcome.pages, blocking=blocking):
            asks.insert(0, look)
        if refused:
            # A refusal here is not a finding to weigh: nothing was written, so there is
            # nothing to look at and nothing to accept. Say what stopped it and stop.
            asks.insert(
                0,
                f"nothing was published: {refused}. Fix that and build again. The user receives only what "
                "this tool publishes: copying deck.pptx anywhere yourself hands the user nothing, and a reply "
                "claiming otherwise would be false",
            )
        elif lost and "pptx_path" in payload and not draft:
            # A published build that lost pages must not read as the end of the run. This
            # is the state the tier's cap creates: `stages/build.py` empties the blocking
            # list when it releases at the cap, so the deck goes out -- which is right,
            # the user gets a deck rather than nothing -- and the reply then said "nothing
            # refuses it". A live run's last build lost two pages to one undefined name,
            # published, and the author never saw it. Publication is not the question a
            # stand-in page asks: it asks for one more build, and unless the reprieves are
            # spent that build costs nothing.
            asks.insert(
                0,
                f"this deck is delivered at {payload['pptx_path']} and page(s) "
                f"{', '.join(str(page) for page in lost)} are not in it: each is a page saying its block raised, "
                "and the traceback is in that page's page_failed finding. That is not a finding to weigh against "
                "the others -- a raised name or a value a chart would not take is one edit, and the deck the user "
                "has is short until it is made. Fix the block(s) and build again"
                + (
                    ", which is not counted against this tier's builds"
                    if reprieve is not None
                    else ". This deck's reprieves for a lost page are spent, so that build is counted like any "
                    "other -- and if it loses a page too, what is delivered now is what the user keeps"
                ),
            )
        elif not blocking and ("pptx_path" in payload or draft):
            # The reply used to end at "look at every page", which is not a next step for a
            # model that has already looked: one run rebuilt the same finished deck eight
            # times, each reply identical, chasing a warning it had already decided to
            # accept. What refuses publication and what merely reports are different
            # questions, and only the first one has to be answered before publishing.
            #
            # `or draft`, because the draft half of this was written for a run that stayed
            # in draft for fourteen builds and could not reach it: a draft has no
            # `pptx_path` -- the stage returns before publication -- so on the one path it
            # was written for, the guard was false and the text never appeared. The test
            # that pinned it passed because its stage returns a path whether or not the
            # build was a draft.
            asks.insert(
                0,
                # Naming the wrong call is worse than naming none: there is no publish
                # tool on this route -- a finished build *is* the delivery -- and a model
                # told to publish went looking for one, said it was not in the toolset,
                # and built again.
                f"this deck is delivered at {payload['pptx_path']} and nothing "
                "refuses it: the findings below are reports, and the renders are the judge. Look at the "
                "pages first and trust what you see -- a finding you look at and disagree with is a finding "
                "to leave alone, and a page that reads wrong is worth fixing whether or not anything "
                "measured it. Answer the ones you agree with by editing the program and building again. "
                "A second reader has already read this deck once on an empty context -- that is "
                "first_reading above, if it is there -- and ppt_review reads it again, or "
                "ppt_review(pages=[...]) reads back the pages you have just changed"
                if "pptx_path" in payload
                else "nothing refuses this draft. The findings are reports and the renders are the judge: "
                "read the pages, fix what looks wrong to you whether or not it was measured, and leave a "
                "finding you disagree with alone. When the pages read right, build again without `draft` -- "
                "that is the call that runs every gate and delivers the deck -- and it runs ppt_review on "
                "it without being asked, so a second reader reads every page on an empty context and hands "
                "back what is wrong. A deck that stays in draft is a deck nobody but you has read",
            )
        if delivery := _delivery_ask(payload, pages=outcome.pages, refused=refused, blocking=blocking):
            asks.insert(0, delivery)
        if "delivered_to" not in payload and (refused or blocking or draft):
            if held_at := delivered_decks(deck.state_dir):
                # Nothing reached the destination this build; what the user holds there is
                # the last deck that passed, and the author should not say otherwise.
                payload["destination_unchanged"] = (
                    f"{held_at[-1]} still holds the last published deck; this build wrote nothing there"
                )
            elif "deliver_to" in payload:
                asks.append(
                    f"the destination {payload['deliver_to']} is kept for this deck: the build that publishes "
                    "writes the deck there as well and names it"
                )
        if changed := result.data.get("delivery_changed"):
            # Before the regression note, because it is about the deck the user is
            # holding rather than about a page: the file this build replaced was not
            # the one the record described, so an edit reached the user that no gate
            # and no reader ever saw.
            asks.insert(0, changed)
        # Last, so it reads first: an author told a page got worse by the fix it just
        # made has to see that before it picks the next fix, or it answers the damage
        # of its own last round.
        regressed = self._regressions(deck, outcome, findings)
        if regressed:
            payload["regressed"] = _regressed(regressed)
            asks.insert(0, _regression_ask(regressed))
        # Last in the list because it is about the shape of the next reply, not about
        # the deck: the calls in one reply run in order, and a run that sent each edit
        # and each build as its own reply spent 32 of 72 iterations on that alternation.
        if findings or draft:
            asks.append(_SAME_REPLY)
        read = await self._first_reading(deck, project, draft, blocking, payload, first=shown)
        # What the reader said and nobody answered, every build until it is answered.
        # The reading itself lives in one reply; a live run delivered a deck with a
        # paragraph the reader had reported buried three builds earlier, because the
        # reply that named it had scrolled past. Listed after the reading so a fresh
        # entry is in the ledger before the ledger is read.
        held = review_ledger.open_findings(deck) if self.review is not None else []
        if held:
            payload["open_findings"] = review_ledger.summary(held)
            asks.append(review_ledger.ask(held))
        if read is not None:
            payload["first_reading"] = read.payload
            # Said out loud, because a budget the author cannot see is a budget it
            # spends as though it were endless: it answered a reading page by page
            # knowing another one would come, and another one came. Only on a draft --
            # the delivered build reads what is left whatever the count says, so a
            # budget quoted there would be a number about nothing.
            if draft:
                left = max(0, READINGS_IN_DRAFT - readings_taken(deck))
                payload["readings_left_in_draft"] = left
                asks.insert(
                    0,
                    f"{left} more automatic reading(s) of this draft, and then the gates are the only "
                    "thing reading it: answer what you agree with in one pass rather than a page at a time"
                    if left
                    else "that was the last automatic reading of this draft -- from here the gates are "
                    "the only thing reading it, until you build without `draft`, which reads what is left",
                )
            asks[0:0] = read.asks
            body = _return.done(blocking=blocking, asks=asks, **payload)
            # The reviewer's pages rather than this call's batch: those are the ones
            # something was found on, and a reply cannot carry both sets.
            return _return.with_images(body, read.blocks) if read.blocks else body
        body = _return.done(blocking=blocking, asks=asks, **payload)
        return await self._with_views(deck, outcome, body, findings, shown)

    async def _first_reading(
        self,
        deck: Project,
        project: str,
        draft: bool,
        blocking: Sequence[Finding],
        payload: dict[str, Any],
        first: Sequence[int] = (),
    ) -> Reading | None:
        """The second reader, whenever enough of the deck is unread to be worth one.

        Drafts included, and that is the change the measurement asked for. Held back
        for the delivered build, the reading arrived at the finish line: one 18-page run
        spent its first eleven builds in draft, so the reader first ran inside the build
        that delivered, read all 18 pages, reported 45 problems on 18 of them -- and the
        deck was published 38 seconds later after one edit, because answering 45
        findings on a finished deck means redoing every page. The same findings five
        pages in change the pattern the rest of the deck is about to repeat.

        What keeps that from shipping unread pages is that a page counts as read only
        while the code that drew it is the code that was read, so a page rewritten after
        its reading is read again -- and the delivered build reads whatever is left
        rather than waiting for five of them.

        A refused build is still not a deck: nothing was published, so there is nothing
        to look at and nothing to accept.

        The record on disk is what says which page-versions have been read, so a resumed
        job does not pay for them twice.
        """
        if self.review is None or blocking or "not_delivered" in payload:
            return None
        # `ppt_outline` takes up to 40 pages and one reading covers 30, so a deck longer
        # than the cap is read across builds: only the unread pages are asked for, and
        # the reader caps its own call, so the remainder comes back on the next build.
        built_pages = payload.get("slides")
        if not isinstance(built_pages, int) or built_pages <= 0:
            return None
        taken = readings_taken(deck)
        if draft and taken >= READINGS_IN_DRAFT:
            return None
        caps = tier.read_caps(deck.workspace)
        if caps.reading_cap is not None and taken >= caps.reading_cap:
            payload["reading_cap"] = (
                f"this deck has had its {caps.reading_cap} reading(s) for the {caps.mode or 'session'} tier; "
                "the gates and the renders in this reply are what reads it from here"
            )
            return None
        spent = reading_seconds_spent(deck)
        if spent >= READING_DECK_BUDGET_S:
            # Said once per build, because a reading that silently stops looks like a
            # deck that came back clean.
            payload["reading_budget"] = (
                f"this deck's readings have taken {spent:.0f}s together, past the {READING_DECK_BUDGET_S:.0f}s a deck "
                "gets; the gates and the renders in this reply are what reads it from here"
            )
            return None
        unread = sorted(set(range(1, built_pages + 1)) - _pages_read(deck))
        if draft:
            floor = UNREAD_PAGES_BEFORE_READING * max(1, taken * REREADING_COSTS)
        elif payload.get("republished"):
            # A revision of a deck already delivered: the author is polishing one or
            # two pages against the gates, and a 240s reading of each republish is
            # more than those pages are worth -- three republishes of one live deck
            # read 9 pages in 12 minutes. The first delivery still reads what is left.
            floor = UNREAD_PAGES_BEFORE_READING
        else:
            floor = 1
        if len(unread) < floor:
            return None
        # The pages this build drew first, the backlog after: the reader takes the list
        # in order and its budget cuts the tail, and a build of `slides=[4]` that read
        # the backlog left page 4 -- the one the author was waiting on -- unread.
        named = [number for number in first if number in unread]
        unread = named + [number for number in unread if number not in named]
        try:
            reply = await self.review.execute(project=project, pages=unread)
        except Exception:  # noqa: BLE001 -- a reading that failed must not cost the delivery
            return None
        said = reply if isinstance(reply, str) else reply.model_text
        try:
            read = json.loads(said[said.index("{") : said.rindex("}") + 1])
        except (ValueError, AttributeError):
            return None
        if not read.get("ok"):
            return None
        asks = [read.get("next_step")] if read.get("next_step") else []
        blocks = list(getattr(reply, "blocks", None) or [])
        return Reading(payload=_read_payload(read), asks=asks, blocks=blocks)

    def _regressions(self, deck: Project, outcome: Any, findings: list[Finding]) -> tuple[Regression, ...]:
        """What this build broke, and the baseline moved on to this build.

        Both, in this order: the comparison has to be made against the previous
        build before the record is overwritten with this one. One basis for the two
        calls so they cannot diverge -- comparing on one rule and recording on
        another would report a page as regressed on a finding that never counted.
        """
        try:
            script = read_script(deck)
        except OSError:
            return ()
        basis: dict[str, Any] = {
            "script": script,
            "sources": outcome.sources,
            "findings": findings,
            "blocking_kinds": self.profile.blocking_kinds,
            "pages": outcome.pages,
        }
        regressed = regress.compare(deck, **basis)
        # The file these findings were measured on, so the record says which bytes it is
        # about. `compare` is not given it: it reads the previous build's record and the
        # digest is not part of the comparison.
        regress.record(deck, **basis, deck_file=outcome.pptx_path)
        return regressed

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
            # A clean page carries its claim on the label line and nothing more. The
            # whole plan rode with every page on every build, and twenty-three builds
            # of one deck repeated the same twenty plans -- 47k characters of a
            # transcript the author never needed to read twice.
            if plan is not None and not by_page.get(number):
                said = [f"Page {number} of {outcome.pages}: {plan.claim}"]
            elif plan is not None:
                said.append(f"Planned claim: {plan.claim}")
                if plan.carries:
                    said.append(f"Planned visual: {plan.carries}")
                # The structure and the way it goes wrong, next to the render of it. Both
                # were written by the plan and read by nothing: a page declared `P14 + M4
                # + M11` and the author drawing it was never told, so the declaration
                # changed what the outline said and not what the page became.
                if plan.layout:
                    stacked = " + ".join((plan.layout, *plan.layers))
                    said.append(f"Planned structure: {stacked}")
                if plan.anti_pattern:
                    said.append(f"Must not have become: {plan.anti_pattern}")
                if plan.says:
                    said.append("Planned supporting points:\n" + "\n".join(f"  - {point}" for point in plan.says))
                if plan.figures:
                    said.append("Planned figures: " + ", ".join(plan.figures))
                if plan.needs:
                    said.append(f"Planned needs: {plan.needs}")
            said += [f"  - {f.message}" for f in by_page.get(number, [])]
            blocks.append(text_block("\n".join(said)))
            blocks.append(image_block(self.views.data_uri(renders[number], label=f"page {number}")))
        return _return.with_images(body, blocks)


def _unready(deck: Project, agreed: DeckBrief | None, stated: dict[str, Any]) -> str | None:
    """Whichever of the three records a build stands on has not been written yet.

    Refused rather than defaulted. Three things about this deck are the user's to
    decide -- its language, its audience, its length -- and all three are checked
    against the finished file, so guessing them here would mean measuring the deck
    against a brief nobody agreed to. What the task said and what each page argues
    are the same kind of thing: not a build's to invent on the author's behalf.

    The order is the order they are written, so the first thing missing is the
    first thing named. A destination already stated travels on every refusal, so
    the author fixes what is missing and builds again without repeating it.
    """
    if agreed is None:
        return _return.failed(
            "no brief recorded for this deck, so there is nothing to build it against",
            hint=(
                "record the language, the audience and the page budget with ppt_brief -- ask the user "
                "with ask_user when there is a user to ask, and read them out of the task when there "
                "is not"
            ),
            **stated,
        )
    if load_plan(intake_path(deck)) is None:
        return _return.failed(
            "this deck's task has not been read, so nothing knows what it is for or what it stands on",
            hint=(
                "call ppt_prepare with the user's request first -- it locates and ingests the materials, "
                "binds a template and records what the request already states"
            ),
            **stated,
        )
    if load_outline(outline_path(deck)) is None:
        return _return.failed(
            "no outline recorded, so nothing has decided what this deck argues or what each page says",
            hint=(
                "plan it with ppt_outline first -- it checks the figures against the catalogue and the "
                "length against the brief, which is a cheap edit there and an expensive one here"
            ),
            **stated,
        )
    return None


def _released_at_cap(caps: tier.Caps, *, reached: int, released: Any, published: bool) -> dict[str, Any]:
    """The findings the tier's build cap let the deck out past, named in the reply.

    `reached` and not the count the build was charged: the release was decided on
    the count this build would have made, and that is the number the author is
    being told about. `published`, because the cap does not release everything --
    a deck the stage still refused is not a deck delivered as it stands.
    """
    if not released or not published:
        return {}
    return {
        "released_at_cap": (
            f"whole-deck build {reached} of the {caps.mode or 'session'} tier's {caps.build_cap}: the deck is "
            f"delivered as it stands, with {len(released)} finding(s) that would have held it back listed above"
        )
    }


def _lost_pages(
    caps: tier.Caps, *, whole: int, lost: Sequence[int], reprieve: int | None, draft: bool
) -> dict[str, Any]:
    """Whether a build that lost a page to a raised block was charged for it.

    Said whichever way the cap went, because it is the difference between a reply
    the author can act on and one it reads as the end of the run.
    """
    if reprieve is not None:
        return {
            "lost_pages_not_counted": (
                f"page(s) {', '.join(str(page) for page in lost)} did not draw, so this build is not counted "
                f"against the {caps.mode or 'session'} tier's {caps.build_cap} whole-deck build(s) "
                f"({whole} spent, reprieve {reprieve} of {tier.CRASH_REPRIEVES}). Fix the block(s) and build again"
            )
        }
    if lost and not draft:
        return {
            "lost_pages_counted": (
                f"page(s) {', '.join(str(page) for page in lost)} did not draw, and this deck has used all "
                f"{tier.CRASH_REPRIEVES} of its reprieve(s) for that, so this build is counted: whole-deck build "
                f"{whole}" + (f" of the {caps.mode} tier's {caps.build_cap}" if caps.build_cap else "")
            )
        }
    return {}


def _delivered(data: dict[str, Any], stated: dict[str, Any]) -> dict[str, Any]:
    """Where the deck landed this build, or the destination it is still bound to.

    One or the other, never both: a reply naming the path the deck reached beside
    the path it is kept for reads as two destinations, and the author passes both
    on to the user.
    """
    landed: dict[str, Any] = {}
    for key in ("delivered_to", "delivered_pdf", "delivered_pdf_kept_back", "delivery_failed"):
        if key in data:
            landed[key] = data[key]
    if stated and "delivered_to" not in landed:
        landed.update(stated)
    return landed


def _pages_ask(
    payload: dict[str, Any],
    unseen: list[int] | None,
    *,
    shown: Sequence[int],
    wanted: Sequence[int] | None,
    pages: int,
    blocking: Sequence[Finding],
) -> str | None:
    """Which pages have not been looked at, and the call that brings the next ones back.

    What the stage still holds unseen is the count that matters: those are the pages
    a build without `slides` comes back with, and the only ones publication waits on.
    A deck with no page-to-code mapping keeps no such record, and is walked by number.
    """
    if unseen is not None:
        remaining = len(unseen)
    else:
        remaining = max(pages - len(set(shown)), 0) if wanted else max(pages - (shown[-1] if shown else 0), 0)
    if not remaining:
        return None if blocking else "look at every page below"
    payload["pages_shown"] = ", ".join(str(number) for number in shown) if shown else "none"
    payload["pages_not_shown"] = remaining
    if unseen is not None:
        listed = ", ".join(str(page) for page in unseen[:12]) + ("..." if len(unseen) > 12 else "")
        payload["pages_not_yet_shown"] = list(unseen)
        more = f"call ppt_build again without slides and page {listed} come(s) back first"
    elif wanted:
        more = f"name it in slides, or omit slides and pass page_from={shown[-1] + 1}"
    else:
        more = f"call ppt_build again with page_from={shown[-1] + 1}"
    return (
        f"look at the {len(shown)} page(s) below; {remaining} of this deck's {pages} "
        f"{'have not been shown to you since their code was written' if unseen is not None else 'are not here'} "
        f"-- {more} -- because a page you have not looked at is a page you have not checked"
    )


def _delivery_ask(
    payload: dict[str, Any], *, pages: int, refused: str | None, blocking: Sequence[Finding]
) -> str | None:
    """The words the author repeats to the user about a deck that reached their path.

    Because a live run asked for the path and the count and got a question about a
    blocked command. Only from a build that published and that nothing refuses:
    otherwise there is no file at that path for the sentence to be true about.
    """
    if refused or blocking or "pptx_path" not in payload:
        return None
    if "delivered_to" in payload:
        return (
            f"tell the user in these terms: the deck is at {payload['delivered_to']} and has {pages} slides"
            + (f"; its PDF preview is {payload['delivered_pdf']}" if "delivered_pdf" in payload else "")
            + (f". Say this too: {payload['delivered_pdf_kept_back']}" if "delivered_pdf_kept_back" in payload else "")
            + f". That is the path the user named; out/ holds the engine's own copy at {payload['pptx_path']}"
        )
    if "delivery_failed" in payload:
        return (
            f"the deck is published under out/ but not at the destination the user named -- "
            f"{payload['delivery_failed']}. State a writable absolute .pptx path with deliver_to and build "
            "again, or tell the user plainly where the deck is and that the named path could not be written"
        )
    return None


def _regressed(regressed: Sequence[Regression]) -> dict[str, Any]:
    """The comparison, page by page, for a reader acting on it rather than reading it."""
    return {
        "note": (
            "these pages carry a blocking finding they did not carry in the previous build. Nothing has "
            "been rolled back: this deck is generated from your program, so putting the file back would "
            "leave the program describing a deck that no longer exists -- and the edit that caused this "
            "may be right in every other respect. The choice of whether to undo it is yours"
        ),
        "pages": {
            str(entry.page): {
                "gained_blocking": list(entry.gained),
                "blocking_before": list(entry.had),
                "page_block_changed": entry.recoded,
            }
            for entry in regressed
        },
    }


def _regression_ask(regressed: Sequence[Regression]) -> str:
    """Said in the reply, not only in the payload.

    A page that got worse is a fact about the author's own last edit, and the whole
    point of reporting it is that the next move should be backwards. A payload key
    the model may or may not read is not where that belongs.
    """
    said = "; ".join(_one_regression(entry) for entry in regressed)
    return (
        f"{len(regressed)} page(s) came back from this build worse than they went in -- {said}. Look at "
        "those pages and consider undoing the edit that introduced this, rather than adding a second fix "
        "on top of the first; nothing was rolled back for you, because the program is the deck"
    )


def _one_regression(entry: Regression) -> str:
    gained = ", ".join(entry.gained)
    had = ", ".join(entry.had) if entry.had else "nothing blocking"
    cause = (
        "its own block changed since then"
        if entry.recoded
        else "its own block did not change, so the shared setup, a helper it calls or the design pass did this"
    )
    return f"page {entry.page} now has {gained} and carried {had} before ({cause})"


def _asks(findings: list[Finding], blocking: list[Finding]) -> list[str]:
    """Every applicable instruction, in the order they have to be answered.

    All of them, not the first: voicing one problem while two stand reads as the
    only thing wrong with the deck. Blocking first, because nothing publishes
    until those clear, then the warnings -- every one of which is the author's,
    since the author owns the program and there is nobody else to hand one to.
    """
    asks: list[str] = []
    for kind, count in sorted(_counts(blocking).items()):
        asks.append(_ASK.get(kind, f"resolve {count} {kind} finding(s)").format(count=count))
    reports = [f for f in findings if f.severity is Severity.WARNING]
    for kind, count in sorted(_counts(reports).items()):
        asks.append(_ASK.get(kind, f"consider {count} {kind} finding(s)").format(count=count))
    return asks


_SAME_REPLY = (
    "send the edits and the next ppt_build in one reply: tool calls run in the order listed, so "
    "edit_file for every page named and then ppt_build together cost one turn, not two"
)

_ASK = {
    "citation": "fix {count} page(s) citing one figure while showing another",
    "band": "consider {count} filled colour bar(s), which carry nothing",
    "unmapped_page": "give each slide its own block in build.py",
    "page_failed": "fix the {count} page(s) whose block raised -- the traceback is in each finding's detail, and a page saying so stands where each should be",
    "displaced_copy": "anchor {count} frame(s) to the top of their box, or place() them: their copy is set above where the box starts and lands on the page's own content",
    "unseen_page": "look at the {count} page(s) you have not been shown: build again without slides and they come back first",
    "evidence": "put something on the pages that are all prose: a figure, a diagram, cards led by icons, a chart -- a table only where a reader compares figures down a column",
    "wide_table": "narrow {count} table(s) or split them",
    # A warning with its own line because it is the one finding a render cannot show:
    # the copy is simply absent from the page, so an author checking the pictures
    # against a generic "consider N boxless_copy finding(s)" sees nothing to fix.
    "boxless_copy": "give {count} box(es) declared with no height or no width the size "
    "their copy needs, or take the block off the page -- the page shows none of those words as it stands",
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
