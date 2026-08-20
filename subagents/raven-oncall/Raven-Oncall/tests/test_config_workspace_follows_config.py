"""The workspace follows the config file, like every other instance path.

cron store, logs, ops ledgers and (since f071914) traces are all derived from
where the config file sits. The workspace was not: it came from a literal written
inside the config, so an instance's identity was stated twice and the two could
disagree.

They did. New instances are made by copying an existing config and editing it,
and that one line points at whoever it was copied from -- measured three times
over 2026-08-13, most visibly when two ML campaigns and a third instance all
pointed at the same workspace and nothing said so. Sharing a workspace means
sharing sessions and memory, and the embedded memory index takes a lock, so two
of them contend.

Deriving costs nothing in compatibility, twice over: an explicit value still wins,
which is every config file on this machine today and every --workspace override;
and with no --config the derived location is ~/.raven/workspace, the constant it
replaces. What changes is only the case nobody had covered -- a config that says
nothing about a workspace now gets its own instead of the default one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.config.schema import Config


def _config(monkeypatch, tmp_path: Path, workspace: str | None) -> Config:
    monkeypatch.setattr("raven.config.paths.get_config_path",
                        lambda: tmp_path / ".raven-m3" / "config.json")
    c = Config()
    if workspace is not None:
        c.agents.defaults.workspace = workspace
    return c


def test_it_follows_the_config_file_when_nothing_is_written(monkeypatch, tmp_path):
    c = _config(monkeypatch, tmp_path, None)
    assert c.workspace_path == tmp_path / ".raven-m3" / "workspace"


def test_two_instances_do_not_share_a_workspace(monkeypatch, tmp_path):
    seen = set()
    for name in ("m3", "m9"):
        monkeypatch.setattr("raven.config.paths.get_config_path",
                            lambda name=name: tmp_path / f".raven-{name}" / "config.json")
        seen.add(Config().workspace_path)
    assert len(seen) == 2


def test_an_explicit_value_still_wins(monkeypatch, tmp_path):
    """Every config file on this machine writes one, and --workspace sets one."""
    c = _config(monkeypatch, tmp_path, str(tmp_path / "elsewhere"))
    assert c.workspace_path == tmp_path / "elsewhere"


def test_the_default_location_does_not_move(monkeypatch, tmp_path):
    """A user who never passes --config keeps the directory they have."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("raven.config.paths.get_config_path",
                        lambda: tmp_path / ".raven" / "config.json")
    assert Config().workspace_path == tmp_path / ".raven" / "workspace"


def test_an_unresolvable_config_path_falls_back(monkeypatch, tmp_path):
    """A workspace is needed to run at all; it must never be the thing that fails."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom():
        raise RuntimeError("no config path here")

    monkeypatch.setattr("raven.config.paths.get_config_path", _boom)
    assert Config().workspace_path == tmp_path / ".raven" / "workspace"


def test_the_helper_derives_it_too(monkeypatch, tmp_path):
    """get_workspace_path(None) is reached from the CLI paths that take no override."""
    from raven.config.paths import get_workspace_path

    monkeypatch.setattr("raven.config.paths.get_config_path",
                        lambda: tmp_path / ".raven-m9" / "config.json")
    assert get_workspace_path() == tmp_path / ".raven-m9" / "workspace"
    assert get_workspace_path(str(tmp_path / "given")) == tmp_path / "given"
