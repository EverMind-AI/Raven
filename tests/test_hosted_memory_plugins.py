"""The three hosted-memory plugins join the host through the plugin seam alone.

Discovery finds each by entry point, the registry builds whichever backend
``memory.backend`` names, and each backend reads only the slice keyed by its
own name -- so the three keys never share a slot.
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.resources as resources
import logging
from pathlib import Path

import pytest

from raven.config.raven import MemoryConfig, PluginsConfig, RavenConfig
from raven.config.schema import Config
from raven.core.plugin_stack import build_plugin_registry, maybe_build_memory_backend
from raven.plugins import PluginContext, ServiceLocator
from raven_everos.backend import EverosBackend
from raven_mem0.backend import Mem0Backend
from raven_memos.backend import MemosBackend
from raven_zep.backend import ZepBackend

BACKENDS = {"mem0": Mem0Backend, "zep": ZepBackend, "memos": MemosBackend}


def _config(backend: str | None, slices: dict | None = None) -> RavenConfig:
    return RavenConfig(
        memory=MemoryConfig(backend=backend),
        plugins=PluginsConfig(config=dict(slices or {})),
        base=Config(),
    )


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEM0_API_KEY", "ZEP_API_KEY", "MEMOS_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_each_plugin_contributes_its_backend_and_its_screen() -> None:
    registry = build_plugin_registry(_config("mem0"))
    assert set(BACKENDS) <= set(registry.memory_backend_names())
    assert set(BACKENDS) <= set(registry.onboard_names())
    assert {n: registry.onboard_plugin_id(n) for n in BACKENDS} == {n: f"{n}-memory" for n in BACKENDS}


@pytest.mark.parametrize("name", sorted(BACKENDS))
def test_installed_by_entry_point_with_its_manifest_as_package_data(name: str) -> None:
    names = {ep.name: ep.value for ep in md.entry_points(group="raven.plugins")}
    assert names[f"{name}-memory"] == f"raven_{name}"
    assert (resources.files(f"raven_{name}") / "raven-plugin.toml").is_file()


@pytest.mark.parametrize("name", sorted(BACKENDS))
def test_each_backend_reads_only_its_own_slice(tmp_path: Path, name: str) -> None:
    slices = {n: {"api_key": f"key-for-{n}-12345678", "base_url": f"https://{n}.test"} for n in BACKENDS}
    backend = maybe_build_memory_backend(tmp_path, _config(name, slices))
    assert isinstance(backend, BACKENDS[name])
    assert backend._api_key == f"key-for-{name}-12345678"
    assert backend._base_url == f"https://{name}.test"


def test_a_plugin_id_slice_reaches_only_its_own_backend(tmp_path: Path) -> None:
    """The host prefers a slice under the plugin id; with one plugin per
    backend that slice can only ever reach the backend it belongs to."""
    slices = {"zep-memory": {"api_key": "zep-by-plugin-id-12345"}, "mem0": {"api_key": "mem0-by-name-12345"}}
    assert maybe_build_memory_backend(tmp_path, _config("zep", slices))._api_key == "zep-by-plugin-id-12345"
    assert maybe_build_memory_backend(tmp_path, _config("mem0", slices))._api_key == "mem0-by-name-12345"
    assert maybe_build_memory_backend(tmp_path, _config("memos", slices))._api_key == ""


def test_missing_slice_still_builds_and_reports_missing(tmp_path: Path) -> None:
    import asyncio

    backend = maybe_build_memory_backend(tmp_path, _config("mem0"))
    assert isinstance(backend, Mem0Backend)
    health = asyncio.run(backend.health())
    assert health.ready is False and health.checks[0].status == "missing"


def test_identity_in_the_slice_passes_the_door_with_a_warning_and_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        backend = maybe_build_memory_backend(
            tmp_path,
            _config("mem0", {"mem0": {"api_key": "k-12345678", "user_id": "from-slice"}}),
        )
    assert isinstance(backend, Mem0Backend)
    assert backend._user_id == "default"
    assert "user_id" in caplog.text and "config_schema" in caplog.text


def test_wrong_type_in_the_slice_does_not_build(tmp_path: Path) -> None:
    assert maybe_build_memory_backend(tmp_path, _config("mem0", {"mem0": {"api_key": 12345}})) is None


def test_everos_is_untouched_by_the_second_plugin(tmp_path: Path) -> None:
    backend = maybe_build_memory_backend(tmp_path, _config("everos"))
    assert isinstance(backend, EverosBackend)


def test_factories_are_sync_and_read_only(tmp_path: Path) -> None:
    """``raven doctor`` constructs without starting: no request, no file."""
    from raven_mem0 import backend as mem0
    from raven_memos import backend as memos
    from raven_zep import backend as zep

    ctx = PluginContext(
        config={"api_key": "k-12345678"},
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"),
    )
    for module in (mem0, zep, memos):
        backend = module.make_backend(ctx)
        assert backend._client is None
    assert list(tmp_path.iterdir()) == []
