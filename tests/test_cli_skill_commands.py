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
    for sub in ("list", "get", "block", "unblock", "remove"):
        assert sub in r.stdout, f"missing subcommand in --help: {sub}"


@pytest.mark.parametrize("subcmd", ["list", "get", "block", "unblock", "remove"])
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


# ============================================================================
# skill block / unblock  (on-disk skillForge.blocklist)
# ============================================================================


def _read_blocklist(cfg: Path) -> list[str]:
    import json

    data = json.loads(cfg.read_text(encoding="utf-8"))
    return data.get("skillForge", {}).get("blocklist", [])


def test_skill_block_adds_to_config(tmp_config: Path) -> None:
    r = runner.invoke(app, ["skill", "block", "tag-memory"])
    assert r.exit_code == 0
    assert "tag-memory" in r.stdout
    assert _read_blocklist(tmp_config) == ["tag-memory"]


def test_skill_block_is_idempotent_case_insensitive(tmp_config: Path) -> None:
    runner.invoke(app, ["skill", "block", "tag-memory"])
    r = runner.invoke(app, ["skill", "block", "Tag-Memory"])
    assert r.exit_code == 0
    assert _read_blocklist(tmp_config) == ["tag-memory"]


def test_skill_unblock_removes_from_config(tmp_config: Path) -> None:
    runner.invoke(app, ["skill", "block", "tag-memory"])
    runner.invoke(app, ["skill", "block", "other"])
    r = runner.invoke(app, ["skill", "unblock", "Tag-Memory"])
    assert r.exit_code == 0
    assert _read_blocklist(tmp_config) == ["other"]


def test_skill_unblock_absent_is_noop(tmp_config: Path) -> None:
    r = runner.invoke(app, ["skill", "unblock", "ghost"])
    assert r.exit_code == 0


# ============================================================================
# skill remove  (installed Hub bundle dirs)
# ============================================================================


def _workspace_config(cfg: Path, tmp_path: Path) -> Path:
    import json

    ws = tmp_path / "ws"
    (ws / "skills" / "hub").mkdir(parents=True)
    cfg.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(ws)}}}),
        encoding="utf-8",
    )
    return ws


def test_skill_remove_deletes_matching_bundles(tmp_config: Path, tmp_path: Path) -> None:
    ws = _workspace_config(tmp_config, tmp_path)
    bundle = ws / "skills" / "hub" / "tag-memory@v1"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("x", encoding="utf-8")
    other = ws / "skills" / "hub" / "keep-me@v2"
    other.mkdir()

    r = runner.invoke(app, ["skill", "remove", "Tag-Memory", "--yes"])
    assert r.exit_code == 0
    assert not bundle.exists()
    assert other.exists()


def test_skill_remove_unknown_exits_1(tmp_config: Path, tmp_path: Path) -> None:
    _workspace_config(tmp_config, tmp_path)
    r = runner.invoke(app, ["skill", "remove", "ghost", "--yes"])
    assert r.exit_code == 1
    assert "No installed Hub bundle" in r.stdout
