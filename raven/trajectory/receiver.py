"""The Bug Report Receiver: an HTTP service that takes Bug Report Packages in.

A receiver accepts a package, checks it (:mod:`raven.trajectory.package_check`),
lands the original bytes under ``<save_dir>/<UTC date>/`` beside a receipt
sidecar, and answers with an Upload Receipt. It does nothing else: no
authentication, no listing, no download — what reaches the port is governed by
the network it listens on.

Landing order is the one invariant readers rely on. The sidecar is written
first and the package is moved into place last, so a package in a date
directory is a committed one: it is never replaced by different content, and a
sidecar that is missing or fails its contract is rebuilt from the package on
the next lookup or at startup. A sidecar without a package is a failed commit
and is removed. The body streams into ``.incoming/`` while it is read, and that
directory is cleared at startup because nothing can be in flight then.

Package inspection hashes up to the configured unpacked limit, so it runs in a
worker thread. Cancelling the request — a client that went away, a timeout,
shutdown — stops that thread cooperatively and waits for it before the
temporary file is removed, so no thread ever reads a file from under itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger

from raven.trajectory.bugreport import is_report_id
from raven.trajectory.package_check import PackageError, PackageLimits, PackageSummary, inspect_package

ENDPOINT_PATH = "/v1/bugreports"
HEALTH_PATH = "/healthz"
REPORT_ID_HEADER = "X-Raven-Report-Id"
SHA256_HEADER = "X-Raven-Package-Sha256"
GZIP_TYPES = ("application/gzip", "application/x-gzip")
RECEIPT_SCHEMA = "bug_report_receipt"
RECEIPT_SCHEMA_VERSION = 1
INCOMING_DIR = ".incoming"
# aiohttp decodes a request body by its Content-Encoding before the handler
# sees it; this interface wants the bytes exactly as they crossed the wire.
RUNNER_OPTIONS: dict[str, Any] = {"auto_decompress": False}
RECEIVER_KEY: web.AppKey[Receiver] = web.AppKey("receiver")

_CHUNK = 64 * 1024
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^rc-[0-9a-f]{12}$")
_RECEIPT_FIELDS = ("receipt_id", "report_id", "sha256", "size_bytes", "received_at")
_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReceiverConfig:
    save_dir: Path
    host: str = "127.0.0.1"
    port: int = 18795
    trusted_proxies: tuple[str, ...] = ()
    limits: PackageLimits = field(default_factory=PackageLimits)
    max_daily_bytes: int = 2 * 2**30
    max_daily_count: int = 500
    min_free_bytes: int = 2**30
    rate_burst: int = 10
    rate_per_10min: int = 30
    concurrency: int = 4
    idle_timeout: float = 30.0
    total_timeout: float = 600.0
    clock: Callable[[], datetime] = _utcnow


class ReceiverError(Exception):
    def __init__(self, status: int, code: str, message: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _json_error(status: int, code: str, message: str = "", *, retry_after: int | None = None) -> web.Response:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return web.json_response({"error": code, "message": message}, status=status, headers=headers)


@dataclass(frozen=True)
class RepairReport:
    rebuilt: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class Existing:
    package: Path
    sidecar: Path
    payload: dict[str, Any]


def _private_dir(path: Path) -> None:
    """Create ``path`` as a 0700 directory; a concurrent creator winning the race is fine."""
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
        return
    os.chmod(path, 0o700)


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` (0600) through a same-directory temp file: write, fsync, replace."""
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _block(metadata: dict[str, Any], key: str) -> dict[str, Any]:
    value = metadata.get(key)
    return value if isinstance(value, dict) else {}


def _package_digest(metadata: dict[str, Any]) -> dict[str, Any]:
    """The triage summary a sidecar carries so a reader need not unpack the package."""
    problem = _block(metadata, "problem")
    redaction = _block(metadata, "redaction")
    return {
        "raven_version": _text(metadata.get("raven_version")),
        "created_at": _text(metadata.get("created_at")),
        "reporter": _text(metadata.get("reporter")),
        "severity": _text(problem.get("severity")),
        "description": _text(problem.get("description")),
        "completeness": _text(_block(metadata, "completeness").get("status")),
        "classification": _text(redaction.get("classification")),
        "risk_accepted": redaction.get("risk_accepted") is True,
    }


def _sidecar_problem(payload: Any, *, report_id: str, date_name: str, package_size: int) -> str | None:
    """Why ``payload`` is not a usable sidecar for this package, or None when it is."""
    if not isinstance(payload, dict):
        return "not an object"
    if payload.get("schema") != RECEIPT_SCHEMA:
        return "schema"
    version = payload.get("schema_version")
    if type(version) is not int or version != RECEIPT_SCHEMA_VERSION:
        return "schema_version"
    receipt_id = payload.get("receipt_id")
    if not isinstance(receipt_id, str) or not _RECEIPT_ID.fullmatch(receipt_id):
        return "receipt_id"
    if payload.get("report_id") != report_id:
        return "report_id"
    sha256 = payload.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        return "sha256"
    size = payload.get("size_bytes")
    if type(size) is not int or size != package_size:
        return "size_bytes"
    received_at = payload.get("received_at")
    if not isinstance(received_at, str) or not received_at:
        return "received_at"
    if payload.get("accounting_date") != date_name:
        return "accounting_date"
    remote = payload.get("remote")
    if not isinstance(remote, dict) or not all(
        isinstance(remote.get(key), str) for key in ("peer", "client", "user_agent")
    ):
        return "remote"
    if not isinstance(payload.get("package"), dict):
        return "package"
    notices = payload.get("notices")
    if not isinstance(notices, list) or not all(isinstance(entry, str) for entry in notices):
        return "notices"
    if not isinstance(payload.get("recovered"), bool):
        return "recovered"
    return None


def receipt_of(payload: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
    """The Upload Receipt a client gets back, cut from a sidecar payload."""
    receipt: dict[str, Any] = {key: payload[key] for key in _RECEIPT_FIELDS}
    receipt["duplicate"] = duplicate
    return receipt


class ReceiverStore:
    """The on-disk side of the receiver: ``<save_dir>/<date>/<id>.tar.gz`` + ``<id>.json``."""

    def __init__(
        self, save_dir: Path, *, clock: Callable[[], datetime] = _utcnow, limits: PackageLimits | None = None
    ) -> None:
        self.save_dir = save_dir
        self._clock = clock
        self._limits = limits or PackageLimits()

    @property
    def incoming(self) -> Path:
        return self.save_dir / INCOMING_DIR

    def prepare(self) -> RepairReport:
        """Create the directories, drop unfinished uploads, and repair what is stored."""
        if not self.save_dir.exists():
            _private_dir(self.save_dir)
        elif self.save_dir.stat().st_mode & 0o077:
            logger.warning("bug report save dir {} is readable by others; expected mode 0700", self.save_dir)
        _private_dir(self.incoming)
        os.chmod(self.incoming, 0o700)
        for stale in self.incoming.iterdir():
            if stale.is_file():
                stale.unlink()
                logger.info("removed an unfinished upload: {}", stale.name)
        return self.repair()

    def repair(self) -> RepairReport:
        rebuilt: list[str] = []
        removed: list[str] = []
        for date_dir in self._date_dirs():
            for package in sorted(date_dir.glob("*.tar.gz")):
                report_id = package.name[: -len(".tar.gz")]
                if not is_report_id(report_id):
                    continue
                sidecar = date_dir / f"{report_id}.json"
                if self._load_sidecar(sidecar, report_id=report_id, package=package) is None:
                    self._rebuild_sidecar(package, sidecar, report_id)
                    rebuilt.append(report_id)
            for sidecar in sorted(date_dir.glob("*.json")):
                report_id = sidecar.stem
                if is_report_id(report_id) and not (date_dir / f"{report_id}.tar.gz").exists():
                    sidecar.unlink()
                    removed.append(report_id)
                    logger.info("removed the sidecar of an uncommitted upload: {}", sidecar.name)
        return RepairReport(tuple(rebuilt), tuple(removed))

    def lookup(self, report_id: str) -> Existing | None:
        """The committed package for ``report_id`` with a usable sidecar, or None."""
        matches = sorted(self.save_dir.glob(f"{_DATE_GLOB}/{report_id}.tar.gz"))
        if matches:
            package = matches[0]
            sidecar = package.parent / f"{report_id}.json"
            payload = self._load_sidecar(sidecar, report_id=report_id, package=package)
            if payload is None:
                payload = self._rebuild_sidecar(package, sidecar, report_id)
            return Existing(package, sidecar, payload)
        for sidecar in self.save_dir.glob(f"{_DATE_GLOB}/{report_id}.json"):
            sidecar.unlink(missing_ok=True)
            logger.info("removed the sidecar of an uncommitted upload: {}", sidecar.name)
        return None

    def new_part(self, report_id: str) -> Path:
        part = self.incoming / f"{report_id}.{secrets.token_hex(4)}.part"
        os.close(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return part

    def receipt_payload(
        self,
        *,
        report_id: str,
        sha256: str,
        size_bytes: int,
        accounting_date: str,
        remote: dict[str, str],
        package: dict[str, Any],
        notices: tuple[str, ...] | list[str],
        recovered: bool,
    ) -> dict[str, Any]:
        return {
            "schema": RECEIPT_SCHEMA,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": f"rc-{secrets.token_hex(6)}",
            "report_id": report_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "received_at": _stamp(self._clock()),
            "accounting_date": accounting_date,
            "remote": dict(remote),
            "package": dict(package),
            "notices": list(notices),
            "recovered": recovered,
        }

    def commit(self, report_id: str, part: Path, payload: dict[str, Any]) -> Path:
        """Land ``part`` as the package: sidecar first, then the rename that commits."""
        date_dir = self.save_dir / payload["accounting_date"]
        _private_dir(date_dir)
        sidecar = date_dir / f"{report_id}.json"
        self._write_sidecar(sidecar, payload)
        final = date_dir / f"{report_id}.tar.gz"
        try:
            os.replace(part, final)
        except BaseException:
            sidecar.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            raise
        logger.info("received bug report {} ({} bytes) into {}", report_id, payload["size_bytes"], date_dir.name)
        return final

    def _date_dirs(self) -> list[Path]:
        return sorted(p for p in self.save_dir.glob(_DATE_GLOB) if p.is_dir() and _DATE_DIR.fullmatch(p.name))

    def _load_sidecar(self, sidecar: Path, *, report_id: str, package: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("sidecar {} is unreadable ({}); rebuilding it", sidecar.name, exc)
            return None
        problem = _sidecar_problem(
            payload, report_id=report_id, date_name=sidecar.parent.name, package_size=package.stat().st_size
        )
        if problem is not None:
            logger.warning("sidecar {} fails its contract ({}); rebuilding it", sidecar.name, problem)
            return None
        return payload

    def _rebuild_sidecar(self, package: Path, sidecar: Path, report_id: str) -> dict[str, Any]:
        digest = _package_digest({})
        notices: tuple[str, ...] = ()
        try:
            summary = inspect_package(package, limits=self._limits, expected_report_id=report_id)
            digest, notices = _package_digest(summary.metadata), summary.notices
        except (PackageError, OSError) as exc:
            logger.warning("package {} does not pass inspection while rebuilding its sidecar: {}", package.name, exc)
        payload = self.receipt_payload(
            report_id=report_id,
            sha256=_file_sha256(package),
            size_bytes=package.stat().st_size,
            accounting_date=sidecar.parent.name,
            remote={"peer": "", "client": "", "user_agent": ""},
            package=digest,
            notices=notices,
            recovered=True,
        )
        self._write_sidecar(sidecar, payload)
        logger.info("rebuilt the receipt sidecar for {}", report_id)
        return payload

    def _write_sidecar(self, sidecar: Path, payload: dict[str, Any]) -> None:
        _write_private(sidecar, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n")


class _IdLocks:
    """One asyncio lock per report id, kept only while someone holds or waits for it."""

    def __init__(self) -> None:
        self._entries: dict[str, list[Any]] = {}

    @contextlib.asynccontextmanager
    async def acquire(self, report_id: str) -> AsyncIterator[None]:
        entry = self._entries.setdefault(report_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._entries.pop(report_id, None)

    def __len__(self) -> int:
        return len(self._entries)


async def _drain(task: asyncio.Task[Any]) -> None:
    """Wait for a shielded task to end, surviving further cancellation; its outcome is discarded."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            continue
    if not task.cancelled():
        task.exception()


async def _finish(task: asyncio.Task[Any], *, on_cancel: Callable[[], None] | None = None) -> Any:
    """Await ``task`` to its end even if this coroutine is cancelled meanwhile."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        await _drain(task)
        raise


def _remote_of(request: web.Request) -> dict[str, str]:
    peer = request.remote or ""
    return {"peer": peer, "client": peer, "user_agent": request.headers.get("User-Agent", "")}


class Receiver:
    """The request side: header checks, streaming the body, inspection, and landing."""

    def __init__(self, config: ReceiverConfig, *, store: ReceiverStore | None = None) -> None:
        self._config = config
        self._store = store or ReceiverStore(config.save_dir, clock=config.clock, limits=config.limits)
        self._locks = _IdLocks()

    @property
    def config(self) -> ReceiverConfig:
        return self._config

    @property
    def store(self) -> ReceiverStore:
        return self._store

    @property
    def locks(self) -> _IdLocks:
        return self._locks

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_upload(self, request: web.Request) -> web.Response:
        part: Path | None = None
        try:
            report_id, declared, expected = self._check_headers(request)
            accounting_date = self._config.clock().astimezone(timezone.utc).strftime("%Y-%m-%d")
            part = self._store.new_part(report_id)
            digest = await self._receive_body(request, part, declared)
            if digest != expected:
                raise ReceiverError(400, "digest_mismatch", f"the body's sha256 does not match {SHA256_HEADER}")
            summary = await self._inspect(part, report_id)
            async with self._locks.acquire(report_id):
                existing = await _finish(asyncio.create_task(asyncio.to_thread(self._store.lookup, report_id)))
                if existing is not None:
                    if existing.payload["sha256"] == digest:
                        return web.json_response(receipt_of(existing.payload, duplicate=True))
                    raise ReceiverError(409, "conflict", "a different package with this report id was already received")
                payload = self._store.receipt_payload(
                    report_id=report_id,
                    sha256=digest,
                    size_bytes=declared,
                    accounting_date=accounting_date,
                    remote=_remote_of(request),
                    package=_package_digest(summary.metadata),
                    notices=summary.notices,
                    recovered=False,
                )
                await _finish(asyncio.create_task(asyncio.to_thread(self._store.commit, report_id, part, payload)))
                return web.json_response(receipt_of(payload, duplicate=False), status=201)
        except ReceiverError as exc:
            return _json_error(exc.status, exc.code, exc.message, retry_after=exc.retry_after)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bug report upload failed")
            return _json_error(500, "internal")
        finally:
            if part is not None:
                part.unlink(missing_ok=True)

    def _check_headers(self, request: web.Request) -> tuple[str, int, str]:
        if request.content_type not in GZIP_TYPES:
            raise ReceiverError(415, "unsupported_media_type", f"expected application/gzip, got {request.content_type}")
        if request.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise ReceiverError(415, "unsupported_media_type", "Content-Encoding is not supported; send the raw bytes")
        declared = request.content_length
        if declared is None:
            raise ReceiverError(411, "length_required", "Content-Length is required")
        limit = self._config.limits.max_package_bytes
        if declared > limit:
            raise ReceiverError(413, "too_large", f"package is {declared} bytes; the limit is {limit}")
        report_id = request.headers.get(REPORT_ID_HEADER, "")
        if not is_report_id(report_id):
            raise ReceiverError(400, "bad_request", f"{REPORT_ID_HEADER} is missing or not a report id")
        expected = request.headers.get(SHA256_HEADER, "").strip()
        if not _SHA256_HEX.fullmatch(expected):
            raise ReceiverError(400, "bad_request", f"{SHA256_HEADER} is missing or not a lowercase sha256")
        return report_id, declared, expected

    async def _receive_body(self, request: web.Request, part: Path, declared: int) -> str:
        digest = hashlib.sha256()
        received = 0
        with part.open("r+b") as handle:
            try:
                while True:
                    chunk = await request.content.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > declared:
                        raise ReceiverError(400, "bad_request", "the body is longer than Content-Length")
                    handle.write(chunk)
                    digest.update(chunk)
            except (aiohttp.ClientError, ConnectionError, asyncio.IncompleteReadError) as exc:
                raise ReceiverError(400, "incomplete_body", f"the body ended early: {exc.__class__.__name__}") from exc
            handle.flush()
            os.fsync(handle.fileno())
        if received != declared:
            raise ReceiverError(400, "incomplete_body", f"received {received} of {declared} bytes")
        return digest.hexdigest()

    async def _inspect(self, part: Path, report_id: str) -> PackageSummary:
        stop = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                inspect_package,
                part,
                limits=self._config.limits,
                expected_report_id=report_id,
                should_stop=stop.is_set,
            )
        )
        try:
            return await _finish(task, on_cancel=stop.set)
        except PackageError as exc:
            raise ReceiverError(400, "bad_package", f"{exc.code}: {exc.message}") from exc


@web.middleware
async def _json_errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as exc:
        code = _HTTP_CODES.get(exc.status) or re.sub(r"[^a-z0-9]+", "_", exc.reason.lower()).strip("_")
        response = _json_error(exc.status, code, exc.reason)
        if "Allow" in exc.headers:
            response.headers["Allow"] = exc.headers["Allow"]
        return response


def build_app(config: ReceiverConfig, *, store: ReceiverStore | None = None) -> web.Application:
    """The aiohttp application: prepares the store, then serves the two routes."""
    receiver = Receiver(config, store=store)
    receiver.store.prepare()
    app = web.Application(middlewares=[_json_errors])
    app[RECEIVER_KEY] = receiver
    app.router.add_post(ENDPOINT_PATH, receiver.handle_upload)
    app.router.add_get(HEALTH_PATH, receiver.handle_health)
    return app


__all__ = [
    "ENDPOINT_PATH",
    "HEALTH_PATH",
    "RECEIVER_KEY",
    "REPORT_ID_HEADER",
    "RUNNER_OPTIONS",
    "SHA256_HEADER",
    "Receiver",
    "ReceiverConfig",
    "ReceiverError",
    "ReceiverStore",
    "RepairReport",
    "build_app",
    "receipt_of",
]
