"""Tests for working-directory selection by local surface assembly."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from loguru import logger

from raven.agent.workdir import default_channel_root
from raven.config.schema import Config
from raven.core.engine_stack import build_local_sessions
from raven.utils.paths import project_slug


@pytest.fixture
def local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    user_home = tmp_path / "user"
    agent_home = user_home / ".raven" / "workspace"
    agent_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: user_home)
    config = Config()
    config.agents.defaults.workspace = str(agent_home)
    return config


@pytest.mark.parametrize(
    "relative",
    ["..", ".", "user_memory", "user_memory/nested", "skills", "skills/nested", "sessions", "sessions/nested"],
)
def test_protected_launch_directory_uses_per_channel_fallback(
    local_config: Config, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    agent_home = local_config.workspace_path
    launch = (agent_home / relative).resolve()
    launch.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(launch)
    launch = Path.cwd()
    warning = Mock()
    monkeypatch.setattr(logger, "warning", warning)

    sessions, resolver = build_local_sessions(local_config, workspace=None)
    root = default_channel_root(agent_home)

    for channel in ("tui", "web", "cli", "a2a"):
        assert resolver.resolve(f"{channel}:one", create=False) == root / channel
        assert resolver.resolve(f"{channel}:two", create=False) == root / channel
    assert resolver.mount_root() == root
    assert not root.exists()
    assert sessions.project_dir == launch
    assert sessions.project_slug == project_slug(launch)
    warning.assert_called_once()
    message, *args = warning.call_args.args
    assert str(launch) in message.format(*args)
    assert str(root) in message.format(*args)
    assert resolver.resolve("tui:one").is_dir()


@pytest.mark.parametrize("relative", ["project", ".", ".."])
def test_project_and_user_home_launch_directories_are_preserved(
    local_config: Config, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    launch = (Path.home() / relative).resolve()
    launch.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(launch)
    launch = Path.cwd()
    warning = Mock()
    monkeypatch.setattr(logger, "warning", warning)

    _, resolver = build_local_sessions(local_config, workspace=None)

    assert resolver.resolve("tui:one", create=False) == launch
    assert resolver.resolve("web:two", create=False) == launch
    assert resolver.mount_root() == launch
    warning.assert_not_called()


def test_explicit_workdir_wins_when_the_launch_directory_is_protected(
    local_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(local_config.workspace_path)
    project = Path.home() / "project"
    project.mkdir()
    warning = Mock()
    monkeypatch.setattr(logger, "warning", warning)

    _, resolver = build_local_sessions(local_config, workspace=str(project))

    assert resolver.resolve("tui:one", create=False) == project.resolve()
    assert resolver.mount_root() == project.resolve()
    warning.assert_not_called()


def test_invalid_explicit_workdir_is_rejected_instead_of_falling_back(
    local_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(local_config.workspace_path)

    with pytest.raises(ValueError, match="agent home"):
        build_local_sessions(local_config, workspace=str(local_config.workspace_path))


def test_session_override_wins_over_the_protected_launch_fallback(
    local_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(local_config.workspace_path)
    project = Path.home() / "project"
    project.mkdir()
    sessions, resolver = build_local_sessions(local_config, workspace=None)
    sessions.get_or_create("tui:pinned").metadata["workdir"] = str(project)

    assert resolver.resolve("tui:pinned", create=False) == project.resolve()
    assert resolver.resolve("tui:other", create=False) == default_channel_root(local_config.workspace_path) / "tui"


def test_agent_home_equal_to_user_home_is_still_protected(
    local_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_config.agents.defaults.workspace = str(Path.home())
    monkeypatch.chdir(Path.home())

    _, resolver = build_local_sessions(local_config, workspace=None)

    assert resolver.resolve("tui:one", create=False) == default_channel_root(local_config.workspace_path) / "tui"


def test_instance_outside_user_home_also_uses_the_fallback(
    local_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = Path.home().parent / "another-instance"
    agent_home = instance / "workspace"
    agent_home.mkdir(parents=True)
    local_config.agents.defaults.workspace = str(agent_home)
    monkeypatch.chdir(instance)

    _, resolver = build_local_sessions(local_config, workspace=None)

    assert resolver.resolve("tui:one", create=False) == instance / "tmp" / "tui"
