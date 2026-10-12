"""Sending a Bug Report Package to a Bug Report Receiver — the client side.

This module decides where a package goes, sends it, checks the Upload Receipt
that comes back, and writes the outcome into the report's ``record.json``. It
is deliberately free of any user interface: it never prints, and it imports
nothing from the CLI or the terminal stack, so the ``report-bug`` command, the
interactive browser, and a web page's RPC method all call the same functions
and render the result their own way.

Where a package goes is one string: :data:`DEFAULT_UPLOAD_URL` is what a build
ships (empty in this repository, which means "do not upload"), and the
``RAVEN_BUGREPORT_URL`` environment variable overrides it when set — to a
value, or to an empty string that switches uploading off. A configured URL
must be https, or http on this machine, unless ``RAVEN_BUGREPORT_ALLOW_HTTP``
says plaintext is acceptable (a lab network, a test).

A receipt is accepted only when every field in it says what the request said:
the ids and the digest match, the size is the file's size, and the duplicate
flag agrees with the status code. Anything else — a captive portal's 200, a
proxy's HTML, a receiver that was pointed at the wrong directory — is a
failure that keeps the package on disk, never a success.

The whole exchange runs under one wall-clock deadline that cancels the
request outright. Per-operation socket timeouts alone cannot bound a transfer:
a peer that trickles a byte before each one expires can hold a call open
forever, and that holds whether it is slow to send response headers, slow to
send the body, or slow to read the upload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from raven import __version__
from raven.security.urls import HubTrustError, require_https
from raven.trajectory.bugreport import (
    STATUS_LOCAL_READY,
    UPLOAD_STATE_FAILED,
    UPLOAD_STATE_UPLOADED,
    load_record,
    save_record,
)
from raven.trajectory.receiver import ENDPOINT_PATH, RECEIPT_ID, REPORT_ID_HEADER, SHA256_HEADER

DEFAULT_UPLOAD_URL = ""
UPLOAD_URL_ENV = "RAVEN_BUGREPORT_URL"
ALLOW_HTTP_ENV = "RAVEN_BUGREPORT_ALLOW_HTTP"
MAX_RESPONSE_BYTES = 64 * 1024

_TRUTHY = {"1", "true", "yes", "on"}
_CHUNK = 64 * 1024
_RETRYABLE_STATUSES = (408, 429)
_RECEIPT_FIELDS = ("receipt_id", "report_id", "sha256", "size_bytes", "received_at", "duplicate")


class UploadConfigError(ValueError):
    """The configured upload URL is not one a package may be sent to."""


class UploadError(Exception):
    """One upload attempt failed; ``retryable`` says whether trying again can help."""

    def __init__(self, reason: str, *, status: int | None = None, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class UploadReceipt:
    receipt_id: str
    report_id: str
    sha256: str
    size_bytes: int
    received_at: str
    duplicate: bool


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def upload_url(environ: Mapping[str, str] | None = None) -> str:
    """The configured receiver URL: the environment override when set, else the build default."""
    env = _environ(environ)
    if UPLOAD_URL_ENV in env:
        return env[UPLOAD_URL_ENV].strip().rstrip("/")
    return DEFAULT_UPLOAD_URL.rstrip("/")


def _shown(url: str) -> str:
    """``url`` the way an error message may show it.

    Userinfo and everything from the first ``?`` or ``#`` are cut on the
    string, before any parsing, so a URL that does not even parse cannot
    echo a secret typed into it.
    """
    cut = min((i for i in (url.find("?"), url.find("#")) if i != -1), default=len(url))
    head, tail = url[:cut], url[cut:]
    prefix, sep, rest = head.partition("://")
    if not sep:
        prefix, rest = "", head
    authority, slash, path = rest.partition("/")
    return f"{prefix}{sep}{authority.rpartition('@')[2]}{slash}{path}{tail[:1] + '...' if tail else ''}"


def validate_upload_url(url: str, *, environ: Mapping[str, str] | None = None) -> str:
    """``url`` without its trailing slash, or :class:`UploadConfigError`.

    The structure is checked the way the request will parse it, so a URL
    that passes here can be sent: a malformed host or port must fail before
    anything is collected, not after the package exists. No error text
    carries the URL as typed: credentials and query strings are where secrets
    end up, and the message is printed and recorded.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise UploadConfigError(f"{UPLOAD_URL_ENV} is not a valid URL: {_shown(url)!r} ({exc})") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UploadConfigError(f"{UPLOAD_URL_ENV} must be an absolute http(s) URL: {_shown(url)!r}")
    if port is not None and not 1 <= port <= 65535:
        raise UploadConfigError(f"{UPLOAD_URL_ENV} has a port outside 1-65535: {_shown(url)!r}")
    if parts.username is not None or parts.password is not None:
        raise UploadConfigError(f"{UPLOAD_URL_ENV} must not carry credentials in the URL: {_shown(url)!r}")
    if "?" in url or "#" in url:
        # Checked on the string: urlsplit reports a bare trailing "?" as an
        # empty query, and the endpoint path appended after it would still be
        # swallowed.
        raise UploadConfigError(
            f"{UPLOAD_URL_ENV} must not carry a query or fragment, the client appends the endpoint path: "
            f"{_shown(url)!r}"
        )
    if _environ(environ).get(ALLOW_HTTP_ENV, "").strip().lower() not in _TRUTHY:
        try:
            require_https(url, what=UPLOAD_URL_ENV)
        except HubTrustError as exc:
            # Its text quotes the URL; by now that is scheme, host, port and
            # path only, since everything else was refused above.
            raise UploadConfigError(str(exc)) from exc
    return url.rstrip("/")


def resolve_upload_url(environ: Mapping[str, str] | None = None) -> str:
    """The validated receiver URL to upload to, or "" when uploading is off."""
    url = upload_url(environ)
    return validate_upload_url(url, environ=environ) if url else ""


def upload_target(base_url: str) -> str:
    """What a disclosure line names: the scheme and host, never a path, query or credentials."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _body(package: Path) -> AsyncIterator[bytes]:
    with package.open("rb") as handle:
        while True:
            piece = handle.read(_CHUNK)
            if not piece:
                return
            yield piece


async def _read_bounded(response: httpx.Response) -> bytes:
    # httpx would inflate a compressed body before handing it over, with no
    # cap on the result; a receiver never compresses, so the header alone
    # settles it and the raw bytes are what gets counted.
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise UploadError(
            "the server compressed its response; a receiver never does", status=response.status_code, retryable=False
        )
    body = bytearray()
    async for piece in response.aiter_raw():
        body += piece
        if len(body) > MAX_RESPONSE_BYTES:
            raise UploadError(
                "the server's response is larger than 64 KiB", status=response.status_code, retryable=False
            )
    return bytes(body)


_NOT_JSON = object()


def _parse_json(body: bytes) -> Any:
    """The decoded body, or :data:`_NOT_JSON`; nesting deep enough to overflow counts as not JSON."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _rejection(status: int, body: bytes) -> str:
    payload = _parse_json(body)
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        message = payload.get("message")
        return f"server rejected ({status} {payload['error']}): {message if isinstance(message, str) else ''}"
    return f"server returned {status}"


def _receipt_problem(payload: Any, *, report_id: str, sha256: str, size: int, duplicate: bool) -> str | None:
    if not isinstance(payload, dict):
        return "not a JSON object"
    receipt_id = payload.get("receipt_id")
    if not isinstance(receipt_id, str) or not RECEIPT_ID.fullmatch(receipt_id):
        return "receipt_id"
    if payload.get("report_id") != report_id:
        return "report_id does not match the request"
    if payload.get("sha256") != sha256:
        return "sha256 does not match the request"
    size_bytes = payload.get("size_bytes")
    if type(size_bytes) is not int or size_bytes != size:
        return "size_bytes does not match the package"
    received_at = payload.get("received_at")
    if not isinstance(received_at, str) or not received_at:
        return "received_at"
    if not isinstance(payload.get("duplicate"), bool) or payload["duplicate"] is not duplicate:
        return "duplicate flag disagrees with the status code"
    return None


async def _receipt_of(response: httpx.Response, *, report_id: str, sha256: str, size: int) -> UploadReceipt:
    status = response.status_code
    if 300 <= status < 400:
        location = response.headers.get("Location", "?")
        raise UploadError(
            f"the server redirected to {location}; point {UPLOAD_URL_ENV} at the receiver itself",
            status=status,
            retryable=False,
        )
    body = await _read_bounded(response)
    if status not in (200, 201):
        retryable = status in _RETRYABLE_STATUSES or status >= 500
        raise UploadError(_rejection(status, body), status=status, retryable=retryable)
    if not response.headers.get("Content-Type", "").lower().startswith("application/json"):
        raise UploadError("malformed receipt: not JSON", status=status, retryable=False)
    payload = _parse_json(body)
    if payload is _NOT_JSON:
        raise UploadError("malformed receipt: not JSON", status=status, retryable=False)
    problem = _receipt_problem(payload, report_id=report_id, sha256=sha256, size=size, duplicate=status == 200)
    if problem is not None:
        raise UploadError(f"malformed receipt: {problem}", status=status, retryable=False)
    return UploadReceipt(**{key: payload[key] for key in _RECEIPT_FIELDS})


async def upload_package_async(
    package: Path,
    *,
    report_id: str,
    sha256: str,
    base_url: str,
    connect_timeout: float = 10.0,
    io_timeout: float = 30.0,
    total_timeout: float = 600.0,
) -> UploadReceipt:
    """Send ``package`` to the receiver at ``base_url`` and return its receipt.

    ``total_timeout`` is a deadline on the whole exchange, enforced by
    cancelling the request; the connection is closed before this returns.
    ``io_timeout`` bounds each socket operation within it.
    """
    size = package.stat().st_size
    op = min(io_timeout, total_timeout)
    url = base_url.rstrip("/") + ENDPOINT_PATH
    host = urlsplit(url).netloc
    headers = {
        "Content-Type": "application/gzip",
        "Content-Length": str(size),
        "Accept-Encoding": "identity",
        REPORT_ID_HEADER: report_id,
        SHA256_HEADER: sha256,
        "User-Agent": f"raven/{__version__}",
    }
    timeout = httpx.Timeout(op, connect=min(connect_timeout, op))
    try:
        async with asyncio.timeout(total_timeout):
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("POST", url, content=_body(package), headers=headers) as response:
                    return await _receipt_of(response, report_id=report_id, sha256=sha256, size=size)
    except TimeoutError as exc:
        raise UploadError(f"the upload exceeded {total_timeout}s", retryable=True) from exc
    except httpx.TimeoutException as exc:
        raise UploadError(f"no progress for {op}s while talking to {host}", retryable=True) from exc
    except httpx.TransportError as exc:
        raise UploadError(f"could not reach {host}: {exc}", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise UploadError(str(exc), retryable=False) from exc


def upload_package(
    package: Path,
    *,
    report_id: str,
    sha256: str,
    base_url: str,
    connect_timeout: float = 10.0,
    io_timeout: float = 30.0,
    total_timeout: float = 600.0,
) -> UploadReceipt:
    """Blocking :func:`upload_package_async`, on an event loop of its own.

    From async code either await :func:`upload_package_async` directly or
    run this in a worker thread (``asyncio.to_thread``).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("run upload_package in a worker thread, or await upload_package_async")
    return asyncio.run(
        upload_package_async(
            package,
            report_id=report_id,
            sha256=sha256,
            base_url=base_url,
            connect_timeout=connect_timeout,
            io_timeout=io_timeout,
            total_timeout=total_timeout,
        )
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upload_record(record_dir: Path, *, base_url: str) -> tuple[dict[str, Any], UploadReceipt]:
    """Upload a landed report's package and write the outcome into its record.

    A report that has no package, or whose package no longer matches what the
    record says was created, is refused without touching the record. A failed
    attempt is recorded as ``failed`` unless the report was already uploaded —
    a delivery that happened is not undone by a retry that did not.
    """
    record = load_record(record_dir)
    report_id = record["report_id"]
    if record["status"] != STATUS_LOCAL_READY:
        raise UploadError(
            f"bug report {report_id} has no package to upload (status {record['status']})", retryable=False
        )
    package = Path(record["package"]["path"])
    expected_sha = record["package"]["sha256"]
    if (
        not package.is_file()
        or package.stat().st_size != record["package"]["size_bytes"]
        or _file_sha256(package) != expected_sha
    ):
        raise UploadError(
            "the package file changed since it was created; delete the report and file a new one", retryable=False
        )
    issue_url = record["upload"].get("issue_url", "")
    try:
        receipt = upload_package(package, report_id=report_id, sha256=expected_sha, base_url=base_url)
    except UploadError as exc:
        if record["upload"].get("state") != UPLOAD_STATE_UPLOADED:
            record["upload"] = {
                "state": UPLOAD_STATE_FAILED,
                "issue_url": issue_url,
                "receipt": "",
                "error": exc.reason,
            }
            save_record(record_dir, record)
        raise
    record["upload"] = {
        "state": UPLOAD_STATE_UPLOADED,
        "issue_url": issue_url,
        "receipt": receipt.receipt_id,
        "uploaded_at": _now_iso(),
        "error": "",
    }
    save_record(record_dir, record)
    return record, receipt


__all__ = [
    "ALLOW_HTTP_ENV",
    "DEFAULT_UPLOAD_URL",
    "MAX_RESPONSE_BYTES",
    "UPLOAD_URL_ENV",
    "UploadConfigError",
    "UploadError",
    "UploadReceipt",
    "resolve_upload_url",
    "upload_package",
    "upload_package_async",
    "upload_record",
    "upload_target",
    "upload_url",
    "validate_upload_url",
]
