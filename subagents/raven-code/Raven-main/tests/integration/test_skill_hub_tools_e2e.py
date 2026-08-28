"""End-to-end test for the read_skill / use_skill tools against a local Hub.

Stands up a real HTTP server emulating the Skill Hub OpenAPI surface (uniform
envelope + ``/skills/{id}`` body + ``/skills/{id}/download`` zip), points a real
:class:`SkillHubClient` at it, and drives both tools through the full path —
real HTTP, real zip download, real safe-extraction to disk. No mocks below the
tool boundary.
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from raven.agent.tools.skill_hub import ReadSkillTool, UseSkillTool
from raven.skill_hub import SkillHubClient

_SKILL_MD = "# Deploy\n\nRun scripts/deploy.sh to ship.\n"
_DEPLOY_SH = "#!/bin/sh\necho deploying\n"


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _SKILL_MD)
        zf.writestr("scripts/deploy.sh", _DEPLOY_SH)
    return buf.getvalue()


def _envelope(result: dict) -> bytes:
    # Mirror the dev/aws Hub, which uses ``error: "success"`` (not "ok") —
    # exercises SkillHubClient's lenient envelope-success check.
    return json.dumps(
        {
            "error": "success",
            "status": 0,
            "requestId": "x",
            "result": result,
        }
    ).encode()


class _HubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test-server logging
        pass

    def do_GET(self):  # noqa: N802 — stdlib handler contract
        if self.path.endswith("/download") or "/download?" in self.path:
            body = _make_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/openapi/v1/skills/" in self.path:
            body = _envelope(
                {
                    "slug": "deploy",
                    "skill_id": "deploy",
                    "version": "v3",
                    "skill_md": _SKILL_MD,
                    "name": "Deploy",
                    "scenario_tags": ["ops", "ship"],
                    "subscores": {"safety": 0.9},
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture()
def hub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_read_skill_fetches_body_from_hub(hub_server: str) -> None:
    client = SkillHubClient(hub_server)
    try:
        out = await ReadSkillTool(client=client).execute(skill_id="hub/deploy")
    finally:
        await client.aclose()
    assert "Deploy" in out
    assert "v3" in out
    assert "Run scripts/deploy.sh" in out
    assert "ops, ship" in out


async def test_use_skill_downloads_and_extracts(
    hub_server: str,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "skills" / "hub"
    client = SkillHubClient(hub_server, cache_dir=cache)
    try:
        out = await UseSkillTool(client=client).execute(skill_id="hub/deploy")
    finally:
        await client.aclose()

    # Tool surfaced a scripts_dir and the body.
    assert "scripts_dir:" in out
    assert "Run scripts/deploy.sh" in out

    # The bundle really landed on disk under the workspace skill tree.
    extracted = cache / "deploy@v3"
    assert (extracted / "SKILL.md").read_text() == _SKILL_MD
    deploy = extracted / "scripts" / "deploy.sh"
    assert deploy.read_text() == _DEPLOY_SH


async def test_use_skill_cache_hit_skips_redownload(
    hub_server: str,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "skills" / "hub"
    client = SkillHubClient(hub_server, cache_dir=cache)
    try:
        first = await UseSkillTool(client=client).execute(skill_id="hub/deploy")
        # Second call: dir already exists → extraction is skipped, still resolves.
        second = await UseSkillTool(client=client).execute(skill_id="hub/deploy")
    finally:
        await client.aclose()
    assert "scripts_dir:" in first
    assert "scripts_dir:" in second


# ── nested-wrapper bundle (the real dev/aws Hub layout) ──────────────


def _make_nested_zip() -> bytes:
    # Real Hub zips wrap the whole skill under one <skill>/ directory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("deploy/SKILL.md", _SKILL_MD)
        zf.writestr("deploy/scripts/deploy.sh", _DEPLOY_SH)
    return buf.getvalue()


class _NestedHubHandler(_HubHandler):
    def do_GET(self):  # noqa: N802 — stdlib handler contract
        if self.path.endswith("/download") or "/download?" in self.path:
            body = _make_nested_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture()
def nested_hub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NestedHubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_use_skill_resolves_scripts_in_nested_bundle(
    nested_hub_server: str,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "skills" / "hub"
    client = SkillHubClient(nested_hub_server, cache_dir=cache)
    try:
        out = await UseSkillTool(client=client).execute(skill_id="hub/deploy")
    finally:
        await client.aclose()

    # scripts_dir must point at the nested <skill>/scripts, not dest/scripts.
    assert "scripts_dir:" in out
    assert "no bundled scripts" not in out
    nested_scripts = cache / "deploy@v3" / "deploy" / "scripts"
    assert (nested_scripts / "deploy.sh").read_text() == _DEPLOY_SH
    assert f"scripts_dir: {nested_scripts}" in out
