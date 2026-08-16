"""EverosBackend — HTTP-only mode.

Adapter injection: tests build :class:`_FakeAdapter` instances and pass
them directly into :class:`EverosBackend(ctx, adapter=...)`. This keeps
the tests hermetic regardless of whether ``everos`` is importable in
the active venv (this matters — everos's runtime requires LLM /
embedding services that the test environment doesn't have).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from raven.memory_engine import MemoryBackend
from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos._server import ProbeResult
from raven.plugin.memory.everos.backend import (
    _PROFILE_MAX_CHARS,
    EverosBackend,
    ServiceState,
    _flatten_profile,
    _HttpEverosAdapter,
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

    async def memorize(
        self,
        session_id,
        payload_messages,
        *,
        is_final=False,
        app_id=None,
        project_id=None,
    ):
        self.memorize_calls.append(
            {
                "session_id": session_id,
                "payload_messages": payload_messages,
                "is_final": is_final,
                "app_id": app_id,
                "project_id": project_id,
            }
        )
        if self.memorize_raises is not None:
            raise self.memorize_raises


@pytest.fixture(autouse=True)
def _no_capability_probe(monkeypatch: pytest.MonkeyPatch):
    """Keep `start()`'s capability warning off the developer's own server.

    `start()` builds a real `_HttpEverosAdapter` when none is injected, so
    without this the lifecycle tests reach localhost:18791 and their output
    depends on what that server answers. Unreachable is the quiet default; the
    tests about the warning install their own answer.
    """
    from raven.plugin.memory.everos import _health

    monkeypatch.setattr(
        _health,
        "probe_capabilities",
        lambda *_a, **_kw: _health.CapabilityReport(reachable=False, error="probe disabled in tests"),
    )
    return monkeypatch


def _ctx(
    tmp_path: Path,
    *,
    user_id: str = "default",
    agent_id: str = "default",
    **config: Any,
) -> PluginContext:
    return PluginContext(
        config=config,
        services=ServiceLocator(workspace=tmp_path, user_id=user_id, agent_id=agent_id),
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

    def test_make_backend_factory(self, tmp_path: Path) -> None:
        b = make_backend(_ctx(tmp_path))
        assert isinstance(b, EverosBackend)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_stop_idempotent(self, tmp_path: Path) -> None:
        b = _backend(tmp_path)
        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()
            await b.stop()
            await b.start()
            await b.stop()

    async def test_start_calls_ensure_everos_server(self, tmp_path: Path) -> None:
        b = EverosBackend(_ctx(tmp_path))
        with patch(
            "raven.plugin.memory.everos._server.ensure_everos_server",
            new=AsyncMock(),
        ) as mock_ensure:
            await b.start()
        mock_ensure.assert_called_once()


class TestColdStartSpeaksUp:
    """The memory service starts on demand every session, and it can fail.

    Both the wait and the failure used to be invisible: the wait was silent and
    the reason went only to the log file, so a broken backend looked like an
    agent that had simply gone quiet.
    """

    async def test_a_real_wait_says_so(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        async def _waits(_base_url: str, *, on_wait=None, **_kw: object) -> None:
            if on_wait is not None:
                on_wait()

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=_waits):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        assert "Starting memory service" in capsys.readouterr().err

    async def test_an_already_running_server_stays_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """``on_wait`` must not fire when there is nothing to wait for: this path
        runs on every session, and a line here would be pure noise."""

        async def _already_up(_base_url: str, *, on_wait=None, **_kw: object) -> None:
            return None

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=_already_up):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        assert "Starting memory service" not in capsys.readouterr().err

    async def test_unconfigured_llm_degrades_with_an_actionable_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """This is the out-of-the-box state: backend defaults to everos while the
        shipped everos.toml has an empty [llm] api_key."""
        from raven.plugin.memory.everos._server import EverosNotConfiguredError

        async def _unconfigured(*_a: object, **_kw: object) -> None:
            raise EverosNotConfiguredError("no llm")

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=_unconfigured):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        err = " ".join(capsys.readouterr().err.split())
        assert "its LLM is not configured" in err
        assert "raven onboard" in err

    async def test_other_failures_name_the_reason(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        async def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("EverOS server exited with code 1")

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=_boom):
            b = EverosBackend(_ctx(tmp_path))
            # Degrades rather than raising: the caller already treats a missing
            # memory service as a degradation, and the state machine keeps
            # probing in case the server turns up later in the session.
            await b.start()

        assert b._state is not ServiceState.READY
        err = " ".join(capsys.readouterr().err.split())
        assert "exited with code 1" in err
        assert "without long-term memory" in err


class TestAUserManagedRootIsReadOnly:
    """Reusing an EverOS the user manages means recording its address, nothing more.

    Writing to it or starting it would take the OME jobstore lock exclusively --
    theirs to grant, not raven's to assume.
    """

    @staticmethod
    def _not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.config import update_everos

        monkeypatch.setattr(update_everos, "everos_owned", lambda: False)

    async def test_an_unreachable_server_is_not_started_for_us(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._not_owned(monkeypatch)
        started: list[int] = []

        async def _ensure(*_a: object, **_kw: object) -> None:
            started.append(1)

        monkeypatch.setattr("raven.plugin.memory.everos._server.ensure_everos_server", _ensure)
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server.probe_health",
            lambda _u, **_kw: ProbeResult.REFUSED,
        )

        b = EverosBackend(_ctx(tmp_path))
        await b.start()

        assert started == [], "started a server on a root raven does not own"
        assert b._state is ServiceState.FOREIGN
        err = " ".join(capsys.readouterr().err.split())
        assert "you manage is not running" in err
        assert "does not start or stop it" in err

    async def test_a_reachable_server_is_simply_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._not_owned(monkeypatch)
        started: list[int] = []

        async def _ensure(*_a: object, **_kw: object) -> None:
            started.append(1)

        monkeypatch.setattr("raven.plugin.memory.everos._server.ensure_everos_server", _ensure)
        monkeypatch.setattr(
            "raven.plugin.memory.everos._server.probe_health",
            lambda _u, **_kw: ProbeResult.OK,
        )

        b = EverosBackend(_ctx(tmp_path))
        await b.start()

        assert started == []
        assert b._state is ServiceState.READY
        assert capsys.readouterr().err == ""

    def test_the_factory_drops_no_templates_into_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._not_owned(monkeypatch)
        from raven.config import update_everos

        seeded: list[int] = []
        monkeypatch.setattr(update_everos, "ensure_everos_home", lambda *_a, **_kw: seeded.append(1))

        make_backend(_ctx(tmp_path))

        assert seeded == [], "wrote template files into a root the user manages"

    def test_an_owned_root_still_gets_its_templates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.config import update_everos

        monkeypatch.setattr(update_everos, "everos_owned", lambda: True)
        seeded: list[int] = []
        monkeypatch.setattr(update_everos, "ensure_everos_home", lambda *_a, **_kw: seeded.append(1))

        make_backend(_ctx(tmp_path))

        assert seeded == [1]


class TestStartWarnsWhenRecallCannotWork:
    """A running server stopped implying a working one in everos 1.2.1."""

    @staticmethod
    def _capabilities(monkeypatch: pytest.MonkeyPatch, **caps: bool) -> None:
        from raven.plugin.memory.everos import _health

        monkeypatch.setattr(
            _health,
            "probe_capabilities",
            lambda *_a, **_kw: _health.CapabilityReport(reachable=True, capabilities=caps),
        )

    @staticmethod
    def _configured(monkeypatch: pytest.MonkeyPatch, *sections: str) -> None:
        from raven.config import update_everos

        monkeypatch.setattr(update_everos, "everos_role_configured", lambda s: s in sections)

    async def test_the_probe_runs_off_the_event_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe and the config read behind it are blocking IO, and start()
        runs on the loop every session begins on: called inline, a wedged server
        stalls that loop for the whole health timeout.
        """
        import threading

        on_main: list[bool] = []

        def _warn(_base_url: str) -> None:
            on_main.append(threading.current_thread() is threading.main_thread())

        monkeypatch.setattr(EverosBackend, "_warn_if_recall_cannot_work", staticmethod(_warn))
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert on_main == [False], "the health probe ran on the event loop's own thread"

    async def test_a_role_configured_but_unbuilt_is_said_out_loud(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Recall silently drops to lexical matching, and the log saying so is
        file-only at runtime -- the user just sees an agent that gets vaguer."""
        self._configured(_no_capability_probe, "llm", "embedding")
        self._capabilities(_no_capability_probe, llm=True, embed=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        # Collapsed: rich wraps at the terminal width, so a raw substring match
        # would depend on how wide the machine running the tests happens to be.
        err = " ".join(capsys.readouterr().err.split())
        assert "falls back to keyword matching" in err
        assert "cascade backfill" in err

    async def test_a_role_the_user_never_configured_is_not_a_warning(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Skipping embedding is a choice the user already made; repeating it at
        every start would be noise, not information."""
        self._configured(_no_capability_probe)  # nothing configured
        self._capabilities(_no_capability_probe, llm=True, embed=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert capsys.readouterr().err == ""

    async def test_writes_are_not_disabled_by_the_warning(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Dropping to a no-op would discard memory the user gets back by fixing
        the provider and running a backfill."""
        self._configured(_no_capability_probe, "llm", "embedding")
        self._capabilities(_no_capability_probe, llm=True, embed=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert isinstance(b._adapter, _HttpEverosAdapter)

    async def test_a_healthy_server_says_nothing(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        self._capabilities(_no_capability_probe, llm=True, embed=True, rerank=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert capsys.readouterr().err == ""

    async def test_a_server_that_cannot_report_says_nothing(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Pre-1.2.1 servers answer a bare status; a warning there would be a
        lie about a working install."""
        self._capabilities(_no_capability_probe)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert capsys.readouterr().err == ""


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
# _flatten_profile — rendering a profile dict for prompt injection
# ---------------------------------------------------------------------------


class TestFlattenProfile:
    def test_ms_suffixed_keys_are_skipped(self) -> None:
        result = _flatten_profile({"name": "Alice", "profile_timestamp_ms": 123})
        assert result == "name: Alice"

    def test_scalar_dict_renders_key_value_lines(self) -> None:
        result = _flatten_profile({"name": "Alice", "tz": "PST"})
        assert result == "name: Alice\ntz: PST"

    def test_list_of_dicts_renders_category_and_description(self) -> None:
        result = _flatten_profile(
            {
                "explicit_info": [
                    {
                        "category": "occupation",
                        "description": "software engineer",
                        "evidence": "seen across many conversations",
                    },
                ],
            }
        )
        assert result == "- occupation: software engineer"
        assert "evidence" not in result
        assert "seen across many conversations" not in result

    def test_list_of_dicts_uses_trait_when_no_category(self) -> None:
        result = _flatten_profile(
            {
                "implicit_traits": [
                    {
                        "trait": "detail-oriented",
                        "description": "asks precise technical questions",
                        "basis": "observed across multiple conversations",
                    },
                ],
            }
        )
        assert result == "- detail-oriented: asks precise technical questions"
        assert "basis" not in result
        assert "observed across multiple conversations" not in result

    def test_category_wins_when_both_label_fields_present(self) -> None:
        result = _flatten_profile(
            {
                "explicit_info": [
                    {
                        "category": "work",
                        "trait": "detail-oriented",
                        "description": "ships backend services",
                    },
                ],
            }
        )
        assert result == "- work: ships backend services"

    def test_list_item_missing_description_renders_label_only(self) -> None:
        result = _flatten_profile({"explicit_info": [{"category": "location", "evidence": "lives in Seattle"}]})
        assert result == "- location"

    def test_list_item_missing_label_renders_description_only(self) -> None:
        result = _flatten_profile({"explicit_info": [{"description": "orphan fact"}]})
        assert result == "- orphan fact"

    def test_list_item_with_neither_label_nor_description_is_skipped(self) -> None:
        result = _flatten_profile({"explicit_info": [{"evidence": "only evidence, nothing else"}]})
        assert result == ""

    def test_non_dict_list_item_renders_as_is(self) -> None:
        result = _flatten_profile({"explicit_info": ["a raw string note"]})
        assert result == "- a raw string note"

    def test_non_dict_profile_data_renders_str(self) -> None:
        assert _flatten_profile("just a string") == "just a string"
        assert _flatten_profile(None) == "None"

    def test_short_profile_is_not_truncated(self) -> None:
        result = _flatten_profile({"name": "Alice", "tz": "PST"})
        assert "truncated" not in result
        assert len(result) <= _PROFILE_MAX_CHARS

    def test_long_profile_is_capped_at_line_boundary_with_visible_marker(
        self,
    ) -> None:
        items = [{"category": f"trait{i}", "description": "d" * 100} for i in range(30)]
        uncapped = "\n".join(f"- trait{i}: {'d' * 100}" for i in range(30))
        assert len(uncapped) > _PROFILE_MAX_CHARS  # sanity: cap must actually engage

        result = _flatten_profile({"explicit_info": items})

        assert len(result) < len(uncapped)
        assert len(result) <= _PROFILE_MAX_CHARS + len("\n[profile truncated, 99999 chars omitted]")
        body, _, marker = result.rpartition("\n")
        assert marker.startswith("[profile truncated, ") and marker.endswith(" chars omitted]")
        # Cut on a line boundary — no bullet is left half-written.
        for line in body.split("\n"):
            assert line.startswith("- trait")

    def test_non_dict_profile_data_is_also_capped(self) -> None:
        result = _flatten_profile("x" * (_PROFILE_MAX_CHARS + 500))
        assert len(result) < _PROFILE_MAX_CHARS + 500
        assert "[profile truncated," in result

    def test_realistic_payload_shape(self) -> None:
        profile_data = {
            "summary": "Works as a software engineer, interested in Python and ML.",
            "explicit_info": [
                {
                    "category": "occupation",
                    "description": "software engineer",
                    "evidence": "2026-06-25 to 2026-07-21, multiple conversations",
                },
            ],
            "implicit_traits": [
                {
                    "trait": "detail-oriented",
                    "description": "asks precise technical questions",
                    "basis": "observed across multiple conversations",
                },
            ],
            "profile_timestamp_ms": 1721990400000,
        }
        result = _flatten_profile(profile_data)
        assert result == (
            "summary: Works as a software engineer, interested in Python and ML.\n"
            "- occupation: software engineer\n"
            "- detail-oriented: asks precise technical questions"
        )
        assert "evidence" not in result
        assert "basis" not in result
        assert "profile_timestamp_ms" not in result


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

    async def test_memorize_failure_is_absorbed_and_accounted(self, tmp_path: Path) -> None:
        """A failed write is recorded here, not raised.

        store runs as a detached task now, so there is no caller left to catch
        anything it throws -- an exception would surface only as asyncio's
        "Task exception was never retrieved". The backend is the last place that
        can both classify the failure (demoting the service state) and remember
        that a turn went unindexed, so it does both instead.
        """
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = _FakeAdapter()
        adapter.memorize_raises = RuntimeError("everos down")
        b = _backend(tmp_path, adapter=adapter)

        await b.store("s", [{"role": "user", "content": "x"}])

        assert b._dropped_writes == 1
        assert b._state is not ServiceState.READY


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


# ---------------------------------------------------------------------------
# Identity — sourced from ServiceLocator, not the plugin config slice
# ---------------------------------------------------------------------------


class TestIdentityFromServices:
    def test_identity_comes_from_services_not_config(self, tmp_path: Path) -> None:
        ctx = PluginContext(
            config={"user_id": "from_config", "agent_id": "from_config"},
            services=ServiceLocator(
                workspace=tmp_path,
                user_id="from_services",
                agent_id="agent_from_services",
            ),
        )
        backend = make_backend(ctx)
        assert backend._user_id == "from_services"
        assert backend._agent_id == "agent_from_services"

    def test_stale_identity_keys_in_config_warn(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ctx = PluginContext(
            config={"user_id": "other"},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        make_backend(ctx)
        assert any("user_id" in r.message for r in caplog.records if r.levelname == "WARNING")

    def test_stale_identity_keys_matching_are_silent(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ctx = PluginContext(
            config={"user_id": "default"},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        make_backend(ctx)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    @pytest.mark.parametrize("bad", ["agent:default", "a/b", "..", "."])
    async def test_illegal_identity_rejected_on_start(self, tmp_path: Path, bad: str) -> None:
        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id=bad, agent_id="default"),
        )
        backend = make_backend(ctx)
        # The message must name the on-disk camelCase key so the user can grep
        # for it in config.json.
        with pytest.raises(ValueError, match="memory.userId"):
            await backend.start()


class TestServiceStateMachine:
    """The session's view of whether memory is usable, and what to do next.

    Before this the backend answered the question once, at start(), and a
    failure swapped in a no-op adapter for the rest of the session -- a server
    that came up two seconds later was never noticed. The state machine
    replaces that one-shot verdict with a fact that can change, and separates
    the two things a failure has to say: is this worth retrying, and is it
    worth waiting for.
    """

    @staticmethod
    def _backend(**cfg):
        from raven.plugin.memory.everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791", **cfg}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        return EverosBackend(ctx, adapter=MagicMock())

    def test_a_real_backend_starts_unknown(self) -> None:
        """Production builds its own HTTP adapter, so the lifecycle is this
        backend's to establish and nothing is known until the first probe."""
        from raven.plugin.memory.everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        assert EverosBackend(ctx)._state is ServiceState.UNKNOWN

    def test_an_injected_adapter_is_assumed_ready(self) -> None:
        """The caller supplied the transport, so it owns what is behind it --
        there is no server here to probe or spawn."""
        from raven.plugin.memory.everos.backend import ServiceState

        assert self._backend()._state is ServiceState.READY

    def test_probe_ok_reaches_ready(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._apply_probe(ProbeResult.OK)
        assert b._state is ServiceState.READY

    def test_timeout_is_unresponsive_not_failed(self) -> None:
        """A hung server is listening, so re-probing it charges the full budget
        every time. Filing it as FAILED would be wrong in the other direction:
        FAILED means the child is gone."""
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._state = ServiceState.READY
        b._apply_probe(ProbeResult.TIMEOUT)
        assert b._state is ServiceState.UNRESPONSIVE

    def test_refused_with_a_live_child_is_starting(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._proc = MagicMock(**{"poll.return_value": None})
        b._apply_probe(ProbeResult.REFUSED)
        assert b._state is ServiceState.STARTING

    def test_refused_with_a_dead_child_is_failed(self) -> None:
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._proc = MagicMock(**{"poll.return_value": 1, "returncode": 1})
        b._apply_probe(ProbeResult.REFUSED)
        assert b._state is ServiceState.FAILED

    def test_refused_with_no_child_of_ours_is_starting(self) -> None:
        """``None`` means another process holds the spawn lock, so there is no
        exit code to read. Calling that FAILED would stop a start that is
        someone else's and going fine."""
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._proc = None
        b._apply_probe(ProbeResult.REFUSED)
        assert b._state is ServiceState.STARTING

    def test_any_state_recovers_to_ready_on_a_later_probe(self) -> None:
        """FAILED is not terminal. The user can start the server by hand in
        another terminal, and the next probe has to see it."""
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        for start in (
            ServiceState.FAILED,
            ServiceState.STARTING,
            ServiceState.UNRESPONSIVE,
            ServiceState.FOREIGN,
        ):
            b = self._backend()
            b._state = start
            b._apply_probe(ProbeResult.OK)
            assert b._state is ServiceState.READY, start

    def test_terminal_states_ignore_probes(self) -> None:
        """UNCONFIGURED and NO_BINARY describe the install, not the process.
        Probing cannot fix either, so a stray OK must not paper over them."""
        from raven.plugin.memory.everos._server import ProbeResult
        from raven.plugin.memory.everos.backend import ServiceState

        for terminal in (ServiceState.UNCONFIGURED, ServiceState.NO_BINARY):
            b = self._backend()
            b._state = terminal
            b._apply_probe(ProbeResult.OK)
            assert b._state is terminal

    def test_may_spawn_only_from_states_that_can_be_fixed_by_spawning(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        can = {ServiceState.UNKNOWN}
        for state in ServiceState:
            b._state = state
            assert b._may_spawn() is (state in can), state

    def test_reports_each_state_once(self) -> None:
        """One line per problem per session. Re-reporting on every turn is how
        a warning becomes something users filter out."""
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend()
        b._state = ServiceState.FAILED
        assert b._should_report() is True
        assert b._should_report() is False
        b._state = ServiceState.UNRESPONSIVE
        assert b._should_report() is True


@pytest.mark.asyncio
class TestRecallNeverBlocks:
    """A turn must not pay for a memory service that is not there.

    recall used to run through an adapter with a 60s timeout, and a failure at
    start() replaced the adapter outright so the session could never recover.
    Both directions are wrong: the healthy path should be able to come back,
    and the unhealthy path should cost nothing.
    """

    @staticmethod
    def _backend(state, adapter=None):
        from raven.plugin.memory.everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter or MagicMock())
        b._state = state
        return b

    async def test_non_ready_returns_immediately_without_touching_the_adapter(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=AssertionError("must not be called"))
        b = self._backend(ServiceState.UNRESPONSIVE, adapter)
        with patch.object(b, "_kick_probe") as kick:
            assert await b.recall("q", user_id="u", top_k=5) == []
        kick.assert_called_once()

    async def test_non_ready_kicks_an_out_of_band_probe(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend(ServiceState.FAILED)
        with patch.object(b, "_kick_probe") as kick:
            await b.recall("q", user_id="u", top_k=5)
        kick.assert_called_once()

    async def test_a_timeout_demotes_so_the_next_turn_is_free(self) -> None:
        import httpx

        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=httpx.ReadTimeout("hung"))
        b = self._backend(ServiceState.READY, adapter)
        assert await b.recall("q", user_id="u", top_k=5) == []
        assert b._state is ServiceState.UNRESPONSIVE

    async def test_a_refusal_consults_the_child_process(self) -> None:
        import httpx

        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=httpx.ConnectError("gone"))
        b = self._backend(ServiceState.READY, adapter)
        b._proc = MagicMock(**{"poll.return_value": 2, "returncode": 2})
        assert await b.recall("q", user_id="u", top_k=5) == []
        assert b._state is ServiceState.FAILED


@pytest.mark.asyncio
class TestStoreIsDiscardedWhenTheServiceIsNotReady:
    """A write to a service that is not there is not worth a task."""

    @staticmethod
    def _backend(state):
        from raven.plugin.memory.everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=AssertionError("must not be called"))
        b = EverosBackend(ctx, adapter=adapter)
        b._state = state
        return b

    async def test_dropped_and_counted(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        b = self._backend(ServiceState.FAILED)
        await b.store("s1", [{"role": "user", "content": "hi"}])
        assert b._dropped_writes == 1


@pytest.mark.asyncio
class TestStoreReportsWhetherItLanded:
    """A caller that can retry needs to know; a caller that cannot may ignore it.

    The MemoryBackend protocol calls store fire-and-forget, and a turn really
    is: one turn's memory lost, next turn a fresh chance. A bulk import is the
    opposite -- its resume state marks a source done, so a write silently
    treated as landed removes the only record that it has not been. A return
    value serves both: ignoring it stays valid, checking it becomes possible.
    """

    @staticmethod
    def _backend(state, adapter):
        from raven.plugin.memory.everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = state
        return b

    async def test_true_when_the_write_lands(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(ServiceState.READY, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is True

    async def test_false_when_the_service_is_not_ready(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=AssertionError("must not be called"))
        b = self._backend(ServiceState.FAILED, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False

    async def test_false_when_the_write_raises(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=RuntimeError("everos down"))
        b = self._backend(ServiceState.READY, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False

    async def test_nothing_to_write_is_not_a_failure(self) -> None:
        """An empty slice and a dropped slice must not look the same: the
        importer would mark a real source failed over a message list that was
        legitimately empty after filtering."""
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(ServiceState.READY, adapter)

        assert await b.store("s", []) is True
        assert await b.store("s", [{"role": "system", "content": "dropped by conversion"}]) is True


@pytest.mark.asyncio
class TestWriteBudgetFollowsTheCaller:
    """Ten seconds is a turn's patience, not an extraction's runtime.

    A per-turn append should not hold a turn open, so it gets a short budget.
    A final flush and an importer batch are the calls that actually make EverOS
    extract, which is why _MEMORIZE_TIMEOUT_S was set to six minutes in the
    first place. Capping every write at the turn's budget silently overrode
    that, and the overrun then filed a slow extraction as a dead service.
    """

    @staticmethod
    def _backend(adapter):
        from raven.plugin.memory.everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = ServiceState.READY
        return b

    async def test_an_incremental_append_gets_the_short_budget(self, monkeypatch) -> None:
        from raven.plugin.memory.everos import backend as mod

        seen: list[float] = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await coro

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy)
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(adapter)

        await b.store("s", [{"role": "user", "content": "x"}], metadata={"is_final": False})

        assert seen == [mod._STORE_TIMEOUT_S]

    async def test_a_final_flush_gets_the_extraction_budget(self, monkeypatch) -> None:
        from raven.plugin.memory.everos import backend as mod

        seen: list[float] = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await coro

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy)
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(adapter)

        await b.store("s", [{"role": "user", "content": "x"}], metadata={"is_final": True})

        assert seen == [mod._MEMORIZE_TIMEOUT_S]

    async def test_a_slow_extraction_is_not_a_dead_service(self) -> None:
        """Demoting on a write timeout would drop the *next* write too, so one
        slow extraction would cascade into losing the batch behind it."""
        from raven.plugin.memory.everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=asyncio.TimeoutError())
        b = self._backend(adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False
        assert b._state is ServiceState.READY


class TestDroppedWritesCountOnlyRealLosses:
    """A fresh install has nothing to lose, and should not be told it lost it.

    The counter fires on any not-ready state, so an install that has never
    configured a memory LLM ends every session with "N turns were not written
    to long-term memory" -- about memory it never had.
    """

    @staticmethod
    def _backend(state):
        from raven.plugin.memory.everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=MagicMock())
        b._state = state
        return b

    @pytest.mark.asyncio
    async def test_never_configured_is_not_a_loss(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        for state in (ServiceState.UNCONFIGURED, ServiceState.NO_BINARY):
            b = self._backend(state)
            await b.store("s", [{"role": "user", "content": "x"}])
            assert b._dropped_writes == 0, state

    @pytest.mark.asyncio
    async def test_a_service_that_should_have_been_there_is(self) -> None:
        from raven.plugin.memory.everos.backend import ServiceState

        for state in (ServiceState.FAILED, ServiceState.UNRESPONSIVE, ServiceState.STARTING):
            b = self._backend(state)
            await b.store("s", [{"role": "user", "content": "x"}])
            assert b._dropped_writes == 1, state


@pytest.mark.asyncio
class TestADeadChildIsReportedAsFailed:
    """FAILED was unreachable from start().

    ``self._proc = await ensure_everos_server(...)`` only assigns when the call
    returns, so the handler for the call raising saw ``_proc`` still None and
    read a child that had already exited as one still booting -- which is the
    one state that keeps probing instead of reporting.
    """

    async def test_a_child_that_exited_reaches_failed(self, tmp_path) -> None:
        from raven.plugin.memory.everos.backend import EverosBackend, ServiceState

        dead = MagicMock(**{"poll.return_value": 1, "returncode": 1})

        async def _boom(*_a, **kw):
            # The real function spawns, then raises when the child dies; the
            # handle exists by then and has to survive the exception.
            report = kw.get("on_proc")
            if report is not None:
                report(dead)
            raise RuntimeError("EverOS server exited with code 1")

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx)

        with patch("raven.plugin.memory.everos._server.ensure_everos_server", new=_boom):
            await b.start()

        assert b._state is ServiceState.FAILED
