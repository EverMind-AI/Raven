"""Tests for the gateway's deliverable download routes. The opaque token is the
trust boundary: no path ever appears in a request."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools._deliverables import DeliverableStore
from raven.rpc.transports.deliverables import add_files_routes, resolve_download


@pytest.fixture
async def client(tmp_path):
    store = DeliverableStore(tmp_path / "deliverables.json")
    app = web.Application()
    add_files_routes(app, store)
    async with TestClient(TestServer(app)) as c:
        c.store = store
        c.tmp_path = tmp_path
        yield c


def _register(store, tmp_path, name="report.pdf", body=b"PDF-BYTES"):
    fp = tmp_path / name
    fp.write_bytes(body)
    return store.register(
        path=str(fp),
        name=name,
        media_type="application/pdf",
        size=len(body),
        conversation="web:s1",
    )


async def test_download_returns_bytes_and_attachment_header(client) -> None:
    record = _register(client.store, client.tmp_path)

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 200
    assert await res.read() == b"PDF-BYTES"
    assert "attachment" in res.headers["Content-Disposition"]
    assert "report.pdf" in res.headers["Content-Disposition"]


async def test_unknown_token_is_410_gone(client) -> None:
    """410, not 404: a token that does not resolve names a deliverable that is
    genuinely gone, and the caller must be able to tell that apart from a 404 --
    which is what an unregistered route answers when the gateway is running code
    without these routes at all. Collapsing both onto 404 made a missing route
    report to the user as an expired file."""
    res = await client.get("/files/download", params={"token": "nope"})
    assert res.status == 410


async def test_missing_token_is_400(client) -> None:
    res = await client.get("/files/download")
    assert res.status == 400


async def test_deleted_file_is_410_and_entry_is_pruned(client) -> None:
    record = _register(client.store, client.tmp_path)
    (client.tmp_path / "report.pdf").unlink()

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 410
    assert client.store.get(record.token) is None


async def test_head_agrees_with_get(client) -> None:
    """The frontend pre-checks with HEAD so a failed download becomes a toast
    instead of a blank tab."""
    record = _register(client.store, client.tmp_path)

    ok = await client.head("/files/download", params={"token": record.token})
    missing = await client.head("/files/download", params={"token": "nope"})

    assert ok.status == 200
    assert missing.status == 410


async def test_archive_zips_every_requested_token(client) -> None:
    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")
    two = _register(client.store, client.tmp_path, "b.txt", b"BBB")

    res = await client.get("/files/download-archive", params=[("token", one.token), ("token", two.token)])

    assert res.status == 200
    archive = zipfile.ZipFile(BytesIO(await res.read()))
    assert sorted(archive.namelist()) == ["a.txt", "b.txt"]
    assert archive.read("a.txt") == b"AAA"


async def test_archive_skips_missing_members(client) -> None:
    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")

    res = await client.get("/files/download-archive", params=[("token", one.token), ("token", "nope")])

    assert res.status == 200
    assert zipfile.ZipFile(BytesIO(await res.read())).namelist() == ["a.txt"]


async def test_archive_with_no_valid_member_is_410(client) -> None:
    res = await client.get("/files/download-archive", params={"token": "nope"})
    assert res.status == 410


async def test_non_ascii_filename_survives_a_latin1_header_encode(client) -> None:
    """The service proxy relays this header through Starlette, which encodes
    header values as latin-1: a raw UTF-8 filename made it raise and turned a
    valid download into a 500. RFC 6266 keeps the real name percent-encoded."""
    record = _register(client.store, client.tmp_path, "季度报告.pdf", b"CJK")

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 200
    assert await res.read() == b"CJK"
    disposition = res.headers["Content-Disposition"]
    disposition.encode("latin-1")
    assert "filename*=UTF-8''%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf" in disposition
    assert 'filename="____.pdf"' in disposition


async def test_control_characters_in_a_filename_are_stripped(client) -> None:
    """CR/LF are legal in a Linux filename but forbidden in a header value;
    stripping them here beats letting aiohttp's validator answer with a 500."""
    record = _register(client.store, client.tmp_path, "re\rport\n.txt", b"CRLF")

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 200
    assert res.headers["Content-Disposition"] == 'attachment; filename="report.txt"'


async def test_archive_head_agrees_with_get(client) -> None:
    """The frontend pre-checks "Download all" with HEAD too, so the branch that
    skips the zip build must answer with the same status."""
    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")

    ok = await client.head("/files/download-archive", params={"token": one.token})
    missing = await client.head("/files/download-archive", params={"token": "nope"})

    assert ok.status == 200
    assert ok.headers["Content-Type"] == "application/zip"
    assert await ok.read() == b""
    assert missing.status == 410


async def test_archive_disambiguates_duplicate_basenames(client) -> None:
    """Two deliverables from different directories share a basename; a plain
    arcname would collapse them and silently lose one on extraction."""
    store, root = client.store, client.tmp_path
    records = []
    for day, body in (("2026-07-29", b"OLD"), ("2026-07-30", b"NEW")):
        folder = root / day
        folder.mkdir()
        (folder / "report.pdf").write_bytes(body)
        records.append(
            store.register(
                path=str(folder / "report.pdf"),
                name="report.pdf",
                media_type="application/pdf",
                size=len(body),
                conversation="web:s1",
            )
        )

    res = await client.get("/files/download-archive", params=[("token", r.token) for r in records])

    assert res.status == 200
    archive = zipfile.ZipFile(BytesIO(await res.read()))
    assert archive.namelist() == ["report.pdf", "report (2).pdf"]
    assert archive.read("report.pdf") == b"OLD"
    assert archive.read("report (2).pdf") == b"NEW"


async def test_archive_temp_file_is_removed_after_streaming(client) -> None:
    """The zip is built off the event loop now; the cleanup that runs on every
    exit path must still leave nothing behind."""
    from raven.config.paths import get_cache_dir

    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")
    tmp_dir = get_cache_dir() / "deliverables"
    before = set(tmp_dir.glob("*.zip")) if tmp_dir.exists() else set()

    res = await client.get("/files/download-archive", params={"token": one.token})

    assert res.status == 200
    assert await res.read()
    assert set(tmp_dir.glob("*.zip")) == before


async def test_an_unregistered_route_still_answers_404(client) -> None:
    """The other half of the 404/410 split. A gateway whose code predates these
    routes answers every download with the router's own 404, and a caller that
    reads 404 as "gone" tells the user their file expired when the file is
    sitting on disk untouched -- exactly the misdiagnosis this split prevents."""
    res = await client.get("/files/nonexistent")
    assert res.status == 404


async def test_resolve_download_drops_stale_entry(tmp_path) -> None:
    store = DeliverableStore(tmp_path / "deliverables.json")
    record = _register(store, tmp_path)
    (tmp_path / "report.pdf").unlink()

    assert resolve_download(store, record.token) is None
    assert store.get(record.token) is None
