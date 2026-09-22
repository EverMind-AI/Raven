"""The face a render is given, and the ways that can go wrong quietly.

Every assertion here stands for a failure that reports nothing: a malformed
configuration that leaves a render with no fonts, a deployment's font policy
dropped while appearing to be honoured, and a Latin face answering for a Han
one. None of them raises, and all of them come back as boxes on a page.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from raven.utils import fonts


def _face_in(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    face = directory / "NotoSansSC-Regular.otf"
    face.write_bytes(b"OTTO placeholder")
    return face


def test_a_path_with_an_ampersand_keeps_the_configuration_well_formed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fontconfig answers a malformed file by discarding all of it, so a path it
    could not parse would leave the render with no fonts whatsoever -- worse
    than the missing Han face this mechanism exists to supply."""
    face_dir = tmp_path / "fonts & co"
    _face_in(face_dir)
    monkeypatch.setenv(fonts.ENV_FONT_DIR, str(face_dir))

    env = fonts.render_env(base={}, scratch=tmp_path / "scratch")
    text = Path(env["FONTCONFIG_FILE"]).read_text(encoding="utf-8")

    root = ElementTree.fromstring(text)
    assert [element.text for element in root.findall("dir")] == [str(face_dir)]


def test_a_configuration_already_in_force_is_inherited_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that set FONTCONFIG_FILE chose its fonts on purpose. Pointing
    the include at the system default instead would undo that choice while
    looking like it had been respected."""
    _face_in(tmp_path / "faces")
    monkeypatch.setenv(fonts.ENV_FONT_DIR, str(tmp_path / "faces"))
    theirs = tmp_path / "their-fonts.conf"
    theirs.write_text("<fontconfig/>", encoding="utf-8")

    env = fonts.render_env(base={"FONTCONFIG_FILE": str(theirs)}, scratch=tmp_path / "scratch")
    text = Path(env["FONTCONFIG_FILE"]).read_text(encoding="utf-8")

    assert str(theirs) in text
    assert fonts.SYSTEM_FONTCONFIG not in text


def test_nothing_is_added_where_there_is_no_face_to_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty directory must leave the environment alone rather than hand the
    renderer a configuration naming nothing, which would hide the host's fonts."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(fonts.ENV_FONT_DIR, str(empty))

    assert fonts.render_env(base={}, scratch=tmp_path / "scratch") == {}


def test_a_host_with_no_face_is_not_silently_taken_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """fc-match answers every query with its nearest approximation, so on a host
    with no Han face it names a Latin one. Taking that for a Han face is how a
    deck gets measured against glyphs that do not exist."""
    monkeypatch.setattr(fonts, "bundled_face", lambda: None)
    monkeypatch.setattr(fonts, "user_han_faces", list)
    monkeypatch.setattr(fonts, "_fc_listed_han", lambda: None)
    monkeypatch.setattr(fonts, "host_han_faces", list)
    monkeypatch.setattr(fonts.sys, "platform", "linux")
    monkeypatch.setattr(fonts.Path, "is_file", lambda self: False)

    assert fonts.han_face() is None
    assert fonts.can_draw_han() is False


def test_the_face_raven_brought_outranks_whatever_the_host_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raven's own directory is the one answer that does not depend on the
    machine, so it decides even where the host also has something."""
    face = _face_in(tmp_path / "faces")
    monkeypatch.setenv(fonts.ENV_FONT_DIR, str(tmp_path / "faces"))
    monkeypatch.setattr(fonts, "host_han_faces", lambda: ["Some Host Face"])

    assert fonts.han_face() == face
    assert fonts.can_draw_han() is True


def test_a_stock_mac_is_not_taken_for_a_host_that_can_set_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    """PingFang sits on every Mac and LibreOffice still does not draw from it --
    that is the failure this module exists for. A face list naming it would
    answer "yes, this host sets Chinese" for precisely the host that does not.

    The list is left intact rather than emptied: its contents are what is under
    test, and a test that replaces them tests nothing.
    """
    monkeypatch.setattr(fonts.sys, "platform", "darwin")
    monkeypatch.setattr(fonts.shutil, "which", lambda name: None)
    monkeypatch.setattr(fonts, "bundled_face", lambda: None)
    monkeypatch.setattr(fonts, "user_han_faces", list)
    monkeypatch.setattr(fonts.Path, "is_file", lambda self: str(self) == "/System/Library/Fonts/PingFang.ttc")

    assert fonts.han_face() is None, "a Mac's own PingFang was taken for a face the renderer can use"
    assert fonts.can_draw_han() is False


def test_a_linux_package_path_still_answers_where_fontconfig_cannot_be_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same list: a host can carry the fontconfig library
    without the fc-list binary, and there a packaged face on disk is reachable.
    Dropping the whole list to solve the Mac case would lose that."""
    packaged = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    monkeypatch.setattr(fonts.sys, "platform", "linux")
    monkeypatch.setattr(fonts.shutil, "which", lambda name: None)
    monkeypatch.setattr(fonts, "bundled_face", lambda: None)
    monkeypatch.setattr(fonts, "user_han_faces", list)
    monkeypatch.setattr(fonts.Path, "is_file", lambda self: str(self) == packaged)

    assert fonts.han_face() == Path(packaged)
    assert fonts.can_draw_han() is True
