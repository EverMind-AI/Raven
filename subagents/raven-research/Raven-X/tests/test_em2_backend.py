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

from raven.memory_engine import FlushableContractTests, MemoryBackend
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

    def test_embedded_mode_selects_real_or_no_op(self, tmp_path: Path) -> None:
        """Embedded mode picks the real adapter when everos imports
        cleanly, else falls back to no-op. Both are valid; we only
        assert the type is one of the two so the test stays hermetic
        whether or not everos is installed in the active venv."""
        b = EverosBackend(_ctx(tmp_path, mode="embedded"))
        assert isinstance(b._adapter, (_NoOpAdapter, _RealEverosAdapter))

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
        # The full episode wins over the summary: everos cuts ``summary``
        # to a 200-character prefix of ``episode``, mid-word.
        assert h.text == "full text"
        assert h.score == pytest.approx(0.92)
        assert h.metadata["type"] == "episode"
        assert h.metadata["owner_type"] == "user"
        assert h.metadata["id"] == "ep1"

    async def test_episode_falls_back_to_summary_when_no_full_text(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                episodes=[
                    SimpleNamespace(
                        id="ep1",
                        session_id="s1",
                        summary="the 200-char prefix",
                        episode="",
                        score=0.5,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="x", top_k=5)
        assert hits[0].text == "the 200-char prefix"

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
                {"role": "tool", "content": "result", "tool_call_id": "call_1"},
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
# Reasoning capture (opt-in fold into the assistant text)
# ---------------------------------------------------------------------------


_TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{}"}}


class TestReasoningCapture:
    async def test_reasoning_is_dropped_by_default(self, tmp_path: Path) -> None:
        """Default stays byte-identical to before the option existed."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)
        await b.store(
            "s",
            [
                {
                    "role": "assistant",
                    "content": "checking the config",
                    "reasoning_content": "SECRET_DELIBERATION",
                    "tool_calls": [_TOOL_CALL],
                },
            ],
        )
        payload = adapter.memorize_calls[0]["payload_messages"]
        assert payload[0]["content"] == "checking the config"

    async def test_reasoning_folded_when_enabled(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, capture_reasoning=True)
        await b.store(
            "s",
            [
                {
                    "role": "assistant",
                    "content": "checking the config",
                    "reasoning_content": "the index exists, so this is a plan problem",
                    "tool_calls": [_TOOL_CALL],
                },
            ],
        )
        content = adapter.memorize_calls[0]["payload_messages"][0]["content"]
        assert content.startswith("[reasoning]\n")
        assert "the index exists, so this is a plan problem" in content
        assert "[/reasoning]" in content
        # The conclusion still ends the message: reasoning precedes it, in
        # generation order, rather than replacing or trailing it.
        assert content.endswith("checking the config")

    async def test_textless_tool_calling_row_gains_its_reasoning(self, tmp_path: Path) -> None:
        """A quarter of real assistant rows are textless with tool_calls;
        without the fold they arrive at EverOS carrying nothing at all."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, capture_reasoning=True)
        await b.store(
            "s",
            [{"role": "assistant", "content": "", "reasoning_content": "need the plan", "tool_calls": [_TOOL_CALL]}],
        )
        assert adapter.memorize_calls[0]["payload_messages"][0]["content"] == (
            "[reasoning]\nneed the plan\n[/reasoning]"
        )

    async def test_final_answer_reasoning_is_not_folded(self, tmp_path: Path) -> None:
        """No tool_calls means the user track's renderer keeps this row, so
        folding here would put deliberation into profiles and facts."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, capture_reasoning=True)
        await b.store(
            "s",
            [{"role": "assistant", "content": "the answer is 42", "reasoning_content": "DELIBERATION"}],
        )
        assert adapter.memorize_calls[0]["payload_messages"][0]["content"] == "the answer is 42"

    async def test_reasoning_clipped_to_its_own_budget(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, capture_reasoning=True, capture_reasoning_max_chars=10)
        await b.store(
            "s",
            [{"role": "assistant", "content": "go", "reasoning_content": "y" * 500, "tool_calls": [_TOOL_CALL]}],
        )
        content = adapter.memorize_calls[0]["payload_messages"][0]["content"]
        assert "y" * 10 in content
        assert "y" * 11 not in content

    async def test_empty_reasoning_changes_nothing(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, capture_reasoning=True)
        await b.store(
            "s",
            [{"role": "assistant", "content": "go", "reasoning_content": "   ", "tool_calls": [_TOOL_CALL]}],
        )
        assert adapter.memorize_calls[0]["payload_messages"][0]["content"] == "go"


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
# Session-id mapping — one host session is one everos session, across processes
# ---------------------------------------------------------------------------


class _FlushableFakeAdapter(_FakeAdapter):
    """``_FakeAdapter`` plus the flush the deferred profile needs."""

    def __init__(self) -> None:
        super().__init__()
        self.flush_calls: list[str] = []

    async def flush(self, session_id: str) -> None:
        self.flush_calls.append(session_id)


class TestSessionIdMapping:
    async def test_same_host_key_resolves_to_one_everos_session_across_instances(
        self,
        tmp_path: Path,
    ) -> None:
        """``-m ... -s one_session`` run twice is one conversation, not two.

        The host restores the session log across processes and the CLI help
        promises the turns accumulate, so the generated everos session id has
        to survive the process too. Held in memory only, each invocation minted
        a fresh uuid and left N unmergeable single-turn buffers.
        """
        a1 = _FlushableFakeAdapter()
        b1 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="raven_dr", defer_extraction=True),
            adapter=a1,
        )
        await b1.store("cli:abc", [{"role": "user", "content": "turn one"}])
        first = a1.memorize_calls[0]["session_id"]

        a2 = _FlushableFakeAdapter()
        b2 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="raven_dr", defer_extraction=True),
            adapter=a2,
        )
        await b2.store("cli:abc", [{"role": "user", "content": "turn two"}])

        assert a2.memorize_calls[0]["session_id"] == first
        assert first.startswith("raven_dr_")

    async def test_a_different_host_key_gets_its_own_session(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(_ctx(tmp_path, session_id_prefix="p"), adapter=adapter)
        await b.store("cli:one", [{"role": "user", "content": "x"}])
        await b.store("cli:two", [{"role": "user", "content": "y"}])
        ids = {c["session_id"] for c in adapter.memorize_calls}
        assert len(ids) == 2

    async def test_the_map_is_scoped_by_app_and_project(self, tmp_path: Path) -> None:
        """``(app_id, project_id)`` is hard isolation server-side, so the same
        host key under a different scope must not inherit the other's id."""
        a1 = _FlushableFakeAdapter()
        b1 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", app_id="raven", project_id="round_a"),
            adapter=a1,
        )
        await b1.store("cli:abc", [{"role": "user", "content": "x"}])

        a2 = _FlushableFakeAdapter()
        b2 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", app_id="raven", project_id="round_b"),
            adapter=a2,
        )
        await b2.store("cli:abc", [{"role": "user", "content": "x"}])

        assert a1.memorize_calls[0]["session_id"] != a2.memorize_calls[0]["session_id"]

    async def test_a_later_process_can_promote_what_an_earlier_one_buffered(
        self,
        tmp_path: Path,
    ) -> None:
        """The point of persisting the map: the last of several ``-m`` calls
        is the one that promotes, and it must resolve the id the first one
        minted."""
        a1 = _FlushableFakeAdapter()
        b1 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=a1,
        )
        await b1.store("cli:abc", [{"role": "user", "content": "x"}])
        minted = a1.memorize_calls[0]["session_id"]

        a2 = _FlushableFakeAdapter()
        b2 = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=a2,
        )
        await b2.flush("cli:abc")

        assert a2.flush_calls == [minted]

    async def test_flush_skips_a_session_that_was_never_written(
        self,
        tmp_path: Path,
    ) -> None:
        """With a prefix configured, no map entry proves no id was ever minted,
        so there is no buffer to promote -- and the flush budget is minutes."""
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(_ctx(tmp_path, session_id_prefix="p"), adapter=adapter)

        await b.flush("cli:never-written")

        assert adapter.flush_calls == []

    async def test_flush_passes_the_host_key_through_without_a_prefix(
        self,
        tmp_path: Path,
    ) -> None:
        """No prefix means the host key *is* the everos session id, and a
        buffer under it may have been written by a process that left no local
        trace -- so there is nothing that could prove it absent."""
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(_ctx(tmp_path), adapter=adapter)

        await b.flush("cli:abc")

        assert adapter.flush_calls == ["cli:abc"]

    async def test_flush_skips_when_the_store_breaker_is_open(
        self,
        tmp_path: Path,
    ) -> None:
        """Every write was suppressed, so nothing is buffered -- promoting it
        would spend the adapter's whole flush budget to learn that."""
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(_ctx(tmp_path), adapter=adapter)
        b._store_breaker_open = True

        await b.flush("cli:abc")

        assert adapter.flush_calls == []

    async def test_an_unwritable_workspace_falls_back_to_process_local(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Persisting the map is best-effort: a read-only workspace costs
        cross-process resumption, not the turn."""
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(_ctx(tmp_path, session_id_prefix="p"), adapter=adapter)
        monkeypatch.setattr(
            Path,
            "write_text",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
        )

        await b.store("cli:abc", [{"role": "user", "content": "x"}])

        assert adapter.memorize_calls[0]["session_id"].startswith("p_")


class TestEverosFlushContract(FlushableContractTests):
    """Run the shared optional-capability contract against the real backend.

    The sample backend in ``test_memory_backend_contract.py`` proves the
    contract's shape; this proves the shipped plugin satisfies it.
    """

    async def make_backend(self):
        import tempfile

        return EverosBackend(
            _ctx(Path(tempfile.mkdtemp())),
            adapter=_FlushableFakeAdapter(),
        )


class TestRequireServiceScope:
    async def test_require_service_warns_when_the_mode_cannot_probe(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only the HTTP adapter has /health. Silently giving a run zero
        protection is worse than not having the switch, and "scripted capture
        that must not write into nothing" is exactly the case that reaches for
        it."""
        b = EverosBackend(
            _ctx(tmp_path, require_service=True),
            adapter=_FakeAdapter(),
        )
        with caplog.at_level("WARNING"):
            await b._check_service()

        assert any("require_service=true has no effect" in r.message for r in caplog.records)

    async def test_no_warning_when_the_switch_is_off(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        b = EverosBackend(_ctx(tmp_path), adapter=_FakeAdapter())
        with caplog.at_level("WARNING"):
            await b._check_service()

        assert not any("require_service" in r.message for r in caplog.records)


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


class TestDeferredSessionSidecar:
    """The sidecar names what a later flush has to promote, so its rows have to
    survive several processes without the file growing once per process."""

    def _rows(self, tmp_path: Path) -> list[dict]:
        """Sidecar rows that parse. Unparseable lines are counted by
        :meth:`_line_count` instead, so a fixture's deliberate junk line does
        not make the helper raise on the way to the assertion."""
        import json

        rows: list[dict] = []
        for line in self._lines(tmp_path):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _lines(self, tmp_path: Path) -> list[str]:
        path = tmp_path / ".everos_sessions.jsonl"
        if not path.exists():
            return []
        return [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    async def test_one_row_survives_several_processes_on_one_session(
        self,
        tmp_path: Path,
    ) -> None:
        """Several ``-m`` calls sharing one ``-s`` are separate processes that
        resolve to the same everos session. The in-memory guard cannot see
        across them, so each appended the same row again."""
        for _ in range(4):
            b = EverosBackend(
                _ctx(
                    tmp_path,
                    session_id_prefix="p",
                    defer_extraction=True,
                    app_id="raven",
                    project_id="proj",
                ),
                adapter=_FlushableFakeAdapter(),
            )
            await b.store("cli:abc", [{"role": "user", "content": "x"}])

        rows = self._rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["app_id"] == "raven"
        assert rows[0]["project_id"] == "proj"

    async def test_distinct_sessions_each_get_a_row(self, tmp_path: Path) -> None:
        """Dedupe must not collapse genuinely different sessions -- the flush
        script would then skip whatever it could not see."""
        b = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=_FlushableFakeAdapter(),
        )
        await b.store("cli:one", [{"role": "user", "content": "x"}])
        await b.store("cli:two", [{"role": "user", "content": "y"}])
        assert len({r["session_id"] for r in self._rows(tmp_path)}) == 2

    async def test_a_corrupt_line_does_not_lose_the_new_row(
        self,
        tmp_path: Path,
    ) -> None:
        """The read only feeds a duplicate check. A malformed line must cost a
        redundant append, never a missing record -- an unrecorded session is a
        session nobody flushes."""
        (tmp_path / ".everos_sessions.jsonl").write_text(
            "not json at all\n", encoding="utf-8"
        )
        b = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=_FlushableFakeAdapter(),
        )
        await b.store("cli:abc", [{"role": "user", "content": "x"}])
        assert len(self._rows(tmp_path)) == 1
        # The junk line is left alone rather than rewritten around: this path
        # only ever appends.
        assert len(self._lines(tmp_path)) == 2

    async def test_the_eager_profile_writes_no_sidecar(self, tmp_path: Path) -> None:
        """Nothing stays buffered when every turn flushes, so there is nothing
        for a later flush to find."""
        b = EverosBackend(_ctx(tmp_path, session_id_prefix="p"), adapter=_FakeAdapter())
        await b.store("cli:abc", [{"role": "user", "content": "x"}])
        assert self._rows(tmp_path) == []


class TestFailureAccounting:
    """A failed store and a failed flush lose different things, so they are
    counted and reported apart."""

    async def test_a_failed_store_is_counted_as_a_store_failure(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        adapter.memorize_raises = RuntimeError("service down")
        b = _backend(tmp_path, adapter=adapter)
        await b.store("s", [{"role": "user", "content": "x"}])
        assert b._store_failures == 1
        assert b._flush_failures == 0

    async def test_a_failed_flush_is_not_counted_as_a_store_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """The turns reached the buffer; only promotion failed. Counting it as a
        store failure sends the reader looking for a write that did happen."""
        adapter = _FlushableFakeAdapter()
        b = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=adapter,
        )
        await b.store("cli:abc", [{"role": "user", "content": "x"}])
        assert b._store_failures == 0

        async def _boom(_session_id: str) -> None:
            raise RuntimeError("500 from /flush")

        adapter.flush = _boom  # type: ignore[method-assign]
        await b.flush("cli:abc")
        assert b._flush_failures == 1
        assert b._store_failures == 0

    async def test_a_failed_flush_does_not_trip_the_store_breaker(
        self,
        tmp_path: Path,
    ) -> None:
        """A flush is one request per task boundary. Letting it open the breaker
        would suppress the writes of a service that is answering /add fine."""
        from raven.plugin.memory.everos.backend import _STORE_BREAKER_THRESHOLD

        adapter = _FlushableFakeAdapter()
        b = EverosBackend(
            _ctx(tmp_path, session_id_prefix="p", defer_extraction=True),
            adapter=adapter,
        )
        await b.store("cli:abc", [{"role": "user", "content": "x"}])

        async def _boom(_session_id: str) -> None:
            raise RuntimeError("boom")

        adapter.flush = _boom  # type: ignore[method-assign]
        for _ in range(_STORE_BREAKER_THRESHOLD + 2):
            await b.flush("cli:abc")

        assert b._flush_failures == _STORE_BREAKER_THRESHOLD + 2
        assert b._store_breaker_open is False

    async def test_teardown_reports_the_two_kinds_separately(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _FakeAdapter()
        adapter.memorize_raises = RuntimeError("service down")
        b = _backend(tmp_path, adapter=adapter)
        warnings: list[str] = []
        b._logger = SimpleNamespace(  # type: ignore[assignment]
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda msg, *a, **k: warnings.append(msg % a if a else msg),
        )
        await b.store("s", [{"role": "user", "content": "x"}])
        b._flush_failures = 2
        await b.stop()

        store_line = [w for w in warnings if "store dispatches failed" in w]
        flush_line = [w for w in warnings if "flush dispatches failed" in w]
        assert len(store_line) == 1
        assert len(flush_line) == 1
        assert "never reached the service" in store_line[0]
        assert "buffered service-side" in flush_line[0]


class TestFlushBudgetInvariants:
    """The budgets on the flush path only work in one order, and getting it
    wrong is silent -- hence assertions on the relationships, not the values."""

    def test_the_flush_budget_clears_the_servers_shipped_cap(self) -> None:
        """everos ships ``memorize.session_lock_timeout_seconds = 360``. At or
        below it the client disconnects first and cannot tell "the server gave
        up" from "the server is still working"."""
        from raven.plugin.memory.everos.backend import _DEFAULT_FLUSH_TIMEOUT_S

        assert _DEFAULT_FLUSH_TIMEOUT_S > 360.0

    def test_the_flush_budget_is_looser_than_the_per_turn_one(self) -> None:
        from raven.plugin.memory.everos.backend import (
            _DEFAULT_FLUSH_TIMEOUT_S,
            _DEFAULT_HTTP_TIMEOUT_S,
        )

        assert _DEFAULT_FLUSH_TIMEOUT_S > _DEFAULT_HTTP_TIMEOUT_S

    def test_teardowns_promote_budget_clears_one_promotion(self) -> None:
        """``promote_all_backend_sessions`` cancels what it has not finished, so
        a budget below one promotion cancels every promotion it starts and the
        setting it implements silently becomes a no-op."""
        from raven.agent.loop.main import _PROMOTE_ALL_BUDGET_S
        from raven.plugin.memory.everos.backend import _DEFAULT_FLUSH_TIMEOUT_S

        assert _PROMOTE_ALL_BUDGET_S > _DEFAULT_FLUSH_TIMEOUT_S


class TestApiKeySourcing:
    """A bearer token must not have to live in a config file."""

    def _adapter(self, tmp_path: Path, **cfg: Any):
        return EverosBackend(_ctx(tmp_path, mode="http", **cfg))._adapter

    def test_the_config_supplies_the_key(self, tmp_path: Path) -> None:
        assert self._adapter(tmp_path, api_key="from-config")._api_key == "from-config"

    def test_the_environment_supplies_it_when_the_config_does_not(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The config had been the only source, which forced a secret into a
        file people share and commit."""
        monkeypatch.setenv("EVEROS_API_KEY", "from-env")
        assert self._adapter(tmp_path)._api_key == "from-env"

    def test_the_config_wins_over_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVEROS_API_KEY", "from-env")
        assert self._adapter(tmp_path, api_key="from-config")._api_key == "from-config"

    def test_no_key_anywhere_stays_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A local service needs no auth; an empty string must not become an
        ``Authorization: Bearer`` header."""
        monkeypatch.delenv("EVEROS_API_KEY", raising=False)
        assert self._adapter(tmp_path)._api_key is None
        assert self._adapter(tmp_path, api_key="")._api_key is None


class TestCleartextApiKeyRefusal:
    """A bearer token must not be put on an unencrypted network hop.

    Construction-time refusal rather than a warning: the request succeeds when
    the token leaks, so nothing downstream ever reveals the exposure.
    """

    def _build(self, tmp_path: Path, **cfg: Any) -> EverosBackend:
        return EverosBackend(_ctx(tmp_path, mode="http", **cfg))

    def test_a_key_over_plain_http_to_a_remote_host_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cleartext"):
            self._build(tmp_path, base_url="http://memory.internal:8000", api_key="k")

    def test_https_is_allowed(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="https://memory.internal", api_key="k")
        assert b._adapter._api_key == "k"

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
    def test_loopback_is_exempt(self, tmp_path: Path, host: str) -> None:
        """A loopback address never leaves the machine, so the token is
        exposed to nothing a local process could not already read."""
        b = self._build(tmp_path, base_url=f"http://{host}:8000", api_key="k")
        assert b._adapter._api_key == "k"

    def test_no_key_means_no_refusal(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="http://memory.internal:8000")
        assert b._adapter._api_key is None

    def test_the_escape_hatch_is_honoured(self, tmp_path: Path) -> None:
        b = self._build(
            tmp_path,
            base_url="http://memory.internal:8000",
            api_key="k",
            allow_insecure_api_key=True,
        )
        assert b._adapter._api_key == "k"

    def test_a_key_from_the_environment_is_checked_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The check must resolve the key the same way the adapter does, or the
        env path would bypass it entirely."""
        monkeypatch.setenv("EVEROS_API_KEY", "from-env")
        with pytest.raises(ValueError, match="cleartext"):
            self._build(tmp_path, base_url="http://memory.internal:8000")

    def test_a_base_url_from_the_environment_is_checked_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVEROS_BASE_URL", "http://memory.internal:8000")
        with pytest.raises(ValueError, match="cleartext"):
            self._build(tmp_path, api_key="k")

    def test_embedded_mode_is_not_subject_to_it(self, tmp_path: Path) -> None:
        """No HTTP request exists to leak the token on."""
        EverosBackend(_ctx(tmp_path, api_key="k", base_url="http://memory.internal:8000"))


class TestRemoteTransportOptions:
    def _adapter(self, tmp_path: Path, **cfg: Any):
        return EverosBackend(_ctx(tmp_path, mode="http", **cfg))._adapter

    def test_base_url_falls_back_to_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVEROS_BASE_URL", "https://memory.example")
        assert self._adapter(tmp_path)._base_url == "https://memory.example"

    def test_config_base_url_wins_over_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("EVEROS_BASE_URL", "https://from-env")
        assert self._adapter(tmp_path, base_url="https://from-config")._base_url == "https://from-config"

    def test_extra_headers_are_sent(self, tmp_path: Path) -> None:
        a = self._adapter(tmp_path, headers={"X-Api-Key": "gw", "X-Tenant": 7})
        assert a._headers() == {"X-Api-Key": "gw", "X-Tenant": "7"}

    def test_authorization_cannot_be_shadowed_by_a_configured_header(self, tmp_path: Path) -> None:
        """Otherwise a stale token in a file would silently win over the env
        var the operator thought they had set."""
        a = self._adapter(
            tmp_path,
            base_url="https://memory.example",
            api_key="real",
            headers={"Authorization": "Bearer stale"},
        )
        assert a._headers()["Authorization"] == "Bearer real"

    def test_headers_must_be_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="mapping"):
            self._adapter(tmp_path, headers=["X-Api-Key: gw"])

    def test_retries_default_to_none(self, tmp_path: Path) -> None:
        """A local service must keep its exact single-attempt behaviour."""
        assert self._adapter(tmp_path)._retries == 0

    @pytest.mark.parametrize("bad", [-1, "2", True, 1.5])
    def test_a_bad_retries_value_is_refused(self, tmp_path: Path, bad: Any) -> None:
        with pytest.raises(ValueError, match="retries"):
            self._adapter(tmp_path, retries=bad)

    def test_a_ca_bundle_path_becomes_an_ssl_context(self, tmp_path: Path) -> None:
        """Not passed to httpx as a string: ``verify=<str>`` is deprecated in
        httpx 0.28 and would break on an upgrade."""
        import ssl

        import certifi

        b = EverosBackend(_ctx(tmp_path, mode="http", tls_verify=certifi.where()))
        assert isinstance(b._resolve_tls_verify(), ssl.SSLContext)

    def test_an_unusable_ca_bundle_is_named_at_construction(self, tmp_path: Path) -> None:
        """httpx would raise a bare SSLError naming neither the file nor the
        setting that produced it."""
        bundle = tmp_path / "ca.pem"
        bundle.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="not a usable CA bundle"):
            EverosBackend(_ctx(tmp_path, mode="http", tls_verify=str(bundle)))


class TestCredentialHeadersRespectTheTlsGuard:
    """The guard must not be bypassable by moving the credential to a header.

    An API gateway in front of EverOS authenticates through a custom header, so
    a check that only inspected ``api_key`` was defeated by exactly the
    configuration the ``headers`` option exists for.
    """

    def _build(self, tmp_path: Path, **cfg: Any) -> EverosBackend:
        return EverosBackend(_ctx(tmp_path, mode="http", **cfg))

    @pytest.mark.parametrize(
        "name",
        ["Authorization", "X-Api-Key", "x-api-key", "X-Auth-Token", "API_KEY", "X-Bearer"],
    )
    def test_a_credential_shaped_header_over_plain_http_is_refused(
        self, tmp_path: Path, name: str,
    ) -> None:
        with pytest.raises(ValueError, match="cleartext"):
            self._build(tmp_path, base_url="http://memory.internal:8000", headers={name: "v"})

    def test_the_message_names_the_offending_header(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="X-Api-Key"):
            self._build(tmp_path, base_url="http://memory.internal:8000",
                        headers={"X-Api-Key": "v"})

    def test_an_ordinary_header_is_not_a_credential(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="http://memory.internal:8000",
                        headers={"X-Tenant": "acme", "Accept-Language": "en"})
        assert b._adapter._extra_headers["X-Tenant"] == "acme"

    def test_https_allows_credential_headers(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="https://memory.internal",
                        headers={"X-Api-Key": "v"})
        assert b._adapter._extra_headers["X-Api-Key"] == "v"

    def test_loopback_allows_them(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="http://127.0.0.1:8000",
                        headers={"X-Api-Key": "v"})
        assert b._adapter._extra_headers["X-Api-Key"] == "v"

    def test_the_escape_hatch_covers_headers_too(self, tmp_path: Path) -> None:
        b = self._build(tmp_path, base_url="http://memory.internal:8000",
                        headers={"X-Api-Key": "v"}, allow_insecure_api_key=True)
        assert b._adapter._extra_headers["X-Api-Key"] == "v"
