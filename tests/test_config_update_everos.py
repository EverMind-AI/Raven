"""Full coverage for ``raven.config.update_everos``.

The onboard memory step writes EverOS model settings to
``~/.everos/raven/config.toml`` through these ops. EverOS reads that file back
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
def everos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the ops library at a throwaway config path."""
    cfg = tmp_path / ".everos" / "config.toml"
    monkeypatch.setattr(ue, "_EVEROS_CONFIG", cfg)
    return cfg


def _read(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# get_everos_config_path / load_everos_config
# ---------------------------------------------------------------------------


def test_config_path_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ue, "_EVEROS_CONFIG", Path("~/.everos/config.toml"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ue.get_everos_config_path() == tmp_path / ".everos" / "config.toml"


def test_load_absent_returns_empty(everos_home: Path) -> None:
    assert ue.load_everos_config() == {}


# ---------------------------------------------------------------------------
# configure_everos_env
# ---------------------------------------------------------------------------


def test_configure_everos_env_points_at_raven_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ue, "_EVEROS_BASE", tmp_path / ".everos" / "raven")
    monkeypatch.delenv("EVEROS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("EVEROS_MEMORY__ROOT", raising=False)

    ue.configure_everos_env()

    base = tmp_path / ".everos" / "raven"
    import os

    assert os.environ["EVEROS_CONFIG_FILE"] == str(base / "config.toml")
    assert os.environ["EVEROS_MEMORY__ROOT"] == str(base)


def test_configure_everos_env_respects_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An operator-set EVEROS_* env must win (setdefault, not overwrite).
    monkeypatch.setattr(ue, "_EVEROS_BASE", tmp_path / ".everos" / "raven")
    monkeypatch.setenv("EVEROS_CONFIG_FILE", "/custom/everos.toml")
    monkeypatch.setenv("EVEROS_MEMORY__ROOT", "/custom/data")

    ue.configure_everos_env()

    import os

    assert os.environ["EVEROS_CONFIG_FILE"] == "/custom/everos.toml"
    assert os.environ["EVEROS_MEMORY__ROOT"] == "/custom/data"


def test_default_config_path_under_raven_home() -> None:
    # The production default lives under ~/.everos/raven, not bare ~/.everos.
    assert ue.get_everos_config_path() == (
        Path("~/.everos/raven/config.toml").expanduser()
    )


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
    leftovers = [p.name for p in everos_home.parent.iterdir() if p.name != "config.toml"]
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
