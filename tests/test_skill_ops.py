"""Unit tests for raven.config.skill_ops (Req2/Req3 phase 1).

read_local_body reads from a fresh catalog; the hub ops delegate to a
SkillHubClient built by ``_build_client`` (the tests' injection point).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from raven.config import skill_ops


@dataclass
class _FakeMeta:
    id: str
    name: str
    description: str
    path: Path
    content: str
    source: str


class _FakeCatalog:
    def __init__(self, metas: list[_FakeMeta]) -> None:
        self._metas = metas

    def gather_all_skills(self) -> list[_FakeMeta]:
        return self._metas


class _FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = False
        self._fail = fail

    async def search(self, q, *, category=None, sort=None, limit=20):
        if self._fail:
            raise RuntimeError("boom")
        return [{"id": "1", "name": "alpha", "q": q, "limit": limit}]

    async def install(self, skill_id):
        return {"slug": "alpha", "version": "v1", "dir": "/ws/skills/hub/alpha@v1", "skill_md": "# a"}

    async def get(self, skill_id):
        return {"skill_md": "# hub body", "name": "alpha", "version": "v1"}

    async def aclose(self):
        self.closed = True


# ── read_local_body ─────────────────────────────────────────────────


def test_read_local_body_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _FakeMeta("id-1", "alpha", "d", Path("/ws/skills/alpha/SKILL.md"), "# alpha body", "workspace")
    monkeypatch.setattr(skill_ops, "_catalog", lambda: _FakeCatalog([meta]))
    out = skill_ops.read_local_body(name="alpha")
    assert out == {
        "skillMd": "# alpha body",
        "name": "alpha",
        "source": "workspace",
        "path": "/ws/skills/alpha/SKILL.md",
        "description": "d",
        "category": None,
        "license": None,
        "tags": [],
        "source_url": None,
        "files": [],
    }


def test_read_local_body_by_id_and_source_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    metas = [
        _FakeMeta("id-1", "alpha", "d", Path("/a/SKILL.md"), "A", "workspace"),
        _FakeMeta("id-2", "alpha", "d", Path("/b/SKILL.md"), "B", "hub"),
    ]
    monkeypatch.setattr(skill_ops, "_catalog", lambda: _FakeCatalog(metas))
    assert skill_ops.read_local_body(skill_id="id-2")["skillMd"] == "B"
    assert skill_ops.read_local_body(name="alpha", source="workspace")["skillMd"] == "A"


def test_read_local_body_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_catalog", lambda: _FakeCatalog([]))
    assert skill_ops.read_local_body(name="nope") is None


# ── hub ops ─────────────────────────────────────────────────────────


async def test_hub_test_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: client)
    out = await skill_ops.hub_test(endpoint="https://h", api_key="k")
    assert out["ok"] is True
    assert client.closed is True


async def test_hub_test_failure_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: _FakeClient(fail=True))
    out = await skill_ops.hub_test(endpoint="https://h")
    assert out["ok"] is False
    assert "boom" in out["detail"]


async def test_hub_test_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: None)
    out = await skill_ops.hub_test()
    assert out["ok"] is False
    assert "endpoint" in out["detail"]


async def test_hub_search_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: _FakeClient())
    items = await skill_ops.hub_search("translate", limit=5)
    assert items[0]["q"] == "translate" and items[0]["limit"] == 5


async def test_hub_search_no_endpoint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: None)
    with pytest.raises(ValueError):
        await skill_ops.hub_search("x")


async def test_hub_install_returns_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: _FakeClient())
    out = await skill_ops.hub_install("id-1")
    assert out == {"ok": True, "slug": "alpha", "version": "v1", "dir": "/ws/skills/hub/alpha@v1"}


async def test_hub_get_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_ops, "_build_client", lambda endpoint=None, api_key=None: _FakeClient())
    out = await skill_ops.hub_get_body("id-1")
    assert out == {"skillMd": "# hub body", "name": "alpha", "version": "v1"}
