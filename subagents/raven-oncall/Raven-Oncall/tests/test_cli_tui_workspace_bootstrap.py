"""Starting the TUI writes the workspace bootstrap files, as `agent` already does.

`sync_workspace_templates` was reachable from `init`, `agent` and `gateway` but
not from `tui`, so a workspace that never met one of those had none of the
bootstrap files -- and nothing said so. `TOOLS.md` is the one that shows: it is
read on every turn and it is where the ops surface is named, so a turn asked to
run an experiment on a remote host had nothing telling it to use `ops_submit`.

Measured 2026-08-07 on a hand-made home (every on-call arm here is one): the loop
called no ops tool for four minutes, wrote its own trial script, and kept its own
ledger under a campaign name it invented, tuning a model the task never named.
The other arm, on the same missing file, used `ops_submit` normally -- so one arm
behaving is not evidence the instrument is sound.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.utils.helpers import sync_workspace_templates


@pytest.fixture(autouse=True)
def _restore_config_path():
    from raven.config import loader

    before = loader._current_config_path
    yield
    loader._current_config_path = before


def _home(tmp_path: Path) -> Path:
    """A raven home made by hand: a config file and an empty workspace."""
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({}), encoding="utf-8")
    return home


def test_a_hand_made_workspace_gets_its_bootstrap_files(tmp_path: Path) -> None:
    workspace = _home(tmp_path) / "workspace"
    assert not (workspace / "TOOLS.md").exists()

    created = sync_workspace_templates(workspace, silent=True)

    assert "TOOLS.md" in created
    assert (workspace / "TOOLS.md").exists()


def test_the_tools_file_names_the_ops_surface(tmp_path: Path) -> None:
    """The failure was not a missing file in the abstract: it was the loop having
    nothing that named `ops_submit`."""
    workspace = _home(tmp_path) / "workspace"

    sync_workspace_templates(workspace, silent=True)

    text = (workspace / "TOOLS.md").read_text(encoding="utf-8")
    assert "ops_submit" in text
    assert "ops_tune_status" in text


def test_the_startup_builder_is_the_thing_that_calls_it(tmp_path: Path) -> None:
    """A static check, and deliberately so. The behaviour lives inside
    ``_build_tui_agent_loop``, which constructs a provider, a session manager, a
    cron service and a plugin registry before it can be driven -- standing all of
    that up would test the mocks. What can go wrong here is narrower: the call
    being moved out of the startup path again, or losing ``silent``, which would
    print template chatter into a terminal this process is drawing a UI on.
    """
    import ast

    import raven.cli.tui_commands as tui

    tree = ast.parse(Path(tui.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_build_tui_agent_loop")
    calls = [
        n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "sync_workspace_templates"
    ]

    assert len(calls) == 1, "the startup builder must bootstrap the workspace exactly once"
    assert any(kw.arg == "silent" and kw.value.value is True for kw in calls[0].keywords)


def test_existing_files_are_not_overwritten(tmp_path: Path) -> None:
    """A workspace already carrying an edited TOOLS.md must keep it: startup is not
    a reset, and overwriting one would change what every later turn is shown."""
    workspace = _home(tmp_path) / "workspace"
    (workspace / "TOOLS.md").write_text("mine", encoding="utf-8")

    created = sync_workspace_templates(workspace, silent=True)

    assert "TOOLS.md" not in created
    assert (workspace / "TOOLS.md").read_text(encoding="utf-8") == "mine"
