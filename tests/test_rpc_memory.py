"""Tests for ``memory.*`` RPC handlers (the GUI's data & memory page).

Both handlers (``memory.stats`` / ``memory.list``) talk to EverOS over
HTTP; tests replace :func:`memory._post` so no sockets open.
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
    # The section's start button is drawn off a real probe; a handler test must
    # not inherit the machine's local everos server.
    monkeypatch.setattr(memory, "_local_server_running", lambda: False)
    # A full server, so no test pays a real capability probe: ``http://x`` is
    # a hostname, and resolving it cost the one search test five seconds.
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=True, rerank=True))


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
    monkeypatch.setattr(memory, "_configured_backend", lambda: "everos")
    monkeypatch.setattr(memory, "_available_backends", lambda: ["everos", "everos-cloud"])
    monkeypatch.setattr(memory, "_cloud_key_set", lambda: False)
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
        "backend": "everos",
        "available_backends": ["everos", "everos-cloud"],
        "cloud_key_set": False,
        "local_running": False,
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


def test_available_backends_follow_what_is_importable(monkeypatch):
    """The section draws one segment per name; a name absent here keeps its
    segment but is disabled, so the pair is stable and the list is the truth."""
    monkeypatch.setattr(memory, "everos_plugin_installed", lambda: True)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "raven_everos_cloud" else None)
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(plugins=SimpleNamespace(disabled=[])),
    )
    assert memory._available_backends() == ["everos", "everos-cloud"]

    # Installed but turned off is not a choice the section can offer: the write
    # would be refused by the same config that disabled it.
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(plugins=SimpleNamespace(disabled=["everos-memory"])),
    )
    assert memory._available_backends() == ["everos-cloud"]

    monkeypatch.setattr(memory, "everos_plugin_installed", lambda: False)
    assert memory._available_backends() == ["everos-cloud"]

    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert memory._available_backends() == []


@pytest.mark.asyncio
async def test_cloud_without_a_key_answers_before_the_network(monkeypatch):
    """No key is a state, not an outage: the section draws its key panel, and the
    handlers must not send requests that can only come back 401."""
    monkeypatch.setattr(memory, "_configured_backend", lambda: "everos-cloud")
    monkeypatch.setattr(memory, "_available_backends", lambda: ["everos", "everos-cloud"])
    monkeypatch.setattr(memory, "_cloud_key_set", lambda: False)
    calls: list[int] = []

    async def _post(*_a, **_k):
        calls.append(1)
        raise AssertionError("no request should be sent without a key")

    monkeypatch.setattr(memory, "_post", _post)
    stats = await memory.memory_stats({})
    listing = await memory.memory_list({"kind": "episode"})

    assert calls == []
    assert stats["ok"] is False and "API key" in stats["note"]
    assert stats["cloud_key_set"] is False
    assert listing["items"] == [] and "API key" in listing["note"]


def test_cloud_key_set_reads_the_same_slice_the_backend_does(monkeypatch):
    """The key panel and the cloud client must not disagree about whether a
    key exists: one resolves the slice, the other asks the same function."""
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config", lambda: SimpleNamespace(plugins=SimpleNamespace(config={}))
    )

    monkeypatch.setattr("raven_everos_cloud.backend.resolve_api_key", lambda _slice: ("ecm-key", "file"))
    assert memory._cloud_key_set() is True

    monkeypatch.setattr("raven_everos_cloud.backend.resolve_api_key", lambda _slice: ("", None))
    assert memory._cloud_key_set() is False


@pytest.mark.asyncio
async def test_stats_keeps_the_switch_fields_when_the_plugin_is_missing(monkeypatch):
    """The note path is the one where the switch matters most: it is how a
    reader leaves a backend whose plugin is gone."""
    monkeypatch.setattr(memory, "_configured_backend", lambda: "everos")
    monkeypatch.setattr(memory, "_available_backends", lambda: ["everos-cloud"])
    with everos_plugin_absent():
        out = await memory.memory_stats({})

    assert out["note"]
    assert out["backend"] == "everos"
    assert out["available_backends"] == ["everos-cloud"]


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
    assert calls[0][0] == "/api/v2/memory/get"
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
    assert calls[0][0] == "/api/v2/memory/search"
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


def _everos_configured(monkeypatch, backend="everos"):
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda *a, **k: SimpleNamespace(
            memory=SimpleNamespace(backend=backend, user_id="u", agent_id="a"), plugins=SimpleNamespace(config={})
        ),
    )


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


def _server_that(embedding: bool | None, rerank: bool | None):
    return lambda base_url: SimpleNamespace(available=lambda s: {"embedding": embedding, "rerank": rerank}.get(s))


@pytest.mark.asyncio
async def test_search_asks_for_what_the_server_can_do(monkeypatch):
    """No embedding: keyword, whatever the track. Without it the server
    answered 422 and the page called it unreachable."""
    post, calls = _post_returning([{"data": {"agent_cases": []}}])
    monkeypatch.setattr(memory, "_post", post)
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=False, rerank=False))

    await memory.memory_list({"kind": "agent_case", "q": "x"})

    assert calls[0][1]["method"] == "keyword"
    assert "enable_llm_rerank" not in calls[0][1]


@pytest.mark.asyncio
async def test_the_agent_tabs_search_by_vector_without_a_cross_encoder(monkeypatch):
    """The default install has no rerank role. The LLM rerank lane the server
    offers instead measured 10-12 s a search -- past this page's timeout once
    the reranker has anything to read -- so the page asks for the dense half
    on its own: under two seconds, no LLM call."""
    post, calls = _post_returning([{"data": {"agent_skills": []}}])
    monkeypatch.setattr(memory, "_post", post)
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=True, rerank=False))

    await memory.memory_list({"kind": "agent_skill", "q": "x"})

    assert calls[0][1]["method"] == "vector"
    assert "enable_llm_rerank" not in calls[0][1]


def test_a_timeout_says_what_it_waited_for():
    """``str()`` of an httpx timeout is empty, which left the page reading
    ``everos unreachable:`` and nothing after the colon."""
    import httpx

    assert memory._everos_error(httpx.ReadTimeout("")) == "everos did not answer within 15s"


@pytest.mark.asyncio
async def test_a_full_server_gets_the_plain_request_and_the_profile_tab_opts_in(monkeypatch):
    post, calls = _post_returning([{"data": {"profiles": []}}])
    monkeypatch.setattr(memory, "_post", post)
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=True, rerank=True))

    await memory.memory_list({"kind": "profile", "q": "x"})

    assert calls[0][1] == {"user_id": "u1", "query": "x", "top_k": 20, "include_profile": True}


@pytest.mark.asyncio
async def test_a_refusal_carries_the_servers_own_sentence(monkeypatch):
    import httpx

    async def _post(base_url, path, body):
        request = httpx.Request("POST", "http://x" + path)
        response = httpx.Response(
            422, json={"error": {"code": "x", "message": "set enable_llm_rerank=true"}}, request=request
        )
        raise httpx.HTTPStatusError("422", request=request, response=response)

    monkeypatch.setattr(memory, "_post", _post)
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=None, rerank=None))

    with pytest.raises(InternalError, match="set enable_llm_rerank=true"):
        await memory.memory_list({"kind": "agent_case", "q": "x"})


# ── the cloud backend behind the same page ───────────────────────────────


def _raven_config(backend, slice_=None):
    plugins = SimpleNamespace(config={"everos-cloud-memory": slice_ or {}})
    return SimpleNamespace(memory=SimpleNamespace(backend=backend, user_id="u1", agent_id="a1"), plugins=plugins)


@pytest.mark.asyncio
async def test_cloud_backend_adds_bearer_header_and_skips_health(monkeypatch):
    """With ``memory.backend = "everos-cloud"`` the page reads the cloud slice,
    sends the key as a Bearer header and never asks the OSS ``/health``."""
    import httpx

    monkeypatch.undo()  # the autouse ``_cfg`` stub stands in for the local plugin; this test wants the real one
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: _raven_config("everos-cloud", {"api_key": "ecm-key-7f3a", "base_url": "http://cloud.test"}),
    )

    def _boom(*a, **k):
        raise AssertionError("the cloud backend must not probe /health")

    monkeypatch.setattr("raven_everos.health.probe_capabilities", _boom)
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"episodes": [], "total_count": 0, "count": 0}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(_handler), **kw))

    assert memory._cfg() == ("http://cloud.test", "u1", "a1")
    assert memory._auth_headers() == {"Authorization": "Bearer ecm-key-7f3a"}
    assert await memory._search_tuning("http://cloud.test", "profile") == {"include_profile": True}
    assert await memory._search_tuning("http://cloud.test", "agent_case") == {}
    await memory._post("http://cloud.test", "/api/v2/memory/get", {"user_id": "u1", "memory_type": "episode"})
    assert seen[0].headers["authorization"] == "Bearer ecm-key-7f3a"
    assert seen[0].url.path == "/api/v2/memory/get"


@pytest.mark.asyncio
async def test_everos_backend_sends_no_header_and_keeps_its_probe(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: _raven_config("everos"))
    monkeypatch.setattr("raven_everos.health.probe_capabilities", _server_that(embedding=False, rerank=True))
    assert memory._auth_headers() == {}
    assert await memory._search_tuning("http://x", "episode") == {"method": "keyword"}


def test_missing_cloud_distribution_note_names_it(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: _raven_config("everos-cloud"))
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None if name == "raven_everos_cloud" else object())
    note = memory._unavailable_note()
    assert note is not None and "everos-cloud-memory" in note and "everos-cloud" in note


def test_installed_cloud_distribution_means_the_page_is_available(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: _raven_config("everos-cloud"))
    assert memory._unavailable_note() is None


# ── memory.startLocal ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_local_answers_its_probe(monkeypatch):
    """The section's button reports success only after the server answers."""
    from raven_everos import server as everos_server

    calls: list[str] = []

    async def _ensure(base_url, *, timeout=10.0):
        calls.append(base_url)
        return None

    monkeypatch.setattr(everos_server, "ensure_everos_server", _ensure)
    monkeypatch.setattr(everos_server, "probe_health", lambda _base: everos_server.ProbeVerdict.OK)
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(plugins=SimpleNamespace(config={})),
    )

    out = await memory.memory_start_local({})

    assert out["ok"] is True and out["running"] is True
    assert out["base_url"].startswith("http")
    assert calls == [out["base_url"]]


@pytest.mark.asyncio
async def test_start_local_reports_a_refusal(monkeypatch):
    from raven_everos import server as everos_server

    async def _ensure(*_a, **_k):
        raise RuntimeError("the LLM role is not configured")

    monkeypatch.setattr(everos_server, "ensure_everos_server", _ensure)

    out = await memory.memory_start_local({})

    assert out["ok"] is False and out["running"] is False
    assert "LLM role" in out["detail"]


# ── memory.applyRuntime ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_runtime_points_the_loop_at_the_configured_backend(monkeypatch):
    """The live swap is the whole point: no generation rebuild, so the section's
    socket stays up. The handler builds the same backend the runtime would."""
    seen: dict = {}
    built: list = []

    class _Loop:
        context = SimpleNamespace(workspace="LOOP-WS")

        async def rebind_memory_backend(self, backend, *, name, user_id, agent_id, memory_top_k):
            seen.update(
                backend=backend,
                name=name,
                user_id=user_id,
                agent_id=agent_id,
                memory_top_k=memory_top_k,
            )

    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(
            memory=SimpleNamespace(backend="everos", user_id="u1", agent_id="a1", memory_top_k=5),
        ),
    )
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda: SimpleNamespace(workspace_path="CONFIG-WS"),
    )
    monkeypatch.setattr(
        "raven.core.plugin_stack.maybe_build_memory_backend",
        lambda workspace, _cfg, **_kwargs: built.append(workspace) or "BACKEND",
    )

    out = await memory.memory_apply_runtime({}, agent_loop_factory=_Loop)

    assert out == {"ok": True, "backend": "everos", "detail": None}
    # The loop's own workspace: the gateway can serve a project other than the
    # one its config file names.
    assert built == ["LOOP-WS"]
    assert seen == {
        "backend": "BACKEND",
        "name": "everos",
        "user_id": "u1",
        "agent_id": "a1",
        "memory_top_k": 5,
    }


@pytest.mark.asyncio
async def test_apply_runtime_without_a_loop_is_reported():
    out = await memory.memory_apply_runtime({}, agent_loop_factory=None)

    assert out["ok"] is False
    assert "no running agent loop" in out["detail"]


@pytest.mark.asyncio
async def test_apply_runtime_reports_a_backend_that_could_not_be_built(monkeypatch):
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(
            memory=SimpleNamespace(backend="everos-cloud", user_id="u", agent_id="a", memory_top_k=5),
        ),
    )
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda: SimpleNamespace(workspace_path=SimpleNamespace()),
    )
    monkeypatch.setattr("raven.core.plugin_stack.maybe_build_memory_backend", lambda _w, _c, **_kwargs: None)

    class _Loop:
        async def rebind_memory_backend(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("nothing to rebind")

    out = await memory.memory_apply_runtime({}, agent_loop_factory=_Loop)

    assert out["ok"] is False
    assert "could not be built" in out["detail"]


# ── the section's probes, on a config or plugin that cannot answer ───────


def _unreadable_config(*_args, **_kwargs):
    raise RuntimeError("the config is unreadable")


def test_backend_for_section_answers_none_for_an_unreadable_config(monkeypatch):
    monkeypatch.setattr("raven.config.raven.load_raven_config", _unreadable_config)

    assert memory._backend_for_section() is None


def test_available_backends_survives_an_unreadable_config(monkeypatch):
    monkeypatch.setattr("raven.config.raven.load_raven_config", _unreadable_config)

    assert isinstance(memory._available_backends(), list)


def test_cloud_key_set_is_false_when_the_plugin_cannot_answer(monkeypatch):
    cloud_backend = pytest.importorskip("raven_everos_cloud.backend")

    def _boom(_slice):
        raise RuntimeError("the plugin is broken")

    monkeypatch.setattr(cloud_backend, "resolve_api_key", _boom)
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(plugins=SimpleNamespace(config={})),
    )

    assert memory._cloud_key_set() is False


@pytest.mark.asyncio
async def test_start_local_reports_an_unreadable_config(monkeypatch):
    monkeypatch.setattr("raven.config.raven.load_raven_config", _unreadable_config)

    out = await memory.memory_start_local({})

    assert out["ok"] is False
    assert "config unreadable" in out["detail"]


@pytest.mark.asyncio
async def test_apply_runtime_reports_a_factory_that_raises():
    def _factory():
        raise RuntimeError("half built")

    out = await memory.memory_apply_runtime({}, agent_loop_factory=_factory)

    assert out["ok"] is False
    assert "not available" in out["detail"]


@pytest.mark.asyncio
async def test_apply_runtime_reports_a_failed_rebind(monkeypatch):
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(
            memory=SimpleNamespace(backend="everos", user_id="u1", agent_id="a1", memory_top_k=5),
        ),
    )
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda: SimpleNamespace(workspace_path="WS"),
    )
    monkeypatch.setattr("raven.core.plugin_stack.maybe_build_memory_backend", lambda _w, _c, **_kwargs: "BACKEND")

    class _Loop:
        async def rebind_memory_backend(self, *_args, **_kwargs):
            raise RuntimeError("swap refused")

    out = await memory.memory_apply_runtime({}, agent_loop_factory=_Loop)

    assert out["ok"] is False
    assert "swap refused" in out["detail"]


# ── memory.switchStatus ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_status_reports_the_turns_in_flight():
    class _Loop:
        active_turns = 2

    out = await memory.memory_switch_status({}, agent_loop_factory=_Loop)

    assert out == {"active": True, "turns": 2}


@pytest.mark.asyncio
async def test_switch_status_is_idle_without_a_loop_or_a_reader():
    class _OlderLoop:
        pass

    assert await memory.memory_switch_status({}, agent_loop_factory=None) == {"active": False, "turns": 0}
    assert await memory.memory_switch_status({}, agent_loop_factory=_OlderLoop) == {"active": False, "turns": 0}


@pytest.mark.asyncio
async def test_switch_status_does_not_block_on_a_factory_that_raises():
    def _factory():
        raise RuntimeError("half built")

    assert await memory.memory_switch_status({}, agent_loop_factory=_factory) == {"active": False, "turns": 0}


@pytest.mark.asyncio
async def test_apply_runtime_builds_with_the_generations_assembly(monkeypatch):
    """The switch rebuilds the backend with the registry and notify boot used,
    not a fresh disk read and a dropped notice."""
    from raven.core.runtime import MemoryAssembly

    seen: dict = {}

    class _Loop:
        context = SimpleNamespace(workspace="LOOP-WS")
        memory_assembly = MemoryAssembly(registry="REGISTRY", notify="NOTIFY")

        async def rebind_memory_backend(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: SimpleNamespace(
            memory=SimpleNamespace(backend="everos", user_id="u", agent_id="a", memory_top_k=5),
        ),
    )
    monkeypatch.setattr(
        "raven.core.plugin_stack.maybe_build_memory_backend",
        lambda workspace, _cfg, **kwargs: seen.update(workspace=workspace, **kwargs) or "BACKEND",
    )

    out = await memory.memory_apply_runtime({}, agent_loop_factory=_Loop)

    assert out["ok"] is True
    assert seen == {"workspace": "LOOP-WS", "registry": "REGISTRY", "notify": "NOTIFY"}
