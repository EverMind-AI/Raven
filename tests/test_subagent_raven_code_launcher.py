"""Raven-Code keeps runtime state in the host Agent home, not the Working directory."""

from __future__ import annotations

import importlib.util
import json
import os
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
    monkeypatch.delenv("CODE_ACP_HOME", raising=False)
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
    # Computed from HERE at import time, so it has to be repointed too: the
    # real folder ships a modes/ directory, and a test that wants one builds it.
    module.MODES_DIR = isolated_launcher_home / "modes"
    return module, host_home


def test_the_engine_is_not_homed_inside_the_host_agent_home(launcher) -> None:
    """The host hands its own Agent home over as a session's working directory,
    and a raven engine refuses to work in a directory containing its own home.
    An engine homed under the host's could never be dispatched to at all --
    while `Connect` still passed, because capability probing opens its session
    on a temporary directory."""
    from raven.agent.workdir import validate_override

    module, host_home = launcher
    host_workspace = host_home / "workspace"

    assert host_workspace not in module.acp_home().parents
    # The guard that was refusing, asked directly.
    validate_override(host_workspace, module.acp_home())


def test_the_engine_home_stays_outside_a_custom_host_agent_home(launcher) -> None:
    """The check is against the CONFIGURED host Agent home, not the default one.

    `host_agent_home()` honours `agents.defaults.workspace`, so an operator who
    points it at `$RAVEN_HOME` puts the raven data directory back inside the very
    tree the host hands over as a working directory -- and the refusal returns.
    """
    from raven.agent.workdir import validate_override

    module, host_home = launcher
    (host_home / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(host_home)}}}), encoding="utf-8"
    )

    assert module.host_agent_home() == host_home
    assert host_home not in module.acp_home().parents
    validate_override(module.host_agent_home(), module.acp_home())


def test_two_raven_instances_do_not_share_an_engine_home(tmp_path: Path, monkeypatch) -> None:
    """`RAVEN_HOME` is what tells two instances on one machine apart.

    The sibling fallback puts its directory BESIDE the Agent home, which is a
    place the siblings can reach: without the instance in the name, `/srv/a` and
    `/srv/b` both land on `/srv/.raven-code`, and the ACP session store and the
    allocation base under it -- one instance's conversations -- are shared with
    the other.
    """
    homes = {}
    for name in ("instance-a", "instance-b"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps({"agents": {"defaults": {"workspace": str(home)}}}), encoding="utf-8"
        )
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.delenv("CODE_ACP_HOME", raising=False)
        monkeypatch.delenv("CODE_STATE_ROOT", raising=False)
        spec = importlib.util.spec_from_file_location(f"raven_code_{name}", _LAUNCHER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        homes[name] = module.acp_home()

    assert homes["instance-a"] != homes["instance-b"]


def test_two_instances_named_alike_are_still_told_apart(tmp_path: Path, monkeypatch) -> None:
    """The directory's own name is in the path for legibility, and cannot carry
    the identity on its own: two `raven` homes under different parents are two
    instances."""
    homes = {}
    for parent in ("p1", "p2"):
        home = tmp_path / parent / "raven"
        home.mkdir(parents=True)
        (home / "config.json").write_text(
            json.dumps({"agents": {"defaults": {"workspace": str(home)}}}), encoding="utf-8"
        )
        monkeypatch.setenv("RAVEN_HOME", str(home))
        monkeypatch.delenv("CODE_ACP_HOME", raising=False)
        spec = importlib.util.spec_from_file_location(f"raven_code_{parent}", _LAUNCHER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        homes[parent] = module.acp_home()

    assert homes["p1"] != homes["p2"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="an effective uid that overrides the write bit is told yes, which the check documents",
)
def test_a_sibling_fallback_that_cannot_be_created_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beside the host Agent home is not always a place this run may write.

    An operator who points `agents.defaults.workspace` at `~` puts the fallback
    at `/Users`, which is correctly outside that home and still fails the first
    `mkdir` with a bare `PermissionError` naming nothing to act on.
    """
    base = tmp_path / "unwritable"
    base.mkdir()
    home = base / "raven"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(home)}}}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.delenv("CODE_ACP_HOME", raising=False)
    monkeypatch.delenv("CODE_STATE_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("raven_code_unwritable", _LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base.chmod(0o500)

    try:
        with pytest.raises(SystemExit) as refusal:
            module.acp_home()
        # Asked, not tried: the destination's absence is what tells the next run
        # whether it already has a home there.
        assert [entry.name for entry in base.iterdir()] == ["raven"]
    finally:
        base.chmod(0o700)

    assert "CODE_ACP_HOME" in str(refusal.value)


def test_a_previous_engine_home_is_adopted(launcher) -> None:
    """Moving the home moves the runtime data directory, and the `sessions/` tree
    with it -- while the host's registry still holds those session ids. Left
    behind, the next turn is answered `unknown session` and a conversation the
    reader was in the middle of starts again from nothing."""
    module, _host_home = launcher
    legacy = module.state_root() / "acp"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "acp-1.jsonl").write_text("{}", encoding="utf-8")

    dest = module.acp_home()
    module.adopt_legacy_acp_home(dest)

    assert (dest / "sessions" / "acp-1.jsonl").read_text(encoding="utf-8") == "{}"
    assert not legacy.exists()


def test_a_home_already_in_the_new_place_is_never_written_over(launcher) -> None:
    """It is the live one."""
    module, _host_home = launcher
    legacy = module.state_root() / "acp"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "old.jsonl").write_text("old", encoding="utf-8")
    dest = module.acp_home()
    (dest / "sessions").mkdir(parents=True)
    (dest / "sessions" / "live.jsonl").write_text("live", encoding="utf-8")

    module.adopt_legacy_acp_home(dest)

    assert (dest / "sessions" / "live.jsonl").read_text(encoding="utf-8") == "live"
    assert not (dest / "sessions" / "old.jsonl").exists()
    assert legacy.is_dir()


def test_a_move_that_fails_is_reported_and_the_run_continues(launcher, monkeypatch: pytest.MonkeyPatch) -> None:
    """The degrade the docstring promises has to be able to run.

    The log file lives in the new home, which does not exist yet when the
    adoption is attempted -- on purpose. Reporting the failure must not be a
    second way to die from inside the handler that exists to survive it.
    """
    module, _host_home = launcher
    legacy = module.state_root() / "acp"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "old.jsonl").write_text("old", encoding="utf-8")
    dest = module.acp_home()
    module._LOG_FILE = dest / "launcher.log"
    assert not dest.exists()

    def _refuse(*args, **kwargs):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr(module.Path, "rename", _refuse)
    monkeypatch.setattr(module.shutil, "move", _refuse)

    module.adopt_legacy_acp_home(dest)

    assert (legacy / "sessions" / "old.jsonl").read_text(encoding="utf-8") == "old"
    recorded = (dest / "launcher.log").read_text(encoding="utf-8")
    assert "could not move the previous ACP home" in recorded
    assert str(legacy) in recorded


def test_the_engine_home_can_be_moved_by_hand(launcher) -> None:
    module, _host_home = launcher
    import os

    os.environ["CODE_ACP_HOME"] = "/tmp/somewhere-else"
    try:
        assert module.acp_home() == Path("/tmp/somewhere-else")
    finally:
        del os.environ["CODE_ACP_HOME"]


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
    assert rendered.parent == host_home / "subagent_sessions" / "raven-code" / "acp"
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


# --- effort modes ---------------------------------------------------------------
#
# The three effort tiers reach the agent as an ACP mode catalogue: `config.json`
# is the baseline (`high`, today's behaviour) and `modes/<id>.json` carries the
# one knob a tier moves. The launcher declares, the agent composes per session.

_MODES_SOURCE = {
    "providers": {"custom": {}},
    "agents": {"defaults": {"provider": "custom", "model": "model", "reasoningEffort": "high"}},
}


def _modes_source(tmp_path: Path) -> Path:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_MODES_SOURCE), encoding="utf-8")
    return source


def _ship_overlays(module, *names: str, body: dict | None = None) -> None:
    module.MODES_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        overlay = body if body is not None else {"agents": {"defaults": {"reasoningEffort": name}}}
        (module.MODES_DIR / f"{name}.json").write_text(json.dumps(overlay), encoding="utf-8")


def test_the_modes_are_declared_for_the_agent_to_compose(launcher, tmp_path: Path) -> None:
    """Diffs, not merged blocks: the baseline stays untouched and is the `high`
    entry, which is why it needs no overlay file and carries no effort key."""
    module, _ = launcher
    _ship_overlays(module, "low", "max")

    rendered = module.render_config(_modes_source(tmp_path), tmp_path / "state", mode="low")

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["acp"]["defaultMode"] == "low"
    assert list(data["acp"]["modes"]) == ["low", "high", "max"]
    assert data["acp"]["modes"]["low"]["reasoningEffort"] == "low"
    assert data["acp"]["modes"]["max"]["reasoningEffort"] == "max"
    assert "reasoningEffort" not in data["acp"]["modes"]["high"]
    for entry in data["acp"]["modes"].values():
        assert entry["name"] and entry["description"]
    assert data["agents"]["defaults"]["reasoningEffort"] == "high"


def test_without_a_mode_flag_sessions_start_on_the_baseline(launcher, tmp_path: Path) -> None:
    module, _ = launcher
    _ship_overlays(module, "low", "max")

    rendered = module.render_config(_modes_source(tmp_path), tmp_path / "state")

    assert json.loads(rendered.read_text(encoding="utf-8"))["acp"]["defaultMode"] == "high"


def test_a_folder_with_no_modes_directory_declares_none(launcher, tmp_path: Path) -> None:
    """The degradation path: no catalogue in the rendered config, which leaves
    the agent's session/set_mode method-not-found -- and keeps a launcher paired
    with an older vendored tree, whose schema knows no `acp` key, launchable."""
    module, _ = launcher

    rendered = module.render_config(_modes_source(tmp_path), tmp_path / "state")

    assert "acp" not in json.loads(rendered.read_text(encoding="utf-8"))


def test_a_mode_without_an_overlay_file_refuses_to_launch(launcher, tmp_path: Path) -> None:
    module, _ = launcher
    _ship_overlays(module, "low")

    with pytest.raises(SystemExit, match="no overlay for mode"):
        module.render_config(_modes_source(tmp_path), tmp_path / "state", mode="max")


def test_an_overlay_with_a_foreign_top_level_key_refuses_to_launch(launcher, tmp_path: Path) -> None:
    module, _ = launcher
    _ship_overlays(module, "low", body={"agents": {"defaults": {"reasoningEffort": "low"}}, "tools": {}})

    with pytest.raises(SystemExit, match="unsupported top-level key"):
        module.render_config(_modes_source(tmp_path), tmp_path / "state")


def test_an_overlay_moving_anything_but_the_effort_refuses_to_launch(launcher, tmp_path: Path) -> None:
    """The tiers differ in effort and nothing else. A knob the agent's mode
    profile does not carry would be declared and then silently ignored."""
    module, _ = launcher
    _ship_overlays(module, "low", body={"agents": {"defaults": {"reasoningEffort": "low", "maxToolIterations": 60}}})

    with pytest.raises(SystemExit, match="maxToolIterations"):
        module.render_config(_modes_source(tmp_path), tmp_path / "state")


def test_an_overlay_that_moves_nothing_refuses_to_launch(launcher, tmp_path: Path) -> None:
    module, _ = launcher
    _ship_overlays(module, "low", body={"agents": {"defaults": {}}})

    with pytest.raises(SystemExit, match="reasoningEffort"):
        module.render_config(_modes_source(tmp_path), tmp_path / "state")


def test_the_shipped_overlays_carry_exactly_the_effort() -> None:
    """The real modes/ files: one knob each, named after the tier."""
    for name in ("low", "max"):
        overlay = json.loads((_LAUNCHER.parent / "modes" / f"{name}.json").read_text(encoding="utf-8"))
        assert overlay == {"agents": {"defaults": {"reasoningEffort": name}}}, name


def test_the_baseline_is_the_high_tier_and_every_tier_is_labelled(launcher) -> None:
    module, _ = launcher
    assert module.BASELINE_MODE == "high"
    assert set(module.MODE_LABELS) == {"low", "high", "max"}
    baseline = json.loads((_LAUNCHER.parent / "config.json").read_text(encoding="utf-8"))
    assert baseline["agents"]["defaults"]["reasoningEffort"] == "high"


def test_acp_start_passes_the_mode_flag_into_the_rendered_config(
    launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = launcher
    _ship_overlays(module, "low", "max")
    source = _modes_source(tmp_path)
    checkout = tmp_path / "checkout"
    raven_bin = checkout / ".venv" / "bin" / "raven"
    raven_bin.parent.mkdir(parents=True)
    raven_bin.write_text("", encoding="utf-8")
    launched: dict[str, object] = {}

    class _Process:
        def __init__(self, argv, *, cwd, env=None):
            launched["config"] = json.loads(Path(argv[3]).read_text(encoding="utf-8"))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", _Process)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--acp", "--mode", "max", "--checkout", str(checkout), "--config", str(source)]
    )

    assert module.main() == 0
    assert launched["config"]["acp"]["defaultMode"] == "max"
    assert set(launched["config"]["acp"]["modes"]) == {"low", "high", "max"}


def test_the_mode_flag_is_refused_off_the_acp_path(launcher, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tier is a session-level choice the ACP client makes; the one-task CLI
    path has no session to put it on, and accepting it there would be a flag
    that silently does nothing."""
    module, _ = launcher
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "x", "--mode", "low"])

    with pytest.raises(SystemExit, match="--mode"):
        module.main()
