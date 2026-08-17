"""EM-2 — EverosBackend embedded mode.

Adapter injection: tests build :class:`_FakeAdapter` instances and pass
them directly into :class:`EverosBackend(ctx, adapter=...)`. This keeps
the tests hermetic regardless of whether ``everos`` is importable in
the active venv (this matters — everos's runtime requires LLM /
embedding services that the test environment doesn't have).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("raven.plugin.memory.everos")

from raven.memory_engine import MemoryBackend
from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos.backend import (
    EverosBackend,
    _NoOpAdapter,
    _RealEverosAdapter,
    make_backend,
)

# ---------------------------------------------------------------------------
# Fake adapter — records calls + returns canned data
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, *, search_response: Any = None) -> None:
        self.search_calls: list[dict] = []
        self.memorize_calls: list[dict] = []
        self.search_response = search_response
        self.search_raises: Exception | None = None
        self.memorize_raises: Exception | None = None

    async def search(self, *, user_id, agent_id, query, top_k):
        self.search_calls.append(
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "query": query,
                "top_k": top_k,
            }
        )
        if self.search_raises is not None:
            raise self.search_raises
        return self.search_response

    async def memorize(self, session_id, payload_messages, *, is_final=False):
        self.memorize_calls.append(
            {
                "session_id": session_id,
                "payload_messages": payload_messages,
                "is_final": is_final,
            }
        )
        if self.memorize_raises is not None:
            raise self.memorize_raises


def _ctx(tmp_path: Path, **config: Any) -> PluginContext:
    return PluginContext(
        config={"mode": "embedded", **config},
        services=ServiceLocator(workspace=tmp_path),
    )


def _backend(tmp_path: Path, **kw: Any) -> EverosBackend:
    adapter = kw.pop("adapter", _FakeAdapter())
    return EverosBackend(_ctx(tmp_path, **kw), adapter=adapter)


# ---------------------------------------------------------------------------
# Construction + Protocol conformance
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_protocol_conformance(self, tmp_path: Path) -> None:
        b = _backend(tmp_path)
        assert isinstance(b, MemoryBackend)

    def test_invalid_mode_raises(self, tmp_path: Path) -> None:
        """A typo'd mode fails fast at construction rather than silently
        degrading to a no-op adapter (memory quietly disabled)."""
        with pytest.raises(ValueError, match="invalid mode"):
            EverosBackend(_ctx(tmp_path, mode="embeded"))

    def test_embedded_mode_defers_adapter_to_start(self, tmp_path: Path) -> None:
        """Embedded mode defers the (heavy everos/lancedb) adapter build to
        start(): at construction the adapter is None, so the ~2-3s import stays
        off the render-blocking build path. start() then selects real/no-op."""
        b = EverosBackend(_ctx(tmp_path, mode="embedded"))
        assert b._adapter is None

    def test_make_backend_factory(self, tmp_path: Path) -> None:
        b = make_backend(_ctx(tmp_path))
        assert isinstance(b, EverosBackend)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_stop_idempotent(self, tmp_path: Path) -> None:
        b = _backend(tmp_path)
        await b.start()
        await b.stop()
        await b.start()
        await b.stop()

    async def test_start_builds_deferred_embedded_adapter(self, tmp_path: Path) -> None:
        """start() builds the embedded adapter that __init__ deferred."""
        b = EverosBackend(_ctx(tmp_path, mode="embedded"))
        assert b._adapter is None
        try:
            await b.start()
            assert isinstance(b._adapter, (_NoOpAdapter, _RealEverosAdapter))
        finally:
            await b.stop()

    async def test_recall_and_store_degrade_before_adapter_built(self, tmp_path: Path) -> None:
        """Before start() builds the deferred adapter, recall returns empty and
        store is a no-op (the render-window degrade), not a crash."""
        b = EverosBackend(_ctx(tmp_path, mode="embedded"))
        assert b._adapter is None
        assert await b.recall("hi", user_id="alice", top_k=5) == []
        await b.store("sess", [{"role": "user", "content": "hi"}])  # must not raise


# ---------------------------------------------------------------------------
# Track-id routing
# ---------------------------------------------------------------------------


class TestTrackIdRouting:
    async def test_user_id_routes_to_user_track(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.recall("hi", user_id="alice", top_k=5)
        assert adapter.search_calls[0]["user_id"] == "alice"
        assert adapter.search_calls[0]["agent_id"] is None

    async def test_agent_id_forwarded_to_search(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        # recall now forwards the passed agent_id straight to search;
        # the configured agent_id is used only by store().
        b = EverosBackend(
            _ctx(tmp_path, agent_id="agt_fixed"),
            adapter=adapter,
        )
        await b.recall("hi", agent_id="agent:passed-in", top_k=3)
        assert adapter.search_calls[0]["agent_id"] == "agent:passed-in"
        assert adapter.search_calls[0]["user_id"] is None

    async def test_recall_without_track_id_returns_empty_no_call(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", top_k=5)
        assert hits == []
        assert adapter.search_calls == []  # adapter never invoked

    async def test_recall_with_both_track_ids_returns_empty_no_call(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="alice", agent_id="agt", top_k=5)
        assert hits == []
        assert adapter.search_calls == []  # adapter never invoked


# ---------------------------------------------------------------------------
# Search → Memory conversion (user-track)
# ---------------------------------------------------------------------------


def _user_search_data(
    episodes: list[Any] | None = None,
    profiles: list[Any] | None = None,
) -> SimpleNamespace:
    """Build a SearchData-shaped namespace for user-track responses."""
    return SimpleNamespace(
        episodes=episodes or [],
        profiles=profiles or [],
        agent_cases=[],
        agent_skills=[],
    )


def _agent_search_data(
    cases: list[Any] | None = None,
    skills: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        episodes=[],
        profiles=[],
        agent_cases=cases or [],
        agent_skills=skills or [],
    )


class TestUserSearchConversion:
    async def test_episodes_become_memories(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                episodes=[
                    SimpleNamespace(
                        id="ep1",
                        session_id="s1",
                        summary="liked espresso",
                        episode="full text",
                        score=0.92,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("coffee", user_id="alice", top_k=5)
        assert len(hits) == 1
        h = hits[0]
        assert h.text == "liked espresso"
        assert h.score == pytest.approx(0.92)
        assert h.metadata["type"] == "episode"
        assert h.metadata["owner_type"] == "user"
        assert h.metadata["id"] == "ep1"

    async def test_episode_falls_back_to_full_text_when_no_summary(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                episodes=[
                    SimpleNamespace(
                        id="ep1",
                        session_id="s1",
                        summary="",
                        episode="raw content",
                        score=0.5,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="x", top_k=5)
        assert hits[0].text == "raw content"

    async def test_profile_rendered_as_key_value_lines(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                profiles=[
                    SimpleNamespace(
                        id="prof1",
                        profile_data={"name": "Alice", "tz": "PST"},
                        score=None,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="alice", top_k=5)
        assert hits[0].text == "name: Alice\ntz: PST"
        assert hits[0].score == pytest.approx(1.0)  # None → 1.0

    async def test_hits_sorted_by_score_desc(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                episodes=[
                    SimpleNamespace(id="a", session_id="s", summary="lo", episode="", score=0.3),
                    SimpleNamespace(id="b", session_id="s", summary="hi", episode="", score=0.9),
                    SimpleNamespace(id="c", session_id="s", summary="mid", episode="", score=0.6),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="x", top_k=5)
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Search → Memory conversion (agent-track)
# ---------------------------------------------------------------------------


class TestAgentSearchConversion:
    async def test_skills_become_memories(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(
            search_response=_agent_search_data(
                skills=[
                    SimpleNamespace(
                        id="sk1",
                        name="git-resolver",
                        description="resolves git refs",
                        content="step 1 ...",
                        confidence=0.85,
                        score=0.77,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("git", agent_id="agent:default", top_k=5)
        assert hits[0].text == "step 1 ..."
        assert hits[0].metadata["name"] == "git-resolver"
        assert hits[0].metadata["confidence"] == pytest.approx(0.85)
        assert hits[0].metadata["type"] == "skill"

    async def test_cases_include_key_insight(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(
            search_response=_agent_search_data(
                cases=[
                    SimpleNamespace(
                        id="c1",
                        task_intent="resolve git conflict",
                        approach="step-by-step",
                        quality_score=0.9,
                        key_insight="use rerere",
                        score=0.8,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("git", agent_id="agent:default", top_k=5)
        assert "resolve git conflict" in hits[0].text
        assert "use rerere" in hits[0].text
        assert hits[0].metadata["type"] == "case"


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    async def test_adapter_search_exception_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        adapter.search_raises = RuntimeError("everos unreachable")
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="x", top_k=5)
        assert hits == []  # logged + swallowed

    async def test_none_response_returns_empty(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(search_response=None)
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="x", top_k=5)
        assert hits == []


# ---------------------------------------------------------------------------
# Store conversion
# ---------------------------------------------------------------------------


class TestStoreConversion:
    async def test_messages_converted_to_everos_shape(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "session-1",
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi back"},
            ],
        )
        assert adapter.memorize_calls[0]["session_id"] == "session-1"
        payload = adapter.memorize_calls[0]["payload_messages"]
        assert len(payload) == 2
        # Required EverOS fields synthesized
        for entry in payload:
            assert isinstance(entry["sender_id"], str) and entry["sender_id"]
            assert isinstance(entry["timestamp"], int) and entry["timestamp"] > 0
            assert entry["role"] in ("user", "assistant", "tool")
            assert isinstance(entry["content"], str) and entry["content"]

    async def test_sender_id_stamped_by_owner_policy(
        self,
        tmp_path: Path,
    ) -> None:
        """assistant/tool sender_id -> configured agent_id; user sender_id
        kept (the user identity the host supplies / recall queries)."""
        adapter = _FakeAdapter()
        b = EverosBackend(_ctx(tmp_path, agent_id="agt_x"), adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "user", "content": "hi", "sender_id": "alice"},
                {"role": "assistant", "content": "hello"},
                {"role": "tool", "content": "result"},
            ],
        )
        by_role = {m["role"]: m["sender_id"] for m in adapter.memorize_calls[0]["payload_messages"]}
        assert by_role["assistant"] == "agt_x"
        assert by_role["tool"] == "agt_x"
        assert by_role["user"] == "alice"

    async def test_system_role_dropped(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "system", "content": "you are an agent"},
                {"role": "user", "content": "hi"},
            ],
        )
        payload = adapter.memorize_calls[0]["payload_messages"]
        roles = [m["role"] for m in payload]
        assert "system" not in roles
        assert roles == ["user"]

    async def test_empty_content_dropped(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "user", "content": ""},
                {"role": "user", "content": "actual"},
            ],
        )
        payload = adapter.memorize_calls[0]["payload_messages"]
        contents = [m["content"] for m in payload]
        assert contents == ["actual"]

    async def test_multimodal_flattens_to_text(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part1"},
                        {"type": "image_url", "image_url": {"url": "..."}},
                        {"type": "text", "text": "part2"},
                    ],
                },
            ],
        )
        payload = adapter.memorize_calls[0]["payload_messages"]
        assert payload[0]["content"] == "part1 part2"

    async def test_empty_messages_skips_adapter(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store("s", [])
        assert adapter.memorize_calls == []

    async def test_all_system_messages_skips_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "system", "content": "x"},
                {"role": "system", "content": "y"},
            ],
        )
        # Conversion yields empty list — adapter skipped.
        assert adapter.memorize_calls == []

    async def test_explicit_sender_id_preserved(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "user", "content": "x", "sender_id": "alice-123"},
            ],
        )
        assert adapter.memorize_calls[0]["payload_messages"][0]["sender_id"] == "alice-123"

    async def test_memorize_exception_swallowed(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        adapter.memorize_raises = RuntimeError("everos down")
        b = _backend(tmp_path, adapter=adapter)
        # Backend.store does NOT raise; AgentLoop's after-turn step
        # should never be derailed by a backend store failure.
        await b.store("s", [{"role": "user", "content": "x"}])


# ---------------------------------------------------------------------------
# Default identity alignment (store side must match recall-side defaults)
# ---------------------------------------------------------------------------


class TestDefaultIdentityAlignment:
    async def test_user_track_default_owner_is_default(self, tmp_path: Path) -> None:
        """Backend with no user_id in config stamps user messages with
        'default', not 'raven-user', so store and recall use the same owner."""
        adapter = _FakeAdapter()
        b = EverosBackend(_ctx(tmp_path), adapter=adapter)
        await b.store("s", [{"role": "user", "content": "hi"}])
        payload = adapter.memorize_calls[0]["payload_messages"]
        assert payload[0]["sender_id"] == "default"

    async def test_agent_track_default_id_is_default(self, tmp_path: Path) -> None:
        """Backend with no agent_id in config resolves _agent_id to 'default',
        not 'agent:default', and stamps assistant messages accordingly."""
        adapter = _FakeAdapter()
        b = EverosBackend(_ctx(tmp_path), adapter=adapter)
        assert b._agent_id == "default"
        await b.store("s", [{"role": "assistant", "content": "hello"}])
        payload = adapter.memorize_calls[0]["payload_messages"]
        assert payload[0]["sender_id"] == "default"

    async def test_explicit_user_and_agent_id_preserved(self, tmp_path: Path) -> None:
        """Explicitly configured user_id and agent_id are used verbatim."""
        adapter = _FakeAdapter()
        b = EverosBackend(_ctx(tmp_path, user_id="alice", agent_id="bob"), adapter=adapter)
        await b.store(
            "s",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        payload = adapter.memorize_calls[0]["payload_messages"]
        by_role = {m["role"]: m["sender_id"] for m in payload}
        assert by_role["user"] == "alice"
        assert by_role["assistant"] == "bob"


# ---------------------------------------------------------------------------
# Feedback — no-op contract
# ---------------------------------------------------------------------------


class TestFeedback:
    async def test_feedback_accepts_any_signals(self, tmp_path: Path) -> None:
        b = _backend(tmp_path)
        await b.feedback({})
        await b.feedback({"kind": "skill_usage", "ids": ["x"]})
        await b.feedback({"arbitrary": object()})


class TestRerankDegrade:
    """``_RealEverosAdapter`` picks the everos search method by track +
    rerank availability: agent-track HYBRID needs a cross-encoder rerank
    provider (everos raises without one), so when rerank is unconfigured
    the adapter degrades the agent track to VECTOR (no rerank). The user
    track never touches the reranker and stays HYBRID regardless.
    """

    @staticmethod
    async def _capture_method(*, rerank_configured: bool, user_id, agent_id):
        from types import SimpleNamespace

        from everos.memory.search.dto import SearchMethod

        adapter = _RealEverosAdapter()
        adapter._rerank_configured = rerank_configured
        captured: dict = {}

        async def _fake_search(req):
            captured["method"] = req.method
            return SimpleNamespace(data=None)

        adapter._search_fn = _fake_search
        await adapter.search(user_id=user_id, agent_id=agent_id, query="q", top_k=5)
        return captured["method"], SearchMethod

    async def test_agent_degrades_to_vector_without_rerank(self) -> None:
        method, SearchMethod = await self._capture_method(
            rerank_configured=False,
            user_id=None,
            agent_id="agent-x",
        )
        assert method == SearchMethod.VECTOR

    async def test_agent_uses_hybrid_with_rerank(self) -> None:
        method, SearchMethod = await self._capture_method(
            rerank_configured=True,
            user_id=None,
            agent_id="agent-x",
        )
        assert method == SearchMethod.HYBRID

    async def test_user_stays_hybrid_without_rerank(self) -> None:
        # User track never hits the cross-encoder lane, so no degrade.
        method, SearchMethod = await self._capture_method(
            rerank_configured=False,
            user_id="user-x",
            agent_id=None,
        )
        assert method == SearchMethod.HYBRID


# ---------------------------------------------------------------------------
# LanceDB schema migration
# ---------------------------------------------------------------------------


class TestMigrateLancedbSchemas:
    """``_migrate_lancedb_schemas`` adds missing columns to existing
    LanceDB tables so upgrading users don't need to manually clear
    their index."""

    @staticmethod
    def _stub_schema(table_name: str, fields: set[str]):
        class _S:
            TABLE_NAME = table_name
            model_fields = {f: None for f in fields}

        return _S

    @staticmethod
    def _stub_table(columns: set[str]):
        import pyarrow as pa

        arrow = pa.schema([pa.field(c, pa.utf8()) for c in sorted(columns)])
        added: list = []

        class _T:
            async def schema(self):
                return arrow

            async def add_columns(self, new_schema):
                added.append(new_schema)

        return _T(), added

    async def test_adds_missing_columns(self) -> None:
        import logging

        from raven.plugin.memory.everos.backend import _migrate_lancedb_schemas

        table, added = self._stub_table({"id", "text"})
        schema_cls = self._stub_schema("tbl", {"id", "text", "new_col"})

        async def _noop():
            return None

        async def _get_table(_name, _schema):
            return table

        result = await _migrate_lancedb_schemas(
            logging.getLogger("test"),
            _schemas=[schema_cls],
            _get_connection=_noop,
            _get_table=_get_table,
        )
        assert result is True
        assert len(added) == 1
        assert "new_col" in added[0].names

    async def test_no_missing_returns_false(self) -> None:
        import logging

        from raven.plugin.memory.everos.backend import _migrate_lancedb_schemas

        table, added = self._stub_table({"id", "text"})
        schema_cls = self._stub_schema("tbl", {"id", "text"})

        async def _noop():
            return None

        async def _get_table(_name, _schema):
            return table

        result = await _migrate_lancedb_schemas(
            logging.getLogger("test"),
            _schemas=[schema_cls],
            _get_connection=_noop,
            _get_table=_get_table,
        )
        assert result is False
        assert len(added) == 0
