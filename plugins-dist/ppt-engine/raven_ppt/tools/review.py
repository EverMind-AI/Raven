"""`ppt_review`: a second reader looks at the built pages and lists what is wrong.

Every other check on this route is measured -- a number off the file or the render,
reported by a gate. This one is a reading, and it exists because the measured half
cannot reach the largest class of defect a delivered deck still carries. Three runs
delivered pages with a colour bar over the lower half, cards holding three lines in
a region twice their content, a figure at a third of the width its band offered, and
not one card carrying a mark; the gates reported some of it as warnings and the
author, having written those pages itself, accepted every one.

That is the reason this is a separate call and not more text in the build reply. The
author is reviewing its own work with the whole history of writing it in context: a
region it deliberately filled reads to it as a decision already made, and a warning
against a decision is a warning it has already answered. The reviewer here starts
empty -- one page, its plan, and the requirements -- so a void is a void.

It refuses nothing and blocks nothing. What comes back is a list, and which of it to
act on is the author's, the same standing the measured warnings have.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.tool import Tool, ToolResult
from raven.utils.images import image_block, text_block
from raven_ppt.backends.script import deck_path, page_failures
from raven_ppt.contracts import Project, brief_path, load_brief, load_outline, outline_path
from raven_ppt.services import review_ledger
from raven_ppt.services.render.pdf import words_by_page
from raven_ppt.services.review_ledger import REFUSED_FIGURE_REASON
from raven_ppt.stages.build import BATCH_VIEWS
from raven_ppt.tools import _return
from raven_ppt.tools._args import ArgumentError, as_ints

# How many pages one call reviews. A whole deck at once is the working case,
# because the author calls this when the deck is built.
MAX_PAGES = 30

# Where the list is left behind. The reply reaches the author's context and nothing
# else: a 19-page review that took seven and a half minutes was unrecoverable an hour
# later, because the transcript truncates a tool result and the tool wrote nothing.
# `blocking.json` beside it exists for the same reason -- what a round concluded has to
# outlive the round, or the next one cannot tell what it fixed.
RECORD_FILE = "review.json"
# Why a page the build stood in for is not read. Such a page is a stand-in this package
# wrote, carrying one line -- "Page 4 did not draw" and the error -- and asking a model
# what is wrong with how it looks spends a call and a page of the reading's budget to be
# told what the build's own `page_failed` finding already said. Measured on a live run: of
# six pages handed to the reader, the two that came back unread were exactly the two the
# build had reported as failed.
STAND_IN_NOT_READ = "a page the build could not draw is a stand-in saying so; its page_failed finding is the reading"

# How many of those requests are in flight at once. A backstop against a whole-deck
# review opening as many concurrent streaming requests as the deck has pages -- each
# carrying an image, against one endpoint -- and not a tuned figure: nothing here has
# ever been observed to be rate limited, and the bound has a measured cost. Nineteen
# pages came to 455s at four at a time where eighteen unbounded came to 209s. At eight,
# a fifteen-page deck was two batches deep, and with one page taking 30 to 160 seconds
# the second batch was what the 240s budget cut: five readings on one live deck read
# 6, 8, 8, 0 and 1 pages. Sixteen puts a deck of the usual length in one batch, so the
# reading takes as long as its slowest page and not as long as two of them. If a run
# is ever actually limited, that is the measurement this should be set from.
READERS = 16
# How long one reading may take, all pages together, before the pages still being
# read are given up and named as unread. Measured on an 88-minute run: six whole-deck
# builds spent 434, 208, 198, 900, 439 and 423 seconds, 43 of the 88 minutes, and the
# 900 was the build tool's own timeout -- every second of it a reasoning model
# thinking about one page's picture, fifteen pages at a time. A page not read within
# the budget comes back on the next build like any other unread page; a build that
# takes four minutes to answer is one the author waits for, one that takes fifteen
# is a turn lost.
READING_BUDGET_S = 240.0
# And how long one page may take before it is given up on its own, without holding
# the round to the budget above. Replayed on the live deck with the reader at low
# effort: fourteen pages answered in 13 to 40 seconds and one request never answered
# at all, so the round lasted the full 240s for a page that came back unread anyway.
# Three times the slowest answered page; a page past it is unread, the rest are not
# late for it.
PAGE_BUDGET_S = 120.0
# And how long all of a deck's readings may take together before the gates are the
# only thing reading it. Measured on a 20-page run: 18 whole-deck builds, each one
# re-reading the pages the last revision touched, 136 of the run's 327 minutes inside
# ppt_build. A reading is worth most on the first draft and least on the eighteenth
# revision, when the author is polishing against the gates anyway.
READING_DECK_BUDGET_S = 900.0

# What one page's reply may cost. A reviewer that finds four problems writes four
# short objects; reasoning models spend from the same budget, which is why this is
# not tight.
# Room for the reply and for a reasoning model's thinking before it, which spends
# from the same budget: at 2500 a reader cut off mid-object was asked again at 5000,
# and the second call cost what the first had. Sized so one call is the call, with
# room for a model that thinks longer than the one it was measured against.
REPLY_TOKENS = 16000

# The document both sides read. The author writes against it and this call judges the
# render by it, and that is the point of it being a file rather than a string here: a
# requirement the reviewer holds a page to and the author never saw is a requirement
# nobody agreed to. It reaches the author as `deck/build/references/design-requirements.md`
# through the same mechanism as every other reference document.
REQUIREMENTS_NAME = "design-requirements.md"

BRIEF = """One page of a finished deck, in {language}. You did not build it. Say what is
wrong with how it looks, so the author can fix it.

Judge it yourself — you can see the page. Below is only what is easy to get backwards.

{requirements}

`reads`: one sentence on how the page comes across. Handsome, plain, busy, thin,
unfinished, a wall of text, one strong figure carrying a weak column. Your own eye, not a
summary of the list. Every page gets one.

`problems`: one entry each. Only what you can see — not whether a number is true. Where and
what, not how far. Nothing for what the page does well. No repeats.

JSON and nothing else:

{{"reads": "one sentence",
 "headline": "the largest words on the page, copied exactly as printed",
 "problems": [{{"kind": "oversized_shape|underfilled_page|too_full|alignment|marks|figure|table|listed|type|claim",
 "where": "...", "what": "...", "fix": "..."}}]}}
"""

# How much of the reader's `headline` has to be on the page it was asked about. On
# the audited decks fourteen `claim` findings described a different page than the one
# the entry named (page 1's entry read as a contents page, page 8's as a paper page);
# whatever crossed them, a headline that is not on the page is the one cheap check
# that a reading is about its page. Words of one character are skipped: CJK
# punctuation and single letters match anything.
HEADLINE_MATCH = 0.5


def requirements() -> str:
    """The design-requirements document, or "" when this checkout ships no skill.

    Read at call time rather than at import: the document is the skill's, and a run
    whose skill was edited between two reviews should be judged by the edited one.
    """
    from raven_ppt.services.assets.script_helpers import REFERENCE_DIRNAME, reference_files

    return reference_files().get(f"{REFERENCE_DIRNAME}/{REQUIREMENTS_NAME}", "")


# The two kinds of blank region are two kinds here rather than one with a note, because
# the answers differ and only one of them is cheap: a round of repair answered every void
# by shrinking the shape over it, which fixes `oversized_shape` and, on the pages that
# were really `underfilled_page`, removes the cover and leaves the room. Counted apart,
# that shows up as the second number not moving.
_ROOM_KINDS = ("oversized_shape", "underfilled_page")
# `listed` is section 3.5: parallel claims set as a list, or as a table whose rows are
# each a thing rather than a column to compare down. The measured gate `listed_claims`
# reads the file for the first half and reported it on four pages of a delivered deck
# that shipped anyway; nothing was reading the render for either half.
_KINDS = (*_ROOM_KINDS, "too_full", "alignment", "marks", "figure", "table", "listed", "type", "claim")


class PptReviewTool(Tool):
    name = "ppt_review"
    description = (
        "Have a second reader look at the pages you built and list what is wrong with them. It renders each "
        "page and reviews it on an empty context -- no memory of writing it -- against the design requirements "
        "in deck/build/references/design-requirements.md, the same document you write against: blank "
        "room, crowding, alignment, missing icons, figure sizing, table geometry, type and whether the page "
        "still carries its planned claim. It returns one list of problems per page, and nothing else: it "
        "refuses nothing, publishes nothing and changes no file. "
        "Call it once the deck builds without anything blocking, before you decide the deck is done -- the "
        "pages you wrote read differently to someone who did not write them. Then answer the entries you "
        "agree with by editing build.py and building again."
    )
    timeout_seconds = 900.0

    def __init__(self, workspace: Path, views: Any, composer: Any | None = None) -> None:
        self.workspace = workspace
        self.views = views
        self.composer = composer
        self.last_reading: dict[str, Any] = {}

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "description": "the deck project, as given to ppt_prepare"},
                "pages": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "description": (
                        "the pages to review; omit to review the whole deck, which is the usual call. "
                        "Name pages when you have just changed those and want them read again"
                    ),
                },
                "dismiss": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "description": "the finding's id, as open_findings lists it"},
                            "reason": {"type": "string", "description": "what you saw on the page that answers it"},
                        },
                        "required": ["id", "reason"],
                    },
                    "description": (
                        "open findings you have looked at and are leaving as they are, each with what you saw. "
                        "Alone, this call reads nothing and returns what is still open; with pages, it dismisses "
                        "first and reads after"
                    ),
                },
            },
            "required": ["project"],
        }

    async def execute(
        self,
        project: str,
        pages: list[int] | None = None,
        dismiss: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))
        verdicts = [entry for entry in (dismiss or ()) if isinstance(entry, dict)]
        if dismiss and not verdicts:
            return _return.failed(
                "dismiss takes a list of {id, reason} objects", hint='dismiss=[{"id": "p14-a1b2c3", "reason": "..."}]'
            )
        closed: list[str] = []
        unknown: list[str] = []
        refused: list[str] = []
        if verdicts:
            missing = [entry for entry in verdicts if not str(entry.get("reason") or "").strip()]
            if missing:
                return _return.failed(
                    "every dismissal needs a reason: what you saw on the page that answers the entry",
                    hint='dismiss=[{"id": "p14-a1b2c3", "reason": "the illustration is the template design"}]',
                )
            closed, unknown, refused = review_ledger.dismiss(deck, verdicts)
            if pages is None:
                # A verdict call, not a reading: the author looked and answered, and
                # what it wants back is the list as it stands now.
                held = review_ledger.open_findings(deck)
                asks = [review_ledger.ask(held)] if held else ["nothing from the second reader is open"]
                if unknown:
                    asks.insert(0, f"{len(unknown)} id(s) are not open findings and were left alone: {unknown}")
                if refused:
                    asks.insert(
                        0, f"{len(refused)} dismissal(s) refused and left open ({refused}): {REFUSED_FIGURE_REASON}"
                    )
                return _return.done(
                    asks=asks,
                    project=project,
                    dismissed=closed,
                    **({"refused": refused} if refused else {}),
                    open_findings=review_ledger.summary(held),
                )
        if self.composer is None:
            return _return.failed(
                "this build has no model configured for a second reading, so ppt_review cannot run",
                hint="look at the renders ppt_build returns and judge them yourself",
            )
        try:
            wanted = as_ints(pages, "pages")
        except ArgumentError as exc:
            return _return.failed(str(exc), hint="pages: [3, 4, 5]")

        built = deck_path(deck)
        if not built.is_file():
            return _return.failed(
                "nothing has been built for this deck yet, so there are no pages to review",
                hint="write deck/build/build.py and call ppt_build first",
            )

        if not requirements():
            return _return.failed(
                f"this checkout ships no {REQUIREMENTS_NAME}, so there is nothing to hold a page to",
                hint="the requirements are the skill's; a build without the skill has no design review",
            )

        renders = _fresh_build_renders(deck, built, wanted) or await self.views.pages(
            built, deck.review_dir / "review", wanted or None
        )
        if not renders:
            return _return.failed(
                "the pages could not be rendered on this machine, so nothing could be looked at",
                hint="the renders ppt_build returns are the only reading available here",
            )
        # A deck longer than the cap is read across builds rather than truncated: the
        # pages an earlier reading already covered go to the back of the queue, so a
        # 40-page deck is read 30 then 10 instead of 30 then the same 30. An explicit
        # `pages=` is the caller's own choice and is left in the order it asked for --
        # the build names the pages it just drew first, and a reading that sorted them
        # by number read the backlog while the page the author was waiting on timed out.
        # A page the build stood in for is not worth a reading, so it does not take a
        # place in the round either; an explicit `pages=` is the caller's own choice.
        stood_in = sorted({int(entry["page"]) for entry in page_failures(deck)} & set(renders)) if not wanted else []
        if wanted:
            shown = [number for number in wanted if number in renders][:MAX_PAGES]
        else:
            queue = sorted(renders, key=lambda number: (number in _already_read(deck), number))
            shown = [number for number in queue if number not in stood_in][:MAX_PAGES]
        read, reads = await self._read(
            deck, renders, shown, words_by_page(deck.review_dir / f"{built.stem}.pdf") or None
        )

        found = {number: problems for number, problems in read.items() if problems}
        clean = [number for number in shown if number in read and not read[number]]
        unread = [number for number in shown if number not in read]
        payload: dict[str, Any] = {
            "project": project,
            "pages_reviewed": len(read),
            "pages_with_something_to_fix": len(found),
            "problems": {str(number): found[number] for number in sorted(found)},
            # How each page reads as a whole, which the list cannot say: a page can carry
            # three small entries and still be the best in the deck, and a page with an
            # empty list can be plain. Kept for every page read, including the clean ones,
            # because "nothing to fix" and "nothing to it" are different answers.
            "reads": {str(number): reads[number] for number in sorted(reads)},
        }
        if clean:
            payload["nothing_found_on"] = clean
        if unread:
            # Named rather than dropped: a page the reviewer could not read is not a
            # page that came back clean, and the two are one list once they are merged.
            payload["could_not_be_read"] = unread
        last = getattr(self, "last_reading", None) or {}
        payload["reading_seconds"] = last.get("seconds", 0.0)
        if last.get("over_budget"):
            payload["reading_budget"] = (
                f"{len(last['over_budget'])} page(s) were still being read when the {READING_BUDGET_S:.0f}s "
                f"reading budget ran out ({', '.join(str(n) for n in last['over_budget'])}); they stay unread "
                "and the next build reads them"
            )
        if stood_in:
            payload["stand_ins_not_read"] = {"pages": stood_in, "why": STAND_IN_NOT_READ}
        if len(renders) > len(shown) + len(stood_in):
            payload["pages_not_reviewed"] = sorted(set(renders) - set(shown) - set(stood_in))
        # What the record is for: the automatic reading runs while a page-version of the
        # deck has not been read, so the pages it covered have to be in it. A deck
        # longer than MAX_PAGES is read across builds rather than truncated silently,
        # and a round that read nothing leaves no record at all -- writing one would
        # mark the deck read and the hook would never fire again.
        payload["pages_read"] = _marked(deck, read)
        if read:
            _record(deck, payload)
            # The ledger after the record: `readings` in the record is the number this
            # reading has, and the ledger stamps its entries with it.
            moved = review_ledger.record_reading(deck, read, render_by_page(deck), readings_taken(deck))
            held = review_ledger.open_findings(deck)
            payload["ledger"] = {**moved, "open": len(held), "dismissed_now": len(closed)}
            if held:
                payload["open_findings"] = review_ledger.summary(held)
        else:
            # No page was read, so nothing is marked -- but the time was spent. Left
            # uncharged, a reader too slow for its budget read 0 of 3 pages in 240s
            # twice on one live deck and the 900s deck budget never noticed: five
            # rounds, 20 minutes, 23 page opinions. The seconds count; the pages do not.
            _charge(deck, float(payload.get("reading_seconds") or 0.0))

        # With the pictures, and this is the whole difference between a list that gets
        # read and one that gets dismissed. The reply used to be text: 27 entries about
        # 18 pages, arriving in a context where compaction keeps one image-bearing
        # message, so the author was asked to "look at the page each one names" with
        # nothing to look at. It answered "most of these are whitespace on the table
        # pages and template decoration, keeping them", made two edits and published.
        worst = sorted(found, key=lambda number: (-len(found[number]), number))[:BATCH_VIEWS]
        blocks: list[Any] = []
        for number in sorted(worst):
            said = [f"Page {number}, and what the second reader said about it:"]
            if number in reads:
                said.append(f"  reads as: {reads[number]}")
            said += [f"  - [{entry['kind']}] {entry['where']}: {entry['what']}" for entry in found[number]]
            blocks.append(text_block("\n".join(said)))
            blocks.append(image_block(self.views.data_uri(renders[number], label=f"page {number}")))
        if unknown:
            payload["not_dismissed"] = unknown
        if refused:
            payload["refused"] = refused
        rest = [number for number in sorted(found) if number not in worst]
        if rest:
            payload["carrying_more_than_shown"] = rest
        asks = _asks(found, clean, unread, rest or None)
        held = review_ledger.open_findings(deck) if read else []
        if held:
            asks.append(review_ledger.ask(held))
        if not blocks:
            return _return.done(asks=asks, **payload)
        body = _return.done(asks=asks, **payload)
        return _return.with_images(body, blocks)

    async def _read(
        self,
        deck: Project,
        renders: dict[int, Path],
        shown: list[int],
        words: dict[int, str] | None = None,
    ) -> tuple[dict[int, list[dict[str, str]]], dict[int, str]]:
        """One page, one empty context, one list. Concurrently, and per page.

        Per page rather than the whole deck in one call because the void is a fact
        about one page and a reviewer holding twenty of them reports the three
        loudest. Failures are dropped from the mapping instead of coming back empty,
        so `could_not_be_read` can say so.
        """
        brief = load_brief(brief_path(deck))
        language = brief.language if brief is not None else "the deck's own language"
        ruled_out = tuple(brief.forbidden) if brief is not None else ()
        outline = load_outline(outline_path(deck))
        planned = {page.page: page for page in outline.pages} if outline is not None else {}
        # The plan is keyed by the finished deck's page numbers. A draft shorter than
        # the outline (a 12-page draft of a 34-page plan) numbers its pages by what it
        # drew, so page 8 of the draft is not page 8 of the plan; asked anyway, the
        # reader reported "the claim is missing" on every such page. No plan, no
        # `claim` finding, until the deck has the outline's length.
        if words is not None and planned and len(words) != len(planned):
            planned = {}
        asked = BRIEF.format(language=language, requirements=requirements())

        reading = asyncio.Semaphore(READERS)
        seconds: dict[int, float] = {}
        misread: list[int] = []

        async def one(number: int) -> tuple[int, list[dict[str, str]]] | None:
            async with reading:
                started = time.monotonic()
                try:
                    reply = await asyncio.wait_for(
                        self.composer.ask(
                            asked,
                            [
                                text_block(_said(number, planned.get(number), ruled_out)),
                                image_block(self.views.data_uri(renders[number], label=f"page {number}")),
                            ],
                            max_tokens=REPLY_TOKENS,
                        ),
                        timeout=PAGE_BUDGET_S,
                    )
                except asyncio.TimeoutError:
                    # Unread, like a page the round's budget cut: `could_not_be_read`
                    # names it and the next build reads it again.
                    seconds[number] = round(time.monotonic() - started, 1)
                    return None
                seconds[number] = round(time.monotonic() - started, 1)
            try:
                payload = json.loads(reply[reply.index("{") : reply.rindex("}") + 1])
            except (ValueError, AttributeError):
                # Said, because the caller counts what came back and this page will
                # simply be missing from it: an unparsed reply and a page nobody asked
                # about are the same absence, and only one of them is a defect.
                logger.info(
                    "ppt_review: page {} came back unreadable ({} chars); it stays unread",
                    number,
                    len(reply or "") if isinstance(reply, str) else 0,
                )
                return None
            if words is not None and not _headline_on_page(str(payload.get("headline") or ""), words.get(number, "")):
                logger.info("ppt_review: page {} came back describing another page; its reading is dropped", number)
                misread.append(number)
                return None
            problems = _vetted(payload.get("problems"))
            if not planned:
                problems = [problem for problem in problems if problem.get("kind") != "claim"]
            return number, problems, str(payload.get("reads") or "").strip()

        # Bounded as a whole, not per page: the budget is what the author waits, and
        # a page still being read when it runs out is left for the next build rather
        # than making this one late.
        started = time.monotonic()
        tasks = {asyncio.ensure_future(one(number)): number for number in shown}
        done, pending = await asyncio.wait(tasks, timeout=READING_BUDGET_S)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        read = [task.result() for task in done if not task.cancelled() and task.exception() is None and task.result()]
        self.last_reading = {
            "seconds": round(time.monotonic() - started, 1),
            "per_page_s": {str(number): seconds[number] for number in sorted(seconds)},
            "over_budget": sorted(tasks[task] for task in pending),
            "misread": sorted(misread),
        }
        logger.info(
            "ppt_review: read {} of {} page(s) in {}s ({} over the {:.0f}s budget)",
            len(read),
            len(shown),
            self.last_reading["seconds"],
            len(pending),
            READING_BUDGET_S,
        )
        return (
            {number: problems for number, problems, _ in read},
            {number: reads for number, _, reads in read if reads},
        )


def _marked(deck: Project, read: Iterable[int]) -> dict[str, str | None]:
    """The record's `pages_read` after this round: each page, at the version it was read.

    The union with what earlier rounds covered, not this round's own list. A 40-page
    deck reads 1-30, then puts 31-40 first and fills the rest of the cap with pages it
    has already read -- so a record holding only the second round is {31-40, 1-20}, the
    third round goes back for 21-30, and the deck is never covered.

    The version is the build record's own fingerprint of the code that drew the page,
    so nothing here decides for a second time what "the same page" means. Empty for a
    page this round read while the build had no fingerprint for it.

    A mark this round did not touch is written back as it was read, `None` included, so
    the old list field's pages stay the old field's pages. Collapsing them to `""` on
    the way out re-labelled every untouched legacy page as a reading taken at an unknown
    version: after a round that covered page 1 of an old `[1, 2, 3]`, pages 2 and 3 had
    known fingerprints and an empty mark, so the next build launched the reader on two
    pages nobody had changed.
    """
    now = render_by_page(deck)
    marks = {**_marks(deck), **{page: now.get(page, "") for page in read}}
    return {str(page): marks[page] for page in sorted(marks)}


def _fresh_build_renders(deck: Project, built: Path, wanted: list[int]) -> dict[int, Path]:
    """The build's own page renders, when every page asked for has one newer than the deck.

    The build rendered every page to measure it fourteen seconds ago; rendering them
    again for the reader was the same fourteen seconds, and the reader's record is
    keyed by the build's renders anyway.
    """
    try:
        drawn = built.stat().st_mtime
    except OSError:
        return {}
    have = {
        number: png for png in deck.review_dir.glob("page-*.png") if (number := _build_render_number(png)) is not None
    }
    fresh = {number: png for number, png in have.items() if png.stat().st_mtime >= drawn}
    if not fresh:
        return {}
    if wanted and any(number not in fresh for number in wanted):
        return {}
    return {number: fresh[number] for number in (wanted or sorted(fresh))}


def render_by_page(deck: Project) -> dict[int, str]:
    """Per page, a fingerprint of what the last build rendered: sha1 of its PNG.

    The version a reading is recorded at. It used to be the fingerprint of the code
    that drew the page, and a revision that touched five pages -- or one line of the
    prelude every page runs -- made every one of them unread again: a 20-page run
    built the whole deck 18 times and re-read pages whose pixels had not moved, 136
    of its 327 minutes. Pixels are what the reader looks at, so pixels are the
    version: a page whose render is byte-identical to the one that was read has been
    read. Pages the last build did not render are left out, so a caller sees
    "unknown" rather than "unchanged".
    """
    import hashlib

    found: dict[int, str] = {}
    for png in deck.review_dir.glob("page-*.png"):
        number = _build_render_number(png)
        if number is None:
            continue
        try:
            found[number] = hashlib.sha1(png.read_bytes(), usedforsecurity=False).hexdigest()
        except OSError:
            continue
    return found


def _build_render_number(png: Path) -> int | None:
    """The page a build render names, or None for any other file in the review directory.

    The build writes `page-001.png`, three digits, and only that spelling is a version:
    the tool's own batch views and a reader's renders land beside them under other
    names, and a looser match let two files answer for one page.
    """
    tail = png.stem.rsplit("-", 1)[-1]
    return int(tail) if len(tail) == 3 and tail.isdigit() else None


def _marks(deck: Project) -> dict[int, str | None]:
    """Page -> the version of it a previous reading covered, out of the record it left.

    The one reader of the record's shape, and the shape is what separates two marks a
    single empty string used to carry. A record written before it kept versions is a
    plain list of page numbers, whose pages come back `None`: the old field made no
    claim about any version. A mapping's `""` is a different statement -- a reading was
    taken while the build had no fingerprint for that page -- and `_already_read` has
    to answer them differently, so they cannot share a value.

    Empty when there is no record or it cannot be read -- both mean "read it again",
    which is the safe way to be wrong.
    """
    try:
        said = json.loads((deck.review_dir / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    read = said.get("pages_read") if isinstance(said, dict) else None
    if isinstance(read, dict):
        return {int(page): (None if code is None else str(code)) for page, code in read.items() if _numbered(page)}
    if isinstance(read, list):
        return {int(page): None for page in read if _numbered(page)}
    return {}


def _numbered(page: Any) -> bool:
    return str(page).lstrip("-").isdigit()


def _already_read(deck: Project) -> set[int]:
    """The pages whose current version a previous reading covered.

    Per page-version rather than per page number, and that is what lets a reading be
    taken before the deck is finished: a page redrawn after it was read is unread
    again, so an early reading cannot leave the rewritten version of a page shipping
    with nobody having looked at it.

    Three marks, three answers, because a mark can say three different things.

    `None` is the old list field, which made no claim about versions; its pages count
    as read or a resumed job pays to read a deck that has already been read.

    `""` is a reading taken while the build had no fingerprint for the page -- a draft
    is exempt from `page_mapping`, so an early reading can cover a page whose version
    nobody knows yet. It counts as read only while that is still true. Once the mapping
    is fixed and a real, possibly rewritten, fingerprint appears, the page is unread:
    treating the empty mark as current there let the delivered build skip the reading it
    promised, for exactly the draft-to-finished transition an early reading introduces.

    A version that is known and unchanged counts as read, and so does one whose current
    version has become unknown -- nothing there says the page changed, and re-reading a
    whole deck on that silence costs a reading per build and finds nothing new.
    """
    now = render_by_page(deck)
    covered = set()
    for page, code in _marks(deck).items():
        if code is None:
            covered.add(page)
        elif not code:
            if not now.get(page):
                covered.add(page)
        elif now.get(page, code) == code:
            covered.add(page)
    return covered


def reading_seconds_spent(deck: Project) -> float:
    """How many seconds this deck's readings have taken so far, all rounds together."""
    try:
        said = json.loads((deck.review_dir / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    try:
        return float(said.get("reading_seconds_total") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def readings_taken(deck: Project) -> int:
    """How many readings this deck has already had.

    The record is rewritten by each reading rather than appended to, so the count has
    to ride in it. It is what bounds the automatic reading: a page is unread again once
    the code that drew it changes, and a revision usually changes several pages, so
    without a count the reading re-arms itself every time the author acts on it.
    Measured on a fifteen-page run: two readings, thirty reader calls, opinions on
    eleven pages, and fifty edits after the first build.
    """
    try:
        said = json.loads((deck.review_dir / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    count = said.get("readings") if isinstance(said, dict) else None
    if isinstance(count, int) and count >= 0:
        return count
    # A record written before the count existed is one reading that happened.
    return 1 if isinstance(said, dict) and said.get("pages_read") is not None else 0


def _charge(deck: Project, seconds: float) -> None:
    """Add a round's seconds to the deck's reading total without marking any page read.

    A quick total failure (every reply unparsable) costs a few seconds and changes
    nothing; a round that hit its whole budget and read nothing costs the budget, which
    is what keeps the next such round from being taken for free.
    """
    if seconds <= 0:
        return
    try:
        deck.review_dir.mkdir(parents=True, exist_ok=True)
        path = deck.review_dir / RECORD_FILE
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = {"schema": "raven_ppt.review.v1", "readings": 0}
        held["reading_seconds_total"] = round(float(held.get("reading_seconds_total") or 0.0) + seconds, 1)
        path.write_text(json.dumps(held, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _record(deck: Project, payload: dict[str, Any]) -> None:
    """Leave the list on disk, and never fail the call over it.

    A review that ran is worth its reply whether or not the record could be written,
    so an unwritable review directory costs the note and not the reading.
    """
    try:
        deck.review_dir.mkdir(parents=True, exist_ok=True)
        (deck.review_dir / RECORD_FILE).write_text(
            json.dumps(
                {
                    "schema": "raven_ppt.review.v1",
                    "readings": readings_taken(deck) + 1,
                    "reading_seconds_total": round(
                        reading_seconds_spent(deck) + float(payload.get("reading_seconds") or 0.0), 1
                    ),
                    **payload,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _said(number: int, plan: Any, ruled_out: tuple[str, ...] = ()) -> str:
    """What the reviewer is told about the page besides the picture.

    And what the user ruled out, when the brief records any: a reader that does not
    know the user asked for the template's own illustrations to stay asked, on three
    readings of one live deck, for the template's illustrations to go -- seven entries
    the author could only leave, and every one of them cost a page of the reply.

    Only what the picture cannot show. It is a model that sees, and handing it what is
    already in front of it dilutes the one thing it is being asked to do -- a version of
    this pasted the table's header and first row in, which are legible in the render.
    Whether the page was cloned and what it was planned to argue are not in the render at
    any resolution, which is the whole test for what belongs here.

    The plan and nothing else of the author's. Handing over the code that drew it
    would give the reviewer the author's own reasons for the layout, which is
    exactly the context this call exists to withhold.

    Except which of the two kinds of page it is, and that one is load-bearing: on a
    page cloned from the template the author fills text into the template's shapes
    and cannot add an icon or restyle a badge. Without it, every cloned page in an
    18-page deck came back asking for icons the page had no way to carry -- 7
    findings, every one of them on a cloned page. The plan already carries it, in
    the prototype the author named.
    """
    said = [f"Page {number}."]
    # Said to every reader, whatever the user ruled out: keeping the template's style
    # is about its layouts and colours, not about its stock illustrations, which are
    # placeholders. A live run kept a whiteboard-meeting illustration on eight of
    # fifteen pages of an elderly-care deck and dismissed every entry about it as
    # "the template's own".
    said.append(
        "The template's own illustrations and stock pictures are placeholders. One that does not depict "
        "what this page is about is a `figure` problem, however well it matches the template's style."
    )
    if ruled_out:
        said.append(
            "The user ruled these out for the whole deck, so a fix that needs one of them is not a fix: "
            + "; ".join(ruled_out)
            + "."
        )
    if plan is None:
        said.append("No plan was recorded for this page; review the picture alone.")
        return "\n".join(said)
    said.append(
        "This page was cloned from the template's own structure: its badges, marks and their "
        "styling are the template's."
        if getattr(plan, "prototype", None)
        else "This page was composed from scratch: every mark and every measurement on it is the author's own."
    )
    said.append(f"Its planned claim: {plan.claim}")
    if plan.carries:
        said.append(f"What it was planned to carry: {plan.carries}")
    return "\n".join(said)


def _headline_on_page(headline: str, text: str) -> bool:
    """Whether enough of the headline the reader quoted is printed on the page.

    True when the reader gave no headline, or the page has no readable text: the
    check refuses a reading only on evidence, never on absence of it.
    """
    tokens = [token for token in re.findall(r"[\w\u4e00-\u9fff]+", headline) if len(token) > 1]
    if not tokens or not text.strip():
        return True
    flat = re.sub(r"\s+", "", text).lower()
    hits = sum(1 for token in tokens if token.lower() in flat)
    return hits / len(tokens) >= HEADLINE_MATCH


def _vetted(problems: Any) -> list[dict[str, str]]:
    """The entries that have the three fields a reader can act on.

    An entry missing `where` is a sentence about the deck rather than a problem on a
    page, and the point of the list is that each line can be found and fixed.
    """
    kept: list[dict[str, str]] = []
    for entry in problems if isinstance(problems, list) else ():
        if not isinstance(entry, dict):
            continue
        where, what = str(entry.get("where") or "").strip(), str(entry.get("what") or "").strip()
        if not where or not what:
            continue
        kind = str(entry.get("kind") or "").strip()
        kept.append(
            {
                "kind": kind if kind in _KINDS else "other",
                "where": where,
                "what": what,
                "fix": str(entry.get("fix") or "").strip(),
            }
        )
    return kept


def _asks(
    found: dict[int, list[dict[str, str]]], clean: list[int], unread: list[int], rest: list[int] | None = None
) -> list[str]:
    """What to do with the list, said in the reply rather than only in the payload."""
    if not found and not clean and unread:
        # Every reply came back empty or unparsable. `kinds[0]` below indexed an empty
        # list here, and the raise was swallowed by the caller *after* the record had
        # been written -- which is the automatic reading disabled for the rest of the
        # deck's life by one transient failure. Nothing is recorded on this path now.
        return [
            f"the second reader returned nothing readable for any of page(s) {unread}, so this deck has "
            "not been read yet. Nothing is recorded, so the next delivered build reads it again; "
            "ppt_review(pages=...) also runs it now"
        ]
    if not found and not unread:
        return [
            f"a second reader looked at {len(clean)} page(s) and found nothing to fix. "
            "Nothing here asks you to change anything"
        ]
    counted = sum(len(entries) for entries in found.values())
    kinds = sorted({entry["kind"] for entries in found.values() for entry in entries})
    asks = [
        f"{counted} problem(s) on {len(found)} page(s), by kind: {', '.join(kinds)}. These are one reader's "
        "reading of the pictures, not measurements: look at the page each one names before you act on it, "
        "and leave alone what you look at and disagree with. Answer the rest by editing build.py and "
        "building again",
        # The kinds are printed above because they say what the round is about. They are
        # also how the list gets dismissed: one sentence about a kind answers every page
        # carrying it without opening any of them. A live run replied "most of these are
        # whitespace on the table pages and template decoration, keeping them", made two
        # edits and published -- against eighteen pages of entries.
        f"take them one page at a time. Each entry names a page, and a verdict on a kind -- "
        f"'most of these are {kinds[0]}, keeping them' -- answers every page carrying it without opening "
        "one. If you leave an entry, open its page first and say what you saw there, by page number",
    ]
    if rest:
        asks.append(
            f"the pages carrying the most are rendered below with their entries beside them. Page(s) "
            f"{rest} carry entries too and are not pictured here: ppt_review(pages={rest[:3]}) reads "
            "those again with their renders, and ppt_build(slides=...) shows them without re-reading"
        )
    kinds_found = {entry["kind"] for entries in found.values() for entry in entries}
    if "oversized_shape" in kinds_found:
        asks.append(
            "an oversized_shape is answered by measuring: card_size for a card and the row drawn at max() of "
            "them, text_size for a band of copy, picture_size for a figure, table_size for a table -- take "
            "that much of the region and leave the rest to the next band. Copy added into a box sized for the "
            "old copy comes back as type under the floor, which is the same page failing the other way, so "
            "measure again after adding"
        )
    if "underfilled_page" in kinds_found:
        asks.append(
            "an underfilled_page is not answered by shrinking anything -- the shapes already fit, and taking "
            "a cover off room leaves the room. Either the page gets more that it needs said (the units, the "
            "year, the figure behind the number, an icon on each card, fewer and wider cards, a point moved "
            "here from a page carrying one too many), or this page and its neighbour become one page. A page "
            "with nothing more to say about its claim should not be a page of its own, and one page fewer is "
            "a real answer rather than a last resort"
        )
    if unread:
        asks.append(f"page(s) {unread} could not be read, so nothing here says whether they are right")
    return asks
