"""RavenConfig: plugins / memory / skill_router sections + migration."""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config.loader import EXTENSION_KEYS
from raven.config.raven import (
    EvalEngineConfig,
    HubSourceConfig,
    MemoryConfig,
    PluginsConfig,
    RavenConfig,
    SkillForgeConfig,
    SkillForgeRouterConfig,
    load_raven_config,
)

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
        assert c.weights == {"local": 0.96, "everos": 0.9, "hub": 0.85}
        assert c.over_fetch_factor == 2
        assert c.dedup_by == "name"
        assert c.top_k == 2
        assert c.rrf_k == 10
        # Hub is the remote source (replaces the retired Mass source);
        # disabled until an endpoint is set.
        assert isinstance(c.hub, HubSourceConfig)
        assert c.hub.endpoint is None
        assert c.hub.api_key is None
        assert c.hub.timeout_s == pytest.approx(2.0)
        assert c.hub.min_safety == pytest.approx(0.7)

    def test_rrf_k_accepts_camel_case_and_rejects_zero(self) -> None:
        assert SkillForgeRouterConfig(rrfK=30).rrf_k == 30
        with pytest.raises(ValidationError):
            SkillForgeRouterConfig(rrf_k=0)

    def test_skill_forge_public_defaults(self) -> None:
        c = SkillForgeConfig()
        assert c.discovery == "pull"
        assert c.embedding_model == "default"
        assert c.embedding_url == "http://localhost:1357"
        assert c.embedding_api_key is None
        assert c.reranker_model == "default"
        assert c.reranker_url == "http://localhost:1357"
        assert c.reranker_api_key is None

        exported = c.model_dump_json()
        assert not re.search(
            r"https?://(?:10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[0-1])\.)",
            exported,
        )

    def test_root_default_factories_wired(self) -> None:
        c = RavenConfig()
        assert isinstance(c.plugins, PluginsConfig)
        assert isinstance(c.memory, MemoryConfig)
        assert isinstance(c.skill_forge.router, SkillForgeRouterConfig)
        assert isinstance(c.eval_engine, EvalEngineConfig)
        assert c.eval_engine.enabled is False


# ---------------------------------------------------------------------------
# Camel ↔ snake key acceptance
# ---------------------------------------------------------------------------


class TestKeyAliasing:
    def test_camel_keys_accepted(self) -> None:
        c = MemoryConfig.model_validate(
            {
                "userId": "alice",
                "agentId": "alpha",
                "memoryTopK": 7,
            }
        )
        assert c.user_id == "alice"
        assert c.agent_id == "alpha"
        assert c.memory_top_k == 7

    def test_snake_keys_accepted(self) -> None:
        c = MemoryConfig.model_validate(
            {
                "user_id": "bob",
                "memory_top_k": 3,
            }
        )
        assert c.user_id == "bob"
        assert c.memory_top_k == 3

    def test_router_hub_subblock_camel(self) -> None:
        c = SkillForgeRouterConfig.model_validate(
            {
                "hub": {"endpoint": "http://hub.test", "timeoutS": 5.0, "minSafety": 0.8},
            }
        )
        assert c.hub.endpoint == "http://hub.test"
        assert c.hub.timeout_s == pytest.approx(5.0)
        assert c.hub.min_safety == pytest.approx(0.8)

    def test_skill_forge_auto_install_tri_state(self) -> None:
        assert SkillForgeConfig().auto_install == "auto"
        c = SkillForgeConfig.model_validate({"autoInstall": "off"})
        assert c.auto_install == "off"
        c = SkillForgeConfig.model_validate({"auto_install": "prompt"})
        assert c.auto_install == "prompt"
        with pytest.raises(ValidationError):
            SkillForgeConfig.model_validate({"autoInstall": "sometimes"})


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


class TestSessionTitleSection:
    """The block was declared on RavenConfig but never lifted out of the file.

    Absent from EXTENSION_KEYS it is worse than ignored: the base loader forbids
    extras, so a config carrying it failed validation and took the whole config
    down -- including the only documented way to turn session naming off.
    """

    def test_session_title_is_an_extension_key_in_both_spellings(self) -> None:
        assert "sessionTitle" in EXTENSION_KEYS
        assert "session_title" in EXTENSION_KEYS

    # Both spellings of the block and both of the key: a config may use either
    # casing throughout, and covering only one leaves half the users breaking.
    @pytest.mark.parametrize("block_key", ["sessionTitle", "session_title"])
    @pytest.mark.parametrize("legacy_key", ["minInputChars", "min_input_chars"])
    def test_the_retired_gate_key_is_dropped_rather_than_rejected(
        self, tmp_path: Path, block_key: str, legacy_key: str
    ) -> None:
        """A config still carrying the old key must load, not take the process down.

        `SessionTitleConfig` forbids extras, and this model is loaded by the
        gateway, the TUI and doctor -- none of which swallow a ValidationError
        the way `turn.send` does. Without the migration a stale key in a
        hand-edited config stops those from starting at all, which is a far
        worse outcome than the missing title it is nominally about.
        """
        path = _write_config(tmp_path, {block_key: {legacy_key: 8, "enabled": False}})

        cfg = load_raven_config(path)

        # Dropped, not carried across: the old key counted code points and the
        # new one counts display columns, so 8 of one is not 8 of the other and
        # reusing the number would silently re-tighten the gate.
        assert cfg.session_title.min_input_width == 6
        # The rest of the block still applies -- this is a migration, not a
        # reason to discard what the user actually configured.
        assert cfg.session_title.enabled is False

    def test_camel_case_block_reaches_the_model(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            {"sessionTitle": {"enabled": False, "budget": 40, "timeoutSeconds": 2.5, "model": "cheap/tier"}},
        )

        st = load_raven_config(path).session_title

        # Every field, not just one: a key that reaches the model but drops its
        # contents would pass a single-field check.
        assert st.enabled is False
        assert st.budget == 40
        assert st.timeout_seconds == 2.5
        assert st.model == "cheap/tier"

    def test_snake_case_block_reaches_the_model(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"session_title": {"enabled": False}})

        assert load_raven_config(path).session_title.enabled is False

    def test_a_config_carrying_the_block_still_loads_as_a_base_config(self, tmp_path: Path) -> None:
        """The failure that mattered: the block used to brick `load_config`."""
        from raven.config import load_config

        path = _write_config(tmp_path, {"sessionTitle": {"enabled": False}})

        base = load_config(path)

        assert base is not None

    def test_an_absent_block_leaves_the_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {})

        st = load_raven_config(path).session_title

        assert st.enabled is True
        assert st.budget == 24
        assert st.timeout_seconds == 8.0
        assert st.min_input_width == 6
        assert st.model is None


class TestSubagentDagSection:
    """Same failure class as TestSessionTitleSection above: a block declared on
    RavenConfig but left out of EXTENSION_KEYS is worse than ignored -- the
    base loader forbids extras, so a config file carrying it fails validation
    and takes the whole config down with it.
    """

    def test_subagent_dag_is_an_extension_key_in_both_spellings(self) -> None:
        assert "subagentDag" in EXTENSION_KEYS
        assert "subagent_dag" in EXTENSION_KEYS

    def test_camel_case_block_reaches_the_model(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            {"subagentDag": {"verdictEnabled": False, "verdictModel": "cheap/tier", "maxContinuations": 5}},
        )

        sd = load_raven_config(path).subagent_dag

        assert sd.verdict_enabled is False
        assert sd.verdict_model == "cheap/tier"
        assert sd.max_continuations == 5

    def test_snake_case_block_reaches_the_model(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"subagent_dag": {"verdict_enabled": False}})

        assert load_raven_config(path).subagent_dag.verdict_enabled is False

    def test_a_config_carrying_the_block_still_loads_as_a_base_config(self, tmp_path: Path) -> None:
        """The failure that would have mattered: the block would have bricked load_config."""
        from raven.config import load_config

        path = _write_config(tmp_path, {"subagentDag": {"verdictEnabled": False}})

        base = load_config(path)

        assert base is not None

    def test_an_absent_block_leaves_the_defaults(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {})

        sd = load_raven_config(path).subagent_dag

        assert sd.verdict_enabled is True
        assert sd.verdict_model is None
        assert sd.verdict_timeout_seconds == 30.0
        assert sd.evidence_budget_chars == 8000
        assert sd.adjudication_timeout_seconds == 600.0
        assert sd.max_continuations == 2


class TestLoaderIntegration:
    def test_loads_new_sections_from_file(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            {
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
            },
        )
        cfg = load_raven_config(path)
        assert cfg.plugins.disabled == ["mem0-memory"]
        # ``mode`` never had a reader and is dropped on load; ``root`` / ``owned``
        # are recorded in its place so every caller reads one recorded decision
        # instead of re-deriving it.
        everos_slice = cfg.plugins.config["everos-memory"]
        assert "mode" not in everos_slice
        assert everos_slice["root"]
        assert isinstance(everos_slice["owned"], bool)
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
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_config(
            tmp_path,
            {
                "plugins": None,
                "memory": None,
                "skillForge": None,
            },
        )
        cfg = load_raven_config(path)
        # ``None`` is treated as "use default" rather than rejected.
        assert isinstance(cfg.plugins, PluginsConfig)
        assert isinstance(cfg.memory, MemoryConfig)
        assert isinstance(cfg.skill_forge.router, SkillForgeRouterConfig)


# ---------------------------------------------------------------------------
# Version floor 4 -- the legacy leaves leave the file, not the schema
# ---------------------------------------------------------------------------


class TestLegacyLeavesMigration:
    def test_legacy_leaves_load_and_are_told_once(self, tmp_path: Path) -> None:
        from raven.config.loader import drain_migration_notices

        path = _write_config(
            tmp_path,
            {
                "skillForge": {"skillsDir": "/srv/skills", "massLibraryDb": "/tmp/old.db"},
                "context": {"engine": "curator"},
            },
        )
        drain_migration_notices()
        cfg = load_raven_config(path)
        assert [d.path for d in cfg.skill_forge.local_dirs] == ["/srv/skills"]
        assert not hasattr(cfg.skill_forge, "mass_library_db")
        assert not hasattr(cfg.context, "engine")
        notices = drain_migration_notices()
        assert any("skillForge.skillsDir" in n for n in notices)
        assert any("skillForge.massLibraryDb" in n for n in notices)
        assert any("context.engine" in n for n in notices)

    def test_an_explicit_local_dirs_list_wins_over_the_legacy_dir(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            {"skill_forge": {"skills_dir": "/srv/old", "local_dirs": [{"path": "/srv/new"}]}},
        )
        cfg = load_raven_config(path)
        assert [d.path for d in cfg.skill_forge.local_dirs] == ["/srv/new"]

    def test_no_legacy_field_no_warning(self, tmp_path: Path) -> None:
        path = _write_config(
            tmp_path,
            {
                "skill_router": {"mass": {"endpoint": "http://m"}},
            },
        )
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
        with pytest.raises(ValidationError):
            # ``extra='forbid'`` — typo catches at startup
            PluginsConfig.model_validate(
                {
                    "disabled": [],
                    "config": {},
                    "unknown_field": True,
                }
            )

    def test_unknown_field_in_memory_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryConfig.model_validate({"backend": "x", "typo": 1})


def test_subagent_questions_defaults_on():
    cfg = RavenConfig()
    assert cfg.subagent_questions.autofill_enabled is True
    assert cfg.subagent_questions.autofill_timeout_seconds == 20.0


def test_subagent_questions_reads_camel_case(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"subagentQuestions": {"autofillEnabled": False}}), encoding="utf-8")
    cfg = load_raven_config(path)
    assert cfg.subagent_questions.autofill_enabled is False
    # The other field keeps its default rather than being reset by a partial block.
    assert cfg.subagent_questions.autofill_timeout_seconds == 20.0
