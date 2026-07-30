"""HermesScanner -- home resolution and memory-file discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.importer.scanners.hermes import HermesScanner, resolve_hermes_home
from raven.importer.types import Platform, SourceKind


def _make_home(
    root: Path,
    *,
    user_md: str | None = "a\n§\nb",
    memory_md: str | None = "m",
) -> Path:
    home = root / ".hermes"
    mem = home / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    if user_md is not None:
        (mem / "USER.md").write_text(user_md, encoding="utf-8")
        (mem / "USER.md.lock").write_text("", encoding="utf-8")
    if memory_md is not None:
        (mem / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    return home


def test_env_var_wins_over_platform_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert resolve_hermes_home() == tmp_path / "custom"


def test_blank_env_var_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "   ")
    assert resolve_hermes_home() != tmp_path / "   "


async def test_missing_home_yields_nothing(tmp_path: Path) -> None:
    results = await HermesScanner(hermes_home=tmp_path / "nope").scan()
    assert results == []


async def test_both_memory_files_discovered(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home).scan()
    keys = {r.source_key for r in results}
    assert keys == {"user-md", "memory-md"}
    assert all(r.platform is Platform.HERMES for r in results)
    assert all(r.kind is SourceKind.MEMORY_FILE for r in results)


async def test_lock_sibling_is_not_a_source(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home).scan()
    assert not any(".lock" in str(p) for r in results for p in r.file_paths)


async def test_missing_one_file_yields_only_the_other(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md=None)
    results = await HermesScanner(hermes_home=home).scan()
    assert [r.source_key for r in results] == ["user-md"]


async def test_scan_result_carries_size_and_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="x" * 40)
    (result,) = [r for r in await HermesScanner(hermes_home=home).scan() if r.source_key == "user-md"]
    assert result.estimated_size == 40
    assert result.mtime > 0
