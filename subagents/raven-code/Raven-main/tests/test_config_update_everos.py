"""Full coverage for ``raven.config.update_everos``.

The onboard memory step writes EverOS model settings to
``<data dir>/everos/everos.toml`` through these ops. EverOS reads that file back
via its own pydantic-settings loader, so a malformed / mislocated write silently
breaks memory at runtime — hence the thorough round-trip + section-preservation
coverage here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import raven.config.update_everos as ue


@pytest.fixture
def everos_home() -> Path:
    """The derived config path. The autouse ``_isolated_data_dir`` fixture
    already redirects the data dir this hangs off, so no patching is needed
    -- and nothing can land in the developer's real home."""
    return ue.get_everos_config_path()


def _read(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# get_everos_config_path / load_everos_config
# ---------------------------------------------------------------------------


def test_config_path_sits_under_the_instance_data_dir() -> None:
    from raven.config.paths import get_data_dir

    assert ue.get_everos_config_path() == get_data_dir() / "everos" / "everos.toml"


def test_load_absent_returns_empty(everos_home: Path) -> None:
    assert ue.load_everos_config() == {}


# ---------------------------------------------------------------------------
# configure_everos_env
# ---------------------------------------------------------------------------


def test_configure_everos_env_points_at_the_instance_home(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.delenv("EVEROS_ROOT", raising=False)

    ue.configure_everos_env()

    assert os.environ["EVEROS_ROOT"] == str(ue.get_everos_home())


def test_configure_everos_env_respects_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator-set EVEROS_ROOT must win (setdefault, not overwrite).
    import os

    monkeypatch.setenv("EVEROS_ROOT", "/custom/root")

    ue.configure_everos_env()

    assert os.environ["EVEROS_ROOT"] == "/custom/root"


def test_configure_everos_env_reports_a_conflicting_override(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Which root is in force decides which store a run reads and writes, so a
    pre-set value that disagrees is announced rather than silently obeyed."""
    monkeypatch.setenv("EVEROS_ROOT", "/custom/root")

    with caplog.at_level("WARNING"):
        ue.configure_everos_env()

    assert "/custom/root" in caplog.text


def test_home_is_not_bucketed_per_workspace() -> None:
    """One root, one server process. Workspaces are separated by the scope's
    project_id instead -- see raven.plugin.memory.everos.scope."""
    from raven.config.paths import get_data_dir

    assert ue.get_everos_home() == get_data_dir() / "everos"


def test_load_round_trips_written_content(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "m", "api_key": "k", "base_url": "u"})
    assert ue.load_everos_config()["llm"] == {"model": "m", "api_key": "k", "base_url": "u"}


# ---------------------------------------------------------------------------
# set_everos_section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", ue.WRITABLE_SECTIONS)
def test_set_each_writable_section(everos_home: Path, section: str) -> None:
    ue.set_everos_section(section, {"model": "m"})
    assert _read(everos_home)[section] == {"model": "m"}


def test_set_creates_file_and_parent_dir(everos_home: Path) -> None:
    assert not everos_home.parent.exists()
    ue.set_everos_section("llm", {"model": "gpt-4o-mini", "api_key": "k", "base_url": "u"})
    assert everos_home.exists()
    assert _read(everos_home)["llm"] == {"model": "gpt-4o-mini", "api_key": "k", "base_url": "u"}


def test_set_drops_none_values(everos_home: Path) -> None:
    ue.set_everos_section("rerank", {"provider": "vllm", "model": "m", "api_key": None})
    assert _read(everos_home)["rerank"] == {"provider": "vllm", "model": "m"}


def test_set_all_none_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": None, "api_key": None})
    assert _read(everos_home)["llm"] == {}


def test_set_empty_fields_writes_empty_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {})
    assert _read(everos_home)["llm"] == {}


def test_set_preserves_other_writable_sections(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a"})
    ue.set_everos_section("embedding", {"model": "b"})
    data = _read(everos_home)
    assert data["llm"] == {"model": "a"}
    assert data["embedding"] == {"model": "b"}


def test_set_preserves_non_writable_sections(everos_home: Path) -> None:
    # EverOS ships [memory]/[sqlite]/... — a model-section write must not clobber them.
    everos_home.parent.mkdir(parents=True)
    everos_home.write_text(
        '[memory]\nroot = "~/.everos"\n\n[sqlite]\njournal_mode = "WAL"\n',
        encoding="utf-8",
    )
    ue.set_everos_section("llm", {"model": "a"})
    data = _read(everos_home)
    assert data["memory"] == {"root": "~/.everos"}
    assert data["sqlite"] == {"journal_mode": "WAL"}
    assert data["llm"] == {"model": "a"}


def test_set_merges_into_existing_section(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a", "api_key": "old"})
    ue.set_everos_section("llm", {"api_key": "new"})
    assert _read(everos_home)["llm"] == {"model": "a", "api_key": "new"}


def test_set_preserves_mixed_value_types(everos_home: Path) -> None:
    # rerank carries ints (timeout_seconds/batch_size) alongside strings.
    ue.set_everos_section(
        "rerank",
        {"provider": "deepinfra", "model": "m", "base_url": "u", "timeout_seconds": 30, "batch_size": 16},
    )
    got = _read(everos_home)["rerank"]
    assert got == {"provider": "deepinfra", "model": "m", "base_url": "u", "timeout_seconds": 30, "batch_size": 16}
    assert isinstance(got["timeout_seconds"], int)


def test_set_unknown_section_rejected(everos_home: Path) -> None:
    for bad in ("sqlite", "memory", "api", "lancedb", ""):
        with pytest.raises(KeyError):
            ue.set_everos_section(bad, {"x": 1})


def test_set_leaves_no_tmp_file(everos_home: Path) -> None:
    # Atomic write goes through a sibling .tmp + os.replace; nothing should linger.
    ue.set_everos_section("llm", {"model": "a"})
    leftovers = [p.name for p in everos_home.parent.iterdir() if p.name != "everos.toml"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# clear_everos_section
# ---------------------------------------------------------------------------


def test_clear_removes_section_keeps_siblings(everos_home: Path) -> None:
    ue.set_everos_section("multimodal", {"model": "m"})
    ue.set_everos_section("llm", {"model": "a"})
    ue.clear_everos_section("multimodal")
    data = _read(everos_home)
    assert "multimodal" not in data
    assert data["llm"] == {"model": "a"}


def test_clear_absent_section_is_noop_no_file(everos_home: Path) -> None:
    # No file yet → clearing must not create one.
    ue.clear_everos_section("rerank")
    assert not everos_home.exists()


def test_clear_absent_section_with_existing_file_preserves_it(everos_home: Path) -> None:
    ue.set_everos_section("llm", {"model": "a"})
    ue.clear_everos_section("rerank")  # rerank not present
    assert _read(everos_home)["llm"] == {"model": "a"}


def test_clear_unknown_section_rejected(everos_home: Path) -> None:
    with pytest.raises(KeyError):
        ue.clear_everos_section("sqlite")


def test_resolving_the_home_does_not_create_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off has to mean no directory, so merely asking for the path must not be
    what creates it -- the tool factory used to seed the home this way and made
    a disabled backend leave files behind."""
    home = ue.get_everos_home()
    assert not home.exists()
    ue.get_everos_config_path()
    assert not home.exists()


def test_importing_the_cli_does_not_resolve_everos_settings() -> None:
    """``configure_everos_env`` only works while EverOS's settings are still
    unresolved: ``load_settings`` is cached, so the first call freezes the root
    for the process. Every everos import in raven is therefore deferred into a
    function body, and a module-level one would silently pin the root to the
    default home. This guards that.
    """
    import subprocess
    import sys

    probe = "import sys, raven.cli.commands; print('everos.config.settings' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout


# ---------------------------------------------------------------------------
# everos_models_configured
# ---------------------------------------------------------------------------


class TestModelsConfigured:
    @pytest.fixture(autouse=True)
    def _no_env_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVEROS_LLM__API_KEY", raising=False)
        monkeypatch.delenv("EVEROS_EMBEDDING__API_KEY", raising=False)

    def test_absent_config_and_env_is_unconfigured(self) -> None:
        assert ue.everos_models_configured() is False

    def test_both_toml_keys_configure(self, everos_home: Path) -> None:
        everos_home.parent.mkdir(parents=True, exist_ok=True)
        everos_home.write_text(
            '[llm]\napi_key = "k1"\n\n[embedding]\napi_key = "k2"\n'
        )
        assert ue.everos_models_configured() is True

    def test_llm_key_alone_is_not_enough(self, everos_home: Path) -> None:
        """The embedding provider is built at server startup too, so an
        LLM-only config still refuses to boot (measured on 1.1.3)."""
        everos_home.parent.mkdir(parents=True, exist_ok=True)
        everos_home.write_text('[llm]\napi_key = "k1"\n')
        assert ue.everos_models_configured() is False

    def test_env_fills_a_section_the_toml_leaves_empty(
        self, everos_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        everos_home.parent.mkdir(parents=True, exist_ok=True)
        everos_home.write_text('[llm]\napi_key = "k1"\n\n[embedding]\napi_key = ""\n')
        monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "k2")
        assert ue.everos_models_configured() is True

    def test_whitespace_keys_do_not_count(self, everos_home: Path) -> None:
        everos_home.parent.mkdir(parents=True, exist_ok=True)
        everos_home.write_text('[llm]\napi_key = "  "\n\n[embedding]\napi_key = " "\n')
        assert ue.everos_models_configured() is False
