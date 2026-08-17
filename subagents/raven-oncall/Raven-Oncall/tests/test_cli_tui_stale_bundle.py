"""Starting the TUI says so when the bundle is older than the sources.

The bundle is what runs; the sources are what the tests and the type checker
read, and nothing connected the two. Measured 2026-08-07: a fix for a prompt that
outlived its own timeout was written, tested, type-checked, committed and pushed,
and the TUI went on running a bundle built three weeks earlier. The round that
shipped it behaved as though the fix did not exist, and the pre-registration
recorded it as active -- both halves were true and neither implied the other.

A warning rather than a refusal: a stale bundle still runs, and blocking startup
over it would be a worse failure than the one being reported.
"""

from __future__ import annotations

import os
from pathlib import Path

from raven.cli.tui_commands import stale_bundle_sources


def _tree(tmp_path: Path, *, built_at: float, src_at: float) -> tuple[Path, Path]:
    ui = tmp_path / "ui-tui"
    (ui / "dist").mkdir(parents=True)
    (ui / "src" / "app").mkdir(parents=True)
    entry = ui / "dist" / "entry.js"
    entry.write_text("bundle", encoding="utf-8")
    source = ui / "src" / "app" / "handler.ts"
    source.write_text("source", encoding="utf-8")
    os.utime(entry, (built_at, built_at))
    os.utime(source, (src_at, src_at))
    return ui, entry


def test_a_source_newer_than_the_bundle_is_reported(tmp_path: Path, monkeypatch) -> None:
    ui, entry = _tree(tmp_path, built_at=1000, src_at=2000)
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", ui)

    assert stale_bundle_sources(entry) == ["src/app/handler.ts"]


def test_a_fresh_bundle_reports_nothing(tmp_path: Path, monkeypatch) -> None:
    ui, entry = _tree(tmp_path, built_at=3000, src_at=2000)
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", ui)

    assert stale_bundle_sources(entry) == []


def test_a_packaged_install_has_no_sources_and_reports_nothing(tmp_path: Path, monkeypatch) -> None:
    """In a wheel the bundle is the only copy there is, so staleness cannot
    happen and a warning would be noise on every start."""
    ui = tmp_path / "ui-tui"
    (ui / "dist").mkdir(parents=True)
    entry = ui / "dist" / "entry.js"
    entry.write_text("bundle", encoding="utf-8")
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", ui)

    assert stale_bundle_sources(entry) == []


def test_the_newest_sources_come_first_and_the_list_is_capped(tmp_path: Path, monkeypatch) -> None:
    """A rebase can leave hundreds newer than the bundle; the point is to name a
    few so the reader can tell which change is missing."""
    ui, entry = _tree(tmp_path, built_at=1000, src_at=1100)
    for i, when in enumerate((1200, 1400, 1300, 1500)):
        p = ui / "src" / f"m{i}.ts"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (when, when))
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", ui)

    out = stale_bundle_sources(entry, limit=3)

    assert out == ["src/m3.ts", "src/m1.ts", "src/m2.ts"]
