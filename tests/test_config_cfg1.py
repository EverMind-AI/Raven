"""CFG-1 — RavenConfig: plugins / memory / skill_router sections + migration."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from raven.config.raven import (
    RavenConfig,
    HubSourceConfig,
    MemoryConfig,
    PluginsConfig,
    SkillForgeRouterConfig,
    load_raven_config,
)
from raven.config.loader import EXTENSION_KEYS


# ---------------------------------------------------------------------------
# Default-construction sanity
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_plugins_defaults(self) -> None:
        c = PluginsConfig()
        assert c.disabled == []
        assert c.config == {}

    def test_memory_defaults(self) -> None:
        c = MemoryConfig()
        assert c.backend == "everos"
        assert c.user_id == "default"
        assert c.agent_id == "default"
        assert c.memory_top_k == 5

    def test_memory_backend_none_disables(self) -> None:
        c = MemoryConfig(backend=None)
        assert c.backend is None

    def test_skill_router_defaults(self) -> None:
        c = SkillForgeRouterConfig()
        assert c.enabled is True
        assert c.weights == {"local": 1.0, "everos": 0.9, "hub": 0.85}
        assert c.over_fetch_factor == 2
        assert c.dedup_by == "name"
        assert c.top_k == 5
        # Hub is the remote source (replaces the retired Mass source);
        # disabled until an endpoint is set.
        assert isinstance(c.hub, HubSourceConfig)
        assert c.hub.endpoint is None
        assert c.hub.api_key is None
        assert c.hub.timeout_s == pytest.approx(2.0)
        assert c.hub.min_safety == pytest.approx(0.7)

    def test_root_default_factories_wired(self) -> None:
        c = RavenConfig()
        assert isinstance(c.plugins, PluginsConfig)
        assert isinstance(c.memory, MemoryConfig)
        assert isinstance(c.skill_forge.router, SkillForgeRouterConfig)


# ---------------------------------------------------------------------------
# Camel ↔ snake key acceptance
# ---------------------------------------------------------------------------


class TestKeyAliasing:
    def test_camel_keys_accepted(self) -> None:
        c = MemoryConfig.model_validate({
            "userId": "alice",
            "agentId": "alpha",
            "memoryTopK": 7,
        })
        assert c.user_id == "alice"
        assert c.agent_id == "alpha"
        assert c.memory_top_k == 7

    def test_snake_keys_accepted(self) -> None:
        c = MemoryConfig.model_validate({
            "user_id": "bob",
            "memory_top_k": 3,
        })
        assert c.user_id == "bob"
        assert c.memory_top_k == 3

    def test_router_hub_subblock_camel(self) -> None:
        c = SkillForgeRouterConfig.model_validate({
            "hub": {"endpoint": "http://hub.test", "timeoutS": 5.0,
                    "minSafety": 0.8},
        })
        assert c.hub.endpoint == "http://hub.test"
        assert c.hub.timeout_s == pytest.approx(5.0)
        assert c.hub.min_safety == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# EXTENSION_KEYS includes the new sections
# ---------------------------------------------------------------------------


class TestExtensionKeys:
    def test_plugins_listed(self) -> None:
        assert "plugins" in EXTENSION_KEYS

    def test_memory_listed(self) -> None:
        assert "memory" in EXTENSION_KEYS

    def test_skill_router_nested_under_skill_forge(self) -> None:
        # The router is no longer a top-level extension block — it nests
        # under skillForge (skillForge.router). Legacy top-level skillRouter
        # is migrated into skillForge.router by _migrate_config.
        assert "skillRouter" not in EXTENSION_KEYS
        assert "skill_router" not in EXTENSION_KEYS
        assert "skillForge" in EXTENSION_KEYS


# ---------------------------------------------------------------------------
# Loader integration — JSON file → RavenConfig roundtrip
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: dict) -> Path:
    """Write a config file + return its path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


class TestLoaderIntegration:
    def test_loads_new_sections_from_file(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {
            "plugins": {
                "disabled": ["mem0-memory"],
                "config": {"everos-memory": {"mode": "embedded"}},
            },
            "memory": {
                "backend": "everos",
                "userId": "alice",
                "memoryTopK": 10,
            },
            # Legacy top-level skillRouter is migrated into skillForge.router;
            # the retired ``mass`` sub-block is dropped during migration.
            "skillRouter": {
                "weights": {"local": 1.5, "everos": 1.0, "hub": 0.7},
                "topK": 8,
                "mass": {"endpoint": "http://mass.internal:9001"},
                "hub": {"endpoint": "http://hub.internal:9001"},
            },
        })
        cfg = load_raven_config(path)
        assert cfg.plugins.disabled == ["mem0-memory"]
        assert cfg.plugins.config["everos-memory"]["mode"] == "embedded"
        assert cfg.memory.backend == "everos"
        assert cfg.memory.user_id == "alice"
        assert cfg.memory.memory_top_k == 10
        assert cfg.skill_forge.router.weights["local"] == pytest.approx(1.5)
        assert cfg.skill_forge.router.top_k == 8
        assert cfg.skill_forge.router.hub.endpoint == "http://hub.internal:9001"

    def test_missing_sections_use_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {})
        cfg = load_raven_config(path)
        # All three default-construct without raising.
        assert cfg.plugins.disabled == []
        assert cfg.memory.backend == "everos"
        assert cfg.skill_forge.router.enabled is True

    def test_explicit_null_section_uses_defaults(
        self, tmp_path: Path,
    ) -> None:
        path = _write_config(tmp_path, {
            "plugins": None,
            "memory": None,
            "skillForge": None,
        })
        cfg = load_raven_config(path)
        # ``None`` is treated as "use default" rather than rejected.
        assert isinstance(cfg.plugins, PluginsConfig)
        assert isinstance(cfg.memory, MemoryConfig)
        assert isinstance(cfg.skill_forge.router, SkillForgeRouterConfig)


# ---------------------------------------------------------------------------
# Deprecation surface — skill_forge.mass_library_db
# ---------------------------------------------------------------------------


class TestMassLibraryDbDeprecation:
    def test_legacy_only_emits_deprecation_warning(
        self, tmp_path: Path,
    ) -> None:
        path = _write_config(tmp_path, {
            "skill_forge": {"mass_library_db": "/tmp/old.db"},
        })
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_raven_config(path)
        deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deps, "expected at least one DeprecationWarning"
        assert "mass_library_db" in str(deps[0].message)
        assert "skillForge.router.hub.endpoint" in str(deps[0].message)

    def test_both_old_and_new_no_deprecation_warning(
        self, tmp_path: Path,
    ) -> None:
        """When the user has set the new Hub endpoint, the legacy field
        becomes a no-op — info log only, no warning."""
        path = _write_config(tmp_path, {
            "skill_forge": {
                "mass_library_db": "/tmp/old.db",
                "router": {"hub": {"endpoint": "http://hub.test"}},
            },
        })
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_raven_config(path)
        deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deps == []

    def test_no_legacy_field_no_warning(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {
            "skill_router": {"mass": {"endpoint": "http://m"}},
        })
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_raven_config(path)
        deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deps == []


# ---------------------------------------------------------------------------
# Frozen behavior of _Base + extra='forbid'
# ---------------------------------------------------------------------------


class TestStrictness:
    def test_unknown_field_in_plugins_rejected(self) -> None:
        with pytest.raises(Exception):
            # ``extra='forbid'`` — typo catches at startup
            PluginsConfig.model_validate({
                "disabled": [], "config": {}, "unknown_field": True,
            })

    def test_unknown_field_in_memory_rejected(self) -> None:
        with pytest.raises(Exception):
            MemoryConfig.model_validate({"backend": "x", "typo": 1})
