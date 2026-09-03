"""Tests for ``memory.*`` RPC handlers (the GUI's data & memory page).

The read paths (``memory.stats`` / ``memory.list``) talk to EverOS over
HTTP; tests replace :func:`memory._post` so no sockets open. The delete
path uses EverOS's in-process repository layer; tests install a stub
``everos.infra.persistence.lancedb`` module and assert the predicates it
receives (including the episode family cascade).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from raven.rpc.errors import ConfigValidationError, InternalError
from raven.rpc.methods import memory
from tests._everos_presence import everos_plugin_absent


@pytest.fixture(autouse=True)
def fake_cfg(monkeypatch):
    monkeypatch.setattr(memory, "_cfg", lambda: ("http://x", "u1", "a1"))


def _post_returning(payloads):
    """A fake ``_post`` yielding queued payloads; records request bodies."""
    calls: list[tuple[str, dict]] = []

    async def _post(base_url, path, body):
        calls.append((path, body))
        return payloads.pop(0)

    return _post, calls


# ── memory.stats ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_reads_all_four_kinds(monkeypatch):
    payloads = [{"data": {"total_count": n}} for n in (7, 1, 3, 2)]
    post, calls = _post_returning(payloads)
    monkeypatch.setattr(memory, "_post", post)
    out = await memory.memory_stats({})
    assert out == {
        "ok": True,
        "base_url": "http://x",
        "episodes": 7,
        "profiles": 1,
        "agent_cases": 3,
        "agent_skills": 2,
    }
    kinds = [b["memory_type"] for _, b in calls]
    assert kinds == ["episode", "profile", "agent_case", "agent_skill"]
    assert calls[0][1]["user_id"] == "u1"
    assert calls[2][1]["agent_id"] == "a1"


@pytest.mark.asyncio
async def test_stats_degrades_when_everos_down(monkeypatch):
    async def _post(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(memory, "_post", _post)
    out = await memory.memory_stats({})
    assert out["ok"] is False
    assert out["episodes"] == 0


# ── memory.list ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_projects_episode_rows(monkeypatch):
    payloads = [
        {
            "data": {
                "episodes": [
                    {
                        "id": "e1",
                        "session_id": "s1",
                        "timestamp": "2026-08-06T00:00:00Z",
                        "subject": "sub",
                        "summary": "sum",
                        "episode": "full text",
                    }
                ],
                "total_count": 41,
            }
        }
    ]
    post, calls = _post_returning(payloads)
    monkeypatch.setattr(memory, "_post", post)
    out = await memory.memory_list({"kind": "episode", "page": 2, "page_size": 10})
    assert calls[0][0] == "/api/v1/memory/get"
    assert calls[0][1]["page"] == 2
    assert out["total"] == 41 and out["page"] == 2
    item = out["items"][0]
    assert item["kind"] == "episode"
    assert item["body"] == "full text"
    assert item["subject"] == "sub"
    assert "score" not in item


@pytest.mark.asyncio
async def test_list_with_query_uses_search(monkeypatch):
    payloads = [
        {
            "data": {
                "agent_skills": [
                    {
                        "id": "sk1",
                        "name": "n",
                        "description": "d",
                        "content": "c",
                        "confidence": 0.8,
                        "maturity_score": 0.5,
                        "score": 0.9,
                    }
                ]
            }
        }
    ]
    post, calls = _post_returning(payloads)
    monkeypatch.setattr(memory, "_post", post)
    out = await memory.memory_list({"kind": "agent_skill", "q": "fallback"})
    assert calls[0][0] == "/api/v1/memory/search"
    assert calls[0][1]["agent_id"] == "a1"
    assert out["page"] == 1
    assert out["items"][0]["score"] == 0.9
    assert out["items"][0]["subject"] == "n"


@pytest.mark.asyncio
async def test_list_rejects_unknown_kind():
    with pytest.raises(ConfigValidationError):
        await memory.memory_list({"kind": "nope"})


@pytest.mark.asyncio
async def test_list_wraps_transport_errors(monkeypatch):
    async def _post(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(memory, "_post", _post)
    with pytest.raises(InternalError):
        await memory.memory_list({"kind": "episode"})


# ── memory.delete ────────────────────────────────────────────────────────


class _FakeRepo:
    def __init__(self, row=None):
        self.row = row
        self.deleted: list[str] = []

    async def get_by_id(self, id_value):
        return self.row

    async def delete(self, predicate):
        self.deleted.append(predicate)


def _install_fake_everos(monkeypatch, repos):
    mod = SimpleNamespace(**repos)
    monkeypatch.setitem(sys.modules, "everos.infra.persistence.lancedb", mod)


@pytest.fixture
def fake_everos_env(monkeypatch):
    monkeypatch.setattr("raven.config.update_everos.configure_everos_env", lambda: None)
    monkeypatch.setattr("raven.config.update_everos.ensure_everos_home", lambda: None)


@pytest.mark.asyncio
async def test_delete_episode_cascades_family(monkeypatch, fake_everos_env):
    ep = _FakeRepo(row=SimpleNamespace(parent_id="mc-1"))
    facts, foresight = _FakeRepo(), _FakeRepo()
    _install_fake_everos(
        monkeypatch,
        {
            "episode_repo": ep,
            "atomic_fact_repo": facts,
            "foresight_repo": foresight,
            "agent_case_repo": _FakeRepo(),
            "agent_skill_repo": _FakeRepo(),
            "user_profile_repo": _FakeRepo(),
        },
    )
    out = await memory.memory_delete({"kind": "episode", "id": "e'1"})
    assert out == {"ok": True, "removed": 1}
    assert ep.deleted == ["id = 'e''1'"]
    assert facts.deleted == ["parent_id = 'mc-1'"]
    assert foresight.deleted == ["parent_id = 'mc-1'"]


@pytest.mark.asyncio
async def test_delete_skill_no_cascade(monkeypatch, fake_everos_env):
    skill = _FakeRepo()
    facts = _FakeRepo()
    _install_fake_everos(
        monkeypatch,
        {
            "episode_repo": _FakeRepo(),
            "atomic_fact_repo": facts,
            "foresight_repo": _FakeRepo(),
            "agent_case_repo": _FakeRepo(),
            "agent_skill_repo": skill,
            "user_profile_repo": _FakeRepo(),
        },
    )
    out = await memory.memory_delete({"kind": "agent_skill", "id": "sk1"})
    assert out["ok"] is True
    assert skill.deleted == ["id = 'sk1'"]
    assert facts.deleted == []


@pytest.mark.asyncio
async def test_delete_validates_params():
    with pytest.raises(ConfigValidationError):
        await memory.memory_delete({"kind": "episode", "id": ""})
    with pytest.raises(ConfigValidationError):
        await memory.memory_delete({"kind": "bogus", "id": "x"})


# ── the plugin that is not there ─────────────────────────────────────────


class TestWithoutTheMemoryPlugin:
    """The backend ships as its own distribution now, and may not be installed.

    Both read methods used to reach it through a module-level constant, so the
    page's first call raised ``ModuleNotFoundError`` at the dispatcher instead
    of answering. Each degrades in the shape it already declared: stats opens
    the page empty, list fails typed.
    """

    @pytest.mark.asyncio
    async def test_stats_opens_the_page_with_nothing_in_it(self):
        with everos_plugin_absent():
            out = await memory.memory_stats({})

        assert out["ok"] is False
        assert out["base_url"] == ""
        assert out["episodes"] == 0

    @pytest.mark.asyncio
    async def test_list_fails_typed_and_names_the_distribution(self):
        with everos_plugin_absent(), pytest.raises(InternalError) as exc:
            await memory.memory_list({"kind": "episode"})

        assert "everos-memory" in str(exc.value)

    @pytest.mark.asyncio
    async def test_an_unknown_kind_is_still_the_first_answer(self):
        """Argument validation does not depend on a backend being installed."""
        with everos_plugin_absent(), pytest.raises(ConfigValidationError):
            await memory.memory_list({"kind": "nope"})
