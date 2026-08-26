"""Unit tests for the host-side Raven-Research ACP launcher (subagents/raven-research/run.py).

The launcher's one job is rendering a config Raven-X can load: `.env` secrets
merged into their slots, the workspace pinned under STATE_ROOT, and leftover
rendered files swept by pid-liveness. The exec itself is not unit-testable
from here; the manifest test pins the registration that reaches it.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-research" / "run.py"


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The launcher module, repointed at tmp_path so no real `.env`, host
    config or state root can leak into a test."""
    spec = importlib.util.spec_from_file_location("raven_research_launcher", _LAUNCHER)
    assert spec and spec.loader
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "HERE", tmp_path)
    monkeypatch.setattr(launcher, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(launcher, "HOST_CONFIG", tmp_path / "host-config.json")
    for name in ("RESEARCH_API_KEY", "RESEARCH_SERPER_API_KEY", "RESEARCH_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return launcher


def _source(tmp_path: Path, extra: dict | None = None) -> Path:
    config = {
        "providers": {"custom": {"apiBase": "https://example.invalid/v1"}},
        "agents": {"defaults": {"provider": "custom", "model": "own-model", "maxToolIterations": 150}},
    }
    for key, value in (extra or {}).items():
        config.setdefault("agents", {}).setdefault("defaults", {})[key] = value
    source = tmp_path / "config.json"
    source.write_text(json.dumps(config), encoding="utf-8")
    return source


def test_secrets_land_in_their_slots_and_the_source_stays_clean(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "k-serper")
    source = _source(tmp_path)

    rendered = mod.render_config(source)

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["providers"]["custom"]["apiKey"] == "k-llm"
    assert data["tools"]["web"]["search"]["apiKey"] == "k-serper"
    assert "k-llm" not in source.read_text(encoding="utf-8")


def test_the_rendered_file_is_private_and_named_after_this_pid(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid in the name is the contract `sweep_stale_renders` reads: the
    exec hands this pid to the server, so liveness of the pid is liveness of
    the server that read the file."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    rendered = mod.render_config(_source(tmp_path))

    assert rendered.parent == mod.STATE_ROOT
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert (rendered.stat().st_mode & 0o777) == 0o600


def test_the_workspace_is_pinned_under_the_state_root(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The schema default is the host raven's own ~/.raven/workspace, which
    this agent must not share."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    rendered = mod.render_config(_source(tmp_path))

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["workspace"] == str(mod.STATE_ROOT / "workspace")


def test_a_workspace_the_config_declares_is_left_alone(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = _source(tmp_path, extra={"workspace": "/elsewhere/ws"})

    rendered = mod.render_config(source)

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["workspace"] == "/elsewhere/ws"


def test_no_key_anywhere_refuses_to_launch(mod, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="RESEARCH_API_KEY"):
        mod.render_config(_source(tmp_path))


def test_the_hosts_whole_provider_block_is_inherited_without_its_limits(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied wholesale, never matched by name; the agent's own operating
    limits stay its own."""
    mod.HOST_CONFIG.write_text(
        json.dumps(
            {
                "providers": {"host": {"apiBase": "https://host.invalid/v1", "apiKey": "host-key"}},
                "agents": {"defaults": {"provider": "host", "model": "host-model", "maxToolIterations": 9}},
            }
        ),
        encoding="utf-8",
    )

    rendered = mod.render_config(_source(tmp_path))

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["providers"] == {"host": {"apiBase": "https://host.invalid/v1", "apiKey": "host-key"}}
    defaults = data["agents"]["defaults"]
    assert defaults["provider"] == "host" and defaults["model"] == "host-model"
    assert defaults["maxToolIterations"] == 150


def test_sweep_removes_only_files_whose_pid_is_gone(mod) -> None:
    state = mod.STATE_ROOT
    state.mkdir(parents=True)
    dead = state / f".config.rendered.{2**22 + 12345}.json"
    alive = state / f".config.rendered.{os.getpid()}.json"
    junk = state / ".config.rendered.notapid.json"
    for f in (dead, alive, junk):
        f.write_text("{}", encoding="utf-8")

    mod.sweep_stale_renders()

    assert not dead.exists()
    assert alive.exists()
    assert not junk.exists()


def test_the_manifest_registers_the_acp_transport() -> None:
    """What discovery reads: kind acp, the launcher as the server command, and
    the cwd pinned so the pool's launch key cannot churn with the task
    workspace. The cli declaration fields must be gone -- an acp entry
    negotiates them in the initialize handshake instead."""
    manifest = json.loads((_LAUNCHER.parent / "subagent.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "acp"
    assert manifest["command"].endswith("run.py")
    assert manifest["cwd"] == "{SUBAGENT_DIR}"
    assert manifest["readyTimeoutMs"] > 0
    for field in ("resumeCommand", "idSource", "transcriptFormat", "readsLocalFiles", "stateful"):
        assert field not in manifest
