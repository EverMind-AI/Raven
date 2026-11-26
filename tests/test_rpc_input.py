"""Tests for the ``input.*`` RPC handlers.

``input.detect_drop``: ui-tui asks this on every submit and every paste, and
rewrites the user's text with the answer -- so a false positive corrupts a
message that merely mentioned a path. Detection is existence-based for exactly
that reason, and these cases pin it.

``input.history.list`` / ``input.history.append``: the composer history over
RPC. The file format itself is covered by ``tests/test_input_history.py``;
here the handlers' coercion of ``limit``/``text``, the declared result models,
and registration are pinned.
"""

from __future__ import annotations

import asyncio
import nturl2path
import os
import struct
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse

import pytest

from raven import input_history

posix_only = pytest.mark.skipif(os.name == "nt", reason="a backslash-escaped space is a POSIX drop form")
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import input as input_methods
from raven.rpc.methods.input import (
    input_detect_drop,
    input_history_append,
    input_history_list,
    register_input_methods,
)
from raven.rpc.models import InputHistoryAppendResult, InputHistoryListResult


@pytest.fixture
def history_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move ``RAVEN_HOME`` so the history handlers never read the real file."""
    home = tmp_path / "raven-home"
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home


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


@posix_only
async def test_a_backslash_escaped_path_matches(tmp_path: Path) -> None:
    target = tmp_path / "my report.pdf"
    target.write_bytes(b"%PDF")
    escaped = str(target).replace(" ", r"\ ")
    result = await input_detect_drop({"text": escaped})
    assert result["matched"] is True
    assert result["name"] == "my report.pdf"


async def test_a_file_uri_matches(tmp_path: Path) -> None:
    """as_uri(), not "file://" + str(target): on Windows the str() is a
    backslash drive path, so concatenation leaves the backslashes in the
    authority and the URI names no path anywhere."""
    target = tmp_path / "dropped file.txt"
    target.write_text("x")
    uri = target.as_uri()
    result = await input_detect_drop({"text": uri})
    assert result["matched"] is True
    assert result["text"] == str(target.resolve())


async def test_a_file_uri_with_a_mixed_case_localhost_matches(tmp_path: Path) -> None:
    """Host names are case-insensitive, so ``LOCALHOST`` is this machine."""
    target = tmp_path / "dropped file.txt"
    target.write_text("x")
    uri = target.as_uri().replace("://", "://LOCALHOST", 1)
    result = await input_detect_drop({"text": uri})
    assert result["matched"] is True
    assert result["text"] == str(target.resolve())


async def test_a_file_uri_from_another_machine_is_not_a_drop(tmp_path: Path) -> None:
    """The guard, not the existence check, must reject a foreign authority.

    ``file://fileserver/share/dropped.txt`` names a path that is absent here
    anyway, so ``matched: False`` holds with the guard deleted. The direct
    assertions are what pin it: the helper names no path, and no second
    candidate is added for the local-looking part of the URI.
    """
    uri = "file://fileserver/share/dropped.txt"
    assert input_methods._file_uri_path(uri) is None
    assert input_methods._candidates(uri) == [uri]
    assert await input_detect_drop({"text": uri}) == {"matched": False}


def test_a_windows_file_uri_keeps_its_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``unquote`` alone leaves ``/C:/...``, a path Windows cannot open. The
    platform and the converter are substituted, as Windows' own converter is
    the same pure string conversion."""
    monkeypatch.setattr(input_methods, "_WINDOWS", True)
    monkeypatch.setattr(input_methods, "url2pathname", nturl2path.url2pathname)
    assert input_methods._file_uri_path("file:///C:/work/a.md") == "C:\\work\\a.md"


def test_a_windows_localhost_authority_keeps_the_unc_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """A localhost authority names this machine, but a share stays a UNC share."""
    monkeypatch.setattr(input_methods, "_WINDOWS", True)
    monkeypatch.setattr(input_methods, "url2pathname", nturl2path.url2pathname)
    assert input_methods._file_uri_path("file://localhost/share/a.md") == "\\\\localhost\\share\\a.md"


def test_an_unconvertible_windows_file_uri_names_no_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nturl2path.url2pathname`` raises ``OSError`` for a doubled drive colon."""
    monkeypatch.setattr(input_methods, "_WINDOWS", True)
    monkeypatch.setattr(input_methods, "url2pathname", nturl2path.url2pathname)
    assert input_methods._file_uri_path("file:///C::/a.md") is None


def test_a_file_uri_with_an_invalid_bracketed_host_names_no_path() -> None:
    """urlparse raises on a bracketed host that is not a valid IPv6 literal."""
    with pytest.raises(ValueError):
        urlparse("file://[bad/a.md")
    assert input_methods._file_uri_path("file://[bad/a.md") is None


def test_a_file_uri_carrying_no_path_names_no_path() -> None:
    """``file://`` and a bare authority have nothing to convert."""
    assert input_methods._file_uri_path("file://") is None
    assert input_methods._file_uri_path("file://localhost") is None


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


# ---------------------------------------------------------------------------
# input.history.list / input.history.append
# ---------------------------------------------------------------------------


async def test_history_append_then_list_round_trips(history_home: Path) -> None:
    appended = await input_history_append({"text": "hello"})
    InputHistoryAppendResult.model_validate(appended)
    assert appended == {"ok": True, "appended": True}

    listed = await input_history_list({})
    InputHistoryListResult.model_validate(listed)
    assert listed == {"entries": ["hello"]}


async def test_history_append_reports_a_duplicate_and_keeps_one_copy(history_home: Path) -> None:
    await input_history_append({"text": "hello"})
    duplicate = await input_history_append({"text": "hello"})
    assert duplicate == {"ok": True, "appended": False}
    assert (await input_history_list({}))["entries"] == ["hello"]


async def test_history_append_ignores_blank_or_missing_text(history_home: Path) -> None:
    assert (await input_history_append({"text": "   "}))["appended"] is False
    assert (await input_history_append({}))["appended"] is False
    assert (await input_history_list({}))["entries"] == []


async def test_history_list_limit_returns_the_newest_entries(history_home: Path) -> None:
    for text in ("one", "two", "three"):
        await input_history_append({"text": text})
    assert (await input_history_list({"limit": 2}))["entries"] == ["two", "three"]
    # ui-web clients send JSON numbers; a numeric string is accepted too.
    assert (await input_history_list({"limit": "1"}))["entries"] == ["three"]


async def test_history_list_tolerates_bad_limits(history_home: Path) -> None:
    for text in ("one", "two"):
        await input_history_append({"text": text})
    # An unparseable or absent limit means "default", not an RPC error: a page
    # asking for its history at boot must still get an answer.
    assert (await input_history_list({"limit": "nonsense"}))["entries"] == ["one", "two"]
    assert (await input_history_list({"limit": None}))["entries"] == ["one", "two"]
    assert (await input_history_list({}))["entries"] == ["one", "two"]
    # A negative limit is clamped to nothing rather than slicing from the end.
    assert (await input_history_list({"limit": -5}))["entries"] == []


async def test_history_list_caps_an_oversized_limit(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("raven.rpc.methods.input.MAX_ENTRIES", 3)
    for i in range(5):
        input_history.append(f"e{i}")

    assert (await input_history_list({"limit": 10**9}))["entries"] == ["e2", "e3", "e4"]
    # The default path is capped by the same constant.
    assert (await input_history_list({}))["entries"] == ["e2", "e3", "e4"]


async def test_history_methods_do_not_block_the_event_loop(history_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """History I/O must run off the loop, or one append stalls every session.

    The file read takes the lock, which can poll for up to ``_LOCK_TIMEOUT_S``;
    on the loop that would freeze streaming tokens and every other RPC. The two
    handlers are gathered together: on the loop they serialise (0.4 s), in
    worker threads they overlap (0.2 s).
    """

    def slow_append(text: str) -> bool:
        time.sleep(0.2)
        return True

    def slow_read(limit: int) -> list[str]:
        time.sleep(0.2)
        return []

    monkeypatch.setattr(input_methods, "append_history", slow_append)
    monkeypatch.setattr(input_methods, "read_entries", slow_read)

    started = time.monotonic()
    appended, listed = await asyncio.gather(
        input_history_append({"text": "slow"}),
        input_history_list({}),
    )
    elapsed = time.monotonic() - started

    assert appended == {"ok": True, "appended": True}
    assert listed == {"entries": []}
    assert elapsed < 0.35, f"history handlers serialised on the event loop ({elapsed:.3f}s)"


async def test_history_methods_are_registered_and_dispatch(history_home: Path) -> None:
    dispatcher = Dispatcher()
    register_input_methods(dispatcher)
    assert {"input.detect_drop", "input.history.list", "input.history.append"} <= set(dispatcher.methods())

    appended = await dispatcher.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "input.history.append", "params": {"text": "via dispatch"}}
    )
    assert appended["result"] == {"ok": True, "appended": True}

    listed = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 2, "method": "input.history.list", "params": {}})
    assert listed["result"] == {"entries": ["via dispatch"]}
