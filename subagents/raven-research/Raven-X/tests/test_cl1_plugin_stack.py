"""CLI plugin-stack helper.

Exercises :func:`build_plugin_registry` and
:func:`maybe_build_memory_backend` against the bundled
``raven.plugin.memory.everos`` plugin installed via entry points.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("raven.plugin.memory.everos")

from raven.cli._plugin_stack import (
    build_plugin_registry,
    maybe_build_memory_backend,
    validate_plugin_config_slice,
    warn_on_memory_identity_mismatch,
)
from raven.config.raven import (
    MemoryConfig,
    PluginsConfig,
    RavenConfig,
)
from raven.memory_engine import MemoryBackend
from raven.plugin import PluginRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    memory_backend: str | None = "everos",
    disabled: list[str] | None = None,
    plugin_config: dict | None = None,
) -> RavenConfig:
    return RavenConfig(
        memory=MemoryConfig(backend=memory_backend),
        plugins=PluginsConfig(
            disabled=list(disabled or []),
            config=dict(plugin_config or {}),
        ),
    )


# ---------------------------------------------------------------------------
# build_plugin_registry
# ---------------------------------------------------------------------------


class TestBuildRegistry:
    def test_returns_registry_with_everos_activated(self) -> None:
        reg = build_plugin_registry(_config())
        assert isinstance(reg, PluginRegistry)
        assert "everos-memory" in reg.activated_ids()
        assert "everos" in reg.memory_backend_names()

    def test_disabled_plugin_id_skipped(self) -> None:
        reg = build_plugin_registry(
            _config(disabled=["everos-memory"]),
        )
        assert "everos-memory" not in reg.activated_ids()
        assert "everos" not in reg.memory_backend_names()


# ---------------------------------------------------------------------------
# maybe_build_memory_backend
# ---------------------------------------------------------------------------


class TestMaybeBuildBackend:
    def test_default_config_builds_everos(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend is not None
        assert isinstance(backend, MemoryBackend)

    def test_memory_backend_none_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(memory_backend=None),
        )
        assert backend is None

    def test_unknown_backend_returns_none_no_raise(
        self,
        tmp_path: Path,
    ) -> None:
        """A user-config typo / missing plugin must NOT crash boot —
        the helper logs + degrades, AgentLoop falls back to legacy."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(memory_backend="nonexistent"),
        )
        assert backend is None

    def test_disabled_backend_returns_none(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                memory_backend="everos",
                disabled=["everos-memory"],
            ),
        )
        assert backend is None


# ---------------------------------------------------------------------------
# Per-plugin config slice resolution
# ---------------------------------------------------------------------------


class TestConfigSliceResolution:
    def test_config_by_plugin_id(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos-memory": {"mode": "embedded", "base_url": "http://x"},
                }
            ),
        )
        # Backend received the config slice keyed by plugin id.
        assert backend._config["mode"] == "embedded"
        assert backend._config["base_url"] == "http://x"

    def test_config_by_backend_name_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """When the user uses the shorter key (backend name), the
        helper still finds it. Useful for handwritten configs."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos": {"mode": "http", "base_url": "http://y"},
                }
            ),
        )
        assert backend._config["mode"] == "http"
        assert backend._config["base_url"] == "http://y"

    def test_plugin_id_takes_precedence_over_backend_name(
        self,
        tmp_path: Path,
    ) -> None:
        """If both keys are present, the canonical (plugin id) wins."""
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(
                plugin_config={
                    "everos-memory": {"marker": "canonical"},
                    "everos": {"marker": "fallback"},
                }
            ),
        )
        assert backend._config["marker"] == "canonical"

    def test_no_config_slice_yields_empty_dict(
        self,
        tmp_path: Path,
    ) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend._config == {}


# ---------------------------------------------------------------------------
# Registry injection — caller can pass a pre-built registry
# ---------------------------------------------------------------------------


class TestRegistryInjection:
    def test_caller_supplied_registry_used(self, tmp_path: Path) -> None:
        reg = build_plugin_registry(_config())
        backend = maybe_build_memory_backend(
            tmp_path,
            _config(),
            registry=reg,
        )
        # Backend constructed via the explicit registry.
        assert backend is not None

    def test_default_construction_creates_internal_registry(
        self,
        tmp_path: Path,
    ) -> None:
        # Sanity: with no registry passed, the helper still works.
        backend = maybe_build_memory_backend(tmp_path, _config())
        assert backend is not None


# ---------------------------------------------------------------------------
# Workspace plumbing through ServiceLocator
# ---------------------------------------------------------------------------


class TestServiceLocatorPlumbing:
    def test_workspace_reaches_backend(self, tmp_path: Path) -> None:
        backend = maybe_build_memory_backend(tmp_path, _config())
        # EverosBackend stores ctx.services on construction.
        assert backend._services.workspace == tmp_path


# ---------------------------------------------------------------------------
# Teardown ordering — drain, promote, stop
# ---------------------------------------------------------------------------


class _RecordingLoop:
    def __init__(self, *, drain_raises=None, promote_raises=None) -> None:
        self.order: list[str] = []
        self._drain_raises = drain_raises
        self._promote_raises = promote_raises

    async def drain_backend_stores(self) -> None:
        self.order.append("drain")
        if self._drain_raises is not None:
            raise self._drain_raises

    async def promote_all_backend_sessions(self) -> None:
        self.order.append("promote")
        if self._promote_raises is not None:
            raise self._promote_raises


class _RecordingBackend:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def stop(self) -> None:
        self._order.append("stop")


class TestShutdownMemoryBackend:
    """The ordering contract the TUI and the gateway both depend on.

    Both surfaces had it open-coded, which is how the promotion step came to
    exist on neither: ``memory.flush_on_task_end`` documents itself for exactly
    these two and was read only by ``raven agent``.
    """

    async def test_drain_then_promote_then_stop(self) -> None:
        from raven.cli._plugin_stack import shutdown_memory_backend

        loop = _RecordingLoop()
        await shutdown_memory_backend(loop, _RecordingBackend(loop.order), promote=True)
        assert loop.order == ["drain", "promote", "stop"]

    async def test_promotion_is_opt_in(self) -> None:
        from raven.cli._plugin_stack import shutdown_memory_backend

        loop = _RecordingLoop()
        await shutdown_memory_backend(loop, _RecordingBackend(loop.order), promote=False)
        assert loop.order == ["drain", "stop"]

    async def test_a_failed_drain_still_reaches_stop(self) -> None:
        """Or the embedded index lock leaks and the next process cannot start."""
        from raven.cli._plugin_stack import shutdown_memory_backend

        loop = _RecordingLoop(drain_raises=RuntimeError("drain blew up"))
        await shutdown_memory_backend(loop, _RecordingBackend(loop.order), promote=True)
        assert loop.order == ["drain", "promote", "stop"]

    async def test_a_failed_promotion_still_reaches_stop(self) -> None:
        from raven.cli._plugin_stack import shutdown_memory_backend

        loop = _RecordingLoop(promote_raises=RuntimeError("everos down"))
        await shutdown_memory_backend(loop, _RecordingBackend(loop.order), promote=True)
        assert loop.order == ["drain", "promote", "stop"]

    async def test_no_backend_is_a_no_op(self) -> None:
        from raven.cli._plugin_stack import shutdown_memory_backend

        loop = _RecordingLoop()
        await shutdown_memory_backend(loop, None, promote=True)
        assert loop.order == []


class TestPluginConfigSchemaCheck:
    """The slice reaches the factory verbatim, so an undeclared key is inert.
    The check exists to make that visible; it must never change the slice."""

    SCHEMA = {
        "mode": {"type": "string", "default": "embedded"},
        "defer_extraction": {"type": "boolean", "default": False},
        "flush_every_turns": {"type": "integer", "default": 1},
        "timeout_s": {"type": "number", "default": 10.0},
        "api_key": {"type": "string"},
    }

    def test_a_valid_slice_says_nothing(self) -> None:
        assert (
            validate_plugin_config_slice(
                {"mode": "http", "defer_extraction": True, "timeout_s": 15.0},
                self.SCHEMA,
                label="x",
            )
            == []
        )

    def test_an_undeclared_key_is_reported_with_the_key_it_resembles(self) -> None:
        """The failure this replaces: a misspelled key is silently absent, so
        the setting reads as having had no effect and the config looks right."""
        problems = validate_plugin_config_slice(
            {"deffer_extraction": True},
            self.SCHEMA,
            label="plugins.config.p",
        )
        assert len(problems) == 1
        assert "deffer_extraction" in problems[0]
        assert "defer_extraction" in problems[0]

    def test_an_unrecognisable_key_is_reported_without_a_guess(self) -> None:
        problems = validate_plugin_config_slice({"totally_unrelated": 1}, self.SCHEMA, label="x")
        assert len(problems) == 1
        assert "did you mean" not in problems[0]

    def test_a_wrong_type_is_reported(self) -> None:
        problems = validate_plugin_config_slice({"flush_every_turns": "1"}, self.SCHEMA, label="x")
        assert len(problems) == 1
        assert "integer" in problems[0]

    def test_a_boolean_does_not_pass_as_a_number(self) -> None:
        """bool is an int subclass, so a stray ``true`` would satisfy a numeric
        declaration under a plain isinstance check."""
        problems = validate_plugin_config_slice({"timeout_s": True}, self.SCHEMA, label="x")
        assert len(problems) == 1
        assert "boolean" in problems[0]

    def test_an_int_satisfies_number(self) -> None:
        assert validate_plugin_config_slice({"timeout_s": 15}, self.SCHEMA, label="x") == []

    def test_an_explicit_null_is_not_a_type_error(self) -> None:
        """``null`` is how a config unsets a key; the factory applies its own
        default. Reporting it as a type error would punish valid config."""
        assert validate_plugin_config_slice({"api_key": None}, self.SCHEMA, label="x") == []

    def test_an_empty_schema_validates_nothing(self) -> None:
        """How a plugin opts out. Declaring nothing must not mean every key is
        wrong."""
        assert validate_plugin_config_slice({"anything": 1}, {}, label="x") == []

    def test_the_shipped_everos_manifest_declares_every_key_it_reads(self) -> None:
        """If the manifest lags the code, the check reports valid config as
        typos on every boot -- which is how a real warning gets tuned out."""
        import re
        import tomllib
        from pathlib import Path

        root = Path(__file__).parent.parent
        toml_path = root / "raven/plugin/memory/everos/raven-plugin.toml"
        manifest = tomllib.loads(toml_path.read_text())
        declared = set(manifest["plugin"].get("config_schema", {}))
        src = (root / "raven/plugin/memory/everos/backend.py").read_text()
        read = set(re.findall(r'_config\.get\(\s*"([a-z0-9_]+)"', src))
        undeclared = sorted(read - declared)
        assert not undeclared, f"backend reads undeclared keys: {undeclared}"


class TestMemoryIdentityMismatch:
    """A mismatch makes recall permanently empty with no error on either side,
    so the only place it can surface is a boot-time warning."""

    def _config(self, user_id: str = "default", agent_id: str = "default"):
        from raven.config.raven import RavenConfig

        return RavenConfig(memory={"userId": user_id, "agentId": agent_id})

    def test_aligned_ids_say_nothing(self) -> None:
        cfg = self._config("alice", "bot")
        slice_ = {"recall_enabled": True, "user_id": "alice", "agent_id": "bot"}
        assert warn_on_memory_identity_mismatch(cfg, slice_) == []

    def test_defaults_on_both_sides_say_nothing(self) -> None:
        """Both sides default to "default", which is why setting neither works
        and setting one does not."""
        assert warn_on_memory_identity_mismatch(self._config(), {"recall_enabled": True}) == []

    def test_a_user_id_mismatch_is_reported(self) -> None:
        cfg = self._config("alice")
        problems = warn_on_memory_identity_mismatch(cfg, {"recall_enabled": True, "user_id": "bob"})
        assert len(problems) == 1
        assert "memory.userId" in problems[0]
        assert "'bob'" in problems[0]

    def test_an_agent_id_mismatch_is_reported(self) -> None:
        cfg = self._config(agent_id="bot")
        problems = warn_on_memory_identity_mismatch(cfg, {"recall_enabled": True, "agent_id": "other"})
        assert len(problems) == 1
        assert "memory.agentId" in problems[0]

    def test_the_store_only_profile_is_silent(self) -> None:
        """``recall_enabled: false`` means nothing reads, so a difference costs
        nothing. Warning here would fire on every measurement arm."""
        cfg = self._config("alice", "bot")
        slice_ = {"recall_enabled": False, "user_id": "x", "agent_id": "y"}
        assert warn_on_memory_identity_mismatch(cfg, slice_) == []

    def test_a_disabled_lane_is_silent_on_its_own_id(self) -> None:
        """Skills recall off means the agent id is never used for recall."""
        cfg = self._config(agent_id="bot")
        slice_ = {
            "recall_enabled": True,
            "recall_skills_enabled": False,
            "agent_id": "other",
        }
        assert warn_on_memory_identity_mismatch(cfg, slice_) == []
