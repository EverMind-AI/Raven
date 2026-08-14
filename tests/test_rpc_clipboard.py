"""Tests for ``clipboard.paste``.

The handler never claims ``attached``: Raven has no per-session attachment
buffer (that is what ``image.attach`` is stubbed for), so it saves the image
where the file tools can read it and reports the path instead. These pin that,
because flipping ``attached`` to true without the buffer would make the TUI
print "image attached" for an image the model never receives.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from raven.rpc.methods import clipboard as mod
from raven.rpc.methods.clipboard import clipboard_paste


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk))


@pytest.fixture(autouse=True)
def _a_reader_exists(monkeypatch):
    """Default every test to a machine that has a clipboard reader.

    ``clipboard_paste`` short-circuits when the platform has none, so without
    this the tests that stub ``_read_clipboard_image`` never reach it. The two
    tests about that short-circuit override this deliberately.
    """
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name if name == "xclip" else None)


def _factory(workspace: Path):
    loop = type("_Loop", (), {"workspace": workspace})()
    return lambda: loop


async def test_an_empty_clipboard_reports_nothing_found(monkeypatch, tmp_path: Path) -> None:
    async def _none():
        return None

    monkeypatch.setattr(mod, "_read_clipboard_image", _none)
    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))
    assert result == {"attached": False, "message": "No image found in clipboard"}


async def test_an_image_is_saved_under_the_workspace(monkeypatch, tmp_path: Path) -> None:
    async def _image():
        return _png(640, 480)

    monkeypatch.setattr(mod, "_read_clipboard_image", _image)
    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))

    saved = list((tmp_path / "clipboard").glob("*.png"))
    assert len(saved) == 1
    assert str(saved[0]) in result["message"]
    assert (result["width"], result["height"]) == (640, 480)
    assert result["token_estimate"] > 0


async def test_it_does_not_claim_the_image_was_attached(monkeypatch, tmp_path: Path) -> None:
    async def _image():
        return _png(10, 10)

    monkeypatch.setattr(mod, "_read_clipboard_image", _image)
    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))
    assert result["attached"] is False


async def test_an_unwritable_target_is_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    async def _image():
        return _png(10, 10)

    monkeypatch.setattr(mod, "_read_clipboard_image", _image)
    # A directory that was never created: the write raises FileNotFoundError,
    # which is the shape a read-only or full filesystem also produces.
    monkeypatch.setattr(mod, "_clipboard_dir", lambda _f: tmp_path / "nested" / "missing")
    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))
    assert result["attached"] is False
    assert "Could not save" in result["message"]


def test_readers_are_filtered_to_binaries_that_exist(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    assert mod._readers() == []

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name if name == "xclip" else None)
    assert [cmd[0] for cmd, _decode in mod._readers()] == ["xclip"]


def test_stock_macos_has_a_reader_without_homebrew(monkeypatch) -> None:
    # pngpaste is a Homebrew package. Offering only that left every stock Mac
    # with no reader at all, and the handler then blamed the clipboard.
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)
    assert [cmd[0] for cmd, _decode in mod._readers()] == ["osascript"]


def test_pngpaste_is_preferred_when_it_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/opt/homebrew/bin/" + name)
    assert [cmd[0] for cmd, _decode in mod._readers()] == ["pngpaste", "osascript"]


def test_osascript_output_decodes_to_the_png_it_names() -> None:
    png = _png(4, 4)
    rendered = b"\xc2\xabdata PNGf" + png.hex().upper().encode() + b"\xc2\xbb\n"
    assert mod._decode_osascript_png(rendered) == png


def test_a_text_only_clipboard_decodes_to_nothing() -> None:
    # `the clipboard as PNGf` on text is an AppleScript error, not image data.
    assert mod._decode_osascript_png(b"execution error: The clipboard does not hold data") is None
    assert mod._decode_osascript_png(b"\xc2\xabdata PNGfZZZZ\xc2\xbb") is None


async def test_a_platform_with_no_reader_says_so_rather_than_no_image(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))
    assert result["attached"] is False
    assert "No image found" not in result["message"]
    assert "xclip" in result["message"] or "wl-clipboard" in result["message"]


async def test_an_empty_clipboard_still_says_no_image_when_a_reader_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name if name == "xclip" else None)

    async def _none():
        return None

    monkeypatch.setattr(mod, "_read_clipboard_image", _none)
    result = await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))
    assert result["message"] == "No image found in clipboard"


async def test_saved_images_are_bounded(monkeypatch, tmp_path: Path) -> None:
    # Nothing else ever reads this directory back to clean it, so the handler
    # has to bound it itself.
    async def _image():
        return _png(10, 10)

    monkeypatch.setattr(mod, "_read_clipboard_image", _image)
    monkeypatch.setattr(mod, "_KEEP", 3)

    for i in range(6):
        # Distinct names: the real one is second-resolution plus a size tag, and
        # this loop is faster than a second.
        monkeypatch.setattr(mod.time, "strftime", lambda _fmt, i=i: f"2026010{i}-000000")
        await clipboard_paste({}, agent_loop_factory=_factory(tmp_path))

    kept = sorted(p.name for p in (tmp_path / "clipboard").glob("clipboard-*"))
    assert len(kept) == 3, kept
