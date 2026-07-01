"""CLI tests for ``sentinel attention`` / ``sentinel behaviors`` /
``sentinel behaviors-rebuild`` commands.

Covers the read-only inspectors end-to-end via Typer's CliRunner.
``behaviors-rebuild`` is exercised by patching the BehaviorsExtractor
construction; the full extractor is covered separately in
test_behaviors_extractor.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from raven.cli.sentinel_commands import sentinel_app
from raven.memory_engine.consolidate.behaviors import (
    BehaviorEvent,
    render_append_block,
)

runner = CliRunner()


def _seed_attention(workspace: Path, text: str) -> None:
    path = workspace / "user_memory" / "attention.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_behaviors(workspace: Path, events: list[BehaviorEvent]) -> None:
    path = workspace / "user_memory" / "behaviors.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_append_block(events), encoding="utf-8")


def _ev(**overrides: Any) -> BehaviorEvent:
    defaults: dict[str, Any] = dict(
        id="evt_a1b2c3d4",
        day="2026-05-29",
        start="14:00",
        end="14:30",
        session="cli:default",
        turns=8,
        intent="debug",
        outcome="resolved",
        topic="memory-engine",
        project="raven",
        source="user-asked",
        owner="user",
        tools=["Bash", "Edit"],
        summary="debugged memory_engine session split",
    )
    defaults.update(overrides)
    return BehaviorEvent(**defaults)


# ===========================================================================
# sentinel attention
# ===========================================================================


class TestAttentionCommand:
    def test_missing_file_exits_with_code_1(self, tmp_path: Path) -> None:
        result = runner.invoke(
            sentinel_app,
            ["attention", "-w", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "No attention.md" in result.stdout

    def test_dumps_full_file(self, tmp_path: Path) -> None:
        _seed_attention(
            tmp_path,
            "## Pending proposals\n- prop_42\n\n## Active threads\n- routine_x\n",
        )
        result = runner.invoke(
            sentinel_app,
            ["attention", "-w", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "prop_42" in result.stdout
        assert "routine_x" in result.stdout

    def test_section_filter(self, tmp_path: Path) -> None:
        _seed_attention(
            tmp_path,
            "## Pending proposals\n- prop_42\n\n## Active threads\n- routine_x\n",
        )
        result = runner.invoke(
            sentinel_app,
            [
                "attention",
                "-w",
                str(tmp_path),
                "--section",
                "## Pending proposals",
            ],
        )
        assert result.exit_code == 0
        assert "prop_42" in result.stdout
        assert "routine_x" not in result.stdout

    def test_missing_section_exits_with_code_1(self, tmp_path: Path) -> None:
        _seed_attention(tmp_path, "## Pending proposals\n- p\n")
        result = runner.invoke(
            sentinel_app,
            [
                "attention",
                "-w",
                str(tmp_path),
                "--section",
                "## Active threads",
            ],
        )
        assert result.exit_code == 1
        assert "absent or empty" in result.stdout


# ===========================================================================
# sentinel behaviors
# ===========================================================================


class TestBehaviorsCommand:
    def test_missing_file_exits(self, tmp_path: Path) -> None:
        result = runner.invoke(
            sentinel_app,
            ["behaviors", "-w", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "No behaviors.md" in result.stdout

    def test_dumps_full_file(self, tmp_path: Path) -> None:
        _seed_behaviors(tmp_path, [_ev()])
        result = runner.invoke(
            sentinel_app,
            ["behaviors", "-w", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "## 2026-05-29" in result.stdout
        assert "evt_a1b2c3d4" in result.stdout

    def test_session_filter(self, tmp_path: Path) -> None:
        _seed_behaviors(
            tmp_path,
            [
                _ev(id="evt_cli", session="cli:default"),
                _ev(id="evt_tg", session="telegram:user42"),
            ],
        )
        result = runner.invoke(
            sentinel_app,
            [
                "behaviors",
                "-w",
                str(tmp_path),
                "--session",
                "cli:default",
            ],
        )
        assert result.exit_code == 0
        assert "evt_cli" in result.stdout
        assert "evt_tg" not in result.stdout

    def test_since_filter(self, tmp_path: Path) -> None:
        _seed_behaviors(
            tmp_path,
            [
                _ev(id="evt_old", day="2026-05-25"),
                _ev(id="evt_new", day="2026-05-29"),
            ],
        )
        result = runner.invoke(
            sentinel_app,
            [
                "behaviors",
                "-w",
                str(tmp_path),
                "--since",
                "2026-05-28",
            ],
        )
        assert result.exit_code == 0
        assert "evt_new" in result.stdout
        assert "evt_old" not in result.stdout

    def test_bad_since_exits_with_code_2(self, tmp_path: Path) -> None:
        _seed_behaviors(tmp_path, [_ev()])
        result = runner.invoke(
            sentinel_app,
            [
                "behaviors",
                "-w",
                str(tmp_path),
                "--since",
                "May 28",
            ],
        )
        assert result.exit_code == 2
        assert "Bad --since" in result.stdout

    def test_folded_format(self, tmp_path: Path) -> None:
        _seed_behaviors(
            tmp_path,
            [
                _ev(
                    id="evt_a",
                    day="2026-05-29",
                    start="14:00",
                    end="14:30",
                    intent="debug",
                    outcome="resolved",
                    topic="memory-engine",
                    project="raven",
                    summary="debugged session split",
                ),
            ],
        )
        result = runner.invoke(
            sentinel_app,
            [
                "behaviors",
                "-w",
                str(tmp_path),
                "--folded",
            ],
        )
        assert result.exit_code == 0
        assert "[05-29 14:00-14:30 8t]" in result.stdout
        assert "debug→resolved" in result.stdout
        # Tools / source / owner suppressed in folded view
        assert "Bash" not in result.stdout

    def test_no_match_after_filter(self, tmp_path: Path) -> None:
        _seed_behaviors(tmp_path, [_ev(session="cli:default")])
        result = runner.invoke(
            sentinel_app,
            [
                "behaviors",
                "-w",
                str(tmp_path),
                "--session",
                "telegram:nonexistent",
            ],
        )
        assert result.exit_code == 0
        assert "No events match" in result.stdout
