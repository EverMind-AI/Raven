"""everos plugin skeleton + end-to-end plugin discovery.

The EverOS backend is its own distribution, built from
``plugins-dist/everos-memory/`` and installed into the environment, so
discovery here points at the entry-point source it registers itself in.

Verifies:

1. The package imports cleanly (manifest TOML shipped + factory
   resolvable).
2. ``PluginDiscovery(entry_points_group=...)`` surfaces the ``everos-memory``
   manifest from the installed distribution.
3. ``PluginRegistry.activate`` accepts the discovered manifest and
   registers the ``everos`` ``memory_backend`` factory.
4. ``build_memory_backend("everos", ...)`` constructs an
   :class:`EverosBackend` that satisfies the host's
   :class:`MemoryBackend` Protocol.
5. The five Protocol methods are awaitable and return the documented
   empty / no-op shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.contracts.memory import Memory, MemoryBackend
from raven.plugins import (
    ManifestOrigin,
    PluginDiscovery,
    ServiceLocator,
    assemble_plugin_registry,
)

# The group the host scans and the distribution registers itself in.
_GROUP = "raven.plugins"


# ---------------------------------------------------------------------------
# Package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_imports_clean(self) -> None:
        import raven_everos
        from raven_everos.backend import EverosBackend, make_backend

        assert raven_everos.__version__ == "1.1.0"
        assert callable(make_backend)
        assert EverosBackend is not None

    def test_manifest_shipped_with_package(self) -> None:
        """``raven-plugin.toml`` is a package-data file inside the
        installed wheel — accessible via importlib.resources."""
        from importlib.resources import files

        manifest = files("raven_everos").joinpath("raven-plugin.toml")
        assert manifest.is_file()
        text = manifest.read_text(encoding="utf-8")
        assert 'id                 = "everos-memory"' in text
        assert "bundled            = false" in text
        assert "enabled_by_default = true" in text


# ---------------------------------------------------------------------------
# Bundled discovery
# ---------------------------------------------------------------------------


class TestEntryPointDiscovery:
    def test_discovered_via_entry_points(self) -> None:
        d = PluginDiscovery(entry_points_group=_GROUP)
        out = d.discover()
        ids = [p.manifest.id for p in out]
        assert "everos-memory" in ids

    def test_discovered_record_marked_as_entry_points(self) -> None:
        d = PluginDiscovery(entry_points_group=_GROUP)
        out = d.discover()
        record = next(p for p in out if p.manifest.id == "everos-memory")
        assert record.source == ManifestOrigin.ENTRY_POINTS
        # The manifest lives inside the installed package, so the record
        # carries no on-disk path for callers to display.
        assert record.location is None

    def test_a_user_drop_in_now_shadows_the_installed_copy(self, tmp_path: Path) -> None:
        """What losing the bundled seat costs: everos is an entry-point
        plugin like any other, so a same-id manifest in the user dir wins.
        That is the documented priority, and it is how a developer swaps in a
        locally edited copy -- but it also means a stale drop-in can shadow
        the shipped default, which the bundled seat used to forbid."""
        user_dir = tmp_path / "user"
        plugin_dir = user_dir / "everos-memory"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "raven-plugin.toml").write_text(
            "[plugin]\n"
            'id = "everos-memory"\n'
            'version = "9.9.9"\n'
            "bundled = false\n"
            "enabled_by_default = true\n"
            "\n"
            "[[plugin.contributes.memory_backends]]\n"
            'name = "everos"\n'
            'factory = "raven_everos.backend:make_backend"\n',
            encoding="utf-8",
        )
        d = PluginDiscovery(entry_points_group=_GROUP, user_dir=user_dir)
        out = d.discover()
        record = next(p for p in out if p.manifest.id == "everos-memory")
        assert record.source == ManifestOrigin.USER
        assert record.manifest.version == "9.9.9"


# ---------------------------------------------------------------------------
# Registry activation + factory wiring
# ---------------------------------------------------------------------------


class TestActivationAndFactory:
    def test_activate_registers_everos_backend(self) -> None:
        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        assert "everos-memory" in reg.activated_ids()
        assert "everos" in reg.memory_backend_names()

    def test_build_returns_protocol_compliant_backend(
        self,
        tmp_path: Path,
    ) -> None:
        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        backend = reg.build_memory_backend(
            "everos",
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        # @runtime_checkable Protocol: isinstance returns True iff all
        # five methods are present.
        assert isinstance(backend, MemoryBackend)


# ---------------------------------------------------------------------------
# Behavior — methods do what their docstrings promise
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path):
    from raven_everos.backend import ServiceState, _NoOpAdapter

    reg = assemble_plugin_registry(entry_points_group=_GROUP)
    be = reg.build_memory_backend(
        "everos",
        config={},
        services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
    )
    be._adapter = _NoOpAdapter()
    # Swapping the adapter after construction is a test-only move; the
    # constructor's adapter= argument is what tells a backend its transport is
    # someone else's problem. Say the same thing here, or the backend keeps
    # treating the service as unproven and refuses to write through the stub.
    be._state = ServiceState.READY
    return be


class TestStubBehavior:
    async def test_lifecycle_is_idempotent(self, backend) -> None:
        await backend.start()
        await backend.stop()
        await backend.start()
        await backend.stop()

    async def test_recall_returns_empty_list(self, backend) -> None:
        hits = await backend.recall("any query", user_id="x", top_k=5)
        assert hits == []
        assert isinstance(hits, list)
        assert all(isinstance(h, Memory) for h in hits)

    async def test_store_reports_that_it_landed(self, backend) -> None:
        """store answers whether the slice was written. The stub accepts
        everything, so it answers True -- a caller that checks the result must
        not read the stub as a permanent failure."""
        result = await backend.store(
            "session-1",
            [{"role": "user", "content": "hi"}],
        )
        assert result is True

    async def test_feedback_accepts_any_dict(self, backend) -> None:
        await backend.feedback({})
        await backend.feedback({"kind": "skill_usage", "ids": ["a", "b"]})


# ---------------------------------------------------------------------------
# Config passthrough
# ---------------------------------------------------------------------------


class TestConfigPassthrough:
    def test_default_constructs_http_adapter(self, tmp_path: Path) -> None:
        from raven_everos.backend import _HttpEverosAdapter

        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        backend = reg.build_memory_backend(
            "everos",
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        assert isinstance(backend._adapter, _HttpEverosAdapter)

    def test_base_url_passed_through(self, tmp_path: Path) -> None:
        from raven_everos.backend import _HttpEverosAdapter

        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        backend = reg.build_memory_backend(
            "everos",
            config={"base_url": "http://custom:9000"},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        assert isinstance(backend._adapter, _HttpEverosAdapter)
        assert backend._adapter._base_url == "http://custom:9000"
