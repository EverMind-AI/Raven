"""Tests for the Bug Report Package uploader (`raven.trajectory.upload`)."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.trajectory import bugreport as breport
from raven.trajectory import receiver as rcv
from raven.trajectory import upload as tupload
from tests.bug_report_receiver_stub import clear_proxy_env
from tests.test_trajectory_bugreport import _full_flow, _valid_payload
from tests.test_trajectory_receiver import DIFFERENT, DIFFERENT_SHA, OTHER_ID, PACKAGE, ROOT, SHA

GOOD_RECEIPT = {
    "receipt_id": "rc-0123456789ab",
    "report_id": ROOT,
    "sha256": SHA,
    "size_bytes": len(PACKAGE),
    "received_at": "2026-10-12T00:00:00Z",
    "duplicate": False,
}


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    clear_proxy_env(monkeypatch)


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    return state


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch):
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))


@pytest.fixture
async def receiver_url(tmp_path):
    server = TestServer(rcv.build_app(rcv.ReceiverConfig(save_dir=tmp_path / "inbox")))
    await server.start_server(**rcv.RUNNER_OPTIONS)
    try:
        yield f"http://{server.host}:{server.port}"
    finally:
        await server.close()


class _Fake:
    """A receiver-shaped endpoint whose response is whatever the test says."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    async def handle(self, request):
        body = await request.read()
        self.requests.append((dict(request.headers), body))
        return await self.respond(request)


async def _fake_server(respond):
    fake = _Fake(respond)
    app = web.Application()
    app.router.add_post(rcv.ENDPOINT_PATH, fake.handle)
    server = TestServer(app)
    await server.start_server(auto_decompress=False)
    return server, f"http://{server.host}:{server.port}", fake


def _json_response(status, payload, **headers):
    async def respond(_request):
        return web.Response(status=status, text=json.dumps(payload), content_type="application/json", headers=headers)

    return respond


def _package_file(tmp_path, body=PACKAGE, name="pkg.tar.gz"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


async def _upload(path, **kw):
    kw.setdefault("report_id", ROOT)
    kw.setdefault("sha256", SHA)
    return await tupload.upload_package_async(path, **kw)


def _dead_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# --- where a package goes -------------------------------------------------------


def test_upload_url_prefers_the_environment(monkeypatch):
    monkeypatch.setattr(tupload, "DEFAULT_UPLOAD_URL", "https://built-in.example/")
    assert tupload.upload_url({}) == "https://built-in.example"
    assert tupload.upload_url({"RAVEN_BUGREPORT_URL": ""}) == ""
    assert tupload.upload_url({"RAVEN_BUGREPORT_URL": " http://127.0.0.1:1/ "}) == "http://127.0.0.1:1"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://ops.example/x/", True),
        ("http://127.0.0.1:1", True),
        ("http://localhost:8080", True),
        ("http://10.1.2.3", False),
        ("ftp://ops.example", False),
        ("ops.example.com", False),
        ("", False),
        ("https://ops.example/base?token=x", False),
        ("https://ops.example/base#frag", False),
        ("https://ops.example/base?", False),
        ("https://user:secret@ops.example/base", False),
        ("https://user@ops.example/base", False),
        ("https://@ops.example/base", False),
    ],
)
def test_validate_upload_url(url, ok):
    if ok:
        assert tupload.validate_upload_url(url, environ={}) == url.rstrip("/")
    else:
        with pytest.raises(tupload.UploadConfigError):
            tupload.validate_upload_url(url, environ={})


def test_allow_http_opens_the_lan_but_not_nonsense():
    env = {"RAVEN_BUGREPORT_ALLOW_HTTP": "1"}
    assert tupload.validate_upload_url("http://10.1.2.3/", environ=env) == "http://10.1.2.3"
    with pytest.raises(tupload.UploadConfigError):
        tupload.validate_upload_url("not a url", environ=env)


@pytest.mark.parametrize("allow_http", [False, True])
def test_credentials_in_the_url_are_refused_without_being_echoed(allow_http):
    env = {"RAVEN_BUGREPORT_ALLOW_HTTP": "1"} if allow_http else {}
    for url in (
        "http://user:secret@10.1.2.3/base",
        "http://user:secret@127.0.0.1/base",
        "https://user:secret@ops.example",
    ):
        with pytest.raises(tupload.UploadConfigError) as exc:
            tupload.validate_upload_url(url, environ=env)
        assert "credentials" in str(exc.value) and "secret" not in str(exc.value)


def test_query_and_fragment_are_refused_so_the_endpoint_path_is_never_swallowed():
    env = {"RAVEN_BUGREPORT_ALLOW_HTTP": "1"}
    for url in ("http://10.1.2.3/base?token=x", "http://10.1.2.3/base#frag", "https://ops.example/base?a=1#b"):
        with pytest.raises(tupload.UploadConfigError, match="query or fragment"):
            tupload.validate_upload_url(url, environ=env)


def test_upload_target_never_names_credentials():
    assert tupload.upload_target("https://user:secret@ops.example:8443/base/") == "https://ops.example:8443"
    assert tupload.upload_target("http://[::1]:18795/x") == "http://[::1]:18795"


BROKEN_URLS = ["https://[broken", "https://ops.example:notaport", "https://ops.example:99999", "https://ops.example:0"]


@pytest.mark.parametrize("url", BROKEN_URLS)
@pytest.mark.parametrize("allow_http", [False, True])
def test_structurally_broken_urls_are_config_errors(url, allow_http):
    env = {"RAVEN_BUGREPORT_ALLOW_HTTP": "1"} if allow_http else {}
    with pytest.raises(tupload.UploadConfigError):
        tupload.validate_upload_url(url, environ=env)


def test_resolve_upload_url_three_states(monkeypatch):
    monkeypatch.setattr(tupload, "DEFAULT_UPLOAD_URL", "")
    assert tupload.resolve_upload_url({}) == ""
    assert tupload.resolve_upload_url({"RAVEN_BUGREPORT_URL": ""}) == ""
    assert (
        tupload.resolve_upload_url({"RAVEN_BUGREPORT_URL": "https://ops.example/base/"}) == "https://ops.example/base"
    )
    with pytest.raises(tupload.UploadConfigError):
        tupload.resolve_upload_url({"RAVEN_BUGREPORT_URL": "http://10.1.2.3"})


def test_upload_target_names_only_the_origin():
    assert tupload.upload_target("https://h:8443/p?q=1") == "https://h:8443"


# --- against the real receiver --------------------------------------------------


async def test_real_package_uploads_and_repeats(receiver_url, state, workspace, tmp_path):
    _dir, record = _full_flow(state, workspace)
    package = Path(record["package"]["path"])
    kw = {"report_id": record["report_id"], "sha256": record["package"]["sha256"], "base_url": receiver_url}
    receipt = await tupload.upload_package_async(package, **kw)
    assert (receipt.report_id, receipt.sha256, receipt.size_bytes) == (
        record["report_id"],
        record["package"]["sha256"],
        package.stat().st_size,
    )
    assert receipt.duplicate is False and rcv.RECEIPT_ID.fullmatch(receipt.receipt_id)
    again = await tupload.upload_package_async(package, **kw)
    assert again.duplicate is True and again.receipt_id == receipt.receipt_id
    sidecar = json.loads(next((tmp_path / "inbox").glob(f"*/{record['report_id']}.json")).read_text(encoding="utf-8"))
    assert sidecar["remote"]["user_agent"].startswith("raven/")


async def test_server_rejections_are_not_retryable(receiver_url, tmp_path):
    with pytest.raises(tupload.UploadError) as exc:
        await _upload(_package_file(tmp_path, PACKAGE + b"x"), base_url=receiver_url)
    assert (exc.value.status, exc.value.retryable) == (400, False)
    assert str(exc.value).startswith("server rejected (400 digest_mismatch)")
    await _upload(_package_file(tmp_path), base_url=receiver_url)
    with pytest.raises(tupload.UploadError) as exc:
        await _upload(_package_file(tmp_path, DIFFERENT, "other.tar.gz"), sha256=DIFFERENT_SHA, base_url=receiver_url)
    assert (exc.value.status, exc.value.retryable) == (409, False)


async def test_unreachable_receiver_is_retryable(tmp_path):
    with pytest.raises(tupload.UploadError) as exc:
        await _upload(_package_file(tmp_path), base_url=_dead_url())
    assert exc.value.retryable is True and "could not reach" in str(exc.value)


# --- the total deadline ---------------------------------------------------------


async def _raw_server(handler, *, rcvbuf=None):
    sock = socket.socket()
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = await asyncio.start_server(handler, sock=sock)
    return server, sock.getsockname()[1]


async def _assert_deadline(coro, *, total=0.3):
    started = time.monotonic()
    with pytest.raises(tupload.UploadError) as exc:
        await coro
    elapsed = time.monotonic() - started
    assert exc.value.retryable is True and f"exceeded {total}s" in str(exc.value)
    assert 0.25 <= elapsed < 1.0, elapsed


async def test_slow_response_headers_hit_the_deadline(tmp_path):
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            await writer.drain()
            while True:
                writer.write(b"a")
                await writer.drain()
                await asyncio.sleep(0.05)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            closed.set()
            writer.close()

    server, port = await _raw_server(handler)
    try:
        await _assert_deadline(
            _upload(_package_file(tmp_path), base_url=f"http://127.0.0.1:{port}", io_timeout=1.0, total_timeout=0.3)
        )
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()


async def test_dripping_response_body_hits_the_deadline(tmp_path):
    disconnected = asyncio.Event()

    async def respond(request):
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        try:
            for _ in range(60):
                await resp.write(b" ")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            # The test server cancels a handler whose client went away.
            disconnected.set()
            raise
        except ConnectionError:
            disconnected.set()
        return resp

    server, url, _fake = await _fake_server(respond)
    try:
        await _assert_deadline(_upload(_package_file(tmp_path), base_url=url, io_timeout=1.0, total_timeout=0.3))
        await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        await server.close()


async def test_blocked_upload_hits_the_deadline(tmp_path, monkeypatch):
    go = asyncio.Event()
    done = asyncio.Event()
    seen = {}

    async def handler(reader, writer):
        await go.wait()
        seen["bytes"] = len(await reader.read(-1))
        done.set()
        writer.close()

    yielded = {"n": 0}
    real_body = tupload._body

    async def counting(package):
        async for piece in real_body(package):
            yielded["n"] += len(piece)
            yield piece

    monkeypatch.setattr(tupload, "_body", counting)
    size = 16 * 1024 * 1024
    big = _package_file(tmp_path, b"\0" * size, "big.tar.gz")
    server, port = await _raw_server(handler, rcvbuf=16 * 1024)
    try:
        await _assert_deadline(
            _upload(big, sha256="0" * 64, base_url=f"http://127.0.0.1:{port}", io_timeout=1.0, total_timeout=0.3)
        )
        assert 0 < yielded["n"] < size
        go.set()
        await asyncio.wait_for(done.wait(), 5)
        assert seen["bytes"] < size
    finally:
        server.close()
        await server.wait_closed()


# --- what counts as a receipt ---------------------------------------------------


@pytest.mark.parametrize(
    "spec,substring",
    [
        ("not-json", "not JSON"),
        ("array", "not a JSON object"),
        ("missing-receipt-id", "receipt_id"),
        ("bad-receipt-id", "receipt_id"),
        ("wrong-report-id", "report_id"),
        ("wrong-sha", "sha256"),
        ("wrong-size", "size_bytes"),
        ("bool-size", "size_bytes"),
        ("duplicate-vs-status", "duplicate flag"),
        ("text-plain", "not JSON"),
        ("huge", "larger than"),
    ],
)
async def test_malformed_receipts_are_refused(tmp_path, spec, substring):
    payload = dict(GOOD_RECEIPT)
    status, content_type, body = 201, "application/json", None
    if spec == "not-json":
        body = "nope"
    elif spec == "array":
        body = "[]"
    elif spec == "missing-receipt-id":
        del payload["receipt_id"]
    elif spec == "bad-receipt-id":
        payload["receipt_id"] = "x"
    elif spec == "wrong-report-id":
        payload["report_id"] = OTHER_ID
    elif spec == "wrong-sha":
        payload["sha256"] = "0" * 64
    elif spec == "wrong-size":
        payload["size_bytes"] += 1
    elif spec == "bool-size":
        payload["size_bytes"] = True
    elif spec == "duplicate-vs-status":
        payload["duplicate"] = True
    elif spec == "text-plain":
        content_type = "text/plain"
    elif spec == "huge":
        payload["padding"] = "x" * (70 * 1024)
    text = body if body is not None else json.dumps(payload)

    async def respond(_request):
        return web.Response(status=status, text=text, content_type=content_type)

    server, url, _fake = await _fake_server(respond)
    try:
        with pytest.raises(tupload.UploadError) as exc:
            await _upload(_package_file(tmp_path), base_url=url)
    finally:
        await server.close()
    assert exc.value.retryable is False and substring in str(exc.value)
    assert str(exc.value).startswith("malformed receipt") or "larger than" in str(exc.value)


@pytest.mark.parametrize("status", [200, 500])
async def test_compressed_responses_are_refused_unexpanded(tmp_path, monkeypatch, status):
    body = gzip.compress(b"\0" * (1 << 20), mtime=0)

    async def respond(_request):
        return web.Response(
            status=status, body=body, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"}
        )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the decoder must never run")

    for name in ("aiter_bytes", "aread", "iter_bytes", "read"):
        monkeypatch.setattr(httpx.Response, name, _forbidden)
    server, url, _fake = await _fake_server(respond)
    try:
        with pytest.raises(tupload.UploadError) as exc:
            await _upload(_package_file(tmp_path), base_url=url)
    finally:
        await server.close()
    assert exc.value.retryable is False and "compressed" in str(exc.value)


DEEP_JSON = b"[" * 10_000 + b"0" + b"]" * 10_000


@pytest.mark.parametrize(
    "status,retryable,needle", [(201, False, "malformed receipt: not JSON"), (500, True, "server returned 500")]
)
async def test_deeply_nested_json_stays_an_upload_error(tmp_path, status, retryable, needle):
    async def respond(_request):
        return web.Response(status=status, body=DEEP_JSON, content_type="application/json")

    server, url, _fake = await _fake_server(respond)
    try:
        with pytest.raises(tupload.UploadError) as exc:
            await _upload(_package_file(tmp_path), base_url=url)
    finally:
        await server.close()
    assert exc.value.retryable is retryable and needle in str(exc.value)


async def test_a_deeply_nested_receipt_is_recorded_as_a_failure(state, workspace):
    async def respond(_request):
        return web.Response(status=201, body=DEEP_JSON, content_type="application/json")

    record_dir, _ = _full_flow(state, workspace)
    server, url, _fake = await _fake_server(respond)
    try:
        with pytest.raises(tupload.UploadError):
            await asyncio.to_thread(tupload.upload_record, record_dir, base_url=url)
    finally:
        await server.close()
    upload = breport.load_record(record_dir)["upload"]
    assert upload["state"] == "failed" and "malformed receipt" in upload["error"]


async def test_redirects_and_error_statuses(tmp_path):
    async def redirect(_request):
        return web.Response(status=302, headers={"Location": "https://elsewhere.example/"})

    async def plain_500(_request):
        return web.Response(status=500, text="boom", content_type="text/plain")

    for respond, retryable, needle in (
        (redirect, False, "redirected to https://elsewhere.example/"),
        (_json_response(503, {"error": "busy", "message": "slots"}), True, "server rejected (503 busy): slots"),
        (plain_500, True, "server returned 500"),
    ):
        server, url, _fake = await _fake_server(respond)
        try:
            with pytest.raises(tupload.UploadError) as exc:
                await _upload(_package_file(tmp_path), base_url=url)
        finally:
            await server.close()
        assert exc.value.retryable is retryable and needle in str(exc.value), str(exc.value)


# --- the record -----------------------------------------------------------------


async def test_upload_record_writes_the_outcome(receiver_url, state, workspace):
    record_dir, _ = _full_flow(state, workspace)
    record, receipt = await asyncio.to_thread(tupload.upload_record, record_dir, base_url=receiver_url)
    upload = record["upload"]
    assert upload["state"] == "uploaded" and upload["receipt"] == receipt.receipt_id
    assert upload["uploaded_at"].endswith("Z") and upload["error"] == "" and upload["issue_url"] == ""
    assert breport.load_record(record_dir)["upload"] == upload


async def test_failed_upload_is_recorded_until_one_succeeds(receiver_url, state, workspace):
    record_dir, _ = _full_flow(state, workspace)
    dead = _dead_url()
    with pytest.raises(tupload.UploadError):
        await asyncio.to_thread(tupload.upload_record, record_dir, base_url=dead)
    failed = breport.load_record(record_dir)
    assert failed["status"] == "local_ready" and failed["upload"]["state"] == "failed" and failed["upload"]["error"]
    _record, receipt = await asyncio.to_thread(tupload.upload_record, record_dir, base_url=receiver_url)
    assert breport.load_record(record_dir)["upload"]["state"] == "uploaded"
    with pytest.raises(tupload.UploadError):
        await asyncio.to_thread(tupload.upload_record, record_dir, base_url=dead)
    kept = breport.load_record(record_dir)["upload"]
    assert kept["state"] == "uploaded" and kept["receipt"] == receipt.receipt_id


def test_upload_record_refuses_without_a_package(tmp_path):
    record_dir = tmp_path / "br-20260904-abcdef"
    record_dir.mkdir()
    breport.save_record(record_dir, _valid_payload(status="failed"))
    with pytest.raises(tupload.UploadError) as exc:
        tupload.upload_record(record_dir, base_url="http://127.0.0.1:1")
    assert "has no package" in str(exc.value) and exc.value.retryable is False
    assert breport.load_record(record_dir)["upload"]["state"] == ""


def test_upload_record_refuses_a_changed_package(state, workspace):
    record_dir, record = _full_flow(state, workspace)
    Path(record["package"]["path"]).write_bytes(b"changed")
    with pytest.raises(tupload.UploadError) as exc:
        tupload.upload_record(record_dir, base_url="http://127.0.0.1:1")
    assert "changed since it was created" in str(exc.value)
    assert breport.load_record(record_dir)["upload"]["state"] == ""


def test_a_damaged_record_is_the_callers_problem(tmp_path):
    record_dir = tmp_path / "br-20260904-abcdef"
    record_dir.mkdir()
    (record_dir / "record.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        tupload.upload_record(record_dir, base_url="http://127.0.0.1:1")


async def test_the_sync_facade_refuses_a_running_loop(tmp_path):
    with pytest.raises(RuntimeError):
        tupload.upload_package(_package_file(tmp_path), report_id=ROOT, sha256=SHA, base_url="http://127.0.0.1:1")


def test_the_module_pulls_in_no_terminal_stack():
    """The module adds nothing from the terminal stack beyond what httpx itself loads.

    httpx imports rich and click for its optional command line, so the check
    is a delta over a bare ``import httpx``, plus the module's own imports.
    """
    code = (
        "import sys, httpx, ast, pathlib; before = set(sys.modules); "
        "import raven.trajectory.upload as m; "
        "added = sorted(n for n in set(sys.modules) - before "
        "if n.split('.')[0] in ('typer', 'rich', 'questionary', 'prompt_toolkit')); "
        "tree = ast.parse(pathlib.Path(m.__file__).read_text()); "
        "own = sorted({(a.name if isinstance(n, ast.Import) else n.module).split('.')[0] "
        "for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) "
        "for a in (n.names if isinstance(n, ast.Import) else [n])}); "
        "print(added, [x for x in own if x in ('typer', 'rich', 'questionary', 'prompt_toolkit')])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[] []"
