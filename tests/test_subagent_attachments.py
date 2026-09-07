"""How a turn's attachments are spelled for a sub-agent: blocks for ACP, a note for text."""

from __future__ import annotations

from pathlib import Path

from raven.agent.subagent.attachments import (
    ATTACHMENTS_NOTE,
    UNDELIVERABLE_NOTE,
    attachment_blocks,
    retarget_note,
    with_attachment_note,
    with_undeliverable_note,
)
from raven.spine.message import Media


def _file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


def test_an_attachment_becomes_a_resource_link_to_its_absolute_path(tmp_path: Path) -> None:
    deck = _file(tmp_path, "house style.pptx")
    (block,) = attachment_blocks([Media(path=str(deck), mime="application/octet-stream", kind="file")])
    assert block["type"] == "resource_link"
    assert block["name"] == "house style.pptx"
    assert block["uri"] == deck.resolve().as_uri() and block["uri"].startswith("file:///")
    assert "mimeType" not in block, "the generic mime the upload path assigns says nothing worth sending"


def test_a_known_mime_rides_the_link_and_a_missing_file_sends_nothing(tmp_path: Path) -> None:
    shot = _file(tmp_path, "shot.png")
    blocks = attachment_blocks(
        [
            Media(path=str(shot), mime="image/png", kind="image"),
            Media(path=str(tmp_path / "gone.pdf"), mime="application/pdf", kind="file"),
        ]
    )
    assert [b["name"] for b in blocks] == ["shot.png"]
    assert blocks[0]["mimeType"] == "image/png"


def test_the_text_note_names_absolute_paths_and_leaves_a_bare_task_alone(tmp_path: Path) -> None:
    deck = _file(tmp_path, "a.pptx")
    noted = with_attachment_note("make a deck", [Media(path=str(deck), mime="", kind="file")])
    assert noted == f"make a deck\n\n{ATTACHMENTS_NOTE}\n- {deck.resolve()}"
    assert with_attachment_note("make a deck", []) == "make a deck"
    assert (
        with_attachment_note("make a deck", [Media(path=str(tmp_path / "gone"), mime="", kind="file")]) == "make a deck"
    )


def test_the_undeliverable_note_names_the_files_that_stayed_behind(tmp_path: Path) -> None:
    noted = with_undeliverable_note("look at these", [Media(path="/x/a.pptx", mime="", kind="file")])
    assert noted == f"look at these\n\n{UNDELIVERABLE_NOTE}\n- a.pptx"
    assert with_undeliverable_note("look at these", []) == "look at these"


def test_a_link_under_the_agent_home_is_named_the_way_the_page_spells_it(tmp_path: Path) -> None:
    """The page's note says ``- uploads/x.pptx``; the link beside it must read as the same file."""
    home = tmp_path / "home"
    deck = home / "uploads" / "house.pptx"
    deck.parent.mkdir(parents=True)
    deck.write_bytes(b"x")
    (block,) = attachment_blocks([Media(path=str(deck), mime="", kind="file")], root=home)
    assert block["name"] == "uploads/house.pptx"
    (outside,) = attachment_blocks([Media(path=str(_file(tmp_path, "else.pptx")), mime="", kind="file")], root=home)
    assert outside["name"] == "else.pptx"


def test_the_notes_bullet_is_retargeted_to_the_absolute_path_and_nothing_else_moves(tmp_path: Path) -> None:
    home = tmp_path / "home"
    deck = home / "uploads" / "house.pptx"
    deck.parent.mkdir(parents=True)
    deck.write_bytes(b"x")
    media = [Media(path=str(deck), mime="", kind="file")]
    text = "use uploads/house.pptx please\n\n[attachments, saved in the workspace]\n- uploads/house.pptx\n- other.txt"

    out = retarget_note(text, media, home)

    assert (
        out
        == f"use uploads/house.pptx please\n\n[attachments, saved in the workspace]\n- {deck.resolve()}\n- other.txt"
    )
    assert retarget_note(text, media, None) == text
    assert retarget_note(text, [Media(path=str(_file(tmp_path, "else.pptx")), mime="", kind="file")], home) == text
