"""CLI tests for ``raven skill``.

Two subcommands are covered with mocked ``SkillService`` so the tests
stay self-contained.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


# ============================================================================
# --help surfaces
# ============================================================================


def test_skill_help_lists_all_subcommands() -> None:
    r = runner.invoke(app, ["skill", "--help"])
    assert r.exit_code == 0
    for sub in ("list", "get"):
        assert sub in r.stdout, f"missing subcommand in --help: {sub}"


@pytest.mark.parametrize("subcmd", ["list", "get"])
def test_skill_subcommand_help_works(subcmd: str) -> None:
    """Every skill subcommand exposes ``--help`` without crashing."""
    r = runner.invoke(app, ["skill", subcmd, "--help"])
    assert r.exit_code == 0


# ============================================================================
# skill list / get  (mock SkillService)
# ============================================================================


def _make_meta(name: str, source: str = "builtin", desc: str = "stub") -> SimpleNamespace:
    return SimpleNamespace(name=name, source=source, description=desc)


def test_skill_list_renders_table(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``skill list`` prints a table when the registry returns metas."""
    fake_svc = SimpleNamespace(
        gather_all_skills=lambda: [_make_meta("alpha"), _make_meta("beta", source="workspace")],
    )
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "list"])
    assert r.exit_code == 0
    assert "alpha" in r.stdout
    assert "beta" in r.stdout
    assert "Skills" in r.stdout  # table title


def test_skill_list_empty_message(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty registry prints the ``No skills found`` notice."""
    fake_svc = SimpleNamespace(gather_all_skills=lambda: [])
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "list"])
    assert r.exit_code == 0
    assert "No skills found" in r.stdout


def test_skill_list_filters_by_source(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--source workspace`` only shows metas matching that source."""
    fake_svc = SimpleNamespace(
        gather_all_skills=lambda: [
            _make_meta("alpha", source="builtin"),
            _make_meta("beta", source="workspace"),
        ],
    )
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "list", "--source", "workspace"])
    assert r.exit_code == 0
    assert "beta" in r.stdout
    assert "alpha" not in r.stdout


def test_skill_get_known_skill(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``skill get <name>`` prints the metadata fields."""
    fake_svc = SimpleNamespace(
        get_skill_metadata=lambda _: {"name": "alpha", "source": "builtin"},
        load_skill=lambda _: "# SKILL.md body",
    )
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "get", "alpha"])
    assert r.exit_code == 0
    assert "alpha" in r.stdout
    assert "builtin" in r.stdout


def test_skill_get_unknown_skill_exits_1(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_svc = SimpleNamespace(
        get_skill_metadata=lambda _: None,
        load_skill=lambda _: None,
    )
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "get", "ghost-skill"])
    assert r.exit_code == 1
    assert "Skill not found" in r.stdout


def test_skill_get_with_body_renders_markdown(tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--with-body`` prints the SKILL.md body section."""
    fake_svc = SimpleNamespace(
        get_skill_metadata=lambda _: {"source": "builtin"},
        load_skill=lambda _: "# Title\nbody text",
    )
    monkeypatch.setattr("raven.cli.skill_commands._build_skill_service", lambda: fake_svc)

    r = runner.invoke(app, ["skill", "get", "alpha", "--with-body"])
    assert r.exit_code == 0
    assert "SKILL.md" in r.stdout
