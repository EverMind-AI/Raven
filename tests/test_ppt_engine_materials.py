"""Source documents in, verified deck out.

These behaviours were the launcher's, each one written against a measured
failure, and they moved with the staging into the plugin's turn hook. Pinned here so the
move cannot quietly lose one.
"""

import zipfile
from pathlib import Path

import pytest

from raven_ppt.plugin import materials


def _pptx(path: Path, slides: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<sld/>")
    return path


# -- reading the task text ---------------------------------------------------


def test_a_fenced_block_names_the_materials():
    text = 'build it\n```raven-ppt\n{"materials": ["/a.md", "/b.md"]}\n```'
    assert materials.inputs_from_prompt(text) == ["/a.md", "/b.md"]


def test_a_declared_template_is_refused_the_way_the_launcher_refuses_it():
    """One channel, both entry points. The launcher raises on this declaration, so
    accepting it here would make the same task text succeed over one transport and
    fail over the other."""
    text = '```raven-ppt\n{"materials": ["/a.md"], "template": "/house.pptx"}\n```'
    with pytest.raises(materials.StagingError) as caught:
        materials.inputs_from_prompt(text)
    assert "ppt_template" in str(caught.value)


def test_a_template_key_that_is_not_a_path_is_left_alone():
    """A style name in an unrelated JSON block is not a path declaration."""
    text = '```json\n{"materials": ["/a.md"], "template": "minimal"}\n```'
    assert materials.inputs_from_prompt(text) == ["/a.md"]


def test_a_plain_json_fence_is_read_too():
    text = '```json\n{"materials": ["/a.md"]}\n```'
    assert materials.inputs_from_prompt(text) == ["/a.md"]


def test_a_fence_that_declares_neither_key_is_not_an_inputs_block():
    """A task may well quote unrelated JSON; only a block carrying one of the two
    keys is a declaration."""
    assert materials.inputs_from_prompt('```json\n{"unrelated": 1}\n```') == []


def test_malformed_json_in_a_fence_is_skipped_not_fatal():
    text = '```raven-ppt\n{not json}\n```\n```raven-ppt\n{"materials": ["/a.md"]}\n```'
    assert materials.inputs_from_prompt(text) == ["/a.md"]


def test_a_declared_block_does_not_filter_by_file_type(tmp_path):
    """The type list guards the prose scan alone: prose names paths that are not
    material, while a declaration is explicit."""
    odd = tmp_path / "notes.xyz"
    odd.write_text("facts", encoding="utf-8")
    declared = materials.inputs_from_prompt('```raven-ppt\n{"materials": ["%s"]}\n```' % odd)
    assert declared == [str(odd)]
    assert materials.materials_from_prompt(f"see {odd}") == []


def test_a_path_followed_by_non_ascii_punctuation_is_still_found(tmp_path):
    """The tail is trimmed a character at a time rather than by stripping ASCII
    full stops: punctuation in any script sits against the path, and a mark that
    is not ASCII stays attached -- so the suffix of the raw match is not ``.md``
    and the whole document used to be dropped without a word."""
    notes = tmp_path / "notes.md"
    notes.write_text("facts", encoding="utf-8")
    assert materials.materials_from_prompt(f"read {notes}\u3002then build") == [str(notes)]


def test_a_path_that_does_not_exist_is_not_material(tmp_path):
    assert materials.materials_from_prompt(f"see {tmp_path / 'ghost.md'}") == []


def test_sources_are_deduplicated_by_real_path(tmp_path):
    """Two names for one file would be staged twice under different names, so the
    prompt would claim material the run does not have."""
    real = tmp_path / "notes.md"
    real.write_text("facts", encoding="utf-8")
    link = tmp_path / "alias.md"
    link.symlink_to(real)
    assert materials.unique_sources([str(real), str(link), str(real)]) == [str(real)]


# -- staging -----------------------------------------------------------------


def test_a_colliding_basename_is_suffixed_not_overwritten(tmp_path):
    """The overwrite loses material exactly as silently as a skipped copy would:
    the copy succeeds, and the prompt lists two entries resolving to one file."""
    one = tmp_path / "a" / "notes.md"
    two = tmp_path / "b" / "notes.md"
    for path, body in ((one, "first"), (two, "second")):
        path.parent.mkdir(parents=True)
        path.write_text(body, encoding="utf-8")
    target = tmp_path / "materials"
    staged = materials.stage(target, [str(one), str(two)], set())
    assert [p.name for _s, p in staged] == ["notes.md", "notes-2.md"]
    assert (target / "notes.md").read_text(encoding="utf-8") == "first"
    assert (target / "notes-2.md").read_text(encoding="utf-8") == "second"


def test_an_unreadable_source_stops_the_run(tmp_path):
    import pytest

    with pytest.raises(materials.StagingError) as exc:
        materials.stage(tmp_path / "materials", [str(tmp_path / "ghost.md")], set())
    assert "ghost.md" in str(exc.value)
    # Actionable rather than a bare errno: the message is what the caller shows.
    assert "absolute path" in str(exc.value)


def test_the_prompt_block_names_the_copy_and_not_the_source(tmp_path):
    """A collision-suffixed copy has a name the source does not, so the listing is
    built from the pairing rather than re-derived."""
    text = materials.describe(
        [("/elsewhere/notes.md", tmp_path / "materials" / "notes-2.md")],
        tmp_path / "materials",
        tmp_path / "out",
    )
    assert "notes.md (from /elsewhere/notes.md) -> " in text
    assert str(tmp_path / "materials" / "notes-2.md") in text
    assert f"Compile the deck under {tmp_path / 'out'}/" in text


def test_a_settled_deck_is_not_told_to_publish_again(tmp_path):
    """The turn after a delivery. Every other turn of a session ends the block with
    "compile the deck", and on the turn where the user says the deck is fine that
    instruction is wrong -- it asks for a build nothing needs. What stands in its
    place states the record and decides nothing: whether this turn is deck work is
    the model's reading of the user's message, not this function's."""
    deck = _pptx(tmp_path / "out" / "intro.pptx")
    text = materials.describe([], tmp_path / "materials", tmp_path / "out", [deck])
    assert f"Compile the deck under {tmp_path / 'out'}/" not in text
    assert str(deck) in text and "stands as published" in text
    assert "another ppt_build that publishes" in text
    assert "the MEDIA line comes from a publish and from nothing else" in text


def test_a_settled_deck_is_not_told_to_go_gathering(tmp_path):
    """The gathering advice is written for a deck with no material, and a deck on the
    publish record has already taken it -- on that turn the text is stale, not just
    redundant. Suppressing it turns on the record, the same fact the standing block
    turns on, and not on any reading of what the turn is for."""
    deck = _pptx(tmp_path / "out" / "intro.pptx")
    text = materials.describe([], tmp_path / "materials", tmp_path / "out", [deck])
    assert "No material staged for this run" not in text
    assert "has to be gathered" not in text
    for tool in ("web_search", "ppt_image_search", "web_fetch", "ppt_fetch"):
        assert tool not in text
    assert str(deck) in text and "stands as published" in text


def test_with_no_deck_standing_both_branches_keep_their_text(tmp_path):
    """The two inputs the fork was measured on, word for word. A deck that has not
    been published yet reads exactly what it read before any of this.

    Asserted as present in the closing block rather than as the last characters of
    it: what this pins is that the sentence still reaches the author, and another
    change may add a sentence of its own after it -- naming a delivery destination,
    say. An `endswith` here would fail on that without the sentence having moved.
    """
    compile_said = (
        f" Compile the deck under {tmp_path / 'out'}/ and end your final reply with the MEDIA line naming it."
    )
    first = materials.describe([], tmp_path / "materials", tmp_path / "out")
    assert "# No material staged for this run" in first and "has to be gathered" in first
    assert compile_said in first

    with_material = materials.describe(
        [("/elsewhere/q3.md", tmp_path / "materials" / "q3.md")], tmp_path / "materials", tmp_path / "out"
    )
    assert "# Material staged for this run" in with_material and "q3.md" in with_material
    assert compile_said in with_material


def test_material_staged_on_a_settled_deck_keeps_its_listing(tmp_path):
    """The two blocks compose: the deck on the record is stated, and the material this
    turn staged is listed as it always was. Neither excludes the other, because the
    listing is a fact about the folder and the standing deck is a fact about the
    record -- and one turn can need both."""
    deck = _pptx(tmp_path / "out" / "intro.pptx")
    text = materials.describe(
        [("/elsewhere/q3.md", tmp_path / "materials" / "q3.md")],
        tmp_path / "materials",
        tmp_path / "out",
        [deck],
    )
    assert "# Material staged for this run" in text and "q3.md" in text
    assert str(deck) in text and "another ppt_build that publishes" in text
    assert f"Compile the deck under {tmp_path / 'out'}/" not in text


def test_only_a_published_deck_counts_as_standing(tmp_path):
    """A copy somebody put under out/ is not a deck this project published, and the
    record rather than the folder is what says so."""
    (tmp_path / "out").mkdir()
    stray = _pptx(tmp_path / "out" / "stray.pptx")
    assert materials.published_decks(tmp_path / "out", set()) == []
    digest = materials._digest(stray)
    assert materials.published_decks(tmp_path / "out", {digest}) == [stray]


def test_a_run_with_nothing_staged_is_told_to_gather_rather_than_refused(tmp_path):
    """The branch this block did not have. A deck author runs with or without
    documents; the launcher stopped refusing and this had to follow."""
    text = materials.describe([], tmp_path / "materials", tmp_path / "out")
    assert "No material staged for this run" in text
    for tool in ("web_search", "web_fetch", "ppt_fetch", "ppt_image_search"):
        assert tool in text
    # The sentence that would forbid everything: it points at an empty directory.
    assert "Use only files under" not in text
    assert f"Compile the deck under {tmp_path / 'out'}/" in text


# -- verifying the deck ------------------------------------------------------


def test_a_file_that_is_not_an_archive_has_no_slides(tmp_path):
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"not a zip")
    assert materials.slide_count(broken) == 0


def test_an_archive_with_no_slide_parts_has_no_slides(tmp_path):
    empty = tmp_path / "empty.pptx"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("docProps/app.xml", "<p/>")
    assert materials.slide_count(empty) == 0


def test_a_missing_file_has_no_slides(tmp_path):
    assert materials.slide_count(tmp_path / "gone.pptx") == 0


def test_the_announcement_chooses_between_this_turns_decks(tmp_path):
    """A build writes intermediates next to the deck, and both names appear in the
    reply -- so the announcement is what picks, not a scan of the whole text."""
    out = tmp_path / "out"
    _pptx(out / "deck.pptx")
    wanted = _pptx(out / "quarterly.pptx", slides=5)
    reply = "I wrote deck.pptx while building.\n\nMEDIA: " + str(wanted)
    assert materials.verified_deck(out, reply, {}) == (wanted, 5)


def test_the_newest_wins_when_the_reply_names_none_of_them(tmp_path):
    import os
    import time

    out = tmp_path / "out"
    old = _pptx(out / "old.pptx")
    new = _pptx(out / "new.pptx", slides=4)
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    assert materials.verified_deck(out, "all done", {}) == (new, 4)


def test_a_deck_from_an_earlier_turn_is_not_a_candidate(tmp_path):
    """One session's job directory accumulates every turn's decks, so a turn that
    published nothing must not fall through to the newest and hand back an earlier
    turn's file as its own result."""
    out = tmp_path / "out"
    earlier = _pptx(out / "earlier.pptx")
    before = materials.deck_mtimes(out)
    assert materials.verified_deck(out, f"MEDIA: {earlier}", before) == (None, 0)


def test_an_empty_directory_publishes_nothing(tmp_path):
    assert materials.verified_deck(tmp_path / "out", "MEDIA: /nowhere.pptx", {}) == (None, 0)


# -- delivery ----------------------------------------------------------------


def test_two_paths_in_one_match_are_both_material(tmp_path):
    """A delimiter the pattern does not end on joins two names into one match.

    The pattern ends a path on ASCII punctuation, so ``a.md, b.md`` arrives as two
    matches and the same sentence written with an ideographic comma arrives as one.
    Taking the first file in it as the answer for the whole match dropped the second
    document without a word. Widening the pattern is the other way to fix this and is
    worse: every mark added to it is a mark a filename may not contain.
    """
    first, second = tmp_path / "one.md", tmp_path / "two.md"
    for path in (first, second):
        path.write_text("facts", encoding="utf-8")

    for joiner in ("\u3001", "\uff09\u548c\uff08", "\u300d\u548c\u300c", "\u060c", "\u0964"):
        assert materials.materials_from_prompt(f"{first}{joiner}{second}") == [
            str(first),
            str(second),
        ], joiner


def test_a_filename_holding_wide_punctuation_survives_a_second_path(tmp_path):
    """The name itself can hold the marks a prose delimiter is made of.

    A pattern taught to end on full-width brackets cuts this path in half and finds
    nothing, which is why the fix is in the walk and not in the character class.
    """
    named = tmp_path / "\u62a5\u544a\uff08\u7ec8\u7248\uff09.md"
    other = tmp_path / "b.md"
    for path in (named, other):
        path.write_text("facts", encoding="utf-8")

    assert materials.materials_from_prompt(f"{named}\u3001{other}") == [str(named), str(other)]


def test_a_name_that_does_not_exist_does_not_hide_the_next_one(tmp_path):
    """The scan carries on past a path that names nothing, not only past a hit."""
    real = tmp_path / "real.md"
    real.write_text("facts", encoding="utf-8")

    assert materials.materials_from_prompt(f"{tmp_path / 'ghost.md'}\u3001{real}") == [str(real)]


def test_with_a_publish_record_only_a_recorded_deck_verifies(tmp_path):
    """A copy the model made under out/ is a valid deck newer than the turn; with the
    publish step's digests in hand it is not a candidate, and is named as unpublished."""
    import hashlib

    out = tmp_path / "out"
    published = _pptx(out / "published.pptx", slides=3)
    copied = _pptx(out / "copied.pptx", slides=4)
    digests = {hashlib.sha256(published.read_bytes()).hexdigest()}

    assert materials.verified_deck(out, f"MEDIA: {copied}", {}, digests) == (published, 3)
    assert materials.verified_deck(out, f"MEDIA: {copied}", {}, set()) == (None, 0)
    assert materials.unpublished_decks(out, {}, digests) == [copied]


# -- the destination the user named ------------------------------------------


def _digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_delivered_path_verifies_by_its_digest(tmp_path):
    """A deck the publish step wrote outside out/ stands on the same terms as one under it."""
    out = tmp_path / "out"
    published = _pptx(out / "deck.pptx", slides=6)
    delivered = tmp_path / "handoff" / "ravenx-intro.pptx"
    delivered.parent.mkdir()
    delivered.write_bytes(published.read_bytes())
    digests = {_digest(published)}

    assert materials.verified_deck(out, f"MEDIA: {delivered}", {}, digests, [delivered]) == (delivered, 6)
    assert materials.verified_deck(out, f"MEDIA: {published}", {}, digests, [delivered]) == (published, 6)
    assert materials.verified_deck(out, f"MEDIA: {delivered}", {}, digests) == (published, 6), (
        "off the record, the path outside out/ is not a candidate and out/ answers"
    )


def test_a_delivery_keeping_the_decks_name_is_told_apart_from_out_by_its_whole_path(tmp_path):
    out = tmp_path / "out"
    published = _pptx(out / "deck.pptx", slides=2)
    delivered = tmp_path / "handoff" / "deck.pptx"
    delivered.parent.mkdir()
    delivered.write_bytes(published.read_bytes())
    digests = {_digest(published)}

    assert materials.verified_deck(out, f"MEDIA: {delivered}", {}, digests, [delivered]) == (delivered, 2)
    assert materials.verified_deck(out, f"MEDIA: {published}", {}, digests, [delivered]) == (published, 2)


def test_an_earlier_turns_delivery_is_not_this_turns(tmp_path):
    out = tmp_path / "out"
    published = _pptx(out / "deck.pptx", slides=2)
    delivered = tmp_path / "handoff" / "intro.pptx"
    delivered.parent.mkdir()
    delivered.write_bytes(published.read_bytes())
    before = materials.deck_mtimes(out, [delivered])

    assert set(before) == {published, delivered}
    assert materials.verified_deck(out, f"MEDIA: {delivered}", before, {_digest(published)}, [delivered]) == (None, 0)


def test_the_prompt_says_where_a_named_destination_goes(tmp_path):
    text = materials.describe([], tmp_path / "materials", tmp_path / "out")
    assert "state that once as ppt_build's deliver_to" in text
