"""Behaviour of ``subagents/install.sh``, exercised as the script a user runs.

The script had no test at all, and the step this suite mostly guards is the one a
user cannot undo by re-running: it reports the config rows that outrank the
discovered ones, and with ``--prune-stale`` it deletes them. Everything is driven
through the real script under ``bash`` against a tree, a host config and a ``uv``
built for the test -- reimplementing its logic in python would test a
transcription of the script rather than the script.

Every run pins ``RAVEN_HOME`` at a directory the test owns, including the runs
that are meant to find no host raven at all. The step's whole purpose is to
delete rows from the host's config, so a test that let it fall through to the
developer's own ``~/.raven/config.json`` would delete their agents.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "subagents" / "install.sh"

_FAKE_UV = """#!/usr/bin/env bash
# Stands in for `uv sync`: leaves the marker the script probes for and reports a
# package delta on stderr, which is the stream the real uv writes it to.
mkdir -p .venv/bin
printf '#!/bin/sh\\n' > .venv/bin/raven
chmod +x .venv/bin/raven
{
  echo "Resolved 200 packages in 1ms"
  echo " + jedi==0.20.0"
  echo " + parso==0.8.7"
  echo " ~ raven==0.1.9 (from file:///x)"
} >&2
"""


def _folder(tree: Path, name: str, agent_name: str) -> Path:
    """One sub-agent folder: manifest, installer, pinned LLM, env template, checkout."""
    folder = tree / name
    (folder / "checkout").mkdir(parents=True)
    (folder / "checkout" / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (folder / "install.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (folder / ".env.example").write_text(f"{name.upper()}_API_KEY=\n", encoding="utf-8")
    (folder / "subagent.json").write_text(
        json.dumps(
            {
                "name": agent_name,
                "kind": "cli",
                "description": "a folder built for the test",
                "enabled": True,
                "command": "{PYTHON} {SUBAGENT_DIR}/run.py --prompt-file {prompt_file}",
                "recommendedLlm": {"provider": "custom", "model": "m", "apiBase": "https://e/v1"},
            }
        ),
        encoding="utf-8",
    )
    (folder / "config.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"provider": "custom", "model": "m"}},
                "providers": {"custom": {"apiBase": "https://e/v1"}},
                "memory": {"backend": "everos", "userId": name, "agentId": name},
            }
        ),
        encoding="utf-8",
    )
    return folder


def _tree(tmp_path: Path, folders: dict[str, str]) -> Path:
    tree = tmp_path / "subagents"
    tree.mkdir()
    (tree / "install.sh").write_bytes(INSTALLER.read_bytes())
    for name, agent_name in folders.items():
        _folder(tree, name, agent_name)
    return tree


def _host(tmp_path: Path, rows: list[dict]) -> Path:
    home = tmp_path / "raven-home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"subagents": {"agents": rows}}), encoding="utf-8")
    return home


def _rows(home: Path) -> list[dict]:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["subagents"]["agents"]


def _cli_row(name: str, command: str, *, enabled: bool = True) -> dict:
    return {"name": name, "kind": "cli", "command": command, "enabled": enabled}


_NO_RAVEN_SHIM = """#!/usr/bin/env bash
echo "ModuleNotFoundError: No module named 'raven'" >&2
exit 1
"""


def _interpreter_without_raven(tree: Path) -> str:
    """A path that runs and cannot import raven, for RAVEN_PYTHON to name.

    Written rather than borrowed. `/usr/bin/python3` was the obvious candidate and
    was wrong twice over: the CI image ships none, so the case silently became "the
    interpreter does not exist" while the tests kept passing, and a machine that
    does ship one may have raven importable in it, which is the opposite condition.

    The script observes the writer only through its exit status and stderr, so a
    shim reproducing those reproduces the condition under test.
    """
    bin_dir = tree.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "python3-without-raven"
    shim.write_text(_NO_RAVEN_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


def _run(
    tree: Path,
    *args: str,
    home: Path,
    reachable_raven: bool = True,
    fake_uv: bool = False,
    raven_python: str | None = None,
):
    env = dict(os.environ)
    env["RAVEN_HOME"] = str(home)
    env.pop("SUBAGENT_PYTHON", None)
    bin_dir = tree.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The script's own helpers call `python3` off PATH, so the test supplies one
    # rather than borrowing the machine's. Borrowing is what failed in CI, whose
    # image ships no `/usr/bin/python3` -- and it is the narrowed PATH below,
    # there so `command -v raven` finds nothing, that takes the inherited one away.
    shim = bin_dir / "python3"
    if not shim.exists():
        shim.symlink_to(sys.executable)
    if reachable_raven:
        env["RAVEN_PYTHON"] = raven_python or sys.executable
    else:
        env.pop("RAVEN_PYTHON", None)
        env["PATH"] = "/usr/bin:/bin"
    if fake_uv:
        uv = bin_dir / "uv"
        uv.write_text(_FAKE_UV, encoding="utf-8")
        uv.chmod(0o755)
    # Last and unconditional: every run needs the shim reachable, and the narrowed
    # branch above has just discarded whatever PATH it would have been found on.
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(tree / "install.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _tree(tmp_path, {"alpha": "Alpha", "beta": "Beta"})


class TestStaleRowReport:
    """A stored row sharing a folder's name outranks the discovered one, so the
    script has to say so whether or not it is allowed to act on it."""

    def test_a_colliding_row_is_named_in_the_report(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", home=home)
        assert result.returncode == 0, result.stderr
        assert "Alpha" in result.stdout
        assert "--prune-stale" in result.stdout

    def test_a_colliding_row_survives_a_run_without_the_flag(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        _run(tree, "--no-sync", home=home)
        assert [row["name"] for row in _rows(home)] == ["Alpha"]

    def test_a_row_no_folder_claims_is_not_reported(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Keeper", "somewhere-else --go")])
        result = _run(tree, "--no-sync", home=home)
        # Paired with the positive: a run that printed nothing at all would satisfy
        # the absence on its own, and that is the state a broken step produces.
        assert "== alpha" in result.stdout
        assert "Keeper" not in result.stdout

    def test_a_disabled_row_warns_that_pruning_re_enables_it(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py", enabled=False)])
        result = _run(tree, "--no-sync", home=home)
        assert "re-enable" in result.stdout.lower()


class TestPruning:
    def test_prune_stale_removes_the_colliding_row(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", "--prune-stale", home=home)
        assert result.returncode == 0, result.stderr
        assert [row["name"] for row in _rows(home)] == []

    def test_prune_stale_keeps_a_row_no_folder_claims(self, tree: Path, tmp_path: Path) -> None:
        home = _host(
            tmp_path,
            [_cli_row("Alpha", "/py /old/alpha/run.py"), _cli_row("Keeper", "somewhere-else --go")],
        )
        _run(tree, "--no-sync", "--prune-stale", home=home)
        assert [row["name"] for row in _rows(home)] == ["Keeper"]

    def test_dry_run_refuses_to_prune(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        _run(tree, "--no-sync", "--dry-run", "--prune-stale", home=home)
        assert [row["name"] for row in _rows(home)] == ["Alpha"]

    def test_the_backup_holds_the_rows_as_they_sat_on_disk(self, tree: Path, tmp_path: Path) -> None:
        """Raw, not a validated round-trip: the write path expands a row to every
        defaulted field, so a backup taken after it would not restore what was there."""
        stored = [_cli_row("Alpha", "/py /old/alpha/run.py"), _cli_row("Keeper", "somewhere-else --go")]
        home = _host(tmp_path, stored)
        _run(tree, "--no-sync", "--prune-stale", home=home)
        backups = sorted(tree.glob("subagents-backup-*.json"))
        assert len(backups) == 1, backups
        assert json.loads(backups[0].read_text(encoding="utf-8")) == stored

    def test_the_backup_is_not_world_readable(self, tree: Path, tmp_path: Path) -> None:
        """An openai row among these carries its own api key. This pins the mode it
        comes to rest at; the window between creation and a later chmod is what the
        implementation closes by opening at 0600, and no test can observe that."""
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        _run(tree, "--no-sync", "--prune-stale", home=home)
        backup = sorted(tree.glob("subagents-backup-*.json"))[0]
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_a_backup_that_cannot_be_written_stops_the_prune(self, tree: Path, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("chmod 0o500 does not block root")
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        tree.chmod(0o500)
        try:
            result = _run(tree, "--no-sync", "--prune-stale", home=home)
        finally:
            tree.chmod(0o700)
        assert "cannot write the backup" in result.stderr
        assert "could not read the host config" not in result.stdout
        assert [row["name"] for row in _rows(home)] == ["Alpha"]

    def test_pruning_nothing_writes_no_backup(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Keeper", "somewhere-else --go")])
        result = _run(tree, "--no-sync", "--prune-stale", home=home)
        assert result.returncode == 0, result.stderr
        assert [row["name"] for row in _rows(home)] == ["Keeper"]
        assert sorted(tree.glob("subagents-backup-*.json")) == []


class TestWhenTheStepCannotRun:
    """An interpreter that cannot import raven, which is the shape of every reason
    the step can fail for once one has been found at all."""

    def test_a_prune_that_could_not_run_exits_non_zero(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", "--prune-stale", home=home, raven_python=_interpreter_without_raven(tree))
        assert result.returncode != 0
        assert [row["name"] for row in _rows(home)] == ["Alpha"]

    def test_a_report_that_could_not_run_still_exits_zero(self, tree: Path, tmp_path: Path) -> None:
        """Reporting is advisory. Only --prune-stale names an action, so only it can
        be reported as not having happened."""
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", home=home, raven_python=_interpreter_without_raven(tree))
        assert result.returncode == 0
        assert "could not read the host config" in result.stdout


class TestWithoutAHostRaven:
    """The first install runs this script before a configured raven exists. That
    ordering is what the config step was taken out of the script for once already,
    so it has to stay a no-op rather than an error."""

    def test_an_unreachable_raven_still_exits_zero(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", "--prune-stale", home=home, reachable_raven=False)
        assert result.returncode == 0, result.stderr

    def test_an_unreachable_raven_leaves_the_config_alone(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [_cli_row("Alpha", "/py /old/alpha/run.py")])
        result = _run(tree, "--no-sync", "--prune-stale", home=home, reachable_raven=False)
        # The positive half: the folders were still processed, so the untouched
        # config is the step declining rather than the script never getting there.
        assert "== alpha" in result.stdout
        assert [row["name"] for row in _rows(home)] == ["Alpha"]

    def test_a_config_that_does_not_exist_is_not_created(self, tree: Path, tmp_path: Path) -> None:
        home = tmp_path / "raven-home"
        home.mkdir()
        result = _run(tree, "--no-sync", "--prune-stale", home=home)
        assert result.returncode == 0, result.stderr
        assert not (home / "config.json").exists()


class TestBuildVisibility:
    """Re-running already fixes a stale venv and builds a folder added by an
    upgrade. What it did not do is say so, which is why nobody knew to re-run."""

    def test_a_freshly_built_folder_is_named_in_the_summary(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [])
        result = _run(tree, home=home, fake_uv=True)
        assert result.returncode == 0, result.stderr
        assert "newly built" in result.stdout
        assert "alpha" in result.stdout.split("newly built")[1]

    def test_the_sync_delta_is_attributed_to_its_folder(self, tree: Path, tmp_path: Path) -> None:
        home = _host(tmp_path, [])
        result = _run(tree, home=home, fake_uv=True)
        assert "2 added" in result.stdout
        assert "1 updated" in result.stdout
