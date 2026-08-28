"""A second reader looking at the built pages and listing what is wrong.

The reply is a model's, so what can be tested here is everything around it: which
deck gets rendered, what the reviewer is and is not told, which entries survive,
and what the author is asked to do with the list. The one case worth its own test
is a page the reviewer could not read, because the tempting shape -- an empty list
-- makes it indistinguishable from a page that came back clean.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from pathlib import Path
from unittest import mock

import pytest

from raven.ppt.contracts import Project
from raven.ppt.tools.review import MAX_PAGES, READERS, PptReviewTool

pytest.importorskip("pptx")


class Views:
    """Renders without LibreOffice: one file per page, and a stable data URI."""

    def __init__(self, pages: int) -> None:
        self.count = pages
        self.asked: list[list[int] | None] = []
        self.rendered: Path | None = None

    async def pages(self, pptx: Path, out_dir: Path, numbers=None):
        self.rendered = pptx
        self.asked.append(list(numbers) if numbers else None)
        out_dir.mkdir(parents=True, exist_ok=True)
        wanted = list(numbers) if numbers else range(1, self.count + 1)
        made = {}
        for number in wanted:
            if 1 <= number <= self.count:
                path = out_dir / f"page-{number:03d}.png"
                path.write_bytes(b"\x89PNG")
                made[number] = path
        return made

    def data_uri(self, png: Path, budget: int | None = None) -> str:
        return f"data:image/png;base64,{png.stem}"


class Composer:
    """Answers each page from a script, and records what it was shown."""

    def __init__(self, replies: dict[int, str] | str) -> None:
        self.replies = replies
        self.systems: list[str] = []
        self.shown: list[str] = []
        self.spent = {"input": 0, "output": 0}
        self.live = 0
        self.most_at_once = 0

    async def ask(self, system: str, parts, *, max_tokens: int) -> str:
        self.live += 1
        self.most_at_once = max(self.most_at_once, self.live)
        await asyncio.sleep(0)
        self.live -= 1
        self.systems.append(system)
        said = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict))
        self.shown.append(said)
        if isinstance(self.replies, str):
            return self.replies
        number = int(said.split("Page ")[1].split(".")[0])
        return self.replies.get(number, '{"problems": []}')


def _deck(tmp_path: Path, *, pages: int = 3, outline: dict | None = None) -> Project:
    deck = Project(workspace=tmp_path, slug="ws")
    deck.build_dir.mkdir(parents=True, exist_ok=True)
    (deck.build_dir / "deck.pptx").write_bytes(b"PK\x03\x04")
    deck.state_dir.mkdir(parents=True, exist_ok=True)
    (deck.state_dir / "brief.json").write_text(
        json.dumps({"language": "Chinese", "audience": "a board", "length": pages, "purpose": "to decide"}),
        encoding="utf-8",
    )
    if outline is not None:
        (deck.state_dir / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    return deck


def _payload(reply) -> dict:
    """The JSON body, whether the reply carried pictures with it or not."""
    said = reply if isinstance(reply, str) else reply.model_text
    return json.loads(said[said.index("{") : said.rindex("}") + 1])


def _pictured(reply) -> list[int]:
    """The page numbers whose renders came back beside their entries."""
    if isinstance(reply, str):
        return []
    shown = []
    for part in reply.blocks or ():
        text = part.get("text") if isinstance(part, dict) else None
        if text and text.startswith("Page "):
            shown.append(int(text.split("Page ")[1].split(",")[0]))
    return shown


_ONE = ('{"reads": "plain, and short of its region", "problems": [{"kind": "oversized_shape", '
        '"where": "the lower third", "what": "a panel twice its copy", "fix": "measure it"}]}')


@pytest.mark.asyncio
async def test_a_deck_longer_than_the_cap_is_covered_by_its_second_round(tmp_path) -> None:
    """The real tool, twice, on a deck longer than one round can hold.

    `pages_read` used to hold the round that had just run. The reader puts pages it
    has not covered first and then fills the cap with pages it has, so round two on a
    40-page deck read 31-40 and 1-20 and recorded exactly that -- round three went
    back for 21-30, and `_first_reading` never saw a record covering the deck. Driven
    through `PptReviewTool` rather than a stand-in, because a fake that writes a fixed
    record cannot show this at all.
    """
    import json as _json

    from raven.ppt.tools.review import MAX_PAGES, RECORD_FILE

    pages = MAX_PAGES + 10
    deck = _deck(tmp_path, pages=pages)
    composer = Composer(_ONE)
    tool = PptReviewTool(tmp_path, Views(pages), composer=composer)

    await tool.execute(project="ws")
    first = _json.loads((deck.review_dir / RECORD_FILE).read_text())["pages_read"]
    assert sorted(int(page) for page in first) == list(range(1, MAX_PAGES + 1)), first

    await tool.execute(project="ws")
    second = _json.loads((deck.review_dir / RECORD_FILE).read_text())["pages_read"]

    # Every page, once: the union of the two rounds and not the second one alone.
    assert sorted(int(page) for page in second) == list(range(1, pages + 1)), second


@pytest.mark.asyncio
async def test_a_record_written_before_it_kept_versions_still_reads(tmp_path) -> None:
    """A resumed job's `review.json` can be a plain list of page numbers.

    Those pages count as read at a version nobody wrote down, which is the whole of
    what the old field said about them: a resumed job neither fails on the record nor
    pays to read a deck that has already been read.
    """
    from raven.ppt.tools.review import RECORD_FILE, _already_read

    deck = _deck(tmp_path, pages=3)
    deck.review_dir.mkdir(parents=True, exist_ok=True)
    (deck.review_dir / RECORD_FILE).write_text(
        json.dumps({"schema": "raven.ppt.review.v1", "pages_read": [1, 3]}), encoding="utf-8"
    )

    assert _already_read(deck) == {1, 3}

    tool = PptReviewTool(tmp_path, Views(3), composer=Composer(_ONE))
    await tool.execute(project="ws")
    written = json.loads((deck.review_dir / RECORD_FILE).read_text(encoding="utf-8"))["pages_read"]

    assert written == {"1": "", "2": "", "3": ""}, "kept, and marked at a version nobody wrote down"


@pytest.mark.asyncio
async def test_a_page_read_before_it_had_a_version_is_unread_once_it_has_one(tmp_path) -> None:
    """An early reading of an unmapped draft page does not cover the mapped page.

    A draft is exempt from `page_mapping`, so a reading can be taken while the build
    has no fingerprint for a page; the record marks it "". The mark held while that was
    still true, but it also held once the mapping was fixed and a real -- possibly
    rewritten -- fingerprint appeared, so the delivered build skipped the reading it had
    promised for the version that was going to ship.
    """
    from raven.ppt.tools.review import RECORD_FILE, _already_read, regress

    deck = _deck(tmp_path, pages=2)
    deck.review_dir.mkdir(parents=True, exist_ok=True)
    (deck.review_dir / RECORD_FILE).write_text(
        json.dumps({"schema": "raven.ppt.review.v1", "pages_read": {"1": "", "2": "drawn-by"}}),
        encoding="utf-8",
    )

    with mock.patch.object(regress, "code_by_page", return_value={}):
        assert _already_read(deck) == {1, 2}, "no version to disagree with, so nothing says either changed"

    with mock.patch.object(regress, "code_by_page", return_value={1: "now-known", 2: "drawn-by"}):
        # Page 2 was read at the version it still carries; page 1 was read at no version
        # and now has one, which is the transition that used to ship unread.
        assert _already_read(deck) == {2}

    with mock.patch.object(regress, "code_by_page", return_value={1: "now-known", 2: "rewritten"}):
        assert _already_read(deck) == set()


@pytest.mark.asyncio
async def test_the_old_list_field_is_not_the_same_mark_as_a_version_nobody_wrote(tmp_path) -> None:
    """A resumed job's plain list keeps counting as read once versions appear.

    The two marks used to share the empty string, so separating them had to leave this
    one alone: the old field made no claim about any version, and a fingerprint turning
    up later is not news about a page it never described.
    """
    from raven.ppt.tools.review import RECORD_FILE, _already_read, regress

    deck = _deck(tmp_path, pages=2)
    deck.review_dir.mkdir(parents=True, exist_ok=True)
    (deck.review_dir / RECORD_FILE).write_text(
        json.dumps({"schema": "raven.ppt.review.v1", "pages_read": [1, 2]}), encoding="utf-8"
    )

    with mock.patch.object(regress, "code_by_page", return_value={1: "now-known", 2: "also-known"}):
        assert _already_read(deck) == {1, 2}


@pytest.mark.asyncio
async def test_a_partly_reviewed_legacy_record_keeps_the_pages_it_did_not_cover(tmp_path) -> None:
    """A round that covers one page of an old list does not re-label the other two.

    `_marks` reads the old field as `None`, but the record it writes back has to keep
    that mark for pages the round never touched. Writing them as `""` says a reading was
    taken at an unknown version, and those pages have known fingerprints -- so the next
    build launched the reader on pages nobody had changed, which is the cost the
    old-record compatibility exists to avoid.
    """
    from raven.ppt.tools.review import RECORD_FILE, _already_read, _marked, regress

    deck = _deck(tmp_path, pages=3)
    deck.review_dir.mkdir(parents=True, exist_ok=True)
    (deck.review_dir / RECORD_FILE).write_text(
        json.dumps({"schema": "raven.ppt.review.v1", "pages_read": [1, 2, 3]}), encoding="utf-8"
    )

    with mock.patch.object(regress, "code_by_page", return_value={1: "one", 2: "two", 3: "three"}):
        written = _marked(deck, [1])
        assert written == {"1": "one", "2": None, "3": None}, "untouched legacy marks survive the write"

        (deck.review_dir / RECORD_FILE).write_text(
            json.dumps({"schema": "raven.ppt.review.v1", "pages_read": written}), encoding="utf-8"
        )
        assert _already_read(deck) == {1, 2, 3}


@pytest.mark.asyncio
async def test_writing_the_record_never_changes_a_page_the_round_did_not_read(tmp_path) -> None:
    """The property both holes in this logic broke, over every record shape it accepts.

    A round writes back marks for pages it never looked at, so serialization must carry
    each of them unchanged. Twice it did not: one revision dropped unfingerprinted pages
    and re-read whole decks on every build, the next collapsed the old list field's mark
    into the new empty one and re-read pages nobody had touched. Both are the same
    mistake -- a write that quietly restates a mark -- and a case list only catches the
    instance somebody thought of.
    """
    from raven.ppt.tools.review import RECORD_FILE, _already_read, _marked, regress

    records: list[object] = [
        [1, 2, 3],
        {"1": None, "2": None, "3": None},
        {"1": "", "2": "", "3": ""},
        {"1": "a", "2": "b", "3": "c"},
        {"1": None, "2": "", "3": "c"},
    ]
    fingerprints = [{}, {1: "a", 2: "b", 3: "c"}, {1: "a", 3: "c"}]

    deck = _deck(tmp_path, pages=3)
    deck.review_dir.mkdir(parents=True, exist_ok=True)
    record = deck.review_dir / RECORD_FILE

    for held in records:
        for now in fingerprints:
            for read in ([], [1], [2], [1, 2], [1, 2, 3]):
                record.write_text(json.dumps({"pages_read": held}), encoding="utf-8")
                with mock.patch.object(regress, "code_by_page", return_value=now):
                    before = _already_read(deck)
                    written = _marked(deck, read)
                    record.write_text(json.dumps({"pages_read": written}), encoding="utf-8")
                    after = _already_read(deck)

                untouched = {1, 2, 3} - set(read)
                assert {page for page in untouched if page in before} == {
                    page for page in untouched if page in after
                }, f"held={held} now={now} read={read} wrote={written}"


@pytest.mark.asyncio
async def test_every_page_is_read_on_its_own_context(tmp_path) -> None:
    """One request per page, and each is given that page alone.

    The whole point of the call: a reviewer holding twenty pages reports the three
    loudest, and a void is a fact about one page.
    """
    deck = _deck(tmp_path, pages=3)
    composer = Composer(_ONE)
    tool = PptReviewTool(tmp_path, Views(3), composer=composer)

    reply = await tool.execute(project="ws")
    payload = _payload(reply)

    assert len(composer.shown) == 3
    assert [said.splitlines()[0] for said in composer.shown] == ["Page 1.", "Page 2.", "Page 3."]
    assert payload["pages_reviewed"] == 3
    assert payload["pages_with_something_to_fix"] == 3


@pytest.mark.asyncio
async def test_the_reviewer_is_told_the_plan_and_not_the_program(tmp_path) -> None:
    """The plan says what the page was for; the code says why it looks as it does.

    Handing over the program would give the reviewer the author's own reasons for
    the layout, which is exactly the context this call exists to withhold.
    """
    deck = _deck(
        tmp_path,
        pages=1,
        outline={
            "schema": "raven.ppt.outline.v1",
            "takeaway": "the memory is the product",
            "pages": [
                {
                    "page": 1,
                    "claim": "memory is the product",
                    "carries": "three cards",
                    "says": ["one", "two"],
                    "figures": [],
                    "section": "the case",
                    "prototype": 4,
                }
            ],
        },
    )
    (deck.build_dir / "build.py").write_text("# the whole program\nslide = deck.add_slide()\n", encoding="utf-8")
    composer = Composer('{"problems": []}')
    tool = PptReviewTool(tmp_path, Views(1), composer=composer)

    await tool.execute(project="ws")
    said = composer.shown[0]

    assert "memory is the product" in said
    assert "three cards" in said
    assert "add_slide" not in said and "build.py" not in said


@pytest.mark.asyncio
async def test_a_cloned_page_is_named_as_one(tmp_path) -> None:
    """The one thing about the author's side the reviewer needs. On a page cloned from
    the template the author fills text into the template's shapes and cannot add an
    icon or restyle a badge -- and without being told, every cloned page in an 18-page
    deck came back asking for icons it had no way to carry: 7 findings, all of them on
    cloned pages. The plan already carries it, in the prototype the author named.
    """
    _deck(
        tmp_path,
        pages=2,
        outline={
            "schema": "raven.ppt.outline.v1",
            "takeaway": "t",
            "pages": [
                {"page": 1, "claim": "a", "carries": "", "says": [], "figures": [], "section": "s", "prototype": 4},
                {"page": 2, "claim": "b", "carries": "", "says": [], "figures": [], "section": "s", "prototype": None},
            ],
        },
    )
    composer = Composer('{"problems": []}')
    tool = PptReviewTool(tmp_path, Views(2), composer=composer)

    await tool.execute(project="ws")
    cloned, composed = sorted(composer.shown)

    assert "cloned from the template" in cloned and "the template's" in cloned
    assert "composed from scratch" in composed
    # And the requirements have to act on it, or the line is decoration.
    assert "cloned" in composer.systems[0], "the requirements have to turn on it"


@pytest.mark.asyncio
async def test_a_page_with_no_plan_is_reviewed_on_the_picture_alone(tmp_path) -> None:
    """A deck whose outline was lost is still worth a reading, and the reviewer has
    to be told which of the two situations it is in -- otherwise the missing claim
    reads as a page that was planned to say nothing."""
    _deck(tmp_path, pages=1)
    composer = Composer('{"problems": []}')
    tool = PptReviewTool(tmp_path, Views(1), composer=composer)

    await tool.execute(project="ws")

    assert "No plan was recorded" in composer.shown[0]


@pytest.mark.asyncio
async def test_a_page_the_reviewer_could_not_read_is_named_rather_than_called_clean(tmp_path) -> None:
    """The failure mode this shape invites: a reply that would not parse becomes an
    empty problem list, and an empty list is what a good page returns. Merging the
    two would report a deck as reviewed that was never looked at."""
    _deck(tmp_path, pages=3)
    composer = Composer({1: _ONE, 2: "the gateway said 503", 3: '{"problems": []}'})
    tool = PptReviewTool(tmp_path, Views(3), composer=composer)

    payload = _payload(await tool.execute(project="ws"))

    assert payload["could_not_be_read"] == [2]
    assert payload["nothing_found_on"] == [3]
    assert list(payload["problems"]) == ["1"]
    assert payload["pages_reviewed"] == 2
    assert "could not be read" in json.dumps(payload)


@pytest.mark.asyncio
async def test_an_entry_nobody_could_find_is_dropped(tmp_path) -> None:
    """`where` is what makes a line actionable. An entry without one is a sentence
    about the deck, and the list's whole value is that each line can be found."""
    _deck(tmp_path, pages=1)
    composer = Composer(
        json.dumps(
            {
                "problems": [
                    {"kind": "type", "what": "the deck feels corporate", "fix": "be bolder"},
                    {"kind": "underfilled_page", "where": "  ", "what": "blank", "fix": "fill it"},
                    {"kind": "made_up", "where": "page 1", "what": "something", "fix": "change it"},
                    "a bare string",
                ]
            }
        )
    )
    tool = PptReviewTool(tmp_path, Views(1), composer=composer)

    payload = _payload(await tool.execute(project="ws"))

    kept = payload["problems"]["1"]
    assert len(kept) == 1
    # The kind was not one this tool names, so it is recorded as other rather than
    # passed through -- a made-up kind in the counts reads as a new class of defect.
    assert kept[0]["kind"] == "other"


@pytest.mark.asyncio
async def test_a_clean_deck_asks_for_nothing(tmp_path) -> None:
    """The reply after a review that found nothing has to say so in one line. The
    predecessor of this route ended every reply with an instruction, and a model
    told to act on an empty list rebuilt the same finished deck eight times."""
    _deck(tmp_path, pages=2)
    tool = PptReviewTool(tmp_path, Views(2), composer=Composer('{"problems": []}'))

    payload = _payload(await tool.execute(project="ws"))

    assert payload["pages_with_something_to_fix"] == 0
    assert payload["nothing_found_on"] == [1, 2]
    assert "found nothing to fix" in payload["next_step"]
    assert "editing build.py" not in payload["next_step"]


@pytest.mark.asyncio
async def test_the_two_kinds_of_blank_region_are_answered_differently(tmp_path) -> None:
    """The whole reason they are two kinds. A round of repair answered every void by
    shrinking the shape over it: that is right for the first and, on a page that was
    really the second, removes a cover and leaves the room -- measured as the second
    count not moving at all across a full before/after (21 to 21).
    """
    _deck(tmp_path, pages=2)
    tool = PptReviewTool(
        tmp_path,
        Views(2),
        composer=Composer(
            {
                1: _ONE,
                2: '{"problems": [{"kind": "underfilled_page", "where": "the lower 40%", '
                '"what": "nothing left to put there", "fix": "merge with the next page"}]}',
            }
        ),
    )

    said = _payload(await tool.execute(project="ws"))["next_step"]

    # The measuring answer, for the shape.
    assert "card_size" in said and "type under the floor" in said
    # And for the page, the one answer a repair round never takes on its own.
    assert "become one page" in said
    assert "not answered by shrinking anything" in said


@pytest.mark.asyncio
async def test_a_page_short_of_content_is_not_told_to_shrink_something(tmp_path) -> None:
    """Only the answer that fits: a deck whose voids are all pages-too-big must not be
    handed the measuring advice, or the round is spent on shapes that already fit."""
    _deck(tmp_path, pages=1)
    tool = PptReviewTool(
        tmp_path,
        Views(1),
        composer=Composer(
            '{"problems": [{"kind": "underfilled_page", "where": "the lower third", '
            '"what": "the page is short", "fix": "one page fewer"}]}'
        ),
    )

    said = _payload(await tool.execute(project="ws"))["next_step"]

    assert "become one page" in said
    assert "card_size" not in said


@pytest.mark.asyncio
async def test_only_the_named_pages_are_rendered(tmp_path) -> None:
    """Naming pages is the call after an edit, and rendering the other seventeen to
    review three is the cost this tool is most likely to be avoided over."""
    _deck(tmp_path, pages=18)
    views = Views(18)
    tool = PptReviewTool(tmp_path, views, composer=Composer('{"problems": []}'))

    payload = _payload(await tool.execute(project="ws", pages=[4, 5]))

    assert views.asked == [[4, 5]]
    assert payload["pages_reviewed"] == 2


@pytest.mark.asyncio
async def test_a_deck_longer_than_one_call_says_which_pages_it_left(tmp_path) -> None:
    """Silent truncation reads as "the whole deck was reviewed"."""
    _deck(tmp_path, pages=MAX_PAGES + 2)
    tool = PptReviewTool(tmp_path, Views(MAX_PAGES + 2), composer=Composer('{"problems": []}'))

    payload = _payload(await tool.execute(project="ws"))

    assert payload["pages_reviewed"] == MAX_PAGES
    assert payload["pages_not_reviewed"] == [MAX_PAGES + 1, MAX_PAGES + 2]


@pytest.mark.asyncio
async def test_the_built_deck_is_what_gets_reviewed(tmp_path) -> None:
    """Not the delivered copy. A run that has just edited its program and rebuilt has
    a newer deck in build/ than in out/, and reviewing the export would report on the
    round before this one."""
    deck = _deck(tmp_path, pages=1)
    deck.exports_dir.mkdir(parents=True, exist_ok=True)
    (deck.exports_dir / "deck.pptx").write_bytes(b"PK\x03\x04older")
    views = Views(1)
    tool = PptReviewTool(tmp_path, views, composer=Composer('{"problems": []}'))

    await tool.execute(project="ws")

    assert views.rendered == deck.build_dir / "deck.pptx"


@pytest.mark.asyncio
async def test_a_deck_that_was_never_built_is_refused_with_the_call_that_builds_it(tmp_path) -> None:
    Project(workspace=tmp_path, slug="ws").state_dir.mkdir(parents=True, exist_ok=True)
    tool = PptReviewTool(tmp_path, Views(0), composer=Composer('{"problems": []}'))

    payload = _payload(await tool.execute(project="ws"))

    assert payload["ok"] is False
    assert "ppt_build" in json.dumps(payload)


@pytest.mark.asyncio
async def test_without_a_model_the_call_says_so_instead_of_reviewing_nothing(tmp_path) -> None:
    """A build with no provider registers this tool like any other, and a reviewer
    that answered "no problems" there would be a clean bill of health from nobody."""
    _deck(tmp_path, pages=1)
    tool = PptReviewTool(tmp_path, Views(1), composer=None)

    payload = _payload(await tool.execute(project="ws"))

    assert payload["ok"] is False
    assert "no model configured" in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_deck_no_render_can_be_taken_of_says_so(tmp_path) -> None:
    """There is nothing to review without pictures, and answering "no problems" on a
    host without LibreOffice is the one wrong reply."""
    _deck(tmp_path, pages=0)
    tool = PptReviewTool(tmp_path, Views(0), composer=Composer('{"problems": []}'))

    payload = _payload(await tool.execute(project="ws"))

    assert payload["ok"] is False
    assert "could not be rendered" in json.dumps(payload)


@pytest.mark.asyncio
async def test_the_requirements_document_is_what_the_reviewer_is_given(tmp_path) -> None:
    """One document, read twice: the author writes against it and this judges by it.

    A requirement held against a page that the author never saw is a requirement nobody
    agreed to, so the two sides cannot have separate copies -- and the reviewer being
    handed the file rather than a paraphrase of it is what makes that true.
    """
    from raven.ppt.tools.review import REQUIREMENTS_NAME, requirements

    _deck(tmp_path, pages=1)
    composer = Composer('{"problems": []}')
    tool = PptReviewTool(tmp_path, Views(1), composer=composer)

    await tool.execute(project="ws")
    said = composer.systems[0]

    assert "in Chinese" in said, "the list has to come back in the language of the pages"
    wanted = requirements()
    assert wanted and wanted in said, f"{REQUIREMENTS_NAME} did not reach the reviewer verbatim"
    # And it is the same file the author is handed in its build directory.
    from raven.ppt.services.assets.script_helpers import REFERENCE_DIRNAME, reference_files

    assert reference_files()[f"{REFERENCE_DIRNAME}/{REQUIREMENTS_NAME}"] == wanted
    # The two-kind distinction is the one this call exists to make, so the reply
    # contract has to carry it as two kinds: one round of repair answered every void
    # by shrinking a shape, which is the answer to only one of the two, and a single
    # kind with the distinction in prose cannot show that the other never moved.
    for kind in ("oversized_shape", "underfilled_page"):
        assert kind in said and kind in wanted, f"{kind} is not in both the contract and the document"
    # And the brief leaves the seeing to the reviewer rather than listing what ugly
    # looks like: a vision model can see crowding and small type without being told.
    assert "Judge it yourself" in said, "the seeing is left to the reviewer"
    assert "not how far" in said, "precise displacements are not what this asks for"


@pytest.mark.asyncio
async def test_the_deck_is_not_read_all_at_once(tmp_path) -> None:
    """The renders next door are on a leash and this side had none, so a whole-deck
    review opened as many concurrent streaming requests as the deck had pages --
    each carrying an image, against one endpoint whose rate limit this code cannot
    see.
    """
    _deck(tmp_path, pages=18)
    composer = Composer('{"problems": []}')
    tool = PptReviewTool(tmp_path, Views(18), composer=composer)

    await tool.execute(project="ws")

    assert composer.most_at_once <= READERS, f"{composer.most_at_once} requests were in flight at once"
    assert len(composer.systems) == 18, "and every page was still read"


@pytest.mark.asyncio
async def test_the_list_outlives_the_call(tmp_path) -> None:
    """The reply reaches the author's context and nothing else. A 19-page review that
    took seven and a half minutes could not be read back an hour later, because the
    transcript truncates a tool result and the tool wrote nothing -- so there was no
    way to tell what the next round had actually fixed.
    """
    from raven.ppt.tools.review import RECORD_FILE

    deck = _deck(tmp_path, pages=2)
    tool = PptReviewTool(tmp_path, Views(2), composer=Composer({1: _ONE, 2: '{"problems": []}'}))

    await tool.execute(project="ws")
    written = json.loads((deck.review_dir / RECORD_FILE).read_text(encoding="utf-8"))

    assert written["schema"] == "raven.ppt.review.v1"
    assert written["problems"]["1"][0]["kind"] == "oversized_shape"
    assert written["nothing_found_on"] == [2]


@pytest.mark.asyncio
async def test_a_review_directory_that_cannot_be_written_still_reviews(tmp_path, monkeypatch) -> None:
    """The reading is the point and the note is a convenience."""
    _deck(tmp_path, pages=1)
    tool = PptReviewTool(tmp_path, Views(1), composer=Composer(_ONE))

    def refuse(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(pathlib.Path, "write_text", refuse)
    payload = _payload(await tool.execute(project="ws"))

    assert payload["pages_with_something_to_fix"] == 1


@pytest.mark.asyncio
async def test_the_list_comes_back_with_the_pages_it_is_about(tmp_path) -> None:
    """The whole difference between a list that gets read and one that gets dismissed.

    The reply was text only: 27 entries about 18 pages, arriving in a context where
    compaction keeps one image-bearing message, so the author was told to "look at the
    page each one names" with nothing to look at. It answered "most of these are
    whitespace on the table pages and template decoration, keeping them", made two
    edits, and published a deck the reviewer had found something on in every page but
    one.
    """
    _deck(tmp_path, pages=3)
    two = '{"problems": [{"kind": "underfilled_page", "where": "the lower third", "what": "empty", "fix": "say more"}, {"kind": "alignment", "where": "the two columns", "what": "not level", "fix": "level them"}]}'
    tool = PptReviewTool(
        tmp_path, Views(3), composer=Composer({1: '{"problems": []}', 2: _ONE, 3: two})
    )

    reply = await tool.execute(project="ws")

    pictured = _pictured(reply)
    assert 3 in pictured and 2 in pictured, "the pages with entries come back with their renders"
    assert 1 not in pictured, "and a page with nothing found is not spent on an image"
    # Each picture is preceded by that page's own entries, so the two are read together.
    said = [p["text"] for p in reply.blocks if isinstance(p, dict) and p.get("text")]
    for_page_three = next(text for text in said if text.startswith("Page 3,"))
    assert "underfilled_page" in for_page_three and "alignment" in for_page_three


@pytest.mark.asyncio
async def test_a_deck_with_more_bad_pages_than_fit_says_which_are_not_pictured(tmp_path) -> None:
    """Silently showing three of eighteen reads as "these are the problems"."""
    from raven.ppt.tools.review import BATCH_VIEWS

    _deck(tmp_path, pages=BATCH_VIEWS + 3)
    tool = PptReviewTool(tmp_path, Views(BATCH_VIEWS + 3), composer=Composer(_ONE))

    reply = await tool.execute(project="ws")
    payload = _payload(reply)

    assert len(_pictured(reply)) == BATCH_VIEWS
    assert payload["carrying_more_than_shown"], "the rest are named"
    assert "not pictured here" in payload["next_step"]
    assert "ppt_review(pages=" in payload["next_step"]


@pytest.mark.asyncio
async def test_every_page_gets_a_verdict_including_a_clean_one(tmp_path) -> None:
    """The list cannot say how a page reads. A page can carry three small entries and
    still be the best in the deck, and a page with an empty list can be plain -- so
    "nothing to fix" and "nothing to it" have to be different answers.
    """
    _deck(tmp_path, pages=2)
    tool = PptReviewTool(
        tmp_path,
        Views(2),
        composer=Composer(
            {
                1: _ONE,
                2: '{"reads": "handsome; one figure carries it", "problems": []}',
            }
        ),
    )

    payload = _payload(await tool.execute(project="ws"))

    assert payload["reads"]["2"].startswith("handsome")
    assert payload["nothing_found_on"] == [2], "and it is still counted as clean"
    assert payload["reads"]["1"], "a page with entries gets one too"


@pytest.mark.asyncio
async def test_the_verdict_rides_with_the_page_it_is_about(tmp_path) -> None:
    """It is read beside the picture or it is a line in a payload nobody opens."""
    _deck(tmp_path, pages=1)
    tool = PptReviewTool(tmp_path, Views(1), composer=Composer(_ONE))

    reply = await tool.execute(project="ws")
    said = [b["text"] for b in reply.blocks if isinstance(b, dict) and b.get("text")]

    assert any("reads as: plain, and short of its region" in text for text in said)
