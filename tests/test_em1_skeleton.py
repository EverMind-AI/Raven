"""EM-1 — everos plugin skeleton + end-to-end plugin discovery.

The EverOS backend ships **bundled** inside raven at
``raven/plugin/memory/everos/`` (not as an external entry-point
package), so discovery here points at the bundled source.

Verifies:

1. The package imports cleanly (manifest TOML shipped + factory
   resolvable).
2. ``PluginDiscovery(bundled_dir=...)`` surfaces the ``everos-memory``
   manifest from the bundled directory.
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

import raven
from raven.memory_engine import Memory, MemoryBackend
from raven.plugin import (
    PluginDiscovery,
    ServiceLocator,
    Source,
    assemble_plugin_registry,
)

# Real bundled-plugins root inside the installed raven package.
_BUNDLED = Path(raven.__path__[0]) / "plugin" / "memory"


# ---------------------------------------------------------------------------
# Package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_imports_clean(self) -> None:
        import raven.plugin.memory.everos
        from raven.plugin.memory.everos.backend import EverosBackend, make_backend

        assert raven.plugin.memory.everos.__version__ == "1.0.0"
        assert callable(make_backend)
        assert EverosBackend is not None

    def test_manifest_shipped_with_package(self) -> None:
        """``raven-plugin.toml`` is a package-data file inside the
        installed wheel — accessible via importlib.resources."""
        from importlib.resources import files

        manifest = files("raven.plugin.memory.everos").joinpath("raven-plugin.toml")
        assert manifest.is_file()
        text = manifest.read_text(encoding="utf-8")
        assert 'id                 = "everos-memory"' in text
        assert "bundled            = true" in text
        assert "enabled_by_default = true" in text


# ---------------------------------------------------------------------------
# Bundled discovery
# ---------------------------------------------------------------------------


class TestBundledDiscovery:
    def test_discovered_via_bundled(self) -> None:
        d = PluginDiscovery(bundled_dir=_BUNDLED)
        out = d.discover()
        ids = [p.manifest.id for p in out]
        assert "everos-memory" in ids

    def test_discovered_record_marked_as_bundled(self) -> None:
        d = PluginDiscovery(bundled_dir=_BUNDLED)
        out = d.discover()
        record = next(p for p in out if p.manifest.id == "everos-memory")
        assert record.source == Source.BUNDLED
        # Bundled discovery has an on-disk manifest path.
        assert record.location is not None
        assert record.location.name == "raven-plugin.toml"

    def test_bundled_shadows_lower_priority_source(self, tmp_path: Path) -> None:
        """Builtin-shadow rule: a same-id manifest in a lower-priority
        source (user dir) must be shadowed by the bundled copy."""
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
            'factory = "raven.plugin.memory.everos.backend:make_backend"\n',
            encoding="utf-8",
        )
        d = PluginDiscovery(bundled_dir=_BUNDLED, user_dir=user_dir)
        out = d.discover()
        record = next(p for p in out if p.manifest.id == "everos-memory")
        # Bundled (version 1.0.0) wins; user-dir version (9.9.9) is shadowed.
        assert record.source == Source.BUNDLED
        assert record.manifest.version == "1.0.0"


# ---------------------------------------------------------------------------
# Registry activation + factory wiring
# ---------------------------------------------------------------------------


class TestActivationAndFactory:
    def test_activate_registers_everos_backend(self) -> None:
        reg = assemble_plugin_registry(bundled_dir=_BUNDLED)
        assert "everos-memory" in reg.activated_ids()
        assert "everos" in reg.memory_backend_names()

    def test_build_returns_protocol_compliant_backend(
        self,
        tmp_path: Path,
    ) -> None:
        reg = assemble_plugin_registry(bundled_dir=_BUNDLED)
        backend = reg.build_memory_backend(
            "everos",
            config={},
            services=ServiceLocator(workspace=tmp_path),
        )
        # @runtime_checkable Protocol: isinstance returns True iff all
        # five methods are present.
        assert isinstance(backend, MemoryBackend)


# ---------------------------------------------------------------------------
# Behavior — methods do what their docstrings promise
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path):
    from raven.plugin.memory.everos.backend import _NoOpAdapter

    reg = assemble_plugin_registry(bundled_dir=_BUNDLED)
    be = reg.build_memory_backend(
        "everos",
        config={},
        services=ServiceLocator(workspace=tmp_path),
    )
    be._adapter = _NoOpAdapter()
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

    async def test_store_returns_none(self, backend) -> None:
        result = await backend.store(
            "session-1",
            [{"role": "user", "content": "hi"}],
        )
        assert result is None

    async def test_feedback_accepts_any_dict(self, backend) -> None:
        await backend.feedback({})
        await backend.feedback({"kind": "skill_usage", "ids": ["a", "b"]})


# ---------------------------------------------------------------------------
# Config passthrough
# ---------------------------------------------------------------------------


class TestConfigPassthrough:
    def test_default_constructs_http_adapter(self, tmp_path: Path) -> None:
        from raven.plugin.memory.everos.backend import _HttpEverosAdapter

        reg = assemble_plugin_registry(bundled_dir=_BUNDLED)
        backend = reg.build_memory_backend(
            "everos",
            config={},
            services=ServiceLocator(workspace=tmp_path),
        )
        assert isinstance(backend._adapter, _HttpEverosAdapter)

    def test_base_url_passed_through(self, tmp_path: Path) -> None:
        from raven.plugin.memory.everos.backend import _HttpEverosAdapter

        reg = assemble_plugin_registry(bundled_dir=_BUNDLED)
        backend = reg.build_memory_backend(
            "everos",
            config={"base_url": "http://custom:9000"},
            services=ServiceLocator(workspace=tmp_path),
        )
        assert isinstance(backend._adapter, _HttpEverosAdapter)
        assert backend._adapter._base_url == "http://custom:9000"
