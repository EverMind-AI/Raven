"""Raven-Code keeps runtime state in the host Agent home, not the Working directory."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-code" / "run.py"


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    (host_home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"host": {"apiKey": "host-key"}},
                "agents": {"defaults": {"workspace": "~/.raven/workspace"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RAVEN_HOME", str(host_home))
    monkeypatch.setenv("CODE_API_KEY", "code-key")
    monkeypatch.delenv("CODE_STATE_ROOT", raising=False)
    monkeypatch.delenv("CODE_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("CODE_JINA_API_KEY", raising=False)
    monkeypatch.delenv("CODE_RUN_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "login-home")

    spec = importlib.util.spec_from_file_location("raven_code_launcher_hotfix", _LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    isolated_launcher_home = tmp_path / "launcher-home"
    isolated_launcher_home.mkdir()
    module.HERE = isolated_launcher_home
    return module, host_home


def test_default_state_root_follows_the_host_agent_home(launcher) -> None:
    module, host_home = launcher

    assert module.state_root() == host_home / "workspace" / "subagent_sessions" / "raven-code"


def test_explicit_host_agent_home_controls_the_state_root(launcher, tmp_path: Path) -> None:
    module, host_home = launcher
    configured_home = tmp_path / "configured-agent-home"
    (host_home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"host": {"apiKey": "host-key"}},
                "agents": {"defaults": {"workspace": str(configured_home)}},
            }
        ),
        encoding="utf-8",
    )

    assert module.host_agent_home() == configured_home
    assert module.state_root() == configured_home / "subagent_sessions" / "raven-code"


def test_state_root_override_supports_relative_and_absolute_paths(
    launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, host_home = launcher
    monkeypatch.setenv("CODE_STATE_ROOT", "custom-state")
    assert module.state_root() == host_home / "workspace" / "custom-state"

    absolute = tmp_path / "absolute-state"
    monkeypatch.setenv("CODE_STATE_ROOT", str(absolute))
    assert module.state_root() == absolute


def test_acp_start_renders_below_agent_home_without_using_the_working_directory(
    launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, host_home = launcher
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"custom": {}},
                "agents": {"defaults": {"provider": "custom", "model": "model"}},
            }
        ),
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    raven_bin = checkout / ".venv" / "bin" / "raven"
    raven_bin.parent.mkdir(parents=True)
    raven_bin.write_text("", encoding="utf-8")
    launched: dict[str, object] = {}

    class _Process:
        def __init__(self, argv, *, cwd, env=None):
            launched["argv"] = argv
            launched["cwd"] = cwd
            launched["env"] = env
            launched["config"] = json.loads(Path(argv[3]).read_text(encoding="utf-8"))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", _Process)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--acp", "--checkout", str(checkout), "--config", str(source)],
    )

    assert module.main() == 0
    rendered = Path(launched["argv"][3])
    assert rendered.parent == host_home / "workspace" / "subagent_sessions" / "raven-code" / "acp"
    assert launched["cwd"] == str(checkout)
    assert launched["config"]["agents"]["defaults"]["workspace"] == str(rendered.parent)
    # The server is launched with the first-write gate armed: session records in
    # the acp partition, repo owners/worktrees under the state root, one bucket.
    child_env = launched["env"]
    assert child_env is not None
    assert child_env["RAVEN_WORKSPACE_ALLOC_BASE"] == str(rendered.parent)
    assert child_env["RAVEN_WORKSPACE_ALLOC_REPOS"] == str(
        host_home / "workspace" / "subagent_sessions" / "raven-code" / "repos"
    )
    assert child_env["RAVEN_WORKSPACE_STATE_BUCKET"] == "acp"
