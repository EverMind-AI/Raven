"""An Imported Conversation from a package made on one machine to reading it on another.

Packages come out of their real writers (``tests/imported_package_builders.py``)
under one raven home; everything after that happens in a second home, through
a dispatcher carrying every method production registers: ``imported.upload``
and its runner, ``session.*`` serving the conversation from ``imports/``,
``trajectory.*`` reading its store, and the read-only guard. Nothing under test
is stood in for; the limits are the shipped ones, so the oversized upload and
the decompression bomb are built at full size.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import struct
import tarfile
import zlib
from pathlib import Path
from typing import Any, Iterator

import pytest

from raven.config.loader import load_config
from raven.rpc.approval_broker import ApprovalBroker
from raven.rpc.confirm_broker import ConfirmBroker
from raven.rpc.credential_broker import CredentialBroker
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.files import MAX_UPLOAD_BYTES
from raven.rpc.methods import register_aligned_methods_except_system
from raven.rpc.methods.system import register_system_methods
from raven.rpc.question_broker import QuestionBroker
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.session.manager import SessionManager
from raven.trajectory import imported
from raven.trajectory import index as tidx
from raven.trajectory import package_check as pcheck
from raven.trajectory import policy as tpol
from raven.trajectory.imported_package import ImportLimits
from tests import imported_package_builders as builders

pytestmark = pytest.mark.integration

SECRET = "sk-e2e0123456789abcdefghijKLMNOP"
PLACEHOLDER = "[REDACTED:pattern.openai-style-key]"
LONG_CONVERSATION_BYTES = 512 * 1024
UNPACKED_LIMIT = ImportLimits().max_unpacked_export
BOMB_MEMBERS = 5
BOMB_MEMBER_BYTES = 256 << 20


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """Packages are built under ``home-a``; the test then moves to ``home-b`` with an empty trace store."""
    paths = {"a": tmp_path / "home-a", "b": tmp_path / "home-b", "src": tmp_path / "src"}
    paths["a"].mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(paths["a"]))
    tpol._reset_for_tests()
    tpol.arm(True)
    tidx._reset_for_tests()
    runner = imported.ImportRunner()
    monkeypatch.setattr(imported, "RUNNER", runner)
    yield paths
    assert runner.stop(timeout=30), "an import outlived its test"
    tidx._reset_for_tests()
    tpol._reset_for_tests()


def _move_to_b(homes: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    homes["b"].mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(homes["b"]))
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(homes["b"] / "traces"))
    sessions = load_config().workspace_path / "sessions"
    assert sessions.is_relative_to(homes["b"]), sessions
    return sessions


async def _sink(_frame: dict[str, Any]) -> None:
    return None


def _dispatcher() -> Dispatcher:
    made = Dispatcher()
    register_aligned_methods_except_system(
        made,
        emitter=SubscriptionEmitter(send_frame=_sink),
        approval_broker=ApprovalBroker(send_frame=_sink),
        credential_broker=CredentialBroker(send_frame=_sink),
        confirm_broker=ConfirmBroker(send_frame=_sink),
        question_broker=QuestionBroker(send_frame=_sink),
    )
    register_system_methods(made)
    return made


async def _call(dispatcher: Dispatcher, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    return response


async def _ok(dispatcher: Dispatcher, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await _call(dispatcher, method, params)
    assert "error" not in response, (method, response)
    return response["result"]


def _code(response: dict[str, Any]) -> int | None:
    error = response.get("error")
    return error["code"] if error else None


def _upload(data: bytes, name: str, *, force: bool = False) -> dict[str, Any]:
    return {"name": name, "content_b64": base64.b64encode(data).decode(), "force": force}


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*")) if path.is_dir() else []


def _imported_dirs() -> list[Path]:
    root = imported.imports_root()
    return sorted(p for p in root.iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name)) if root.is_dir() else []


def _left_behind() -> list[str]:
    """Everything under ``imports/`` except an empty staging directory."""
    return [name for name in _tree(imported.imports_root()) if name != imported.STAGING_DIR]


async def _imported_rows(dispatcher: Dispatcher) -> list[dict[str, Any]]:
    rows = (await _ok(dispatcher, "session.list", {"channels": ["tui", "imported"]}))["sessions"]
    return [row for row in rows if row["source"] == "imported"]


async def _entries(dispatcher: Dispatcher, key: str) -> tuple[list[dict[str, Any]], str]:
    for _ in range(100):
        first = await _ok(dispatcher, "trajectory.list", {"session_key": key, "limit": 2})
        if first["index_state"]["phase"] == "ready":
            break
        await asyncio.sleep(0.2)
    entries = list(first["entries"])
    cursor = first["next_cursor"]
    while cursor:
        page = await _ok(dispatcher, "trajectory.list", {"session_key": key, "cursor": cursor, "limit": 2})
        entries += page["entries"]
        cursor = page["next_cursor"]
    return entries, first["epoch"]


async def _block(dispatcher: Dispatcher, key: str, entry: dict[str, Any], epoch: str, block: str) -> dict[str, Any]:
    params = {
        "session_key": key,
        "entry_id": entry["entry_id"],
        "entry_revision": entry["revision"],
        "epoch": epoch,
        "block_id": block,
    }
    return await _ok(dispatcher, "trajectory.block", params)


async def _all_messages(dispatcher: Dispatcher, key: str, entry: dict[str, Any], epoch: str) -> list[dict]:
    body = await _block(dispatcher, key, entry, epoch, "messages")
    assert body["availability"] == "available" and not body["truncated"], body
    items = list(body["data"]["items"])
    while body["next_cursor"]:
        params = {
            "session_key": key,
            "entry_id": entry["entry_id"],
            "entry_revision": entry["revision"],
            "epoch": epoch,
            "block_id": "messages",
            "cursor": body["next_cursor"],
        }
        body = await _ok(dispatcher, "trajectory.block", params)
        items += body["data"]["items"]
    return items


def _texts(resumed: dict[str, Any]) -> list[str]:
    return [m["text"] for m in resumed["messages"] if m["role"] in ("user", "assistant")]


# ── a package read on another machine ─────────────────────────────────


async def test_an_export_reads_in_another_home_like_the_original(homes, monkeypatch):
    package, source = builders.build_export(homes["src"], long_input=True)
    sessions = _move_to_b(homes, monkeypatch)
    before = _tree(sessions)
    dispatcher = _dispatcher()

    result = await _ok(dispatcher, "imported.upload", _upload(package.read_bytes(), package.name))
    assert result["status"] == "imported"
    assert result["kind"] == "export"
    assert result["title"] == builders.TITLE
    assert result["source_name"] == package.name
    key = result["session_key"]

    (row,) = await _imported_rows(dispatcher)
    assert (row["id"], row["title"]) == (key, builders.TITLE)
    assert row["imported"] == {"kind": "export", "source_name": package.name}
    archived = (await _ok(dispatcher, "session.list", {"channels": ["tui", "imported"], "archived": True}))["sessions"]
    assert key not in [r["id"] for r in archived]

    resumed = await _ok(dispatcher, "session.resume", {"session_id": key})
    assert resumed["info"]["read_only"] is True
    assert resumed["info"]["title"] == builders.TITLE
    assert _texts(resumed) == [builders.FIRST_INPUT, "found three hotels", builders.SECOND_INPUT, "booked"]

    entries, epoch = await _entries(dispatcher, key)
    assert {e["turn_number"] for e in entries if e["origin"] == "main"} == {1, 3}
    assert any(e["origin"] == "subagent" for e in entries)
    llm = next(e for e in entries if e["kind"] == "llm.input" and e["turn_number"] == 1)
    detail = await _ok(dispatcher, "trajectory.detail", {"session_key": key, "entry_id": llm["entry_id"]})
    assert detail["session_key"] == key, "the page files a descriptor under the key it asked with"
    blocks = {b["id"]: b["availability"] for b in detail["blocks"]}
    assert blocks["messages"] == blocks["system"] == blocks["prompt"] == "available"
    messages = await _all_messages(dispatcher, key, llm, epoch)
    assert messages == source.first_call
    assert len(json.dumps(messages)) > LONG_CONVERSATION_BYTES

    (directory,) = _imported_dirs()
    assert (directory / "meta.json").is_file()
    assert _tree(imported.imports_root() / imported.STAGING_DIR) == []
    assert not (homes["a"] / imported.IMPORTS_DIR).exists()
    assert _tree(sessions) == before


async def test_a_bug_report_shows_its_description_and_its_placeholders(homes, monkeypatch):
    package, source = builders.build_bug_report(homes["src"], long_input=True, secret=SECRET)
    sessions = _move_to_b(homes, monkeypatch)
    before = _tree(sessions)
    dispatcher = _dispatcher()

    result = await _ok(dispatcher, "imported.upload", _upload(package.read_bytes(), package.name))
    assert result["kind"] == "bugreport"
    assert result["title"] == "The hotel was never booked"
    key = result["session_key"]

    resumed = await _ok(dispatcher, "session.resume", {"session_id": key})
    texts = _texts(resumed)
    assert texts[-1] == f"booked {PLACEHOLDER}"
    assert all(SECRET not in text for text in texts)

    entries, epoch = await _entries(dispatcher, key)
    llm = next(e for e in entries if e["kind"] == "llm.input")
    assert await _all_messages(dispatcher, key, llm, epoch) == source.first_call
    tool = next(e for e in entries if e["kind"] == "tool.output")
    output = await _block(dispatcher, key, tool, epoch, "result")
    assert output["availability"] == "available"
    assert output["data"]["text"] == f"weather: sunny {PLACEHOLDER}"

    stored = [p for p in imported.imports_root().rglob("*") if p.is_file()]
    assert stored and not [p for p in stored if SECRET.encode() in p.read_bytes()]
    assert _tree(sessions) == before


# ── the reader's own copy ──────────────────────────────────────────────


async def test_the_same_package_again_then_rename_then_delete(homes, monkeypatch):
    package, _ = builders.build_export(homes["src"])
    _move_to_b(homes, monkeypatch)
    dispatcher = _dispatcher()
    data = package.read_bytes()

    first = await _ok(dispatcher, "imported.upload", _upload(data, package.name))
    again = await _ok(dispatcher, "imported.upload", _upload(data, "renamed-on-disk.tar.gz"))
    assert again["status"] == "duplicate"
    assert again["session_key"] == first["session_key"]
    assert again["title"] == first["title"]
    assert len(_imported_dirs()) == 1

    forced = await _ok(dispatcher, "imported.upload", _upload(data, package.name, force=True))
    assert forced["status"] == "imported"
    assert forced["session_key"] != first["session_key"]
    assert len(_imported_dirs()) == 2
    assert len(await _imported_rows(dispatcher)) == 2

    key = first["session_key"]
    renamed = await _ok(dispatcher, "session.title", {"session_id": key, "title": "  Coast trip,\nreviewed  "})
    assert renamed["title"] == "Coast trip, reviewed"
    assert {r["id"]: r["title"] for r in await _imported_rows(dispatcher)}[key] == "Coast trip, reviewed"
    assert (await _ok(dispatcher, "session.resume", {"session_id": key}))["info"]["title"] == "Coast trip, reviewed"
    directory = imported.get(key).path
    assert json.loads((directory / "meta.json").read_text(encoding="utf-8"))["title"] == "Coast trip, reviewed"
    assert _code(await _call(dispatcher, "session.title", {"session_id": key, "title": "  "})) == -32602

    await _entries(dispatcher, key)
    deleted = await _ok(dispatcher, "session.delete", {"session_id": key})
    assert deleted["deleted"] == key
    assert not directory.exists()
    assert [r["id"] for r in await _imported_rows(dispatcher)] == [forced["session_key"]]
    assert _code(await _call(dispatcher, "session.resume", {"session_id": key})) == -32001
    assert _code(await _call(dispatcher, "trajectory.list", {"session_key": key})) == -32001


# ── what the server refuses ────────────────────────────────────────────

WRITES = [
    ("turn.send", {"session_key": "{key}", "text": "hello"}),
    ("session.branch", {"session_id": "{key}"}),
    ("session.clear", {"session_id": "{key}"}),
    ("session.undo", {"session_id": "{key}"}),
    ("session.compress", {"session_id": "{key}"}),
    ("session.pin", {"session_id": "{key}", "pinned": True}),
    ("session.archive", {"session_id": "{key}", "archived": True}),
    ("session.set_mode", {"session_key": "{key}", "mode": "max"}),
    ("session.set_harness", {"session_key": "{key}", "harness": "x"}),
    ("session.set_knowledge", {"session_id": "{key}", "knowledge_bases": []}),
    ("trajectory.export", {"session_key": "{key}", "trace_ids": ["trace-a"], "file_name": "x.tar.gz"}),
    ("bugreport.start", {"session_key": "{key}", "trace_ids": ["trace-a"]}),
    ("fs.upload", {"name": "a.txt", "content_b64": "", "session": "{key}"}),
    ("config.set", {"session_id": "{key}", "key": "model", "value": "x"}),
    ("turn.send", {"channel": "imported", "chat_id": "{id}", "text": "hello"}),
]


async def test_writes_are_refused_and_the_workspace_gains_nothing(homes, monkeypatch):
    package, _ = builders.build_export(homes["src"])
    sessions = _move_to_b(homes, monkeypatch)
    dispatcher = _dispatcher()
    key = (await _ok(dispatcher, "imported.upload", _upload(package.read_bytes(), package.name)))["session_key"]
    before_sessions = _tree(sessions)
    before_store = _tree(imported.get(key).path)
    before_messages = _texts(await _ok(dispatcher, "session.resume", {"session_id": key}))
    created: list[str] = []
    real = SessionManager.get_or_create

    def spy(self: SessionManager, session_key: str) -> Any:
        created.append(session_key)
        return real(self, session_key)

    monkeypatch.setattr(SessionManager, "get_or_create", spy)
    for method, template in WRITES:
        params = {
            name: value.format(key=key, id=key.split(":", 1)[1]) if isinstance(value, str) else value
            for name, value in template.items()
        }
        response = await _call(dispatcher, method, params)
        assert _code(response) == -32029, (method, response)
        assert response["error"]["message"] == "imported_read_only"
        assert response["error"]["data"]["method"] == method

    assert _texts(await _ok(dispatcher, "session.resume", {"session_id": key})) == before_messages
    assert _tree(imported.get(key).path) == before_store
    assert _tree(sessions) == before_sessions
    assert [k for k in created if imported.is_imported_key(k)] == []


def _members(package: Path) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    with tarfile.open(package, "r:gz") as tar:
        return [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]  # type: ignore[union-attr]


def _repack(package: Path, extra: list[tarfile.TarInfo]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for info, data in _members(package):
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
        for info in extra:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return out.getvalue()


def _root(package: Path) -> str:
    return _members(package)[0][0].name.split("/", 1)[0]


def _deflated(data: bytes, *, last: bool = False) -> bytes:
    """Raw deflate that refers to nothing before it and ends on a byte boundary, so pieces concatenate."""
    deflate = zlib.compressobj(1, zlib.DEFLATED, -15)
    return deflate.compress(data) + deflate.flush(zlib.Z_FINISH if last else zlib.Z_FULL_FLUSH)


def _bomb(package: Path) -> bytes:
    """The real export plus zero-filled members that together pass the unpacked limit.

    Each member fits the budget on its own, so the reader has to inflate about
    a gigabyte before the running total passes the limit; on the wire the whole
    package is a few MB. The gzip stream is assembled from one deflated
    run of zeros repeated, because deflating the full gigabyte takes far longer
    than reading it back.
    """
    assert BOMB_MEMBER_BYTES < UNPACKED_LIMIT < BOMB_MEMBERS * BOMB_MEMBER_BYTES
    prefix = io.BytesIO()
    tar = tarfile.open(fileobj=prefix, mode="w")
    for info, data in _members(package):
        tar.addfile(info, io.BytesIO(data) if data is not None else None)
    zeros = bytes(16 << 20)
    run = _deflated(zeros)
    pieces, crc, size = [], 0, 0

    def plain(data: bytes, *, last: bool = False) -> None:
        nonlocal crc, size
        pieces.append(_deflated(data, last=last))
        crc, size = zlib.crc32(data, crc), size + len(data)

    plain(prefix.getvalue()[: tar.offset])
    for n in range(BOMB_MEMBERS):
        info = tarfile.TarInfo(f"{_root(package)}/padding-{n}.bin")
        info.size = BOMB_MEMBER_BYTES
        plain(info.tobuf())
        for _ in range(BOMB_MEMBER_BYTES // len(zeros)):
            pieces.append(run)
            crc, size = zlib.crc32(zeros, crc), size + len(zeros)
    plain(bytes(2 * tarfile.BLOCKSIZE), last=True)
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    return header + b"".join(pieces) + struct.pack("<II", crc, size & 0xFFFFFFFF)


def _traversal(package: Path) -> bytes:
    info = tarfile.TarInfo(f"{_root(package)}/../escape.txt")
    info.size = 1
    return _repack(package, [info])


def _symlink(package: Path) -> bytes:
    info = tarfile.TarInfo(f"{_root(package)}/passwd")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    return _repack(package, [info])


def _truncated(package: Path) -> bytes:
    data = package.read_bytes()
    return data[: len(data) // 2]


@pytest.mark.parametrize(
    "make,reasons",
    [
        (_truncated, {pcheck.CODE_NOT_GZIP_TAR}),
        (_traversal, {pcheck.CODE_BAD_MEMBER_NAME}),
        (_symlink, {pcheck.CODE_BAD_MEMBER_TYPE}),
        (_bomb, {pcheck.CODE_UNPACKED_TOO_LARGE}),
    ],
    ids=["truncated", "traversal", "symlink", "bomb"],
)
async def test_a_damaged_or_hostile_package_is_refused_and_leaves_nothing(homes, monkeypatch, tmp_path, make, reasons):
    package, _ = builders.build_export(homes["src"])
    data = make(package)
    assert len(data) < MAX_UPLOAD_BYTES
    _move_to_b(homes, monkeypatch)
    dispatcher = _dispatcher()

    response = await _call(dispatcher, "imported.upload", _upload(data, "hostile.tar.gz"))
    assert _code(response) == -32030, response
    assert response["error"]["data"]["reason"] in reasons, response["error"]
    assert _left_behind() == []
    assert list(tmp_path.rglob("escape.txt")) == []
    assert await _imported_rows(dispatcher) == []

    accepted = await _ok(dispatcher, "imported.upload", _upload(package.read_bytes(), package.name))
    assert accepted["status"] == "imported"


async def test_an_upload_past_the_limit_is_refused_before_it_is_decoded(homes, monkeypatch):
    _move_to_b(homes, monkeypatch)
    dispatcher = _dispatcher()
    size = MAX_UPLOAD_BYTES + 1
    padding = (3 - size % 3) % 3
    text = "A" * ((size + 2) // 3 * 4 - padding) + "=" * padding
    assert len(base64.b64decode(text)) == size

    response = await _call(dispatcher, "imported.upload", {"name": "huge.tar.gz", "content_b64": text})
    assert _code(response) == -32030
    assert response["error"]["data"]["reason"] == pcheck.CODE_TOO_LARGE
    assert not imported.RUNNER.busy
    assert not imported.imports_root().exists()
    assert await _imported_rows(dispatcher) == []
