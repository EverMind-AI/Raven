"""Tests for ``input.detect_drop``.

ui-tui asks this on every submit and every paste, and rewrites the user's text
with the answer -- so a false positive corrupts a message that merely mentioned
a path. Detection is existence-based for exactly that reason, and these cases
pin it.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from raven.rpc.methods.input import input_detect_drop


def _png(width: int, height: int) -> bytes:
    """Smallest valid PNG header the size probe can read."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + struct.pack(">I", zlib.crc32(chunk))


async def test_a_bare_existing_path_matches(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hi")
    result = await input_detect_drop({"text": str(target)})
    assert result["matched"] is True
    assert result["name"] == "notes.txt"
    assert result["text"] == str(target.resolve())
    assert result["is_image"] is False


async def test_prose_that_merely_looks_like_a_path_does_not_match() -> None:
    result = await input_detect_drop({"text": "check /etc/definitely-not-here/config.yaml please"})
    assert result == {"matched": False}


async def test_a_path_that_does_not_exist_does_not_match(tmp_path: Path) -> None:
    assert await input_detect_drop({"text": str(tmp_path / "absent.txt")}) == {"matched": False}


async def test_empty_text_does_not_match() -> None:
    assert await input_detect_drop({"text": "   "}) == {"matched": False}
    assert await input_detect_drop({}) == {"matched": False}


async def test_a_quoted_path_matches_and_comes_back_unquoted(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    target.write_bytes(b"%PDF")
    result = await input_detect_drop({"text": f'"{target}"'})
    assert result["matched"] is True
    assert result["text"] == str(target.resolve())


async def test_a_backslash_escaped_path_matches(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    target.write_bytes(b"%PDF")
    escaped = str(target).replace(" ", r"\ ")
    result = await input_detect_drop({"text": escaped})
    assert result["matched"] is True
    assert result["name"] == "my report.pdf"


async def test_a_file_uri_matches(tmp_path: Path) -> None:
    target = tmp_path / "dropped file.txt"
    target.write_text("x")
    uri = "file://" + str(target).replace(" ", "%20")
    result = await input_detect_drop({"text": uri})
    assert result["matched"] is True
    assert result["text"] == str(target.resolve())


async def test_a_directory_is_not_a_file_drop(tmp_path: Path) -> None:
    assert await input_detect_drop({"text": str(tmp_path)}) == {"matched": False}


async def test_an_image_reports_dimensions_and_a_token_estimate(tmp_path: Path) -> None:
    target = tmp_path / "shot.png"
    target.write_bytes(_png(800, 600))
    result = await input_detect_drop({"text": str(target)})
    assert result["is_image"] is True
    assert (result["width"], result["height"]) == (800, 600)
    assert result["token_estimate"] > 0


async def test_image_detection_reads_magic_bytes_not_the_suffix(tmp_path: Path) -> None:
    lying = tmp_path / "notreally.png"
    lying.write_text("this is plain text")
    result = await input_detect_drop({"text": str(lying)})
    assert result["matched"] is True
    assert result["is_image"] is False
