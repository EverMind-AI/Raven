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
