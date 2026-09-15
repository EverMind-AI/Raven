"""Tests for ``memory.*`` RPC handlers (the GUI's data & memory page).

The read paths (``memory.stats`` / ``memory.list``) talk to EverOS over
HTTP; tests replace :func:`memory._post` so no sockets open. The delete
path uses EverOS's in-process repository layer; tests install a stub
``everos.infra.persistence.lancedb`` module and assert the predicates it
receives (including the episode family cascade).
"""

from __future__ import annotations

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
        "note": None,
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


class _RecordingBackend:
    """A backend that records the delete it was asked for and answers it.

    The host has no business knowing how a memory is stored, so these tests
    assert what crosses the seam -- the id and the backend's own kind string --
    and nothing about storage. The tests they replace asserted LanceDB
    predicates, which is how the host came to delete an index row while the
    markdown behind it, EverOS's source of truth, kept the text: the memory
    reappeared on the next rebuild of that file after the user was told it was
    gone.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str | None]] = []

    async def delete(self, memory_id: str, *, kind: str | None = None) -> bool:
        self.calls.append((memory_id, kind))
        return self.answer


def _loop_with(backend):
    return lambda: SimpleNamespace(backend=backend)


@pytest.mark.asyncio
async def test_delete_goes_through_the_backend_that_owns_the_memory():
    backend = _RecordingBackend()

    out = await memory.memory_delete({"kind": "episode", "id": "ep-1"}, agent_loop_factory=_loop_with(backend))

    assert out == {"ok": True, "removed": 1}
    assert backend.calls == [("ep-1", "episode")]


@pytest.mark.asyncio
async def test_the_kind_reaches_the_backend_verbatim():
    """It is the backend's own vocabulary, echoed back from its listing."""
    backend = _RecordingBackend()

    await memory.memory_delete({"kind": "agent_skill", "id": "sk1"}, agent_loop_factory=_loop_with(backend))

    assert backend.calls == [("sk1", "agent_skill")]


@pytest.mark.asyncio
async def test_a_backend_that_will_not_delete_is_reported_not_claimed():
    """``False`` is a real answer -- this kind cannot be removed here. Reporting
    success would repeat the bug this path was rewritten for."""
    backend = _RecordingBackend(answer=False)

    with pytest.raises(InternalError):
        await memory.memory_delete({"kind": "profile", "id": "p1"}, agent_loop_factory=_loop_with(backend))


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
    async def test_list_answers_an_empty_page_that_says_why(self):
        """Not an error: there is no store to list, and a retry button offers
        an action that cannot help. The page shows the sentence instead."""
        with everos_plugin_absent():
            out = await memory.memory_list({"kind": "episode"})

        assert out["items"] == []
        assert "everos-memory" in out["note"]

    @pytest.mark.asyncio
    async def test_an_unknown_kind_is_still_the_first_answer(self):
        """Argument validation does not depend on a backend being installed."""
        with everos_plugin_absent(), pytest.raises(ConfigValidationError):
            await memory.memory_list({"kind": "nope"})


# ── why the page is empty ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_plugin_explains_itself_instead_of_showing_zeros():
    """Four zeros read as "your memories are gone". They are not gone; this
    install never had the plugin that keeps them."""
    with everos_plugin_absent():
        stats = await memory.memory_stats({})
        listing = await memory.memory_list({"kind": "episode"})

    assert stats["ok"] is False
    assert "everos-memory" in stats["note"]
    assert listing["items"] == [] and "everos-memory" in listing["note"]


@pytest.mark.asyncio
async def test_a_different_backend_says_this_page_is_not_where_they_are(monkeypatch):
    """The plugin is installed and memory works -- somewhere this page does not
    read. Showing zeros here says the opposite of what is true."""
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda *a, **k: SimpleNamespace(
            memory=SimpleNamespace(backend="mem0", user_id="u", agent_id="a"), plugins=SimpleNamespace(config={})
        ),
    )

    stats = await memory.memory_stats({})
    listing = await memory.memory_list({"kind": "episode"})

    assert stats["ok"] is False
    assert "mem0" in stats["note"]
    assert listing["items"] == [] and "mem0" in listing["note"]


@pytest.mark.asyncio
async def test_the_configured_backend_gets_no_note(monkeypatch):
    """The note exists to explain an empty page, not to decorate a working one."""
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda *a, **k: SimpleNamespace(
            memory=SimpleNamespace(backend="everos", user_id="u", agent_id="a"), plugins=SimpleNamespace(config={})
        ),
    )
    post, _ = _post_returning([{"data": {"total_count": 3}} for _ in range(4)])
    monkeypatch.setattr(memory, "_post", post)

    stats = await memory.memory_stats({})

    assert stats["note"] is None
