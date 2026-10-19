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
from raven.trajectory.receiver_auth import client_response
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


FAKE_CHALLENGE = "c1.0.fake-challenge"
CHALLENGE_REPLY = json.dumps({"challenge": FAKE_CHALLENGE, "expires_in": 300}).encode()


class _Fake:
    """A receiver-shaped endpoint whose response is whatever the test says."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.challenges = []

    async def handle(self, request):
        body = await request.read()
        self.requests.append((dict(request.headers), body))
        return await self.respond(request)


async def _issue_fake_challenge(_request):
    return web.Response(body=CHALLENGE_REPLY, content_type="application/json")


async def _fake_server(respond, *, method="POST", path=rcv.ENDPOINT_PATH, challenge=_issue_fake_challenge):
    """``respond`` answers ``method path``; ``challenge`` answers the challenge route, unless None."""
    fake = _Fake(respond)
    app = web.Application()
    app.router.add_route(method, path, fake.handle)
    if challenge is not None and path != rcv.CHALLENGE_PATH:
        issuer = _Fake(challenge)
        fake.challenges = issuer.requests
        app.router.add_post(rcv.CHALLENGE_PATH, issuer.handle)
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


SECRET_BEARING_URLS = [
    "https://user:secret@ops.example:notaport",
    "https://user:secret@[broken",
    "https://user:secret@ops.example:99999",
    "https://user:secret@ops.example:0",
    "ftp://user:secret@ops.example",
    "user:secret@ops.example",
    "https://ops.example/base?token=secret",
    "https://ops.example/base#secret",
    "https://ops.example/base?",
    "http://user:secret@10.1.2.3/base?token=secret",
    "https://user:secret?x@ops.example",
    "https://user:secret#x@ops.example",
    "https://user:secret?x@ops.example/p?q=secret",
    "https://ops.example?token=secret@x",
]


@pytest.mark.parametrize("url", SECRET_BEARING_URLS)
@pytest.mark.parametrize("allow_http", [False, True])
def test_no_rejected_url_error_echoes_a_secret(url, allow_http):
    """Whatever check refuses the URL -- a parse failure before userinfo is even
    available, the scheme, the port range, the query rule -- the message shows
    at most scheme, host, port and path."""
    env = {"RAVEN_BUGREPORT_ALLOW_HTTP": "1"} if allow_http else {}
    with pytest.raises(tupload.UploadConfigError) as exc:
        tupload.validate_upload_url(url, environ=env)
    message = str(exc.value)
    assert "secret" not in message and "token" not in message and "user" not in message
    assert tupload.UPLOAD_URL_ENV in message


def test_rejected_url_errors_still_name_the_host():
    with pytest.raises(tupload.UploadConfigError, match=r"'https://ops\.example:notaport'"):
        tupload.validate_upload_url("https://user:secret@ops.example:notaport", environ={})
    with pytest.raises(tupload.UploadConfigError, match=r"'https://ops\.example/base\?\.\.\.'"):
        tupload.validate_upload_url("https://ops.example/base?token=secret", environ={})


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


def _after_challenges(handler):
    """Answer every challenge request on a connection, then give ``handler`` the first other request.

    Whether the client reuses the connection for the upload or opens a new
    one, the behaviour under test meets the upload, not the challenge.
    """

    async def serve(reader, writer):
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (ConnectionError, asyncio.IncompleteReadError):
                writer.close()
                return
            if not head.startswith(f"POST {rcv.CHALLENGE_PATH} ".encode()):
                await handler(head, reader, writer)
                return
            reply = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            writer.write(reply % len(CHALLENGE_REPLY) + CHALLENGE_REPLY)
            await writer.drain()

    return serve


def _request_line(head):
    return head.split(b"\r\n", 1)[0]


async def _assert_deadline(coro, *, total=0.3, limit=2.5):
    """The total deadline, not an I/O timeout or the server's pace, is what ended the upload.

    The message names the path that raised; the elapsed time only has to show
    the request was cut short of the alternatives (an io_timeout of 1 s, a
    3 s drip), with room for a loaded CI runner to deliver the cancellation late.
    """
    started = time.monotonic()
    with pytest.raises(tupload.UploadError) as exc:
        await coro
    elapsed = time.monotonic() - started
    assert exc.value.retryable is True and f"exceeded {total}s" in str(exc.value)
    assert 0.25 <= elapsed < limit, elapsed


async def test_slow_response_headers_hit_the_deadline(tmp_path):
    closed = asyncio.Event()
    seen = []

    async def handler(head, _reader, writer):
        seen.append(_request_line(head))
        try:
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

    server, port = await _raw_server(_after_challenges(handler))
    try:
        await _assert_deadline(
            _upload(_package_file(tmp_path), base_url=f"http://127.0.0.1:{port}", io_timeout=1.0, total_timeout=0.3)
        )
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
    assert seen == [f"POST {rcv.ENDPOINT_PATH} HTTP/1.1".encode()]


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

    async def handler(head, reader, writer):
        seen["request"] = _request_line(head)
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
    server, port = await _raw_server(_after_challenges(handler), rcvbuf=16 * 1024)
    try:
        await _assert_deadline(
            _upload(big, sha256="0" * 64, base_url=f"http://127.0.0.1:{port}", io_timeout=1.0, total_timeout=0.3)
        )
        assert 0 < yielded["n"] < size
        go.set()
        await asyncio.wait_for(done.wait(), 5)
        assert seen["bytes"] < size
        assert seen["request"] == f"POST {rcv.ENDPOINT_PATH} HTTP/1.1".encode()
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


# --- probing the receiver -------------------------------------------------------


HELLO = {"service": rcv.SERVICE_NAME, "protocol": rcv.PROTOCOL_VERSION}


async def _fake_hello(respond, path=rcv.HELLO_PATH):
    return await _fake_server(respond, method="GET", path=path)


async def _probe_error(respond):
    server, url, fake = await _fake_hello(respond)
    try:
        with pytest.raises(tupload.ProbeError) as exc:
            await tupload.probe_receiver_async(url)
    finally:
        await server.close()
    return exc.value, fake


async def test_probe_finds_a_real_receiver(receiver_url):
    await tupload.probe_receiver_async(receiver_url)


async def test_probe_follows_a_path_prefix():
    server, url, fake = await _fake_hello(_json_response(200, HELLO), path="/prefix" + rcv.HELLO_PATH)
    try:
        await tupload.probe_receiver_async(f"{url}/prefix/")
    finally:
        await server.close()
    assert len(fake.requests) == 1


async def test_probe_of_a_dead_port_says_so():
    with pytest.raises(tupload.ProbeError) as exc:
        await tupload.probe_receiver_async(_dead_url())
    assert "could not reach" in exc.value.reason and exc.value.status is None


def _respond(status, body, *, content_type="application/json", **headers):
    async def respond(_request):
        return web.Response(status=status, body=body, headers={"Content-Type": content_type, **headers})

    return respond


NOT_A_RECEIVER = "does not answer as a Raven bug report receiver"


@pytest.mark.parametrize(
    "respond,status,needle",
    [
        (_json_response(404, {"error": "not_found"}), 404, f"{NOT_A_RECEIVER} (HTTP 404 on /v1/hello)"),
        (_respond(200, b"<html></html>", content_type="text/html"), 200, NOT_A_RECEIVER),
        (_respond(200, b"[]"), 200, NOT_A_RECEIVER),
        (_json_response(200, {"service": "other", "protocol": 1}), 200, NOT_A_RECEIVER),
        (_json_response(200, {**HELLO, "protocol": "1"}), 200, NOT_A_RECEIVER),
        (_json_response(200, {**HELLO, "protocol": True}), 200, NOT_A_RECEIVER),
        (_json_response(200, {**HELLO, "protocol": 2}), 200, "speaks protocol 2; this Raven speaks 1"),
        (_respond(200, b" " * (tupload.MAX_RESPONSE_BYTES + 1)), 200, "larger than 64 KiB"),
        (_respond(200, gzip.compress(json.dumps(HELLO).encode()), **{"Content-Encoding": "gzip"}), 200, "compressed"),
    ],
    ids=["404", "html", "array", "service", "protocol-str", "protocol-bool", "protocol-2", "oversized", "gzip"],
)
async def test_probe_refuses_what_is_not_a_receiver(respond, status, needle):
    error, _fake = await _probe_error(respond)
    assert error.status == status and needle in error.reason, error.reason


async def test_probe_does_not_follow_redirects():
    error, fake = await _probe_error(_respond(302, b"", Location="https://elsewhere.example/"))
    assert error.status == 302 and "redirected to https://elsewhere.example/" in error.reason
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "location,shown",
    [
        ("https://user:pw-secret@sso.example/login", "sso.example/login"),
        ("https://sso.example/login?ticket=tk-secret", "https://sso.example/login..."),
        ("https://sso.example/login#code=cd-secret", "https://sso.example/login..."),
        ("/login?ticket=tk-secret", "/login..."),
        ("//user:pw-secret@sso.example/login?ticket=tk-secret", "sso.example/login..."),
        ("\\\\user:pw-secret@sso.example/login", "sso.example/login"),
        ("?ticket=tk-secret", ""),
        ("", ""),
        ("   ", ""),
        ("  /login  ", "/login"),
    ],
)
def test_redirect_target_keeps_only_what_is_safe_to_show(location, shown):
    assert tupload._redirect_target(location) == shown


def test_redirect_target_is_bounded():
    assert tupload._redirect_target("/" + "a" * 500) == "/" + "a" * 199


@pytest.mark.parametrize(
    "location,needle",
    [
        ("https://user:pw-secret@sso.example/login", "redirected to sso.example/login;"),
        ("https://sso.example/login?ticket=tk-secret", "redirected to https://sso.example/login...;"),
        ("https://sso.example/login#code=cd-secret", "redirected to https://sso.example/login...;"),
        ("/login?ticket=tk-secret", "redirected to /login...;"),
        ("//user:pw-secret@sso.example/login?ticket=tk-secret", "redirected to sso.example/login...;"),
        ("\\\\user:pw-secret@sso.example/login", "redirected to sso.example/login;"),
        ("?ticket=tk-secret", "answered with a redirect;"),
        (None, "answered with a redirect;"),
    ],
)
async def test_probe_redirects_never_echo_secrets(location, needle):
    headers = {} if location is None else {"Location": location}
    error, fake = await _probe_error(_respond(302, b"", **headers))
    assert error.status == 302 and "secret" not in error.reason, error.reason
    assert needle in error.reason and "point RAVEN_BUGREPORT_URL at the receiver itself" in error.reason
    assert len(fake.requests) == 1


async def _assert_probe_deadline(coro, *, timeout=0.3, limit=2.5):
    """The probe's own deadline ended it, well before the server's 3 s pace would have."""
    started = time.monotonic()
    with pytest.raises(tupload.ProbeError) as exc:
        await coro
    elapsed = time.monotonic() - started
    assert f"within {timeout}s" in exc.value.reason
    assert 0.25 <= elapsed < limit, elapsed


async def test_probe_slow_headers_hit_the_deadline():
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            await writer.drain()
            for _ in range(60):
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
        await _assert_probe_deadline(tupload.probe_receiver_async(f"http://127.0.0.1:{port}", timeout=0.3))
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()


def _dripping_hello(disconnected):
    async def respond(request):
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        try:
            for _ in range(60):
                await resp.write(b" ")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            disconnected.set()
            raise
        except ConnectionError:
            disconnected.set()
        return resp

    return respond


async def test_probe_dripping_body_hits_the_deadline():
    disconnected = asyncio.Event()
    server, url, _fake = await _fake_hello(_dripping_hello(disconnected))
    try:
        await _assert_probe_deadline(tupload.probe_receiver_async(url, timeout=0.3))
        await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        await server.close()


async def test_sync_probe_obeys_the_deadline():
    disconnected = asyncio.Event()
    server, url, _fake = await _fake_hello(_dripping_hello(disconnected))
    try:
        await _assert_probe_deadline(asyncio.to_thread(tupload.probe_receiver, url, timeout=0.3))
    finally:
        await server.close()


async def test_sync_probe_refuses_a_running_loop():
    with pytest.raises(RuntimeError):
        tupload.probe_receiver("http://127.0.0.1:1")


# --- the upload challenge -------------------------------------------------------


async def test_the_upload_answers_a_fresh_challenge(tmp_path):
    server, url, fake = await _fake_server(_json_response(201, GOOD_RECEIPT))
    try:
        await _upload(_package_file(tmp_path), base_url=url)
    finally:
        await server.close()
    assert [body for _headers, body in fake.challenges] == [b""]
    headers = fake.requests[0][0]
    assert headers[rcv.CHALLENGE_HEADER] == FAKE_CHALLENGE
    expected = client_response(FAKE_CHALLENGE, report_id=ROOT, sha256=SHA, size_bytes=len(PACKAGE))
    assert headers[rcv.RESPONSE_HEADER] == expected


async def _upload_against(tmp_path, *, challenge, respond=None):
    server, url, fake = await _fake_server(respond or _json_response(201, GOOD_RECEIPT), challenge=challenge)
    try:
        with pytest.raises(tupload.UploadError) as exc:
            await _upload(_package_file(tmp_path), base_url=url)
    finally:
        await server.close()
    return exc.value, fake


@pytest.mark.parametrize(
    "challenge,retryable,needle",
    [
        (_json_response(404, {"error": "not_found"}), False, "does not issue challenges (HTTP 404 on /v1/challenge)"),
        (_json_response(500, {"error": "internal"}), True, "server rejected (500 internal)"),
        (_json_response(503, {"error": "busy"}), True, "server rejected (503 busy)"),
        (_json_response(429, {"error": "rate_limited"}), True, "server rejected (429 rate_limited)"),
        (_json_response(408, {"error": "timeout"}), True, "server rejected (408 timeout)"),
        (_respond(200, b"<html></html>", content_type="text/html"), False, "malformed challenge"),
        (_json_response(200, []), False, "malformed challenge"),
        (_json_response(200, {"challenge": 1}), False, "malformed challenge"),
        (_json_response(200, {"challenge": "has space"}), False, "malformed challenge"),
        (_json_response(200, {"challenge": "a" * 257}), False, "malformed challenge"),
        (_respond(200, gzip.compress(CHALLENGE_REPLY), **{"Content-Encoding": "gzip"}), False, "compressed"),
    ],
    ids=["404", "500", "503", "429", "408", "html", "array", "number", "space", "too-long", "gzip"],
)
async def test_a_failed_challenge_stops_before_the_upload(tmp_path, challenge, retryable, needle):
    error, fake = await _upload_against(tmp_path, challenge=challenge)
    assert error.retryable is retryable and needle in error.reason, error.reason
    assert fake.requests == []


@pytest.mark.parametrize(
    "location",
    ["https://user:pw-secret@sso.example/login?ticket=tk-secret", "//user:pw-secret@sso.example/x", None],
    ids=["absolute", "network-path", "missing"],
)
async def test_a_redirected_challenge_names_no_secret(tmp_path, location):
    headers = {} if location is None else {"Location": location}
    error, fake = await _upload_against(tmp_path, challenge=_respond(302, b"", **headers))
    assert error.retryable is False and error.status == 302
    assert "secret" not in error.reason and "point RAVEN_BUGREPORT_URL at the receiver itself" in error.reason
    assert fake.requests == [] and len(fake.challenges) == 1


@pytest.mark.parametrize(
    "payload,retryable,needles",
    [
        (
            {"error": "unauthorized", "message": "this receiver only takes uploads from Raven"},
            False,
            ["(401 unauthorized: this receiver only takes uploads from Raven)", "may be different versions"],
        ),
        ({"error": "challenge_expired", "message": "gone"}, True, ["401 challenge_expired", "try again"]),
        ([], False, ["(401)", "may be different versions"]),
    ],
    ids=["unauthorized", "expired", "no-code"],
)
async def test_a_refused_answer_says_whether_to_retry(tmp_path, payload, retryable, needles):
    error, _fake = await _upload_against(
        tmp_path, challenge=_issue_fake_challenge, respond=_json_response(401, payload)
    )
    assert error.status == 401 and error.retryable is retryable
    assert all(needle in error.reason for needle in needles), error.reason


async def test_a_slow_challenge_hits_the_total_deadline(tmp_path):
    async def drip(request):
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        for _ in range(60):
            await resp.write(b" ")
            await asyncio.sleep(0.05)
        return resp

    server, url, fake = await _fake_server(_json_response(201, GOOD_RECEIPT), challenge=drip)
    try:
        await _assert_deadline(_upload(_package_file(tmp_path), base_url=url, io_timeout=1.0, total_timeout=0.3))
    finally:
        await server.close()
    assert fake.requests == []


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
