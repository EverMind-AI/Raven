"""FB-1 — qualified_id feedback dispatcher.

Exercises:
1. ``_filter_qualified_ids`` helper — prefix matching, native id
   extraction, malformed input tolerance.
2. ``AgentLoop._dispatch_backend_feedback`` — only ``everos/*`` is
   forwarded; ``local/`` / ``mass/`` / unprefixed entries are dropped
   silently; backend exceptions swallowed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.agent.loop import AgentLoop
from raven.agent.loop.main import _filter_qualified_ids

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, *args, **kwargs):
        raise NotImplementedError

    async def chat_with_retry(self, *args, **kwargs):
        raise NotImplementedError


class _FakeBackend:
    def __init__(self) -> None:
        self.feedback_calls: list[dict] = []
        self.feedback_raises: Exception | None = None

    async def start(self):
        pass

    async def stop(self):
        pass

    async def store(self, session_id, messages, *, metadata=None):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        return []

    async def feedback(self, signals: dict[str, Any]) -> None:
        self.feedback_calls.append(signals)
        if self.feedback_raises is not None:
            raise self.feedback_raises


def _make_loop(workspace: Path, *, backend=None) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# _filter_qualified_ids
# ---------------------------------------------------------------------------


class TestFilterQualifiedIds:
    def test_extracts_native_for_matching_prefix(self) -> None:
        out = _filter_qualified_ids(
            ["everos/abc", "local/x", "mass/y", "everos/def"],
            "everos",
        )
        assert out == ["abc", "def"]

    def test_returns_empty_when_no_match(self) -> None:
        out = _filter_qualified_ids(
            ["local/x", "mass/y"],
            "everos",
        )
        assert out == []

    def test_none_input(self) -> None:
        assert _filter_qualified_ids(None, "everos") == []

    def test_empty_list(self) -> None:
        assert _filter_qualified_ids([], "everos") == []

    def test_unprefixed_legacy_id_skipped(self) -> None:
        """Legacy raw skill names (no ``<source>/`` prefix) are skipped
        — there's no safe routing target for them, so the dispatcher
        is conservatively silent rather than mis-routing."""
        out = _filter_qualified_ids(
            ["git-resolver", "everos/abc"],
            "everos",
        )
        assert out == ["abc"]

    def test_malformed_entries_skipped(self) -> None:
        out = _filter_qualified_ids(
            ["everos/", "everos/valid", "", None, 42, "everos/another"],
            "everos",
        )
        # ``everos/`` (empty native) and non-strings drop.
        assert out == ["valid", "another"]

    def test_prefix_collision_avoided(self) -> None:
        """``everos`` is NOT a prefix of ``everos_light/...`` because
        the separator is the slash. The helper compares against
        ``everos/`` literally."""
        out = _filter_qualified_ids(
            ["everos_light/x", "everos/y"],
            "everos",
        )
        assert out == ["y"]


# ---------------------------------------------------------------------------
# _dispatch_backend_feedback
# ---------------------------------------------------------------------------


class TestDispatcherFeedback:
    async def test_no_backend_is_noop(self, tmp_path: Path) -> None:
        agent = _make_loop(tmp_path, backend=None)
        # Just returns without error.
        await agent._dispatch_backend_feedback(
            "s",
            ["everos/a"],
            ["everos/b"],
        )

    async def test_no_everos_ids_skips_backend(
        self,
        tmp_path: Path,
    ) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback(
            "s",
            ["local/x", "mass/y"],
            None,
        )
        # local/ + mass/ → no everos ids → no backend call.
        assert b.feedback_calls == []

    async def test_empty_ids_skips_backend(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback("s", [], [])
        assert b.feedback_calls == []

    async def test_none_ids_skips_backend(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback("s", None, None)
        assert b.feedback_calls == []

    async def test_everos_ids_forwarded(self, tmp_path: Path) -> None:
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback(
            "session-x",
            injected_skill_ids=["local/a", "everos/inj1", "everos/inj2"],
            used_skill_ids=["everos/used1"],
        )
        assert len(b.feedback_calls) == 1
        signals = b.feedback_calls[0]
        assert signals["kind"] == "skill_usage"
        assert signals["session_id"] == "session-x"
        assert signals["injected"] == ["inj1", "inj2"]
        assert signals["used"] == ["used1"]

    async def test_only_injected_no_used(self, tmp_path: Path) -> None:
        """The 'used' arg defaults to None — dispatch still fires if
        injected has everos ids."""
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback(
            "s",
            injected_skill_ids=["everos/x"],
        )
        assert len(b.feedback_calls) == 1
        assert b.feedback_calls[0]["injected"] == ["x"]
        assert b.feedback_calls[0]["used"] == []

    async def test_backend_feedback_exception_swallowed(
        self,
        tmp_path: Path,
    ) -> None:
        """Feedback is best-effort telemetry — never aborts after-turn."""
        b = _FakeBackend()
        b.feedback_raises = RuntimeError("everos overloaded")
        agent = _make_loop(tmp_path, backend=b)
        # Returns normally despite the raise.
        await agent._dispatch_backend_feedback("s", ["everos/x"], None)
        # Backend was still hit (so we know exception came from feedback).
        assert len(b.feedback_calls) == 1

    async def test_signal_shape_stable_for_consumers(
        self,
        tmp_path: Path,
    ) -> None:
        """The forwarded signal dict has a documented shape that
        EverosBackend (and future feedback consumers) can rely on."""
        b = _FakeBackend()
        agent = _make_loop(tmp_path, backend=b)
        await agent._dispatch_backend_feedback(
            "session-key",
            injected_skill_ids=["everos/a"],
            used_skill_ids=["everos/b"],
        )
        signals = b.feedback_calls[0]
        assert set(signals.keys()) == {
            "kind",
            "session_id",
            "injected",
            "used",
        }
        assert isinstance(signals["injected"], list)
        assert isinstance(signals["used"], list)
