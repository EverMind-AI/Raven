"""The face a render is given, and the ways that can go wrong quietly.

Every assertion here stands for a failure that reports nothing: a malformed
configuration that leaves a render with no fonts, a deployment's font policy
dropped while appearing to be honoured, and a Latin face answering for a Han
one. None of them raises, and all of them come back as boxes on a page.
"""

from __future__ import annotations

import subprocess
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


def test_each_platform_is_offered_the_directories_it_actually_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """These are where a font lands without privileges, and where the installer
    writes its fallback. Naming a directory the platform does not read would
    make the fallback invisible to the renderer it was fetched for."""
    monkeypatch.setattr(fonts.sys, "platform", "darwin")
    assert fonts.user_font_dirs() == (Path.home() / "Library" / "Fonts", Path("/Library") / "Fonts")

    monkeypatch.setattr(fonts.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert fonts.user_font_dirs() == (Path.home() / ".local" / "share" / "fonts",)

    monkeypatch.setenv("XDG_DATA_HOME", "/somewhere/share")
    assert fonts.user_font_dirs() == (Path("/somewhere/share/fonts"),)


def test_a_latin_face_in_a_user_directory_is_not_mistaken_for_a_han_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is scanned by name, so the names have to discriminate: a
    machine whose user fonts are all Latin must still report having no Han face,
    or the installer skips the step that would have given it one."""
    directory = tmp_path / "fonts"
    directory.mkdir()
    for name in ("DejaVuSans.ttf", "Inter-Regular.otf", "notes.txt"):
        (directory / name).write_bytes(b"x")
    monkeypatch.setattr(fonts, "user_font_dirs", lambda: (directory, tmp_path / "absent"))

    assert fonts.user_han_faces() == []

    (directory / "NotoSansSC-Regular.otf").write_bytes(b"x")
    (directory / "NotoSansCJK-Regular.ttc").write_bytes(b"x")
    assert [p.name for p in fonts.user_han_faces()] == [
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
    ]


def test_a_broken_fc_list_answers_empty_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs on the render path. A probe that raises would turn "cannot say
    which fonts exist" into a failed conversion, which is a worse answer than
    the boxes it was checking for."""
    monkeypatch.setattr(fonts.shutil, "which", lambda name: "/usr/bin/fc-list")

    def _explode(*args: object, **kwargs: object) -> object:
        raise OSError("fc-list is not really there")

    monkeypatch.setattr(fonts.subprocess, "run", _explode)
    assert fonts.host_han_faces() == []
    assert fonts._fc_listed_han() is None

    monkeypatch.setattr(fonts.shutil, "which", lambda name: None)
    assert fonts.host_han_faces() == []
    assert fonts._fc_listed_han() is None


def test_the_host_families_come_back_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """fc-list prints one line per face, so a family with several weights repeats;
    what a caller wants to know is which families exist."""
    monkeypatch.setattr(fonts.shutil, "which", lambda name: "/usr/bin/fc-list")
    output = "Noto Sans CJK SC,Noto Sans CJK SC Regular\nNoto Sans CJK SC,Bold\nPingFang SC\n\n"
    monkeypatch.setattr(fonts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""))
    assert fonts.host_han_faces() == ["Noto Sans CJK SC", "PingFang SC"]


def test_the_file_fontconfig_names_is_used_when_it_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fc-list can name a path that is gone -- a stale cache outlives the file it
    described -- and handing measurement a path that cannot be opened turns a
    missing font into a crash."""
    real = tmp_path / "NotoSansCJK-Regular.ttc"
    real.write_bytes(b"x")
    monkeypatch.setattr(fonts.shutil, "which", lambda name: "/usr/bin/fc-list")

    listed = f"/gone/StaleEntry.ttc\n{real}\n"
    monkeypatch.setattr(fonts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, listed, ""))
    assert fonts._fc_listed_han() == real

    # And a cache that outlived every file it described answers nothing, rather
    # than a path that cannot be opened.
    gone = "/gone/One.ttc\n/gone/Two.ttc\n"
    monkeypatch.setattr(fonts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, gone, ""))
    assert fonts._fc_listed_han() is None


def test_the_hint_speaks_the_package_manager_of_the_host_it_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hint naming a command this host does not have is a dead end at the exact
    moment somebody is trying to fix the problem."""
    monkeypatch.setattr(fonts.sys, "platform", "darwin")
    assert "brew" in fonts.install_hint()

    monkeypatch.setattr(fonts.sys, "platform", "win32")
    assert "YaHei" in fonts.install_hint()

    monkeypatch.setattr(fonts.sys, "platform", "linux")
    monkeypatch.setattr(fonts.shutil, "which", lambda name: "/usr/bin/apt-get")
    assert fonts.install_hint() == "sudo apt-get install -y fonts-noto-cjk"

    monkeypatch.setattr(fonts.shutil, "which", lambda name: None)
    assert "package manager" in fonts.install_hint()


def test_windows_is_believed_where_nothing_can_be_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every Windows edition ships a Han face, and nothing there answers fc-list.
    Reporting that host as unable to draw Chinese would send it chasing a font it
    already has."""
    monkeypatch.setattr(fonts, "han_face", lambda: None)
    monkeypatch.setattr(fonts, "host_han_faces", list)

    monkeypatch.setattr(fonts.sys, "platform", "win32")
    assert fonts.can_draw_han() is True

    monkeypatch.setattr(fonts.sys, "platform", "linux")
    assert fonts.can_draw_han() is False


def test_a_host_fontconfig_vouches_for_can_draw_even_with_no_file_located(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """han_face answers with a path and needs one; can_draw_han only needs to know
    that something exists, so a host whose fonts fontconfig knows but whose files
    this cannot name still counts."""
    monkeypatch.setattr(fonts, "han_face", lambda: None)
    monkeypatch.setattr(fonts, "host_han_faces", lambda: ["Noto Sans CJK SC"])
    monkeypatch.setattr(fonts.sys, "platform", "linux")

    assert fonts.can_draw_han() is True


def test_a_user_directory_face_is_preferred_over_asking_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order matters: a face sitting in the user's own directory is one this
    machine definitely has, while fc-list's answer depends on a cache."""
    face = tmp_path / "NotoSansSC-Regular.otf"
    face.write_bytes(b"x")
    monkeypatch.setattr(fonts, "bundled_face", lambda: None)
    monkeypatch.setattr(fonts, "user_han_faces", lambda: [face])
    monkeypatch.setattr(fonts, "_fc_listed_han", lambda: Path("/somewhere/Other.ttc"))

    assert fonts.han_face() == face
