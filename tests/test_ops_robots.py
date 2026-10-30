"""Persistence, isolation, stale results, and bounded read-only robot health checks."""

import asyncio
import gzip
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.ops.robots import (
    ConflictError,
    MissingError,
    RegistryError,
    RobotStore,
    probe_status,
    validate,
    validate_url,
)


def data(**changes):
    return dict(
        domain="personal",
        confirm_personal=True,
        name="Test personal device",
        type="sparkbot",
        connection_mode="metadata",
        **changes,
    )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "registry"
        self.db = RobotStore(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_metadata_and_absent_records_preserve_existing_data(self):
        original = self.db.save(data())
        for payload in [
            None,
            {**data(), "name": " "},
            {**data(), "name": 4},
            {**data(), "name": "bad\x00name"},
            {**data(), "name": "x" * 81},
            {**data(), "type": "unsupported"},
            {**data(), "connection_mode": "ssh"},
        ]:
            with self.subTest(payload=payload), self.assertRaises(RegistryError):
                self.db.save(payload)
        missing = "0" * 32
        for action in [
            lambda: self.db.get(missing),
            lambda: self.db.delete(missing),
            lambda: self.db.restore(missing),
            lambda: self.db.save({**data(), "revision": 1}, missing),
        ]:
            with self.assertRaises(MissingError):
                action()
        self.assertEqual(self.db.list()[0]["id"], original["id"])

    def test_registry_size_and_stored_domain_are_enforced(self):
        row = self.db.save(data())
        with self.db.connect() as db:
            for i in range(99):
                db.execute(
                    "INSERT INTO devices VALUES(?,1,?,NULL,NULL,?)",
                    (f"{i:032x}", json.dumps(validate(data())), "fixture"),
                )
        with self.assertRaises(RegistryError):
            self.db.save(data())
        self.assertEqual(len(self.db.list()), 100)
        with self.db.connect() as db:
            db.execute(
                "UPDATE devices SET data=? WHERE id=?",
                (json.dumps({**validate(data()), "domain": "company"}), row["id"]),
            )
        with self.assertRaises(RegistryError):
            self.db.list()

    @unittest.skipIf(os.name == "nt", "POSIX symlink and permission boundary")
    def test_symbolic_link_stores_are_refused(self):
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RegistryError):
            RobotStore(alias)
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (other / "devices.sqlite3").symlink_to(self.db.path)
        with self.assertRaises(RegistryError):
            RobotStore(other)

    def test_crud_persistence_cas_and_recovery(self):
        original = self.db.save(data())
        self.assertEqual(original["status"], "unknown")
        reopened = RobotStore(self.root)
        edited = reopened.save(
            {**data(), "name": "Edited personal device", "revision": original["revision"]}, original["id"]
        )
        self.assertEqual(edited["revision"], 2)
        with self.assertRaises(ConflictError):
            reopened.save({**data(), "revision": 1}, original["id"])
        reopened.delete(original["id"])
        self.assertEqual(reopened.list(), [])
        self.assertEqual(len(RobotStore(self.root).list(trash=True)), 1)
        restored = reopened.restore(original["id"])
        self.assertEqual(restored["name"], "Edited personal device")
        self.assertEqual(restored["status"], "unknown")
        with self.assertRaises(MissingError):
            reopened.restore(original["id"])
        if os.name != "nt":
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)

    def test_domain_and_credential_control_fields_are_rejected(self):
        for payload in (
            {**data(), "domain": "company"},
            {**data(), "confirm_personal": False},
            {**data(), "api_key": "not-a-key"},
            {**data(), "command": "move"},
            {**data(), "type": "agv", "connection_mode": "http_status"},
        ):
            with self.subTest(payload=payload), self.assertRaises(RegistryError):
                validate(payload)
        self.assertEqual(self.db.list(), [])

    def test_no_dns_public_loopback_credentials_or_control_urls(self):
        for address in [
            "http://localhost:8080/health",
            "http://127.0.0.1:8080/health",
            "http://169.254.169.254/health",
            "http://8.8.8.8/health",
            "http://[::1]/health",
            "http://u:p@192.168.1.2:8080/health",
            "http://192.168.1.2:8080/health?command=move",
            "http://192.168.1.2:8080/health#token",
            "http://192.168.1.2:22/health",
            "http://192.168.1.2:0/health",
            "http://192.168.1.2:8080/\nhealth",
            "http://192.168.1.2:8080/move",
        ]:
            with self.subTest(address=address), self.assertRaises(RegistryError):
                validate_url(address)
        self.assertEqual(validate_url("http://192.168.1.2:8080/health"), "http://192.168.1.2:8080/health")

    def test_status_stales_and_late_result_cannot_resurrect_or_overwrite(self):
        row = self.db.save({**data(), "connection_mode": "http_status", "status_url": "http://192.168.1.2:8080/health"})
        observation = {"checked_epoch": time.time(), "status": "reachable"}
        self.assertEqual(self.db.observe(row["id"], row["revision"], observation)["status"], "reachable")
        with patch("raven.ops.robots.time.time", return_value=time.time() + 61):
            self.assertEqual(self.db.get(row["id"])["status"], "unknown")
        updated = self.db.save({**data(), "revision": row["revision"]}, row["id"])
        with self.assertRaises(ConflictError):
            self.db.observe(row["id"], row["revision"], observation)
        self.db.delete(row["id"])
        with self.assertRaises(ConflictError):
            self.db.observe(row["id"], updated["revision"], observation)
        self.assertEqual(self.db.list(), [])


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode = "good"
        self.redirect_hits = 0

        async def handler(request):
            if self.mode == "deep":
                return web.Response(body=b"[" * 5000 + b"0" + b"]" * 5000)
            if self.mode == "compressed":
                return web.Response(
                    body=gzip.compress(b'{"ok":true,"project":"fixture"}'), headers={"Content-Encoding": "gzip"}
                )
            if self.mode == "nonfinite":
                return web.Response(body=b'{"ok":true,"project":"fixture","uptime_seconds":NaN}')
            if self.mode == "redirect":
                raise web.HTTPFound("/control-target")
            if self.mode == "oversize":
                return web.json_response({"ok": True, "padding": "x" * 17000})
            if self.mode == "invalid":
                return web.Response(text="<html>not a device</html>")
            if self.mode == "http_error":
                return web.json_response({"ok": True}, status=503)
            if self.mode == "unhealthy":
                return web.json_response({"ok": False, "project": "fixture"})
            if self.mode == "shape":
                return web.json_response([])
            if self.mode == "identity":
                return web.json_response({"ok": True, "project": "wrong"})
            # Fragmented transfer exercises bounded reads without truncating JSON.
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"ok":true,"project":"fixture",')
            await asyncio.sleep(0.01)
            await response.write(b'"chassis_online":false,"secret":"must-not-leak"}')
            await response.write_eof()
            return response

        async def redirect_target(request):
            self.redirect_hits += 1
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/health", handler)
        app.router.add_get("/control-target", redirect_target)
        self.server = TestServer(app)
        await self.server.start_server()
        self.device = {"status_url": str(self.server.make_url("/health")), "expected_project": "fixture"}

    async def asyncTearDown(self):
        await self.server.close()

    async def test_fragmented_response_allowlist_and_identity(self):
        result = await probe_status(self.device, test_loopback=True)
        self.assertEqual(result["status"], "reachable")
        self.assertIs(result["observed"]["chassis_online"], False)
        self.assertNotIn("secret", result["observed"])
        self.mode = "identity"
        self.assertEqual((await probe_status(self.device, test_loopback=True))["status"], "error")

    async def test_failure_is_never_online_and_redirect_not_followed(self):
        for mode in [
            "redirect",
            "oversize",
            "invalid",
            "http_error",
            "unhealthy",
            "shape",
            "compressed",
            "nonfinite",
            "deep",
        ]:
            self.mode = mode
            with self.subTest(mode=mode):
                self.assertNotEqual((await probe_status(self.device, test_loopback=True))["status"], "reachable")
        self.assertEqual(self.redirect_hits, 0)
        await self.server.close()
        self.assertEqual((await probe_status(self.device, test_loopback=True))["status"], "not_connected")
