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


async def test_user_md_entries_are_all_user_role(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="fact one\n§\nfact two")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.session_id == "import-hermes-user-md"
    assert [m.role for m in session.messages] == ["user", "user", "user"]
    assert [m.content for m in session.messages[1:]] == ["fact one", "fact two"]


async def test_memory_md_entries_are_assistant_after_a_user_preamble(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md="agent noted X\n§\nagent noted Y")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "memory-md"]
    session = await scanner.read(result)
    assert [m.role for m in session.messages] == ["user", "assistant", "assistant"]


async def test_bare_section_sign_inside_an_entry_is_not_split(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="see § 4.2 for details\n§\nsecond")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    bodies = [m.content for m in session.messages[1:]]
    assert bodies == ["see § 4.2 for details", "second"]


async def test_blank_entries_dropped_and_empty_file_yields_preamble_only(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\n\n§\n   \n§\nb")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert [m.content for m in session.messages[1:]] == ["a", "b"]


async def test_empty_file_yields_zero_messages(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.messages == ()


async def test_timestamps_are_monotonic_from_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\nb")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    stamps = [m.timestamp for m in session.messages]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert stamps[0] == int(result.mtime * 1000)


async def test_preamble_language_follows_content(tmp_path: Path) -> None:
    cjk = await _preamble(tmp_path / "cjk", "中文事实一\n§\n中文事实二")
    latin = await _preamble(tmp_path / "latin", "an english fact")
    assert "Hermes" in cjk and any("一" <= c <= "鿿" for c in cjk)
    assert "Hermes" in latin and not any("一" <= c <= "鿿" for c in latin)


async def test_preamble_language_looks_past_the_first_entry(tmp_path: Path) -> None:
    # A latin first entry long enough to fill is_cjk's 200-char default sample,
    # followed by CJK entries. Sampling only the head would pick the wrong
    # language for a file that is mostly Chinese.
    body = ("an english fact that runs on for a while " * 6) + "\n§\n" + "中文事实"
    preamble = await _preamble(tmp_path / "mixed", body)
    assert any("一" <= c <= "鿿" for c in preamble)


async def _preamble(root: Path, body: str) -> str:
    home = _make_home(root, user_md=body)
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    return session.messages[0].content
