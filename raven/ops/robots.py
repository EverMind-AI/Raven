"""Profile-scoped personal robot records and bounded, explicit read-only health checks."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

TYPES = {"sparkbot", "http_device", "robot_dog", "agv", "mobile_manipulator", "other"}
FIELDS = {
    "name",
    "type",
    "model",
    "location",
    "note",
    "connection_mode",
    "status_url",
    "expected_project",
    "domain",
    "confirm_personal",
    "revision",
}
OBSERVED = {"ok", "project", "version", "uptime_seconds", "wifi_rssi", "chassis_online"}
TTL = 60
MAX_RESPONSE = 16384


class RegistryError(ValueError):
    status = 400


class ConflictError(RegistryError):
    status = 409


class MissingError(RegistryError):
    status = 404


def now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value, maximum, *, required=False):
    if not isinstance(value, str):
        raise RegistryError("Fields must be text")
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 and c != "\n" for c in value):
        raise RegistryError("A field is empty, too long, or contains unsupported characters")
    return value


def validate_url(value, *, test_loopback=False):
    """Literal RFC1918 IPv4 only: no DNS, proxies, redirects, queries or credentials."""
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        private = any(address in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        valid_address = address.version == 4 and (private or (test_loopback and address.is_loopback))
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if (
            not valid_address
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"/health", "/status"}
            or not (port in {80, 443} or 1024 <= port <= 65535)
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise RegistryError(
            "Use a private IPv4 /health or /status URL without credentials, query parameters, or fragments"
        ) from None
    return value


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise RegistryError("Unsupported fields; credentials and control interfaces cannot be registered")
    if payload.get("domain") != "personal" or payload.get("confirm_personal") is not True:
        raise RegistryError("Only confirmed personal devices are supported in this registry")
    kind = payload.get("type")
    mode = payload.get("connection_mode", "metadata")
    if kind not in TYPES or mode not in {"metadata", "http_status"}:
        raise RegistryError("Unsupported device type or connection mode")
    if kind not in {"sparkbot", "http_device"} and mode != "metadata":
        raise RegistryError("Robot dogs, AGVs, and mobile manipulators support metadata registration only")
    result = {
        "domain": "personal",
        "name": clean_text(payload.get("name", ""), 80, required=True),
        "type": kind,
        "connection_mode": mode,
    }
    for key, limit in (("model", 100), ("location", 100), ("note", 500), ("expected_project", 80)):
        result[key] = clean_text(payload.get(key, ""), limit)
    result["status_url"] = validate_url(payload.get("status_url", "")) if mode == "http_status" else ""
    return result


class RobotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise RegistryError("The robot directory cannot be a symbolic link")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "devices.sqlite3"
        if self.path.is_symlink():
            raise RegistryError("The robot database cannot be a symbolic link")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL, observation TEXT, deleted_at TEXT, updated_at TEXT NOT NULL)"
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def unpack(row):
        data = json.loads(row["data"])
        if data.get("domain") != "personal":
            raise RegistryError("The stored security domain does not match")
        observation = json.loads(row["observation"]) if row["observation"] else None
        status = "unknown" if data["connection_mode"] == "metadata" else "not_connected"
        stale = False
        if observation:
            age = time.time() - observation["checked_epoch"]
            stale = age < -5 or age > TTL
            status = "unknown" if stale else observation["status"]
        return {
            **data,
            "id": row["id"],
            "revision": row["revision"],
            "status": status,
            "observation": observation,
            "stale": stale,
            "deleted_at": row["deleted_at"],
            "updated_at": row["updated_at"],
        }

    def list(self, *, trash=False):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM devices WHERE deleted_at IS "
                + ("NOT NULL" if trash else "NULL")
                + " ORDER BY updated_at DESC"
            ).fetchall()
        return [self.unpack(row) for row in rows]

    def get(self, device_id, *, include_deleted=False):
        with self.connect() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or (row["deleted_at"] and not include_deleted):
            raise MissingError("The robot does not exist or has been moved to trash")
        return self.unpack(row)

    def save(self, payload, device_id=None):
        data = validate(payload)
        stamp = now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if device_id:
                old = db.execute("SELECT * FROM devices WHERE id=? AND deleted_at IS NULL", (device_id,)).fetchone()
                if old is None:
                    raise MissingError("The robot does not exist")
                if type(payload.get("revision")) is not int or payload["revision"] != old["revision"]:
                    raise ConflictError("The robot was edited by another client; refresh before retrying")
                db.execute(
                    "UPDATE devices SET data=?, observation=NULL, revision=revision+1, updated_at=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), stamp, device_id),
                )
            else:
                if db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= 100:
                    raise RegistryError("The registry has reached its limit of 100 records")
                device_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO devices VALUES(?,1,?,NULL,NULL,?)",
                    (device_id, json.dumps(data, ensure_ascii=False), stamp),
                )
        return self.get(device_id)

    def delete(self, device_id):
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE devices SET deleted_at=?, revision=revision+1 WHERE id=? AND deleted_at IS NULL",
                (now(), device_id),
            )
            if cursor.rowcount != 1:
                raise MissingError("The robot does not exist or has been moved to trash")

    def restore(self, device_id):
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE devices SET deleted_at=NULL, observation=NULL, revision=revision+1, updated_at=? WHERE id=? AND deleted_at IS NOT NULL",
                (now(), device_id),
            )
            if cursor.rowcount != 1:
                raise MissingError("The robot does not exist in trash")
        return self.get(device_id)

    def observe(self, device_id, revision, observation):
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE devices SET observation=? WHERE id=? AND revision=? AND deleted_at IS NULL",
                (json.dumps(observation, ensure_ascii=False), device_id, revision),
            )
            if cursor.rowcount != 1:
                raise ConflictError("The robot changed during the check; the stale result was not saved")
        return self.get(device_id)


def _nonfinite_json(value):
    raise ValueError("Non-finite JSON constant")


async def probe_status(device, *, test_loopback=False):
    url = validate_url(device["status_url"], test_loopback=test_loopback)
    result = {
        "status": "error",
        "checked_at": now(),
        "checked_epoch": time.time(),
        "http_status": None,
        "detail": "",
        "observed": {},
    }
    try:
        async with ClientSession(
            trust_env=False, timeout=ClientTimeout(total=3), raise_for_status=False, auto_decompress=False
        ) as client:
            async with client.get(
                url, allow_redirects=False, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            ) as response:
                result["http_status"] = response.status
                if response.status != 200:
                    result["detail"] = (
                        "The endpoint returned an error; redirects were not followed and no credentials were sent"
                    )
                    return result
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        result["detail"] = "The status response exceeded 16 KiB"
                        return result
        try:
            data = json.loads(raw, parse_constant=_nonfinite_json)
        except (ValueError, UnicodeDecodeError, RecursionError):
            result["detail"] = "The endpoint did not return valid JSON"
            return result
        if not isinstance(data, dict):
            result["detail"] = "Unsupported status response shape"
            return result
        expected = device.get("expected_project")
        if expected and data.get("project") != expected:
            result["detail"] = "The project identity did not match the registered device"
            return result
        result["observed"] = {
            key: value
            for key, value in data.items()
            if key in OBSERVED
            and isinstance(value, (str, bool, int, float))
            and (not isinstance(value, float) or math.isfinite(value))
            and (not isinstance(value, str) or len(value) <= 100)
        }
        if data.get("ok") is True:
            result.update(
                status="reachable",
                detail="The status endpoint responded; chassis, audio, and motion capabilities have not been verified",
            )
        else:
            result.update(status="unknown", detail="The endpoint is reachable but did not confirm device health")
    except (asyncio.TimeoutError, ClientError, OSError):
        result.update(
            status="not_connected",
            detail="Could not connect to the status endpoint; check device power, address, and network",
        )
    return result
