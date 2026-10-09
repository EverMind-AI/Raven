"""The cloud plugin is found, activated and built the way every plugin is -- and
stands without the engine packages it must never import."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from raven.contracts.memory import MemoryBackend
from raven.plugins import ManifestOrigin, PluginDiscovery, ServiceLocator, assemble_plugin_registry

pytestmark = pytest.mark.everos_cloud

_GROUP = "raven.plugins"


class TestPackageSurface:
    def test_manifest_shipped_with_package(self) -> None:
        from importlib.resources import files

        manifest = files("raven_everos_cloud").joinpath("raven-plugin.toml")
        assert manifest.is_file()
        import tomllib

        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        assert data["plugin"]["id"] == "everos-cloud-memory" and data["plugin"]["bundled"] is False
        assert [c["name"] for c in data["plugin"]["contributes"]["memory_backends"]] == ["everos-cloud"]
        assert [c["name"] for c in data["plugin"]["contributes"]["onboard"]] == ["everos-cloud"]
        # Identity is the host's (C4): the slice declares a key and an address, nothing else.
        assert set(data["plugin"]["config_schema"]) == {"api_key", "base_url"}
        # Only the key may be written from the settings page; the endpoint never (H7).
        assert data["plugin"]["config_schema"]["api_key"].get("settable") is True
        assert "settable" not in data["plugin"]["config_schema"]["base_url"]


class TestEntryPointDiscovery:
    def test_discovered_via_entry_points(self) -> None:
        out = PluginDiscovery(entry_points_group=_GROUP).discover()
        record = next(p for p in out if p.manifest.id == "everos-cloud-memory")
        assert record.source == ManifestOrigin.ENTRY_POINTS and record.location is None


class TestActivationAndFactory:
    def test_activate_registers_backend_and_screen(self) -> None:
        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        assert "everos-cloud-memory" in reg.activated_ids()
        assert "everos-cloud" in reg.memory_backend_names()
        assert "everos-cloud" in reg.onboard_names()
        assert reg.onboard_plugin_id("everos-cloud") == "everos-cloud-memory"

    def test_build_returns_protocol_compliant_backend(self, tmp_path: Path) -> None:
        reg = assemble_plugin_registry(entry_points_group=_GROUP)
        backend = reg.build_memory_backend(
            "everos-cloud",
            config={},
            services=ServiceLocator(workspace=tmp_path, user_id="default", agent_id="default"),
        )
        assert isinstance(backend, MemoryBackend)


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)


def test_package_imports_with_everos_and_raven_everos_blocked() -> None:
    """C3: the plugin stands without the engine or the local plugin installed."""
    out = _python(
        "import sys\n"
        "sys.modules['everos'] = None\n"
        "sys.modules['raven_everos'] = None\n"
        "import pkgutil, importlib, raven_everos_cloud\n"
        "for m in pkgutil.walk_packages(raven_everos_cloud.__path__, 'raven_everos_cloud.'):\n"
        "    importlib.import_module(m.name)\n"
        "print('ok')\n"
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_init_is_import_cheap() -> None:
    out = _python(
        "import sys, raven_everos_cloud\n"
        "assert 'httpx' not in sys.modules, 'httpx imported by __init__'\n"
        "assert 'raven_everos_cloud.backend' not in sys.modules, 'backend imported by __init__'\n"
        "print('ok')\n"
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
