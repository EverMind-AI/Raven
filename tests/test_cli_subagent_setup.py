"""Onboard's sub-agent step: discovery, key placement, and registration."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.cli import onboard_commands, subagent_setup


def _folder(
    root: Path,
    name: str,
    *,
    installer: bool = True,
    checkout: str | None = "Raven-main",
    venv: bool = True,
    model: str = "anthropic/claude-opus-5",
    manifest_model: str | None = None,
    display: str | None = None,
    api_base: str = "https://gw.example/api/v1",
) -> Path:
    """Build one plausible sub-agent folder under ``root``."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "subagent.json").write_text(
        json.dumps(
            {
                "name": display or name,
                "description": f"{name} does things",
                "recommendedLlm": {"model": manifest_model or model, "apiBase": api_base},
            }
        ),
        encoding="utf-8",
    )
    (folder / "config.json").write_text(
        json.dumps(
            {
                "providers": {"custom": {"apiBase": api_base}},
                "agents": {"defaults": {"provider": "custom", "model": model}},
            }
        ),
        encoding="utf-8",
    )
    (folder / ".env.example").write_text(
        f"{subagent_setup._env_var(name)}=\nOTHER=keep\n",
        encoding="utf-8",
    )
    if installer:
        (folder / "install.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    if checkout:
        project = folder / checkout
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
        if venv:
            binaries = project / ".venv" / "bin"
            binaries.mkdir(parents=True)
            (binaries / "raven").write_text("#!/bin/sh\n", encoding="utf-8")
            (binaries / "raven").chmod(0o755)
    return folder


class _ScriptedSelect:
    """Answers each prompt from a script, matched by a substring of its message."""

    def __init__(self, answers: list[tuple[str, Any]]) -> None:
        self._answers = list(answers)
        self.asked: list[str] = []
        self.offered: list[list[Any]] = []

    def select(self, message: str, choices: list[Any] | None = None, **_kwargs: Any) -> Any:
        self.asked.append(message)
        self.offered.append([getattr(c, "value", c) for c in (choices or [])])
        for index, (needle, answer) in enumerate(self._answers):
            if needle in message:
                self._answers.pop(index)
                return _Answer(answer)
        raise AssertionError(f"unscripted prompt: {message}")

    def confirm(self, message: str, **_kwargs: Any) -> Any:
        """Same script, same matching. Kept beside ``select`` because the build
        offer is a confirm and the key choice is a select, and a run reaches both."""
        self.asked.append(message)
        for index, (needle, answer) in enumerate(self._answers):
            if needle in message:
                self._answers.pop(index)
                return _Answer(answer)
        raise AssertionError(f"unscripted confirm: {message}")

    @staticmethod
    def Choice(_title: str, value: Any = None, **_kwargs: Any) -> Any:  # noqa: N802 - questionary's spelling
        return _Choice(value)


class _Answer:
    def __init__(self, value: Any) -> None:
        self._value = value

    def ask(self) -> Any:
        return self._value


class _Choice:
    def __init__(self, value: Any) -> None:
        self.value = value


class _AlwaysConfirm:
    """Answers every confirm with the one scripted answer."""

    def __init__(self, answer: bool) -> None:
        self._answer = answer
        self.asked: list[str] = []

    def confirm(self, message: str, **_kwargs: Any) -> Any:
        self.asked.append(message)
        return _Answer(self._answer)


class _RecordingConsole:
    """Collects every line the step prints, for output-shape assertions."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args: Any, **_kwargs: Any) -> None:
        self.lines.append(" ".join(str(arg) for arg in args))


# --------------------------------------------------------------------------- discovery


def test_subagents_root_is_none_for_a_wheel_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A wheel puts raven/ in site-packages, where there is no sibling tree. That
    # absence is the gate, so it must read as "nothing to do", not as an error.
    package = tmp_path / "site-packages" / "raven"
    package.mkdir(parents=True)
    monkeypatch.setattr("raven.__file__", str(package / "__init__.py"))
    assert subagent_setup.subagents_root() is None


def test_subagents_root_finds_the_tree_of_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "raven").mkdir()
    (tmp_path / "subagents").mkdir()
    monkeypatch.setattr("raven.__file__", str(tmp_path / "raven" / "__init__.py"))
    assert subagent_setup.subagents_root() == tmp_path / "subagents"


def test_discover_skips_a_folder_that_ships_no_installer(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-code")
    _folder(tmp_path, "raven-docs", installer=False)
    assert [f.path.name for f in subagent_setup.discover(tmp_path)] == ["raven-code"]


def test_discover_skips_a_folder_with_unreadable_json(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-code")
    broken = _folder(tmp_path, "raven-broken")
    (broken / "config.json").write_text("{not json", encoding="utf-8")
    assert [f.path.name for f in subagent_setup.discover(tmp_path)] == ["raven-code"]


def test_discover_reports_the_model_that_actually_runs(tmp_path: Path) -> None:
    # config.json is what the launcher renders; subagent.json only annotates. A
    # step that offered the manifest's model would name one the agent never runs.
    _folder(tmp_path, "raven-code", model="runs/this", manifest_model="claims/that")
    assert subagent_setup.discover(tmp_path)[0].recommended_model == "runs/this"


@pytest.mark.parametrize(
    ("folder", "expected"),
    [("raven-code", "CODE_API_KEY"), ("raven-oncall", "ONCALL_API_KEY"), ("deep-thought", "DEEP_THOUGHT_API_KEY")],
)
def test_env_var_drops_the_raven_prefix(folder: str, expected: str) -> None:
    assert subagent_setup._env_var(folder) == expected


def test_venv_ready_is_false_until_the_checkout_has_one(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-code", venv=False)
    assert subagent_setup.discover(tmp_path)[0].venv_ready is False


def test_a_present_but_unexecutable_launcher_is_not_built(tmp_path: Path) -> None:
    # `subagents/install.sh` classifies the same folder with `[ -x ]`. Testing
    # existence here instead would have the installer call a folder unbuilt while
    # the wizard offered it, and the entry would fail the moment it was picked.
    _folder(tmp_path, "raven-code")
    launcher = tmp_path / "raven-code" / "Raven-main" / ".venv" / "bin" / "raven"
    launcher.chmod(0o644)
    assert subagent_setup.discover(tmp_path)[0].venv_ready is False


def test_a_folder_with_two_projects_has_no_checkout(tmp_path: Path) -> None:
    # Two candidates is ambiguous, and guessing one would build the wrong venv.
    folder = _folder(tmp_path, "raven-code")
    second = folder / "Vendored"
    second.mkdir()
    (second / "pyproject.toml").write_text('[project]\nname = "y"\n', encoding="utf-8")
    found = subagent_setup.discover(tmp_path)[0]
    assert found.checkout is None and found.venv_ready is False


# --------------------------------------------------------------------------- key placement


def test_write_key_replaces_the_template_slot_in_place(tmp_path: Path) -> None:
    # Appending a second assignment would leave the file disagreeing with itself:
    # the launchers take the first non-empty value.
    _folder(tmp_path, "raven-code")
    folder = subagent_setup.discover(tmp_path)[0]
    subagent_setup.write_key(folder, "sk-live")
    body = (folder.path / ".env").read_text(encoding="utf-8")
    assert body.count("CODE_API_KEY=") == 1
    assert "CODE_API_KEY=sk-live" in body
    assert "OTHER=keep" in body


def test_write_key_appends_when_the_template_has_no_slot(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-code")
    folder = subagent_setup.discover(tmp_path)[0]
    (folder.path / ".env").write_text("OTHER=keep\n", encoding="utf-8")
    subagent_setup.write_key(folder, "sk-live")
    assert "CODE_API_KEY=sk-live" in (folder.path / ".env").read_text(encoding="utf-8")


def test_write_key_creates_from_the_template_and_locks_it_down(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-code")
    folder = subagent_setup.discover(tmp_path)[0]
    assert not (folder.path / ".env").exists()
    subagent_setup.write_key(folder, "sk-live")
    env = folder.path / ".env"
    assert "OTHER=keep" in env.read_text(encoding="utf-8")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- registration
#
# There is none here any more. This step used to end by running the folder's
# `install.py` to write a config row; `vendored_agents` materializes a row per
# folder on every table build instead, so the folder being on disk is what puts
# it on the table. The written row was strictly worse: it baked in the folder's
# absolute path and outranked the discovered one, so an upgrade that moved the
# tree left it naming a launcher that no longer existed.
# See tests/test_subagent_vendored_agents.py for what replaced it.


# --------------------------------------------------------------------------- pruning stale config rows
#
# A config row written by an older install.py outranks the manifest of the
# folder it names, so a manifest a git pull updated never reaches the roster.
# The step below deletes such rows (backing the list up first), leaving the
# folders to vendored discovery.


def _stale_config(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a host config carrying ``rows`` under ``subagents.agents``."""
    (path / "config.json").write_text(json.dumps({"subagents": {"agents": rows}}), encoding="utf-8")
    return path / "config.json"


def test_prune_removes_rows_that_shadow_folder_manifests(tmp_path: Path) -> None:
    # A row whose name matches a folder's manifest is deleted even when its
    # content matches: it still outranks the discovered row.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(
        tmp_path,
        [
            {"name": "research-helper", "kind": "cli", "command": "echo hi"},
            {"name": "Raven-Research", "kind": "cli", "enabled": True},
        ],
    )
    folders = subagent_setup.discover(tmp_path)
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders,
        tmp_path,
        SimpleNamespace(print=lambda *_a, **_k: None),
        _AlwaysConfirm(True),
        warnings,
        config_path=config,
    )

    assert removed == 1
    assert warnings == []
    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["research-helper"]
    backups = list(tmp_path.glob("subagents-backup-*.json"))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    assert [row["name"] for row in backup] == ["research-helper", "Raven-Research"]
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_prune_is_a_no_op_without_colliding_rows(tmp_path: Path) -> None:
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "research-helper", "kind": "cli", "command": "echo hi"}])
    folders = subagent_setup.discover(tmp_path)
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders,
        tmp_path,
        SimpleNamespace(print=lambda *_a, **_k: None),
        _AlwaysConfirm(True),
        warnings,
        config_path=config,
    )

    assert removed == 0
    assert warnings == []
    assert list(tmp_path.glob("subagents-backup-*.json")) == []
    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["research-helper"]


def test_prune_leaves_the_config_untouched_when_the_backup_cannot_be_written(
    tmp_path: Path,
) -> None:
    # A backup that cannot land must stop the prune: a half-deleted list with
    # no backup is exactly what the backup exists to prevent.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "Raven-Research", "kind": "cli", "enabled": True}])
    folders = subagent_setup.discover(tmp_path)
    not_a_directory = tmp_path / "not-a-dir"
    not_a_directory.write_text("x", encoding="utf-8")
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders,
        not_a_directory,
        SimpleNamespace(print=lambda *_a, **_k: None),
        _AlwaysConfirm(True),
        warnings,
        config_path=config,
    )

    assert removed == 0
    assert warnings and "backup" in warnings[0]
    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["Raven-Research"]


def test_backup_is_created_private_and_truncated(tmp_path: Path) -> None:
    # The list can hold an openai row's api key, so the file must be private
    # from the first byte -- and a pre-existing file of the same name (two runs
    # in one second) must be truncated and re-chmodded, not reused.
    backup = tmp_path / "backup.json"
    backup.write_text("stale", encoding="utf-8")
    backup.chmod(0o644)

    subagent_setup._write_private_json(backup, [{"name": "fresh"}])

    assert json.loads(backup.read_text(encoding="utf-8")) == [{"name": "fresh"}]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_prune_stops_short_of_crashing_when_a_survivor_fails_validation(
    tmp_path: Path,
) -> None:
    # remove_agent validates the surviving rows before writing, and a config
    # the schema rejects must surface as a warning, not take the wizard down.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(
        tmp_path,
        [
            {"name": "Raven-Research", "kind": "cli", "command": "run.py"},
            {"name": "twin", "kind": "cli", "command": "a"},
            {"name": "twin", "kind": "cli", "command": "b"},
        ],
    )
    folders = subagent_setup.discover(tmp_path)
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders,
        tmp_path,
        SimpleNamespace(print=lambda *_a, **_k: None),
        _AlwaysConfirm(True),
        warnings,
        config_path=config,
    )

    assert removed == 0
    assert warnings and "twin" in warnings[0] and "Raven-Research" in warnings[0]
    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["Raven-Research", "twin", "twin"]
    assert len(list(tmp_path.glob("subagents-backup-*.json"))) == 1


def test_prune_asks_first_and_keeps_everything_when_declined(tmp_path: Path) -> None:
    # A same-name config row is also how a user edits a vendored agent, so the
    # deletion must not run unasked; a decline keeps the rows and writes nothing.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "Raven-Research", "kind": "cli", "enabled": True}])
    folders = subagent_setup.discover(tmp_path)
    q = _AlwaysConfirm(False)
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders, tmp_path, SimpleNamespace(print=lambda *_a, **_k: None), q, warnings, config_path=config
    )

    assert removed == 0
    assert q.asked, "the prune asked before deleting"
    assert warnings == []
    assert list(tmp_path.glob("subagents-backup-*.json")) == []
    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["Raven-Research"]


def test_prune_flags_a_disabled_row_in_the_confirm_listing(tmp_path: Path) -> None:
    # Deleting an enabled=false row flips behaviour (the agent comes back), so
    # the listing must say so before the user answers the confirm.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "Raven-Research", "kind": "cli", "enabled": False}])
    folders = subagent_setup.discover(tmp_path)
    console = _RecordingConsole()
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders, tmp_path, console, _AlwaysConfirm(True), warnings, config_path=config
    )

    assert removed == 1
    flagged = [line for line in console.lines if "enabled=false" in line and "Raven-Research" in line]
    assert flagged, console.lines


def test_prune_reports_an_unreadable_config(tmp_path: Path) -> None:
    # read_raw_or_raise raises ConfigReadError on a malformed file, and the
    # wizard must carry on with a warning rather than crash in step 5.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    folders = subagent_setup.discover(tmp_path)
    warnings: list[str] = []

    removed = subagent_setup._prune_shadowing_rows(
        folders,
        tmp_path,
        SimpleNamespace(print=lambda *_a, **_k: None),
        _AlwaysConfirm(True),
        warnings,
        config_path=config,
    )

    assert removed == 0
    assert warnings and "sub-agents" in warnings[0]


def test_prune_summary_goes_through_the_wizard_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every line in this step renders in the UI language; the summary must
    # not be the one line that prints both at once.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "Raven-Research", "kind": "cli", "enabled": True}])
    folders = subagent_setup.discover(tmp_path)
    console = _RecordingConsole()
    rendered: list[str] = []
    monkeypatch.setattr(
        subagent_setup,
        "t",
        lambda text, **arguments: rendered.append(text.format(**arguments) if arguments else text) or "RENDERED",
    )

    removed = subagent_setup._prune_shadowing_rows(
        folders, tmp_path, console, _AlwaysConfirm(True), [], config_path=config
    )

    assert removed == 1
    assert any("Removed 1 shadowing" in text for text in rendered)
    assert any("Backed up the previous list" in text for text in rendered)
    assert console.lines[-1] == "RENDERED"


def test_configure_non_interactive_prunes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The unattended skip covers the prune too: a write the caller never
    # saw offered is not something a non-interactive run should perform.
    _folder(tmp_path, "raven-research", display="Raven-Research")
    config = _stale_config(tmp_path, [{"name": "Raven-Research", "kind": "cli", "enabled": True}])
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: config)
    monkeypatch.setattr(
        onboard_commands, "_require_questionary", lambda: pytest.fail("prompted in non-interactive mode")
    )
    warnings: list[str] = []

    assert subagent_setup.configure_subagents(non_interactive=True, warnings=warnings) == 0

    remaining = json.loads(config.read_text(encoding="utf-8"))["subagents"]["agents"]
    assert [row["name"] for row in remaining] == ["Raven-Research"]


# --------------------------------------------------------------------------- the step


def test_configure_is_a_no_op_without_a_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: None)
    warnings: list[str] = []
    assert subagent_setup.configure_subagents(warnings=warnings) == 0
    assert warnings == []


def test_configure_non_interactive_never_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An unattended install must not put agents in a roster nobody asked for.
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(
        onboard_commands, "_require_questionary", lambda: pytest.fail("prompted in non-interactive mode")
    )
    warnings: list[str] = []
    assert subagent_setup.configure_subagents(non_interactive=True, warnings=warnings) == 0
    assert warnings and "sub-agents" in warnings[0]


def test_configure_skips_a_folder_that_is_not_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Registering an agent whose venv is missing puts a name in the roster that
    # fails the moment it is picked.
    _folder(tmp_path, "raven-code", venv=False)
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: tmp_path / "config.json")
    scripted = _ScriptedSelect([])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    assert subagent_setup.configure_subagents(warnings=[]) == 0, "an unbuilt folder is not a ready agent"
    assert scripted.asked == [], "and it is never asked whose key it should spend"


def test_configure_inherit_writes_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "inherit")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert not (tmp_path / "raven-code" / ".env").exists()
    assert "inherit" in scripted.offered[0] and "own" in scripted.offered[0]


def test_configure_own_key_writes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "own")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda *_a, **_kw: "sk-mine")
    monkeypatch.setattr(
        "raven.cli._key_probe.probe_models",
        lambda *_a, **_kw: {"ok": True, "status": "ok", "model_ids": [], "error": None},
    )
    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert "CODE_API_KEY=sk-mine" in (tmp_path / "raven-code" / ".env").read_text(encoding="utf-8")


def test_configure_skip_writes_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "skip")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    assert subagent_setup.configure_subagents(warnings=[]) == 0

    # Skipping declines the key choice, not the agent: the row is discovered from
    # the folder either way. What "skip" costs it is a key of its own, so it
    # inherits the host's LLM at dispatch.
    assert not (tmp_path / "raven-code" / ".env").exists()


def test_configure_falls_back_to_this_ravens_llm_on_a_bad_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A key that fails its probe must still be able to end in a working agent,
    # and the fallback must not leave the bad key on disk.
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(subagent_setup, "get_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "own"), ("What now", "inherit")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda *_a, **_kw: "sk-bad")
    monkeypatch.setattr(
        "raven.cli._key_probe.probe_models",
        lambda *_a, **_kw: {"ok": False, "status": "http_401", "model_ids": None, "error": "nope"},
    )
    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert not (tmp_path / "raven-code" / ".env").exists()


def test_the_recommended_model_leads_the_menu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Order is the whole point of the first option: the recommended model is only
    # reachable through a key of its own, so it must not sit below the fallback.
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "skip")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    subagent_setup.configure_subagents(warnings=[])
    assert scripted.offered[0] == ["own", "inherit", "skip"]


def test_an_openrouter_folder_reuses_the_hosts_key_without_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder(tmp_path, "raven-code", api_base="https://openrouter.ai/api/v1")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "sk-or-host")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "own")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    monkeypatch.setattr(
        onboard_commands, "_prompt_api_key", lambda *_a, **_kw: pytest.fail("asked for a key it already had")
    )
    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert "CODE_API_KEY=sk-or-host" in (tmp_path / "raven-code" / ".env").read_text(encoding="utf-8")


def test_a_folder_on_another_gateway_does_not_reuse_the_openrouter_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A key is valid at the endpoint it was issued for. Reusing an OpenRouter key
    # against a private gateway would fail as a bad credential rather than as the
    # configuration mistake it is.
    _folder(tmp_path, "raven-code", api_base="http://10.0.0.9:3000/v1")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "sk-or-host")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "own")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    monkeypatch.setattr(onboard_commands, "_prompt_api_key", lambda *_a, **_kw: "sk-typed")
    monkeypatch.setattr(
        "raven.cli._key_probe.probe_models",
        lambda *_a, **_kw: {"ok": True, "status": "ok", "model_ids": [], "error": None},
    )
    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert "CODE_API_KEY=sk-typed" in (tmp_path / "raven-code" / ".env").read_text(encoding="utf-8")


def _host_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, providers: dict[str, Any]) -> None:
    """Point the config loader at a written config under tmp_path.

    A real file rather than a stubbed reader: the verdict runs through
    `providers.auth` and the value through `update_providers`, and stubbing
    either would test the stub instead of the rule that a key in `custom` is not
    an OpenRouter key.

    Through `RAVEN_HOME` rather than `set_config_path`, which writes a module
    global that outlives the test and then wins over the environment for the
    rest of the session -- `test_config_loader.py` asserts exactly that
    precedence, so leaving one behind breaks it. `Path.home` moves too:
    credentials are looked for under `~/.raven` whatever the config path says.
    """
    home = tmp_path / ".raven"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("CHATGPT_TOKEN_DIR", "GITHUB_COPILOT_TOKEN_DIR", "MINIMAX_OAUTH_TOKEN_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_host_openrouter_key_ignores_a_key_in_another_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The host having *a* key is not the host having *that* key: a key in
    # `custom` belongs to whichever gateway that section points at.
    _host_config(tmp_path, monkeypatch, {"custom": {"apiKey": "sk-private", "apiBase": "http://10.0.0.9:3000/v1"}})
    assert subagent_setup.host_openrouter_key() == ""


def test_host_openrouter_key_reads_the_openrouter_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_config(
        tmp_path,
        monkeypatch,
        {"openrouter": {"apiKey": "sk-or-1"}, "custom": {"apiKey": "sk-other", "apiBase": "http://10.0.0.9:3000/v1"}},
    )
    assert subagent_setup.host_openrouter_key() == "sk-or-1"


def test_host_openrouter_key_is_empty_when_no_provider_is_set_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_config(tmp_path, monkeypatch, {})
    assert subagent_setup.host_openrouter_key() == ""


def test_an_oauth_host_is_not_offered_its_own_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The launchers accept a literal key and nothing else.

    An OAuth sign-in leaves `providers` with no `apiKey`, so `inherit_llm`
    returns "" and the run exits -- after the wizard has already said
    "registered". Offering the option at all is the defect.
    """
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: False)
    scripted = _ScriptedSelect([("Set up", "skip")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    subagent_setup.configure_subagents(warnings=[])
    assert scripted.offered[0] == ["own", "skip"]


def test_a_host_with_a_literal_key_keeps_the_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, "raven-code")
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: tmp_path)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Set up", "skip")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    subagent_setup.configure_subagents(warnings=[])
    assert scripted.offered[0] == ["own", "inherit", "skip"]


def test_an_oauth_sign_in_is_not_a_key_to_lend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors `inherit_llm`, not `providers.auth`: auth calls this host
    # configured, and the launcher still has nothing to inherit.
    _host_config(tmp_path, monkeypatch, {"openai_codex": {"models": []}})
    assert subagent_setup.host_can_lend_a_key() is False


def test_a_literal_key_anywhere_is_a_key_to_lend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_config(tmp_path, monkeypatch, {"custom": {"apiKey": "sk-x", "apiBase": "http://10.0.0.9:3000/v1"}})
    assert subagent_setup.host_can_lend_a_key() is True


# --------------------------------------------------------------------------- the build offer
#
# Everything past `_offer_to_build`'s first guard was unreached: the older test
# pointed `subagents_root` at a tree with no `install.sh`, so the function
# returned on the missing-installer branch and the confirm, the `bash install.sh
# <folder>` call and the "re-read `venv_ready` rather than trust the exit status"
# check never ran.


def _tree_with_installer(tmp_path: Path, *, venv: bool = False) -> Path:
    _folder(tmp_path, "raven-code", venv=venv)
    (tmp_path / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return tmp_path


def _build_call(monkeypatch: pytest.MonkeyPatch, *, code: int = 0, builds: Path | None = None) -> list[list[str]]:
    """Stand in for the installer subprocess, recording the argv it was given."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        if builds is not None:
            launcher = builds / ".venv" / "bin" / "raven"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
        return SimpleNamespace(returncode=code, stdout="", stderr="uv sync failed\n")

    monkeypatch.setattr(subagent_setup.subprocess, "run", fake_run)
    return seen


def test_declining_the_build_leaves_the_folder_alone_and_asks_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree_with_installer(tmp_path)
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: root)
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Build it now", False)])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    calls = _build_call(monkeypatch)

    assert subagent_setup.configure_subagents(warnings=[]) == 0
    assert calls == [], "declining must not run the installer"
    assert not any("Set up" in asked for asked in scripted.asked), "and must not go on to the key question"


def test_accepting_runs_the_installer_for_that_folder_then_asks_about_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree_with_installer(tmp_path)
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: root)
    monkeypatch.setattr(subagent_setup, "host_openrouter_key", lambda: "")
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Build it now", True), ("Set up", "inherit")])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    calls = _build_call(monkeypatch, builds=root / "raven-code" / "Raven-main")

    assert subagent_setup.configure_subagents(warnings=[]) == 1
    assert calls == [["bash", str(root / "install.sh"), "raven-code"]], "one folder, not the whole tree"


def test_a_build_that_leaves_no_launcher_warns_whatever_it_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`returncode == 0` is not the claim that matters.

    The script does several things per folder; the one this step needs is a
    launcher raven can start. Trusting the status would carry on to the key
    question for an agent that cannot run.
    """
    root = _tree_with_installer(tmp_path)
    monkeypatch.setattr(subagent_setup, "subagents_root", lambda: root)
    monkeypatch.setattr(subagent_setup, "host_can_lend_a_key", lambda: True)
    scripted = _ScriptedSelect([("Build it now", True)])
    monkeypatch.setattr(onboard_commands, "_require_questionary", lambda: scripted)
    _build_call(monkeypatch, code=0, builds=None)
    warnings: list[str] = []

    assert subagent_setup.configure_subagents(warnings=warnings) == 0
    assert warnings and "build failed" in warnings[0]
    assert not any("Set up" in asked for asked in scripted.asked)
