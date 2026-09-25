"""EverosBackend — HTTP-only mode.

Adapter injection: tests build :class:`_FakeAdapter` instances and pass
them directly into :class:`EverosBackend(ctx, adapter=...)`. This keeps
the tests hermetic regardless of whether ``everos`` is importable in
the active venv (this matters — everos's runtime requires LLM /
embedding services that the test environment doesn't have).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from raven.contracts.memory import MemoryBackend
from raven.plugins import PluginContext, ServiceLocator

# What the backend told the host through ServiceLocator.notify; the host (not
# the plugin) decides how to show it, so the tests read the channel, not stderr.
NOTICES: list[str] = []


@pytest.fixture(autouse=True)
def _fresh_notices():
    NOTICES.clear()
    yield
    NOTICES.clear()


from raven_everos.backend import (
    _PROFILE_MAX_CHARS,
    EverosBackend,
    ServiceState,
    _flatten_profile,
    _HttpEverosAdapter,
    as_ms_epoch,
    convert_messages,
    make_backend,
)
from raven_everos.server import ProbeVerdict

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
    from raven_everos import health

    monkeypatch.setattr(
        health,
        "probe_capabilities",
        lambda *_a, **_kw: health.CapabilityReport(reachable=False, error="probe disabled in tests"),
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
        services=ServiceLocator(workspace=tmp_path, user_id=user_id, agent_id=agent_id, notify=NOTICES.append),
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


class TestMakeBackendIsReadOnly:
    """``raven doctor`` constructs a backend only to call ``health()``,
    never ``start()`` -- so ``make_backend`` must touch neither disk nor the
    environment. Creating the EverOS home and pointing it at ``EVEROS_ROOT``
    happens in ``start()`` instead, on every start path including one built
    with a fake (non-HTTP) adapter.
    """

    def test_construction_creates_no_home_and_sets_no_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from raven.config.paths import get_data_dir

        monkeypatch.delenv("EVEROS_ROOT", raising=False)

        make_backend(_ctx(tmp_path))

        assert not (get_data_dir() / "everos").exists()
        assert "EVEROS_ROOT" not in os.environ

    async def test_start_creates_the_home_with_the_fake_adapter_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from raven.config.paths import get_data_dir

        monkeypatch.delenv("EVEROS_ROOT", raising=False)

        b = _backend(tmp_path)
        await b.start()

        home = get_data_dir() / "everos"
        assert os.environ["EVEROS_ROOT"] == str(home)
        assert (home / "everos.toml").exists()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_stop_idempotent(self, tmp_path: Path) -> None:
        b = _backend(tmp_path)
        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()
            await b.stop()
            await b.start()
            await b.stop()

    async def test_a_role_the_migration_could_not_move_is_said_out_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A role whose vendor cannot be named is left unset on purpose -- a
        guess would send memory's traffic to the wrong endpoint. Silent, that is
        a slot the person has no reason to look at; the notice is the only thing
        that sends them to it."""
        said: list[str] = []
        monkeypatch.setattr(
            "raven_everos.config.migrate_roles",
            lambda: ["EverOS llm: could not tell which provider serves https://nobody/v1"],
        )

        b = EverosBackend(_ctx(tmp_path))
        b.notify = said.append
        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert any("could not tell which provider" in m for m in said), said

    async def test_start_binds_all_four_roles_into_this_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`understand_media` runs multimodal *here*, not in the spawned server.

        It reads EverOS's cached settings out of this process's environment, so a
        role bound only for the child is a role that tool cannot use -- which is
        how a configured multimodal model went on answering "not configured" to
        the only caller that needed it.
        """
        import json

        from raven import home as raven_home
        from raven_everos import config as cf

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "providers": {"openrouter": {"apiKey": "sk-or", "apiBase": "https://openrouter.ai/api/v1"}},
                    "embedding": {"model": "text-embedding-3-small", "provider": "openrouter"},
                    "plugins": {
                        "config": {
                            "everos-memory": {
                                "owned": True,
                                "root": str(tmp_path / ".everos"),
                                "llm": {"model": "the-llm", "provider": "openrouter"},
                                "multimodal": {"model": "the-pinned-model", "provider": "openrouter"},
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        raven_home.set_config_path(cfg)
        monkeypatch.setattr(cf, "_BOUND_HERE", set())
        for role in ("LLM", "EMBEDDING", "RERANK", "MULTIMODAL"):
            for name in ("MODEL", "BASE_URL", "API_KEY", "DIMENSIONS", "PROVIDER"):
                monkeypatch.delenv(f"EVEROS_{role}__{name}", raising=False)
        monkeypatch.delenv(cf.PROVENANCE_ENV, raising=False)

        try:
            b = EverosBackend(_ctx(tmp_path))
            with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
                await b.start()

            assert os.environ["EVEROS_MULTIMODAL__MODEL"] == "the-pinned-model"
            assert os.environ["EVEROS_LLM__MODEL"] == "the-llm"
            # Recorded as ours, or the next reader takes raven's own binding for
            # an operator's export and stops writing the slot at all.
            assert "EVEROS_MULTIMODAL__MODEL" in os.environ[cf.PROVENANCE_ENV].split(",")
        finally:
            raven_home.set_config_path(None)

    async def test_start_migrates_the_roles_before_it_reads_one(self, tmp_path: Path) -> None:
        """The whole answer to "does the upgrade need a command run by hand".

        An install upgrading into the role pins still has its four roles in
        everos.toml, which raven no longer reads for them -- so a start that did
        not migrate would send the service four blank roles and long-term memory
        would stop. Before the environment is built, not after: the spawn reads
        the pins this creates.
        """
        order: list[str] = []

        def _migrate() -> list[str]:
            order.append("migrate")
            return []

        async def _ensure(*a: object, **kw: object) -> None:
            order.append("ensure")

        b = EverosBackend(_ctx(tmp_path))
        with (
            patch("raven_everos.config.migrate_roles", new=_migrate),
            patch("raven_everos.server.ensure_everos_server", new=_ensure),
        ):
            await b.start()

        assert order[:1] == ["migrate"], order
        assert "ensure" in order

    async def test_start_calls_ensure_everos_server(self, tmp_path: Path) -> None:
        b = EverosBackend(_ctx(tmp_path))
        with patch(
            "raven_everos.server.ensure_everos_server",
            new=AsyncMock(),
        ) as mock_ensure:
            await b.start()
        mock_ensure.assert_called_once()


class TestShutdownFlushesUnfinishedSessions:
    """A session shorter than ``flush_every_turns`` crosses its flush
    boundary never, and a restart resets ``_turn_counts`` to zero -- so
    without a shutdown flush, a session's already-buffered content sits in
    EverOS's server-side buffer with no extraction ever triggered.
    """

    async def test_stop_flushes_a_session_short_of_its_boundary(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)
        await b.store("s", [{"role": "user", "content": "1"}])
        await b.store("s", [{"role": "user", "content": "2"}])
        assert all(call["is_final"] is False for call in adapter.memorize_calls)

        await b.stop()

        assert adapter.memorize_calls[-1]["session_id"] == "s"
        assert adapter.memorize_calls[-1]["is_final"] is True

    async def test_stop_does_not_reflush_a_session_already_on_the_boundary(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=2)
        await b.store("s", [{"role": "user", "content": "1"}])
        await b.store("s", [{"role": "user", "content": "2"}])
        assert adapter.memorize_calls[-1]["is_final"] is True
        calls_before_stop = len(adapter.memorize_calls)

        await b.stop()

        assert len(adapter.memorize_calls) == calls_before_stop

    async def test_a_session_that_never_wrote_is_left_alone(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)

        await b.stop()

        assert adapter.memorize_calls == []

    async def test_a_failed_final_flush_does_not_block_other_sessions(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)
        adapter.memorize_raises = None
        await b.store("s1", [{"role": "user", "content": "1"}])
        await b.store("s2", [{"role": "user", "content": "1"}])

        adapter.memorize_raises = RuntimeError("everos down")
        await b.stop()

        flushed = {c["session_id"] for c in adapter.memorize_calls if c["is_final"]}
        assert flushed == {"s1", "s2"}

    async def test_the_sweep_is_bounded_when_a_flush_hangs(self, tmp_path: Path, monkeypatch) -> None:
        import time

        from raven_everos import backend as mod

        monkeypatch.setattr(mod, "_SHUTDOWN_FLUSH_BUDGET_S", 0.05)

        class _Hangs:
            async def memorize(self, session_id, payload_messages, *, is_final=False, app_id=None, project_id=None):
                await asyncio.sleep(30)

        b = _backend(tmp_path, adapter=_Hangs(), flush_every_turns=4)
        # Set the turn count directly rather than through a real store() call --
        # the hanging adapter would make that call itself hang for the full
        # _STORE_TIMEOUT_S, which is not what this test is bounding.
        b._turn_counts["s"] = 1

        t0 = time.monotonic()
        await b.stop()

        assert time.monotonic() - t0 < 1.0


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

        with patch("raven_everos.server.ensure_everos_server", new=_waits):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        assert "Starting memory service" in " ".join(NOTICES)

    async def test_an_already_running_server_stays_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """``on_wait`` must not fire when there is nothing to wait for: this path
        runs on every session, and a line here would be pure noise."""

        async def _already_up(_base_url: str, *, on_wait=None, **_kw: object) -> None:
            return None

        with patch("raven_everos.server.ensure_everos_server", new=_already_up):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        assert "Starting memory service" not in " ".join(NOTICES)

    async def test_unconfigured_llm_degrades_with_an_actionable_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """This is the out-of-the-box state: backend defaults to everos while the
        shipped everos.toml has an empty [llm] api_key."""
        from raven_everos.server import EverosNotConfiguredError

        async def _unconfigured(*_a: object, **_kw: object) -> None:
            raise EverosNotConfiguredError("no llm")

        with patch("raven_everos.server.ensure_everos_server", new=_unconfigured):
            b = EverosBackend(_ctx(tmp_path))
            await b.start()

        err = " ".join(" ".join(NOTICES).split())
        assert "its LLM is not configured" in err
        assert "raven onboard" in err

    async def test_other_failures_name_the_reason(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        async def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("EverOS server exited with code 1")

        with patch("raven_everos.server.ensure_everos_server", new=_boom):
            b = EverosBackend(_ctx(tmp_path))
            # Degrades rather than raising: the caller already treats a missing
            # memory service as a degradation, and the state machine keeps
            # probing in case the server turns up later in the session.
            await b.start()

        assert b._state is not ServiceState.READY
        err = " ".join(" ".join(NOTICES).split())
        assert "exited with code 1" in err
        assert "without long-term memory" in err


class TestAUserManagedRootIsReadOnly:
    """Reusing an EverOS the user manages means recording its address, nothing more.

    Writing to it or starting it would take the OME jobstore lock exclusively --
    theirs to grant, not raven's to assume.
    """

    @staticmethod
    def _not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
        from raven_everos import config as ue

        monkeypatch.setattr(ue, "everos_owned", lambda: False)

    async def test_an_unreachable_server_is_not_started_for_us(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._not_owned(monkeypatch)
        started: list[int] = []

        async def _ensure(*_a: object, **_kw: object) -> None:
            started.append(1)

        monkeypatch.setattr("raven_everos.server.ensure_everos_server", _ensure)
        monkeypatch.setattr(
            "raven_everos.server.probe_health",
            lambda _u, **_kw: ProbeVerdict.REFUSED,
        )

        b = EverosBackend(_ctx(tmp_path))
        await b.start()

        assert started == [], "started a server on a root raven does not own"
        assert b._state is ServiceState.FOREIGN
        err = " ".join(" ".join(NOTICES).split())
        assert "you manage is not running" in err
        assert "does not start or stop it" in err

    async def test_a_reachable_server_is_simply_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        self._not_owned(monkeypatch)
        started: list[int] = []

        async def _ensure(*_a: object, **_kw: object) -> None:
            started.append(1)

        monkeypatch.setattr("raven_everos.server.ensure_everos_server", _ensure)
        monkeypatch.setattr(
            "raven_everos.server.probe_health",
            lambda _u, **_kw: ProbeVerdict.OK,
        )

        b = EverosBackend(_ctx(tmp_path))
        await b.start()

        assert started == []
        assert b._state is ServiceState.READY
        assert NOTICES == []

    async def test_the_factory_drops_no_templates_into_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._not_owned(monkeypatch)
        from raven_everos import config as ue

        seeded: list[int] = []
        monkeypatch.setattr(ue, "ensure_everos_home", lambda *_a, **_kw: seeded.append(1))
        monkeypatch.setattr(
            "raven_everos.server.probe_health",
            lambda _u, **_kw: ProbeVerdict.REFUSED,
        )

        b = EverosBackend(_ctx(tmp_path))
        await b.start()

        assert seeded == [], "wrote template files into a root the user manages"

    async def test_an_owned_root_still_gets_its_templates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from raven_everos import config as ue

        monkeypatch.setattr(ue, "everos_owned", lambda: True)
        seeded: list[int] = []
        monkeypatch.setattr(ue, "ensure_everos_home", lambda *_a, **_kw: seeded.append(1))

        b = EverosBackend(_ctx(tmp_path))
        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert seeded == [1]


class TestStartWarnsWhenRecallCannotWork:
    """A running server stopped implying a working one in everos 1.2.1."""

    @staticmethod
    def _capabilities(monkeypatch: pytest.MonkeyPatch, **caps: bool) -> None:
        from raven_everos import health

        monkeypatch.setattr(
            health,
            "probe_capabilities",
            lambda *_a, **_kw: health.CapabilityReport(reachable=True, capabilities=caps),
        )

    @staticmethod
    def _configured(monkeypatch: pytest.MonkeyPatch, *sections: str) -> None:
        from raven_everos import config as ue

        monkeypatch.setattr(ue, "everos_role_configured", lambda s: s in sections)

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

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
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

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        # Collapsed: rich wraps at the terminal width, so a raw substring match
        # would depend on how wide the machine running the tests happens to be.
        err = " ".join(" ".join(NOTICES).split())
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

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert NOTICES == []

    async def test_writes_are_not_disabled_by_the_warning(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Dropping to a no-op would discard memory the user gets back by fixing
        the provider and running a backfill."""
        self._configured(_no_capability_probe, "llm", "embedding")
        self._capabilities(_no_capability_probe, llm=True, embed=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert isinstance(b._adapter, _HttpEverosAdapter)

    async def test_a_healthy_server_says_nothing(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        self._capabilities(_no_capability_probe, llm=True, embed=True, rerank=False)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert NOTICES == []

    async def test_a_server_that_cannot_report_says_nothing(
        self, tmp_path: Path, _no_capability_probe, capsys: pytest.CaptureFixture
    ) -> None:
        """Pre-1.2.1 servers answer a bare status; a warning there would be a
        lie about a working install."""
        self._capabilities(_no_capability_probe)
        b = EverosBackend(_ctx(tmp_path))

        with patch("raven_everos.server.ensure_everos_server", new=AsyncMock()):
            await b.start()

        assert NOTICES == []


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

    async def test_top_k_truncation_keeps_the_profile(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter(
            search_response=_user_search_data(
                episodes=[
                    SimpleNamespace(id=f"e{i}", session_id="s", summary=f"fact {i}", episode="", score=0.5)
                    for i in range(5)
                ],
                profiles=[SimpleNamespace(id="prof1", profile_data={"name": "Alice"}, score=None)],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("q", user_id="alice", top_k=2)
        assert len(hits) == 2
        assert [h.metadata["type"] for h in hits] == ["profile", "episode"]


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

    async def test_cases_carry_intent_approach_and_insight(self, tmp_path: Path) -> None:
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
        assert hits[0].text == "resolve git conflict\n\nstep-by-step\n\nuse rerere"
        assert hits[0].metadata["type"] == "case"

    async def test_case_missing_fields_leave_no_blank_paragraph(
        self,
        tmp_path: Path,
    ) -> None:
        """An everos that answers without ``approach`` still reads as prose."""
        adapter = _FakeAdapter(
            search_response=_agent_search_data(
                cases=[
                    SimpleNamespace(
                        id="c1",
                        task_intent="resolve git conflict",
                        key_insight="use rerere",
                        score=0.8,
                    ),
                ],
            )
        )
        b = _backend(tmp_path, adapter=adapter)
        hits = await b.recall("git", agent_id="agent:default", top_k=5)
        assert hits[0].text == "resolve git conflict\n\nuse rerere"


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
        """A failed write is reported here, not raised.

        store runs as a detached task now, so there is no caller left to catch
        anything it throws -- an exception would surface only as asyncio's
        "Task exception was never retrieved". The backend classifies the
        failure (demoting the service state) and reports it through the
        return value; remembering that a turn went unindexed is the caller's
        job (the AgentLoop's own retry-aware count), not the backend's.
        """
        from raven_everos.backend import ServiceState

        adapter = _FakeAdapter()
        adapter.memorize_raises = RuntimeError("everos down")
        b = _backend(tmp_path, adapter=adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False
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
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default", notify=NOTICES.append),
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
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default", notify=NOTICES.append),
        )
        make_backend(ctx)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    @pytest.mark.parametrize("bad", ["agent:default", "a/b", "..", "."])
    async def test_illegal_identity_is_reported_as_a_config_error(
        self, tmp_path: Path, bad: str, capsys: pytest.CaptureFixture
    ) -> None:
        from raven_everos.backend import ServiceState

        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id=bad, agent_id="default", notify=NOTICES.append),
        )
        backend = make_backend(ctx)
        await backend.start()

        # The message must name the on-disk camelCase key so the user can grep
        # for it in config.json, and it must reach the terminal: the callers all
        # swallow a raise into logger.exception, and a one-shot CLI run writes no
        # log file for it to land in.
        err = " ".join(" ".join(NOTICES).split())
        assert "memory.userId" in err
        # The accepted-character class must survive rich's markup parser: it
        # looks exactly like a tag, and a swallowed one leaves the user matching
        # their id against "^+$".
        assert "[a-zA-Z0-9_.@+-]" in err
        assert backend._state is ServiceState.BAD_IDENTITY

    async def test_illegal_identity_does_not_report_a_service_outage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The service is fine; the config is not, and the backend itself no
        longer prints a shutdown summary at all -- that moved to the caller's
        own honest count -- so this only pins that ``stop()`` stays silent."""
        ctx = PluginContext(
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id="a/b", agent_id="default", notify=NOTICES.append),
        )
        backend = make_backend(ctx)
        await backend.start()
        NOTICES.clear()

        assert await backend.store("s1", [{"role": "user", "content": "hi"}]) is False
        await backend.stop()
        assert "unavailable" not in " ".join(NOTICES)


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
        from raven_everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791", **cfg}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        return EverosBackend(ctx, adapter=MagicMock())

    def test_a_real_backend_starts_unknown(self) -> None:
        """Production builds its own HTTP adapter, so the lifecycle is this
        backend's to establish and nothing is known until the first probe."""
        from raven_everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        assert EverosBackend(ctx)._state is ServiceState.UNKNOWN

    def test_an_injected_adapter_is_assumed_ready(self) -> None:
        """The caller supplied the transport, so it owns what is behind it --
        there is no server here to probe or spawn."""
        from raven_everos.backend import ServiceState

        assert self._backend()._state is ServiceState.READY

    def test_probe_ok_reaches_ready(self) -> None:
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        b = self._backend()
        b._apply_probe(ProbeVerdict.OK)
        assert b._state is ServiceState.READY

    def test_timeout_is_unresponsive_not_failed(self) -> None:
        """A hung server is listening, so re-probing it charges the full budget
        every time. Filing it as FAILED would be wrong in the other direction:
        FAILED means the child is gone."""
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        b = self._backend()
        b._state = ServiceState.READY
        b._apply_probe(ProbeVerdict.TIMEOUT)
        assert b._state is ServiceState.UNRESPONSIVE

    def test_refused_with_a_live_child_is_starting(self) -> None:
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        b = self._backend()
        b._proc = MagicMock(**{"poll.return_value": None})
        b._apply_probe(ProbeVerdict.REFUSED)
        assert b._state is ServiceState.STARTING

    def test_refused_with_a_dead_child_is_failed(self) -> None:
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        b = self._backend()
        b._proc = MagicMock(**{"poll.return_value": 1, "returncode": 1})
        b._apply_probe(ProbeVerdict.REFUSED)
        assert b._state is ServiceState.FAILED

    def test_refused_with_no_child_of_ours_is_starting(self) -> None:
        """``None`` means another process holds the spawn lock, so there is no
        exit code to read. Calling that FAILED would stop a start that is
        someone else's and going fine."""
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        b = self._backend()
        b._proc = None
        b._apply_probe(ProbeVerdict.REFUSED)
        assert b._state is ServiceState.STARTING

    def test_any_state_recovers_to_ready_on_a_later_probe(self) -> None:
        """FAILED is not terminal. The user can start the server by hand in
        another terminal, and the next probe has to see it."""
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        for start in (
            ServiceState.FAILED,
            ServiceState.STARTING,
            ServiceState.UNRESPONSIVE,
            ServiceState.FOREIGN,
        ):
            b = self._backend()
            b._state = start
            b._apply_probe(ProbeVerdict.OK)
            assert b._state is ServiceState.READY, start

    def test_terminal_states_ignore_probes(self) -> None:
        """UNCONFIGURED, NO_BINARY and BAD_IDENTITY describe the install and its
        config, not the process. Probing cannot fix any of them, so a stray OK
        must not paper over them."""
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        for terminal in (ServiceState.UNCONFIGURED, ServiceState.NO_BINARY, ServiceState.BAD_IDENTITY):
            b = self._backend()
            b._state = terminal
            b._apply_probe(ProbeVerdict.OK)
            assert b._state is terminal

    def test_reports_each_state_once(self) -> None:
        """One line per problem per session. Re-reporting on every turn is how
        a warning becomes something users filter out."""
        from raven_everos.backend import ServiceState

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
        from raven_everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter or MagicMock())
        b._state = state
        return b

    async def test_non_ready_returns_immediately_without_touching_the_adapter(self) -> None:
        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=AssertionError("must not be called"))
        b = self._backend(ServiceState.UNRESPONSIVE, adapter)
        with patch.object(b, "_kick_probe") as kick:
            assert await b.recall("q", user_id="u", top_k=5) == []
        kick.assert_called_once()

    async def test_non_ready_kicks_an_out_of_band_probe(self) -> None:
        from raven_everos.backend import ServiceState

        b = self._backend(ServiceState.FAILED)
        with patch.object(b, "_kick_probe") as kick:
            await b.recall("q", user_id="u", top_k=5)
        kick.assert_called_once()

    async def test_a_timeout_demotes_so_the_next_turn_is_free(self) -> None:
        import httpx

        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=httpx.ReadTimeout("hung"))
        b = self._backend(ServiceState.READY, adapter)
        assert await b.recall("q", user_id="u", top_k=5) == []
        assert b._state is ServiceState.UNRESPONSIVE

    async def test_an_answered_request_that_failed_leaves_the_state_alone(self) -> None:
        """A status line proves the service is up; only this call failed.

        Demoting on it made one unprocessable payload cost every later call in
        the process: they short-circuit on the state guard without reaching the
        wire. A real import lost four of five sources to a single 500.
        """
        import httpx

        from raven_everos.backend import ServiceState

        request = httpx.Request("POST", "http://localhost:18791/api/v2/memory/add")
        response = httpx.Response(500, request=request)
        adapter = MagicMock()
        adapter.search = AsyncMock(side_effect=httpx.HTTPStatusError("boom", request=request, response=response))
        b = self._backend(ServiceState.READY, adapter)

        assert await b.recall("q", user_id="u", top_k=5) == []
        assert b._state is ServiceState.READY

    async def test_a_refusal_consults_the_child_process(self) -> None:
        import httpx

        from raven_everos.backend import ServiceState

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
        from raven_everos.backend import EverosBackend

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

    async def test_dropped(self) -> None:
        from raven_everos.backend import ServiceState

        b = self._backend(ServiceState.FAILED)
        assert await b.store("s1", [{"role": "user", "content": "hi"}]) is False


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
        from raven_everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = state
        return b

    async def test_true_when_the_write_lands(self) -> None:
        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(ServiceState.READY, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is True

    async def test_false_when_the_service_is_not_ready(self) -> None:
        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=AssertionError("must not be called"))
        b = self._backend(ServiceState.FAILED, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False

    async def test_false_when_the_write_raises(self) -> None:
        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=RuntimeError("everos down"))
        b = self._backend(ServiceState.READY, adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False

    async def test_nothing_to_write_is_not_a_failure(self) -> None:
        """An empty slice and a dropped slice must not look the same: the
        importer would mark a real source failed over a message list that was
        legitimately empty after filtering."""
        from raven_everos.backend import ServiceState

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

    The short budget follows the slice's size rather than being flat: an append
    is not free work either -- EverOS may carve a boundary out of one, which
    runs a model -- so a hundred-message importer batch needs more patience
    than a turn's handful.
    """

    @staticmethod
    def _backend(adapter):
        from raven_everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = ServiceState.READY
        return b

    async def test_an_incremental_append_gets_the_short_budget(self, monkeypatch) -> None:
        from raven_everos import backend as mod

        seen: list[float] = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await coro

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy)
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(adapter)

        await b.store("s", [{"role": "user", "content": "x"}], metadata={"is_final": False})

        assert seen == [mod._store_budget(1)]
        assert seen[0] >= mod._STORE_TIMEOUT_S

    async def test_a_bulk_batch_gets_more_than_a_turns_patience(self, monkeypatch) -> None:
        """The importer hands over up to a hundred messages in one append.

        At the flat turn budget that call timed out against a server still
        working on it, which demoted the backend and failed every source behind
        it in the same run.
        """
        from raven_everos import backend as mod

        seen: list[float] = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await coro

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy)
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(adapter)

        batch = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        await b.store("s", batch, metadata={"is_final": False})

        assert seen == [mod._store_budget(100)]
        assert seen[0] > mod._STORE_TIMEOUT_S * 2

    async def test_a_bulk_write_gets_the_extraction_budget_however_small(self, monkeypatch) -> None:
        """The importer marks its appends ``bulk``: nothing waits on them, and
        EverOS extracts on the add itself, so a per-message estimate is the
        wrong shape -- a fifty-message batch measured 24s against a real
        service and a hundred ran past six minutes. Only the extraction budget
        holds that, and it must not depend on the slice being large.
        """
        from raven_everos import backend as mod

        seen: list[float] = []

        async def _spy(coro, timeout=None):
            seen.append(timeout)
            return await coro

        monkeypatch.setattr(mod.asyncio, "wait_for", _spy)
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        b = self._backend(adapter)

        await b.store("s", [{"role": "user", "content": "x"}], metadata={"is_final": False, "bulk": True})

        assert seen == [mod._MEMORIZE_TIMEOUT_S]
        adapter.memorize.assert_awaited_once()
        assert adapter.memorize.await_args.kwargs["is_final"] is False

    async def test_a_final_flush_gets_the_extraction_budget(self, monkeypatch) -> None:
        from raven_everos import backend as mod

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
        from raven_everos.backend import ServiceState

        adapter = MagicMock()
        adapter.memorize = AsyncMock(side_effect=asyncio.TimeoutError())
        b = self._backend(adapter)

        assert await b.store("s", [{"role": "user", "content": "x"}]) is False
        assert b._state is ServiceState.READY


class TestNotReadyStoresAllReportFalseTheSameWay:
    """The backend no longer keeps its own dropped-write count (that
    distinction -- "never configured" vs. "really failed" -- only ever
    mattered for the plugin's own shutdown message, which is now the
    AgentLoop's job, driven by its own retry-aware count instead). What the
    backend still owes every caller is a truthful, uniform return value.
    """

    @staticmethod
    def _backend(state):
        from raven_everos.backend import EverosBackend

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=MagicMock())
        b._state = state
        return b

    @pytest.mark.asyncio
    async def test_a_write_that_really_was_lost_reports_false(self) -> None:
        """BAD_IDENTITY belongs here, not with the never-configured states: the
        service works, the config is wrong, and the turn it refuses is gone."""
        from raven_everos.backend import ServiceState

        retryable = (
            ServiceState.BAD_IDENTITY,
            ServiceState.FAILED,
            ServiceState.UNRESPONSIVE,
            ServiceState.STARTING,
        )
        for state in retryable:
            b = self._backend(state)
            assert await b.store("s", [{"role": "user", "content": "x"}]) is False, state

    async def test_a_service_that_was_never_configured_reports_no_failure(self) -> None:
        """There is no memory service to fail, so nothing was lost.

        Reporting False here would make the AgentLoop retry for a minute per
        turn and then announce lost turns to an install that never had any.
        """
        from raven_everos.backend import ServiceState

        never_had = (
            ServiceState.UNCONFIGURED,
            ServiceState.NO_BINARY,
        )
        for state in never_had:
            b = self._backend(state)
            assert await b.store("s", [{"role": "user", "content": "x"}]) is True, state


class _SchemaStrictAdapter:
    """A fake that refuses what production refuses.

    everos 1.2.3 declares ``MemorizeAddRequest.messages`` with
    ``min_length=1``, so an add carrying an empty list is a 422 and the flush
    behind it never goes out. A fake that accepts the empty add hides exactly
    that, which is how a shutdown flush that never reached the server passed
    its tests.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(self, **kw):
        return None

    async def memorize(self, session_id, payload_messages, *, is_final=False, app_id=None, project_id=None):
        if payload_messages:
            self.calls.append("add")
        elif not is_final:
            return
        else:
            # No add is issued for a flush-only call; issuing one would 422.
            pass
        if is_final:
            self.calls.append("flush")

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
class TestShutdownFlushReachesTheServer:
    """A session that ends short of its flush boundary still gets extracted.

    Only a flush makes everos extract. The default fires one per turn, so this
    exercises the batched configuration, where a conversation can end before
    ever reaching a boundary.
    """

    async def test_a_short_session_is_flushed_at_stop(self, tmp_path: Path) -> None:
        adapter = _SchemaStrictAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)
        b._state = ServiceState.READY
        assert await b.store("s", [{"role": "user", "content": "hi"}]) is True
        assert adapter.calls == ["add"]

        await b.stop()
        assert "flush" in adapter.calls, adapter.calls

    async def test_the_flush_only_call_sends_no_empty_add(self, tmp_path: Path) -> None:
        adapter = _SchemaStrictAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)
        b._state = ServiceState.READY
        await b.store("s", [{"role": "user", "content": "hi"}])
        adapter.calls.clear()

        await b.stop()
        assert adapter.calls == ["flush"], adapter.calls

    async def test_a_session_already_on_the_boundary_is_not_reflushed(self, tmp_path: Path) -> None:
        adapter = _SchemaStrictAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=1)
        b._state = ServiceState.READY
        await b.store("s", [{"role": "user", "content": "hi"}])
        assert adapter.calls == ["add", "flush"]
        adapter.calls.clear()

        await b.stop()
        assert adapter.calls == [], adapter.calls


@pytest.mark.asyncio
class TestTheHttpAdapterSkipsAnEmptyAdd:
    """The unit under test is the real HTTP client, not a fake of it."""

    async def test_a_flush_only_memorize_posts_only_the_flush(self) -> None:
        posted: list[str] = []

        class _Resp:
            def raise_for_status(self) -> None:
                pass

            def json(self):
                return {}

        class _Client:
            async def post(self, url, **kw):
                posted.append(url)
                return _Resp()

        adapter = _HttpEverosAdapter(base_url="http://x", api_key=None)
        adapter._client = _Client()
        await adapter.memorize("s", [], is_final=True)
        assert posted == ["http://x/api/v2/memory/flush"], posted

    async def test_an_empty_non_final_memorize_posts_nothing(self) -> None:
        posted: list[str] = []

        class _Client:
            async def post(self, url, **kw):
                posted.append(url)
                raise AssertionError("should not be reached")

        adapter = _HttpEverosAdapter(base_url="http://x", api_key=None)
        adapter._client = _Client()
        await adapter.memorize("s", [], is_final=False)
        assert posted == []


@pytest.mark.asyncio
class TestACancelledFlushIsStillOwed:
    """A boundary the code intended is not a flush the server confirmed.

    With a flush on every turn, turn one already sits on the modulo boundary,
    so a sweep that reads the turn counter concludes there is nothing owed --
    even when that turn's flush was cancelled mid-flight, which is what a
    two-second teardown budget does to a seven-second flush. The add landed,
    so the content is buffered on the server and only a flush will extract it.
    """

    async def test_a_flush_cancelled_mid_flight_is_retried_at_stop(self, tmp_path: Path) -> None:
        import asyncio

        class _FlushHangs:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.block = True

            async def search(self, **kw):
                return None

            async def memorize(self, session_id, payload_messages, *, is_final=False, app_id=None, project_id=None):
                if payload_messages:
                    self.calls.append("add")
                if is_final:
                    if self.block:
                        self.calls.append("flush-started")
                        await asyncio.sleep(30)
                    self.calls.append("flush")

            async def aclose(self) -> None:
                pass

        adapter = _FlushHangs()
        b = _backend(tmp_path, adapter=adapter)
        b._state = ServiceState.READY

        task = asyncio.create_task(b.store("s", [{"role": "user", "content": "hi"}]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.calls == ["add", "flush-started"], adapter.calls

        adapter.block = False
        await b.stop()
        assert "flush" in adapter.calls, adapter.calls

    async def test_a_confirmed_flush_is_not_repeated_at_stop(self, tmp_path: Path) -> None:
        adapter = _SchemaStrictAdapter()
        b = _backend(tmp_path, adapter=adapter)
        b._state = ServiceState.READY
        await b.store("s", [{"role": "user", "content": "hi"}])
        assert adapter.calls == ["add", "flush"]
        adapter.calls.clear()

        await b.stop()
        assert adapter.calls == [], adapter.calls


@pytest.mark.asyncio
class TestADeadChildIsReportedAsFailed:
    """FAILED was unreachable from start().

    ``self._proc = await ensure_everos_server(...)`` only assigns when the call
    returns, so the handler for the call raising saw ``_proc`` still None and
    read a child that had already exited as one still booting -- which is the
    one state that keeps probing instead of reporting.
    """

    async def test_a_child_that_exited_reaches_failed(self, tmp_path) -> None:
        from raven_everos.backend import EverosBackend, ServiceState

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

        with patch("raven_everos.server.ensure_everos_server", new=_boom):
            await b.start()

        assert b._state is ServiceState.FAILED


@pytest.mark.asyncio
class TestASessionPicksUpAServiceThatArrivesLate:
    """The two halves compose: a turn with no memory leaves one that has it.

    The state transition and the out-of-band kick are each covered on their
    own, which is not the same as the property they exist for -- a session that
    began without a memory service must start using one that comes up while it
    is still running, with no restart. Before the state machine that was
    impossible by construction: the first failure swapped in a no-op adapter
    and the session was done.
    """

    @staticmethod
    def _backend(adapter):
        from raven_everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = ServiceState.STARTING
        return b

    async def test_a_later_turn_recalls_once_the_server_answers(self, monkeypatch) -> None:
        from raven_everos import backend as mod
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        adapter = MagicMock()
        adapter.search = AsyncMock(return_value=None)
        b = self._backend(adapter)
        # No rate limit in the way: the point is the composition, not the
        # throttle, which has its own coverage.
        monkeypatch.setattr(mod, "_PROBE_MIN_INTERVAL_S", 0.0)

        answers = iter([ProbeVerdict.REFUSED, ProbeVerdict.OK])
        monkeypatch.setattr(
            "raven_everos.server.probe_health",
            lambda _u, **_kw: next(answers, ProbeVerdict.OK),
        )

        # Turn one: nothing there. Returns empty without touching the adapter,
        # and leaves a probe behind.
        assert await b.recall("q", user_id="u", top_k=5) == []
        adapter.search.assert_not_awaited()
        await b._probe_task

        # The server came up between the turns.
        assert await b.recall("q", user_id="u", top_k=5) == []
        await b._probe_task
        assert b._state is ServiceState.READY

        # Turn three actually reaches it -- no restart, same backend object.
        await b.recall("q", user_id="u", top_k=5)
        adapter.search.assert_awaited()

    async def test_a_terminal_state_never_recovers_this_way(self, monkeypatch) -> None:
        """UNCONFIGURED describes the install. A server answering on that port
        is somebody else's, and adopting it would hide a missing memory LLM."""
        from raven_everos import backend as mod
        from raven_everos.backend import ServiceState
        from raven_everos.server import ProbeVerdict

        adapter = MagicMock()
        adapter.search = AsyncMock(return_value=None)
        b = self._backend(adapter)
        b._state = ServiceState.UNCONFIGURED
        monkeypatch.setattr(mod, "_PROBE_MIN_INTERVAL_S", 0.0)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u, **_kw: ProbeVerdict.OK)

        assert await b.recall("q", user_id="u", top_k=5) == []
        assert b._probe_task is None, "probed a state no probe can resolve"
        assert b._state is ServiceState.UNCONFIGURED


@pytest.mark.asyncio
class TestTheDegradationWarningOnASelfManagedServer:
    """The one place raven can only talk, it was silent.

    The warning gated on the local toml's embedding role. A self-managed
    install records no root, so that read lands on the fallback root -- the
    fabricated one doctor was fixed to stop trusting. It usually does not
    exist, the gate reads False, and the warning never fires on exactly the
    path whose comment argues the case for it is strongest: raven cannot repair
    somebody else's embedding config, so saying so is the only move it has.

    When a stale raven-managed root does exist the gate passes instead, and the
    advice points at a log raven never wrote for that server.
    """

    @staticmethod
    def _ctx():
        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.services.notify = NOTICES.append
        ctx.logger = MagicMock()
        return ctx

    async def _start_unowned(self, monkeypatch, *, caps: dict):
        from raven_everos import health
        from raven_everos.backend import EverosBackend
        from raven_everos.server import ProbeVerdict

        monkeypatch.setattr("raven_everos.config.everos_owned", lambda: False)
        monkeypatch.setattr(
            "raven_everos.config.everos_role_configured",
            lambda _s: pytest.fail("read the local toml for a root raven does not own"),
        )
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u, **_kw: ProbeVerdict.OK)
        monkeypatch.setattr(
            health,
            "probe_capabilities",
            lambda *_a, **_kw: health.CapabilityReport(reachable=True, capabilities=caps),
        )
        b = EverosBackend(self._ctx())
        await b.start()
        return b

    async def test_it_speaks_when_the_server_says_embedding_is_down(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        await self._start_unowned(monkeypatch, caps={"llm": True, "embed": False})

        err = " ".join(" ".join(NOTICES).split())
        assert "embedding is unavailable" in err
        # Their server, their log. Pointing at raven's is a dead end.
        assert "everos-server.log" not in err

    async def test_it_stays_quiet_when_the_server_says_embedding_is_fine(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        await self._start_unowned(monkeypatch, caps={"llm": True, "embed": True})

        assert "embedding is unavailable" not in " ".join(NOTICES)

    async def test_a_server_too_old_to_report_is_not_condemned(
        self, monkeypatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An empty capability map is silence, not a negative."""
        await self._start_unowned(monkeypatch, caps={})

        assert "embedding is unavailable" not in " ".join(NOTICES)


class TestConvertMessagesTimestamps:
    """everos's DTO wants ms epoch, so the conversion must produce one."""

    def test_iso_string_becomes_ms_epoch(self) -> None:
        # instance_log.build_turn stamps rows with datetime.now().isoformat(),
        # so this is the shape a trace payload actually arrives in.
        out = convert_messages(
            [{"role": "user", "content": "hi", "timestamp": "2026-08-20T09:53:46.693637"}],
            agent_id="coder",
            user_id="liv",
        )
        assert isinstance(out[0]["timestamp"], int)
        assert out[0]["timestamp"] == int(datetime(2026, 8, 20, 9, 53, 46, 693637).timestamp() * 1000)

    def test_ms_epoch_int_passes_through(self) -> None:
        out = convert_messages(
            [{"role": "user", "content": "hi", "timestamp": 1755683626693}],
            agent_id="coder",
            user_id="liv",
        )
        assert out[0]["timestamp"] == 1755683626693

    def test_seconds_epoch_is_scaled_to_ms(self) -> None:
        out = convert_messages(
            [{"role": "user", "content": "hi", "timestamp": 1755683626}],
            agent_id="coder",
            user_id="liv",
        )
        assert out[0]["timestamp"] == 1755683626000

    def test_unparseable_timestamp_falls_back_to_now(self) -> None:
        before = int(time.time() * 1000)
        out = convert_messages(
            [{"role": "user", "content": "hi", "timestamp": "not a date"}],
            agent_id="coder",
            user_id="liv",
        )
        assert isinstance(out[0]["timestamp"], int)
        assert out[0]["timestamp"] >= before

    def test_missing_timestamp_still_falls_back_to_now(self) -> None:
        before = int(time.time() * 1000)
        out = convert_messages([{"role": "user", "content": "hi"}], agent_id="coder", user_id="liv")
        assert out[0]["timestamp"] >= before

    def test_nan_returns_none(self) -> None:
        assert as_ms_epoch(float("nan")) is None

    def test_positive_infinity_returns_none(self) -> None:
        assert as_ms_epoch(float("inf")) is None

    def test_negative_infinity_returns_none(self) -> None:
        assert as_ms_epoch(float("-inf")) is None

    def test_bool_is_not_a_timestamp(self) -> None:
        assert as_ms_epoch(True) is None
        assert as_ms_epoch(False) is None

    def test_zero_and_negative_number_return_none(self) -> None:
        assert as_ms_epoch(0) is None
        assert as_ms_epoch(-5) is None

    def test_seconds_epoch_float_is_scaled_to_ms(self) -> None:
        assert as_ms_epoch(1755683626.5) == 1755683626500

    def test_iso_string_without_microseconds(self) -> None:
        assert as_ms_epoch("2026-08-20T09:53:46") == int(datetime(2026, 8, 20, 9, 53, 46).timestamp() * 1000)

    def test_iso_string_with_utc_offset(self) -> None:
        expected = int(datetime(2026, 8, 20, 9, 53, 46, tzinfo=timezone.utc).timestamp() * 1000)
        assert as_ms_epoch("2026-08-20T09:53:46+00:00") == expected

    def test_iso_string_with_z_suffix(self) -> None:
        expected = int(datetime(2026, 8, 20, 9, 53, 46, tzinfo=timezone.utc).timestamp() * 1000)
        assert as_ms_epoch("2026-08-20T09:53:46Z") == expected

    def test_pre_epoch_iso_string_returns_none(self) -> None:
        # A negative ms value would survive a caller's `as_ms_epoch(...) or
        # now_ms` fallback -- a negative int is truthy -- and silently keep a
        # bogus pre-1970 stamp instead of falling back to now.
        assert as_ms_epoch("1969-12-31T00:00:00Z") is None


class TestConvertMessagesIsReusable:
    """The conversion is callable without an EverosBackend instance."""

    def test_owner_routing_honours_the_given_ids(self) -> None:
        out = convert_messages(
            [
                {"role": "user", "content": "read it"},
                {"role": "assistant", "content": "reading"},
                {"role": "tool", "content": "result", "tool_call_id": "c1"},
            ],
            agent_id="coder",
            user_id="liv",
        )
        assert [row["sender_id"] for row in out] == ["liv", "coder", "coder"]

    def test_method_delegates_to_the_function(self) -> None:
        messages = [{"role": "user", "content": "hi", "timestamp": 1755683626693}]
        assert EverosBackend._convert_messages(messages, agent_id="coder", user_id="liv") == convert_messages(
            messages, agent_id="coder", user_id="liv"
        )


class TestEveryTurnIsExtracted:
    """A flush is what makes EverOS extract, and only a flush does.

    Batching it every N turns leaves a session that ends before the boundary
    unextracted, and the turn counter is per-process, so that includes every
    short run. Firing one per turn is affordable again now that the write is
    off the turn's critical path.
    """

    def test_flush_defaults_to_every_turn(self, tmp_path: Path) -> None:
        assert _backend(tmp_path)._flush_every_turns == 1

    def test_an_explicit_config_value_still_wins(self, tmp_path: Path) -> None:
        assert _backend(tmp_path, flush_every_turns=4)._flush_every_turns == 4


@pytest.mark.asyncio
class TestRetriesShareOneFlushDecision:
    """A retry is not a new turn: it is the same turn, tried again.

    Before this, every attempt at a record -- retry or not -- advanced
    ``_turn_counts`` and recomputed ``is_final`` from scratch. A record that
    needed all five AgentLoop attempts therefore looked like five turns to
    the flush cadence, and whichever attempt happened to land on the flush
    boundary got promoted to the 360s extraction budget even though the
    record's first attempt was not final.
    """

    async def test_five_attempts_advance_the_counter_once(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)

        for attempt in range(5):
            await b.store("s", [{"role": "user", "content": "x"}], metadata={"attempt": attempt})

        assert b._turn_counts["s"] == 1

    async def test_no_attempt_gets_the_final_budget_the_first_did_not(self, tmp_path: Path) -> None:
        """Session "s" starts at turn 1 of a flush-every-4 cadence: the first
        attempt is not final, so none of its retries may become final either --
        even the fourth retry, which lands on what would be turn 4."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)

        for attempt in range(5):
            await b.store("s", [{"role": "user", "content": "x"}], metadata={"attempt": attempt})

        assert [call["is_final"] for call in adapter.memorize_calls] == [False] * 5

    async def test_a_final_attempt_stays_final_on_retry(self, tmp_path: Path) -> None:
        """The reverse must also hold: if the first attempt lands on the flush
        boundary, a retry of that same record must not silently downgrade it
        back to a plain append."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=1)

        for attempt in range(3):
            await b.store("s", [{"role": "user", "content": "x"}], metadata={"attempt": attempt})

        assert [call["is_final"] for call in adapter.memorize_calls] == [True] * 3
        assert b._turn_counts["s"] == 1

    async def test_an_explicit_is_final_still_overrides_attempt_tracking(self, tmp_path: Path) -> None:
        """A bulk-import caller supplying ``is_final`` directly (the other,
        pre-existing override) must not be reinterpreted as an attempt-0 turn."""
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter, flush_every_turns=4)

        await b.store("s", [{"role": "user", "content": "x"}], metadata={"is_final": True, "attempt": 0})

        assert adapter.memorize_calls[0]["is_final"] is True
        assert "s" not in b._turn_counts


@pytest.mark.asyncio
async def test_a_write_failed_by_our_own_stop_does_not_demote_the_service(tmp_path: Path) -> None:
    """`stop` closes the transport under writes that are still on the wire.

    Classifying that as a service fault blames EverOS for this process's exit,
    and leaves the next session opening against a state this one invented. The
    The loss is still reported: `False` is what StorePipeline counts.
    """

    class _ClosedAdapter:
        async def memorize(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("client has been closed")

        async def aclose(self) -> None:
            return None

    b = _backend(tmp_path, adapter=_ClosedAdapter())
    b._state = ServiceState.READY
    messages = [{"role": "user", "content": "hi"}]

    await b.stop()
    assert await b.store("s1", messages) is False

    assert b._state is ServiceState.READY


class TestHealth:
    """What raven doctor and raven import read off the backend."""

    def _patch(self, monkeypatch, *, owned=True, configured=(), report=None):
        from raven_everos import config as ue
        from raven_everos import health

        monkeypatch.setattr(ue, "everos_owned", lambda: owned)
        monkeypatch.setattr(ue, "everos_root", lambda: Path("/root/everos"))
        monkeypatch.setattr(ue, "everos_role_configured", lambda s: s in configured)
        monkeypatch.setattr(health, "probe_capabilities", lambda _u: report or health.CapabilityReport(reachable=False))

    async def test_a_server_that_is_not_running_is_not_a_fault(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, configured=("llm",))
        h = await _backend(tmp_path).health()
        assert h.ready is False
        assert not [c for c in h.checks if c.status == "missing"]
        assert any(c.label == "server" and "starts on demand" in (c.hint or "") for c in h.checks)

    async def test_an_unbuilt_required_role_is_a_fault(self, tmp_path, monkeypatch):
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            configured=("llm", "embedding"),
            report=CapabilityReport(reachable=True, capabilities={"llm": False, "embed": True}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is False
        assert [c.label for c in h.checks if c.status == "missing"] == ["llm"]

    async def test_an_unbuilt_optional_role_costs_quality_not_function(self, tmp_path, monkeypatch):
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            configured=("llm", "embedding"),
            report=CapabilityReport(reachable=True, capabilities={"llm": True, "embed": False}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is True
        assert not [c for c in h.checks if c.status == "missing"]
        assert any(c.label == "embedding" and c.status == "degraded" for c in h.checks)

    async def test_an_unbuilt_multimodal_role_is_reported(self, tmp_path, monkeypatch):
        """Pins the ``multimodal`` -> ``multimodal_llm`` capability key: the
        section name and the key the server answers with differ, so a report
        that says the role failed must still reach the ``multimodal`` check."""
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            configured=("llm", "multimodal"),
            report=CapabilityReport(reachable=True, capabilities={"llm": True, "multimodal_llm": False}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is True
        assert not [c for c in h.checks if c.status == "missing"]
        assert any(c.label == "multimodal" and c.status == "degraded" for c in h.checks)

    async def test_a_self_managed_server_reports_only_what_it_says(self, tmp_path, monkeypatch):
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            owned=False,
            report=CapabilityReport(reachable=True, capabilities={"llm": True, "embed": False}),
        )
        h = await _backend(tmp_path).health()
        labels = {c.label for c in h.checks}
        assert "rerank" not in labels and "multimodal" not in labels
        assert any(c.label == "memories" and "managed by you" in (c.hint or "") for c in h.checks)

    async def test_a_server_that_reports_no_capabilities_is_not_condemned(self, tmp_path, monkeypatch):
        from raven_everos.health import CapabilityReport

        self._patch(monkeypatch, configured=("llm",), report=CapabilityReport(reachable=True, capabilities={}))
        h = await _backend(tmp_path).health()
        assert h.ready is True
        assert not [c for c in h.checks if c.status == "missing"]
        assert any(c.label == "capabilities" for c in h.checks)

    async def test_a_bad_identity_is_a_config_fault_not_a_server_one(self, tmp_path, monkeypatch):
        self._patch(monkeypatch)
        h = await _backend(tmp_path, user_id="../x").health()
        assert h.ready is False
        assert h.checks[0].label == "identity" and h.checks[0].status == "missing"
        assert "server log" not in (h.checks[0].hint or "")

    async def test_the_probe_follows_the_configured_address(self, tmp_path, monkeypatch):
        """Probing the default while the backend reads its own slice reports on
        a server nobody is using: someone who moved everos off 18791 is told it
        is not running."""
        from raven_everos import health

        asked: list[str] = []
        self._patch(monkeypatch, configured=("llm",))
        monkeypatch.setattr(
            health,
            "probe_capabilities",
            lambda url: asked.append(url) or health.CapabilityReport(reachable=False),
        )

        h = await _backend(tmp_path, base_url="http://localhost:29999").health()

        assert asked == ["http://localhost:29999"]
        assert any(c.label == "address" and c.hint == "http://localhost:29999" for c in h.checks)

    async def test_a_running_server_with_its_roles_built_is_reported_as_such(self, tmp_path, monkeypatch):
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            configured=("llm", "embedding"),
            report=CapabilityReport(reachable=True, capabilities={"llm": True, "embed": True}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is True
        assert any(c.label == "server" and c.hint == "running" for c in h.checks)
        assert any(c.label == "embedding" and c.status == "ok" for c in h.checks)

    async def test_a_check_names_where_the_memories_are(self, tmp_path, monkeypatch):
        """ "Where are my memories" is answered here, so nobody has to read
        config.json by hand."""
        self._patch(monkeypatch, configured=("llm",))
        h = await _backend(tmp_path).health()
        assert any(c.label == "memories" and c.hint == "/root/everos" for c in h.checks)

    async def test_an_unconfigured_optional_role_names_what_it_costs(self, tmp_path, monkeypatch):
        """Per role rather than one blanket "optional": they degrade
        differently, and someone weighing up embedding needs to know it costs
        semantic recall specifically. Never a fault."""
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            configured=("llm",),
            report=CapabilityReport(reachable=True, capabilities={"llm": True, "embed": True, "rerank": False}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is True
        notes = {c.label: (c.status, c.hint) for c in h.checks}
        assert notes["embedding"] == ("degraded", "not configured (recall matches keywords, not meaning)")
        assert notes["rerank"][0] == "degraded"
        assert notes["multimodal"][1] == "not configured (images, PDFs and audio stay out of memory)"

    async def test_a_self_managed_root_is_never_read_from_disk(self, tmp_path, monkeypatch):
        """No root is recorded for a root the user runs, so ``everos_root()``
        would answer with a fallback -- a directory that is not theirs and holds
        none of their memories -- and the roles read out of that directory's toml
        would describe an install nobody is using."""
        from raven_everos import config as ue
        from raven_everos.health import CapabilityReport

        self._patch(monkeypatch, owned=False, report=CapabilityReport(reachable=True, capabilities={"llm": True}))
        monkeypatch.setattr(
            ue,
            "everos_role_configured",
            lambda _s: pytest.fail("read the local toml for a root raven does not own"),
        )

        h = await _backend(tmp_path).health()

        assert not any("/root/everos" in (c.hint or "") for c in h.checks)

    async def test_a_self_managed_server_that_cannot_build_its_llm_is_a_fault(self, tmp_path, monkeypatch):
        """ "What the server knows about" and "what it built" must not be one
        list: over one capability map those conditions are mutually exclusive,
        which left the fault list structurally empty and reported a server that
        could not build its LLM as healthy."""
        from raven_everos.health import CapabilityReport

        self._patch(
            monkeypatch,
            owned=False,
            report=CapabilityReport(reachable=True, capabilities={"llm": False, "embed": True}),
        )
        h = await _backend(tmp_path).health()
        assert h.ready is False
        assert [c.label for c in h.checks if c.status == "missing"] == ["llm"]

    async def test_a_self_managed_server_that_is_down_says_who_starts_it(self, tmp_path, monkeypatch):
        """Raven starts the server it owns on demand and never touches one it
        does not, so "not running" needs two different sentences."""
        self._patch(monkeypatch, owned=False)
        h = await _backend(tmp_path).health()
        assert h.ready is False
        assert any(c.label == "server" and "start it yourself" in (c.hint or "") for c in h.checks)


@pytest.mark.asyncio
class TestDeleteChangesTheSourceOfTruth:
    """Markdown is EverOS's source of truth; ``.index/`` is derived from it.

    A delete that only removed the index row un-deleted itself: cascade
    re-embeds every entry the file still carries on the next append, so the
    memory came back after the user had been told it was gone. These run
    against real files under a temporary root, with no writer mocked -- a fake
    writer would happily record a call this bug also made.
    """

    @staticmethod
    def _root(tmp_path, monkeypatch):
        monkeypatch.setenv("EVEROS_ROOT", str(tmp_path))
        return tmp_path

    @staticmethod
    def _episode_log(root):
        directory = root / "default_app" / "default_project" / "users" / "u1" / "episodes"
        directory.mkdir(parents=True)
        path = directory / "episode-2026-09-15.md"
        path.write_text(
            "---\n"
            "id: episode_log_u1_2026-09-15\n"
            "type: episode_daily\n"
            "file_type: episode_daily\n"
            "schema_version: 1\n"
            "user_id: u1\n"
            "track: user\n"
            "date: '2026-09-15'\n"
            "entry_count: 2\n"
            "---\n"
            "<!-- entry:ep_keep -->\n## ep_keep\nkeep me\n<!-- /entry:ep_keep -->\n"
            "<!-- entry:ep_drop -->\n## ep_drop\ndrop me\n<!-- /entry:ep_drop -->\n",
            encoding="utf-8",
        )
        return path

    async def test_an_episode_is_retired_in_the_file_that_owns_it(self, tmp_path, monkeypatch) -> None:
        root = self._root(tmp_path, monkeypatch)
        path = self._episode_log(root)
        backend = _backend(SimpleNamespace())
        row = SimpleNamespace(
            md_path="default_app/default_project/users/u1/episodes/episode-2026-09-15.md",
            entry_id="ep_drop",
        )
        monkeypatch.setattr(
            "everos.infra.persistence.lancedb.episode_repo.get_by_id",
            AsyncMock(return_value=row),
        )

        assert await backend.delete("u1_ep_drop", kind="episode") is True

        body = path.read_text(encoding="utf-8")
        assert "deprecated_entries" in body and "ep_drop" in body
        # Search filters `deprecated_by IS NULL`, and cascade re-applies the map
        # on every sync -- so the entry stays gone across rebuilds without the
        # adapter rewriting a file format EverOS owns.
        assert "keep me" in body and "drop me" in body

    async def test_a_skill_is_removed_from_disk(self, tmp_path, monkeypatch) -> None:
        """The one destructive operation EverOS's skill writer has, used as it
        is. A skill is a directory, not an entry in a log, so retiring it the
        way an episode is retired would leave it on disk and recallable."""
        from everos.core.persistence import MemoryRoot
        from everos.infra.persistence.markdown.mds import AgentSkillFrontmatter

        self._root(tmp_path, monkeypatch)
        root = MemoryRoot.resolve()
        skill_dir = (
            root.agents_dir("default", "default")
            / "a1"
            / AgentSkillFrontmatter.SKILLS_CONTAINER_NAME
            / AgentSkillFrontmatter.skill_dir_name("tokenizer edge cases")
        )
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
        backend = _backend(SimpleNamespace())
        monkeypatch.setattr(
            "everos.infra.persistence.lancedb.agent_skill_repo.get_by_id",
            AsyncMock(return_value=SimpleNamespace(owner_id="a1", name="tokenizer edge cases")),
        )

        assert await backend.delete("a1_tokenizer", kind="agent_skill") is True
        assert not skill_dir.exists()

    async def test_a_skill_row_that_names_nothing_removes_nothing(self, tmp_path, monkeypatch) -> None:
        self._root(tmp_path, monkeypatch)
        backend = _backend(SimpleNamespace())
        monkeypatch.setattr(
            "everos.infra.persistence.lancedb.agent_skill_repo.get_by_id",
            AsyncMock(return_value=None),
        )

        assert await backend.delete("nope", kind="agent_skill") is False

    async def test_an_id_nothing_matches_changes_no_file(self, tmp_path, monkeypatch) -> None:
        root = self._root(tmp_path, monkeypatch)
        path = self._episode_log(root)
        before = path.read_text(encoding="utf-8")
        backend = _backend(SimpleNamespace())
        monkeypatch.setattr(
            "everos.infra.persistence.lancedb.episode_repo.get_by_id",
            AsyncMock(return_value=None),
        )

        assert await backend.delete("no-such-id", kind="episode") is False
        assert path.read_text(encoding="utf-8") == before

    async def test_a_kind_everos_cannot_remove_says_so_and_touches_nothing(self, tmp_path, monkeypatch) -> None:
        """EverOS has no entry-level writer for a case log and no deletion at
        all for a profile. Inventing one here would put this adapter back in
        the business of owning a file format EverOS does not expose."""
        root = self._root(tmp_path, monkeypatch)
        path = self._episode_log(root)
        before = path.read_text(encoding="utf-8")
        backend = _backend(SimpleNamespace())

        assert await backend.delete("p1", kind="profile") is False
        assert await backend.delete("c1", kind="agent_case") is False
        assert path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
class TestRecallSession:
    """Reading back what one finished call left behind.

    An exact filter, not a search: the host asks after a sub-agent has run in a
    process of its own, and there is no query to rank by.
    """

    @staticmethod
    def _ready(adapter):
        from raven_everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "default"
        ctx.services.user_id = "default"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = ServiceState.READY
        return b

    async def test_one_track_per_call_reaches_the_adapter(self) -> None:
        adapter = MagicMock()
        adapter.get_session = AsyncMock(return_value=[])
        backend = self._ready(adapter)

        await backend.recall_session("cli:abc", user_id="u1")

        adapter.get_session.assert_awaited_once_with("cli:abc", user_id="u1", agent_id=None)

    async def test_rows_become_memories_with_their_kind(self) -> None:
        adapter = MagicMock()
        adapter.get_session = AsyncMock(
            return_value=[
                {"id": "e1", "_memory_type": "episode", "subject": "Audit", "episode": "Ran the audit."},
                {
                    "id": "c1",
                    "_memory_type": "agent_case",
                    "task_intent": "fix the tokenizer",
                    "key_insight": "contractions are one token",
                },
            ]
        )
        backend = self._ready(adapter)

        out = await backend.recall_session("cli:abc", agent_id="a1")

        assert [m.metadata["type"] for m in out] == ["episode", "agent_case"]
        assert out[0].text == "Audit - Ran the audit."
        assert "contractions are one token" in out[1].text

    async def test_both_tracks_or_neither_is_a_caller_bug(self) -> None:
        adapter = MagicMock()
        adapter.get_session = AsyncMock(return_value=[])
        backend = self._ready(adapter)

        assert await backend.recall_session("s") == []
        assert await backend.recall_session("s", user_id="u", agent_id="a") == []
        adapter.get_session.assert_not_awaited()

    async def test_an_unreachable_service_is_empty_not_a_raise(self) -> None:
        """A caller writing an audit trail gets "nothing to report", not a
        failed run."""
        import httpx

        adapter = MagicMock()
        adapter.get_session = AsyncMock(side_effect=httpx.ReadTimeout("hung"))
        backend = self._ready(adapter)

        assert await backend.recall_session("s", user_id="u") == []


@pytest.mark.asyncio
class TestStoreConventions:
    """The two metadata keys the contract asks every backend to honour."""

    @staticmethod
    def _ready(adapter):
        from raven_everos.backend import EverosBackend, ServiceState

        ctx = MagicMock()
        ctx.config = {"base_url": "http://localhost:18791"}
        ctx.services.agent_id = "host-agent"
        ctx.services.user_id = "host-user"
        ctx.logger = MagicMock()
        b = EverosBackend(ctx, adapter=adapter)
        b._state = ServiceState.READY
        return b

    async def test_flush_extracts_now_rather_than_on_the_next_turn(self) -> None:
        """The caller is handing over a conversation that has already ended and
        will read the result back immediately."""
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        backend = self._ready(adapter)
        backend._flush_every_turns = 100

        await backend.store("s", [{"role": "user", "content": "x"}], metadata={"flush": True})

        assert adapter.memorize.await_args.kwargs["is_final"] is True

    async def test_the_owners_a_real_config_produces_are_honoured(self) -> None:
        """The block reaches store as an agent wrote it in raven's config, and
        raven's config spells its keys in camelCase.

        Reading only the contract's snake_case made the override silently never
        fire for a real config: every sub-agent's memories went under the
        host's own identity, where recall for that agent never looks. A
        hand-built metadata dict hid it, which is why this one is built the way
        the product builds it.
        """
        from raven.agent.subagent_memory import scope_from_config
        from raven.config.schema import SubagentMemoryConfig

        scope = scope_from_config(
            SubagentMemoryConfig.model_validate({"userId": "liv", "agentId": "coder", "source": "trace"})
        )
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        backend = self._ready(adapter)
        captured: dict = {}
        backend._convert_messages = lambda messages, *, agent_id, user_id: (
            captured.update(  # type: ignore[method-assign]
                agent_id=agent_id, user_id=user_id
            )
            or [{"role": "user", "content": "x"}]
        )

        await backend.store("s", [{"role": "user", "content": "x"}], metadata={"flush": True, **scope.block})

        assert captured == {"user_id": "liv", "agent_id": "coder"}

    async def test_per_call_owners_do_not_change_the_backends_identity(self) -> None:
        """The content is a sub-agent's; filing it under the host would put it
        where recall for that agent never looks. The default identity stays the
        one the host granted."""
        adapter = MagicMock()
        adapter.memorize = AsyncMock(return_value=None)
        backend = self._ready(adapter)

        await backend.store(
            "s",
            [{"role": "user", "content": "x"}],
            metadata={"user_id": "sub-user", "agent_id": "sub-agent"},
        )

        assert (backend._user_id, backend._agent_id) == ("host-user", "host-agent")


class TestRequestBodiesMatchEverosModels:
    """Every body raven sends is checked against EverOS's own request models.

    The wire contract lives in the everos package this plugin pins, so the
    models are the one source that says whether a call will be accepted or
    refused with 422 -- ``tool_call_id`` on a tool row, the owner xor, the
    ``top_k`` bounds, the four kind names. An upgrade that moves any of them
    fails here rather than in a user's gateway log.
    """

    def test_a_tool_exchange_is_accepted_by_the_add_route(self) -> None:
        from everos.entrypoints.api.routes.memorize import MemorizeAddRequest, MemorizeFlushRequest

        from raven.contracts.llm_provider import ToolCallRequest
        from raven.providers.tool_calls import openai_tool_call

        call = ToolCallRequest(id="call_abc", name="read_file", arguments={"path": "a.txt"})
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "read a.txt", "sender_id": "default"},
            {"role": "assistant", "content": "", "tool_calls": [openai_tool_call(call)]},
            {"role": "tool", "tool_call_id": "call_abc", "name": "read_file", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "it says hello"}]},
        ]
        converted = convert_messages(messages, agent_id="raven", user_id="default")

        request = MemorizeAddRequest.model_validate({"session_id": "s1", "messages": converted})
        assert [m.role for m in request.messages] == ["user", "assistant", "tool", "assistant"]
        assert request.messages[1].tool_calls is not None and request.messages[1].tool_calls[0].id == "call_abc"
        assert request.messages[2].tool_call_id == "call_abc"
        MemorizeFlushRequest.model_validate({"session_id": "s1", "app_id": "default", "project_id": "default"})

    @pytest.mark.parametrize(
        "body",
        [
            {"query": "q", "top_k": 5, "user_id": "default", "include_profile": True},
            {"query": "q", "top_k": 5, "agent_id": "raven"},
            {"query": "q", "top_k": 5, "agent_id": "raven", "enable_llm_rerank": True},
            {"query": "q", "top_k": 5, "user_id": "default", "include_profile": True, "method": "keyword"},
            {"user_id": "default", "query": "q", "top_k": 100},
        ],
        ids=["user-hybrid", "agent-hybrid", "agent-without-cross-encoder", "keyword-without-embedding", "memory-page"],
    )
    def test_every_search_shape_is_accepted(self, body: dict[str, Any]) -> None:
        from everos.memory.search.dto import SearchRequest

        SearchRequest.model_validate(body)

    def test_the_memory_page_listing_names_the_kinds_everos_knows(self) -> None:
        from everos.memory.get.dto import GetRequest

        from raven.rpc.methods.memory import _KINDS, _owner_body

        for kind in _KINDS:
            GetRequest.model_validate(
                _owner_body(kind, "default", "raven") | {"memory_type": kind, "page": 1, "page_size": 20}
            )


class TestTheProfileReachesThePromptAsText:
    def test_a_namespace_profile_is_rendered_not_repred(self) -> None:
        """The search response arrives as namespaces; handed one, the renderer
        fell through to ``str()`` and put ``namespace(...)`` -- evidence fields
        included -- into every prompt."""
        from raven_everos.backend import _jsonify

        data = _jsonify(
            {
                "episodes": [],
                "profiles": [
                    {
                        "id": "p",
                        "user_id": "u",
                        "profile_data": {
                            "explicit_info": [{"category": "location", "description": "Seattle", "evidence": "said so"}]
                        },
                    }
                ],
                "agent_cases": [],
                "agent_skills": [],
            }
        )

        text = EverosBackend._search_data_to_memories(data, "user")[0].text

        assert "namespace(" not in text
        assert "location" in text and "Seattle" in text
        assert "said so" not in text


class TestAToolCallOnlyTurn:
    def test_none_content_is_stored_empty(self) -> None:
        from raven.contracts.llm_provider import ToolCallRequest
        from raven.providers.tool_calls import openai_tool_call

        call = ToolCallRequest(id="c1", name="read_file", arguments={})
        out = convert_messages(
            [{"role": "assistant", "content": None, "tool_calls": [openai_tool_call(call)]}], agent_id="raven"
        )

        assert out[0]["content"] == "" and out[0]["tool_calls"][0]["id"] == "c1"


class TestCapabilitiesAreNotCachedWhenUnanswered:
    async def test_a_failed_probe_is_asked_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _HttpEverosAdapter("http://127.0.0.1:1")
        answers = iter([{}, {"embed": False}])

        async def probe():
            return next(answers)

        monkeypatch.setattr(adapter, "_probe_capabilities", probe)

        assert await adapter._capabilities() == {}
        assert await adapter._capabilities() == {"embed": False}
        assert await adapter._capabilities() == {"embed": False}


class TestSearchStaysWithinTheServersBounds:
    async def test_top_k_is_clamped_to_what_the_server_takes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _HttpEverosAdapter("http://127.0.0.1:1")
        seen: dict[str, Any] = {}

        class _Resp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"data": {}}

        async def post(url, json, headers, **kw):
            seen.update(json)
            return _Resp()

        monkeypatch.setattr(adapter._client, "post", post)
        monkeypatch.setattr(adapter, "_search_tuning", AsyncMock(return_value={}))

        await adapter.search(user_id="u", agent_id=None, query="q", top_k=500)

        assert seen["top_k"] == 100

    async def test_an_empty_query_asks_nothing(self, tmp_path: Path) -> None:
        adapter = _FakeAdapter()
        b = _backend(tmp_path, adapter=adapter)

        assert await b.recall("   ", user_id="u", agent_id=None, top_k=5) == []
        assert adapter.search_calls == []


class TestAGatewayStartsTheServerAgain:
    """A running gateway had no path back to a server that went away: the only
    spawn was in ``start()``. The probe that finds nothing listening now starts
    one, when this root is raven's, no child of ours still runs, nothing holds
    the lock, and not more than once per cooldown."""

    def _ready(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EverosBackend:
        b = EverosBackend(_ctx(tmp_path), adapter=_HttpEverosAdapter("http://127.0.0.1:1"))
        b._state = ServiceState.READY
        monkeypatch.setattr("raven_everos.config.everos_owned", lambda: True)
        monkeypatch.setattr("raven_everos.config.everos_root", lambda: tmp_path / "root")
        return b

    async def test_nothing_listening_and_nothing_holding_the_lock_starts_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = self._ready(tmp_path, monkeypatch)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u: ProbeVerdict.REFUSED)
        monkeypatch.setattr("raven_everos.server.lock_holder", lambda _r, **_kw: None)
        ensure = AsyncMock(return_value=None)
        monkeypatch.setattr("raven_everos.server.ensure_everos_server", ensure)

        await b._probe_once()
        assert ensure.await_count == 1 and b._state is ServiceState.READY

        await b._probe_once()
        assert ensure.await_count == 1, "not again within the cooldown"

    async def test_a_port_that_is_slow_to_refuse_counts_as_gone_when_nothing_holds_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seen on a Windows box: a dead port takes two to four seconds to be
        refused, longer than the probe waits, so the verdict is TIMEOUT."""
        b = self._ready(tmp_path, monkeypatch)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u: ProbeVerdict.TIMEOUT)
        monkeypatch.setattr("raven_everos.server.lock_holder", lambda _r, **_kw: None)
        ensure = AsyncMock(return_value=None)
        monkeypatch.setattr("raven_everos.server.ensure_everos_server", ensure)

        await b._probe_once()

        assert ensure.await_count == 1 and b._state is ServiceState.READY

    async def test_a_holder_still_draining_is_left_alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        b = self._ready(tmp_path, monkeypatch)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u: ProbeVerdict.REFUSED)
        monkeypatch.setattr("raven_everos.server.lock_holder", lambda _r, **_kw: SimpleNamespace(pid=1))
        ensure = AsyncMock(return_value=None)
        monkeypatch.setattr("raven_everos.server.ensure_everos_server", ensure)

        await b._probe_once()

        assert ensure.await_count == 0 and b._state is ServiceState.STARTING

    async def test_a_child_of_ours_still_running_means_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = self._ready(tmp_path, monkeypatch)
        b._proc = SimpleNamespace(pid=7, poll=lambda: None)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u: ProbeVerdict.REFUSED)
        ensure = AsyncMock(return_value=None)
        monkeypatch.setattr("raven_everos.server.ensure_everos_server", ensure)

        await b._probe_once()

        assert ensure.await_count == 0

    async def test_a_start_that_fails_keeps_probing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        b = self._ready(tmp_path, monkeypatch)
        monkeypatch.setattr("raven_everos.server.probe_health", lambda _u: ProbeVerdict.REFUSED)
        monkeypatch.setattr("raven_everos.server.lock_holder", lambda _r, **_kw: None)
        monkeypatch.setattr(
            "raven_everos.server.ensure_everos_server", AsyncMock(side_effect=RuntimeError("still shutting down"))
        )

        await b._probe_once()

        assert b._state is ServiceState.STARTING


class TestAPinThatCannotServeIsWithheld:
    def test_a_narrow_pin_is_kept_from_the_spawn_and_said_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from raven_everos import backend as backend_mod
        from raven_everos import config as cfg

        monkeypatch.setattr("raven.config.update.configured_embedding_width", lambda: 768)
        monkeypatch.setattr(
            cfg, "role_pin", lambda section: ("bge-base", "deepinfra") if section == "embedding" else None
        )
        monkeypatch.setattr(backend_mod, "_EMBEDDING_WIDTHS", {})
        said: list[str] = []
        b = _backend(tmp_path)
        b.notify = said.append
        try:
            b._withhold_an_embedding_that_cannot_serve()
            assert "embedding" in cfg.withheld_roles()
            assert len(said) == 1 and "768" in said[0] and "keyword" in said[0]

            b._withhold_an_embedding_that_cannot_serve()
            assert len(said) == 1, "measured and said once per pin"
        finally:
            cfg.release_role("embedding")

    def test_a_pin_wide_enough_is_handed_over(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven_everos import backend as backend_mod
        from raven_everos import config as cfg

        monkeypatch.setattr("raven.config.update.configured_embedding_width", lambda: 1024)
        monkeypatch.setattr(cfg, "role_pin", lambda section: ("qwen3", "deepinfra") if section == "embedding" else None)
        monkeypatch.setattr(backend_mod, "_EMBEDDING_WIDTHS", {})
        b = _backend(tmp_path)

        b._withhold_an_embedding_that_cannot_serve()

        assert "embedding" not in cfg.withheld_roles()


class TestAWindowsShutdownDrainsItsOwnServer:
    async def test_the_child_this_process_started_is_stopped_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from raven_everos.server import StopOutcome

        b = _backend(tmp_path)
        b._proc = SimpleNamespace(pid=77, poll=lambda: None)
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("raven_everos.config.everos_owned", lambda: True)
        asked: list[tuple[int, dict[str, Any]]] = []
        monkeypatch.setattr(
            "raven_everos.server.stop_pid", lambda pid, **kw: asked.append((pid, kw)) or StopOutcome.STOPPED
        )

        await b.stop()

        assert asked == [(77, {"timeout": 60.0, "grace": 60.0})]

    async def test_a_server_another_process_started_is_left_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = _backend(tmp_path)
        monkeypatch.setattr("sys.platform", "win32")
        asked: list[int] = []
        monkeypatch.setattr("raven_everos.server.stop_pid", lambda pid, **kw: asked.append(pid))

        await b.stop()

        assert asked == []
