"""Isolated home and activation layer (A) of the agent package validator."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from evolver.validator.activation import check_activation, check_rendered, judge_a4, judge_a5, run_probe
from evolver.validator.home import build_isolated_home, scrubbed_env
from evolver.validator.report import ValidatorEnvironmentError
from evolver.validator.snapshot import take_snapshot
from tests.evolver_validator_stub import make_stub_agent, scaffold_template

FLASH = "deepseek/deepseek-flash"


def _host_config(path: Path, **extra) -> Path:
    data = {"providers": {"openrouter": {"apiKey": "sk-test"}}, **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _home(tmp_path: Path, **extra):
    snap = take_snapshot(make_stub_agent(tmp_path / "src", mode="ok"), tmp_path / "run", engine=None)
    return build_isolated_home(
        tmp_path / "run", snap, host_config=_host_config(tmp_path / "host.json", **extra), model=FLASH
    )


def test_home_config_carries_model_providers_and_memory_off(tmp_path):
    home = _home(tmp_path)
    config_path = home.path / "config.json"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assert cfg["agents"]["defaults"]["model"] == FLASH
    assert cfg["memory"]["backend"] is None
    assert cfg["providers"] == {"openrouter": {"apiKey": "sk-test"}}
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert home.path == tmp_path / "run" / "home"
    assert (home.agent_dir / "subagent.json").is_file()
    assert home.agent_dir == home.path / "agents" / "stub-agent"
    assert home.env["RAVEN_HOME"] == str(home.path)


def test_only_lendable_blocks_are_copied_from_host_config(tmp_path):
    tools = {
        "restrictToWorkspace": False,
        "web": {"providers": {"serper": {"apiKey": "s"}}},
        "media": {"image": {"x": 1}},
    }
    home = _home(tmp_path, permissions={"mode": "full"}, plugins={"disabled": ["x"]}, tools=tools)
    cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
    assert set(cfg) == {"agents", "providers", "memory", "tools"}
    assert cfg["tools"] == {"web": tools["web"], "media": tools["media"]}


@pytest.mark.parametrize(
    "host, provider",
    [
        ({"providers": {"deepseek": {"apiKey": "k", "models": [FLASH]}, "openrouter": {"apiKey": "k"}}}, "deepseek"),
        (
            {
                "providers": {"custom": {"apiKey": "k"}, "deepseek": {"apiKey": "k"}},
                "agents": {"defaults": {"model": FLASH, "provider": "custom"}},
            },
            "custom",
        ),
        ({"providers": {"deepseek": {"apiKey": "k"}, "openrouter": {"apiKey": "k"}}}, "deepseek"),
        ({"providers": {"openrouter": {"apiKey": "k"}}}, ""),
    ],
)
def test_home_names_the_provider_for_the_model(tmp_path, host, provider):
    snap = take_snapshot(make_stub_agent(tmp_path / "src", mode="ok"), tmp_path / "run", engine=None)
    (tmp_path / "host.json").write_text(json.dumps(host), encoding="utf-8")
    home = build_isolated_home(tmp_path / "run", snap, host_config=tmp_path / "host.json", model=FLASH)
    cfg = json.loads((home.path / "config.json").read_text(encoding="utf-8"))
    assert cfg["agents"]["defaults"].get("provider", "") == provider == home.provider


def test_route_targets_are_installed_beside_the_folder(tmp_path):
    shipped = tmp_path / "shipped"
    target = make_stub_agent(shipped / "other", mode="ok", name="Stub-Target").rename(shipped / "other" / "stub-target")
    folder = make_stub_agent(tmp_path / "src", mode="ok", routes=[{"to": "Stub-Target", "owes": ".txt"}])
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    home = build_isolated_home(
        tmp_path / "run",
        snap,
        host_config=_host_config(tmp_path / "host.json"),
        model=FLASH,
        shipped_root=shipped / "other",
    )
    assert (home.path / "agents" / target.name / "subagent.json").is_file()


def test_missing_host_config_is_environment_failure(tmp_path):
    snap = take_snapshot(make_stub_agent(tmp_path / "src", mode="ok"), tmp_path / "run", engine=None)
    with pytest.raises(ValidatorEnvironmentError, match="host config"):
        build_isolated_home(tmp_path / "run", snap, host_config=tmp_path / "nope.json", model=FLASH)


def test_missing_providers_is_environment_failure(tmp_path):
    snap = take_snapshot(make_stub_agent(tmp_path / "src", mode="ok"), tmp_path / "run", engine=None)
    (tmp_path / "host.json").write_text(json.dumps({"agents": {}}), encoding="utf-8")
    with pytest.raises(ValidatorEnvironmentError, match="providers"):
        build_isolated_home(tmp_path / "run", snap, host_config=tmp_path / "host.json", model=FLASH)


def test_scrub_removes_agent_prefix_and_raven_vars():
    env = scrubbed_env(
        {"CODE_API_KEY": "k", "CODE_STATE_ROOT": "/s", "RAVEN_HOME": "/real", "RAVEN_X": "1", "PATH": "/bin"},
        folder_name="raven-code",
        home=Path("/iso"),
    )
    assert env == {"PATH": "/bin", "RAVEN_HOME": "/iso"}


def test_scrub_uses_folder_prefix_not_manifest_name():
    env = scrubbed_env({"CODE_API_KEY": "k", "RAVEN_CODE_API_KEY": "k2"}, folder_name="raven-code", home=Path("/iso"))
    assert env == {"RAVEN_HOME": "/iso"}
    kept = scrubbed_env({"DEMO_AGENT_API_KEY": "k"}, folder_name="raven-code", home=Path("/iso"))
    assert kept == {"DEMO_AGENT_API_KEY": "k", "RAVEN_HOME": "/iso"}


def _failed(layer) -> set[str]:
    return {r.rule for r in layer.results if r.status == "fail"}


def _stub_home(tmp_path: Path, **stub):
    folder = make_stub_agent(tmp_path / "src", **stub)
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    home = build_isolated_home(tmp_path / "run", snap, host_config=_host_config(tmp_path / "host.json"), model=FLASH)
    return home, snap


def _stub_pids(home: Path) -> set[str]:
    """Stub servers started for this test's isolated home (parallel tests start their own)."""
    out = subprocess.run(["pgrep", "-f", "acp_stub_server.py"], capture_output=True, text=True).stdout
    mine = set()
    for pid in out.split():
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"RAVEN_HOME={home}".encode() in environ:
            mine.add(pid)
    return mine


def test_discovery_matches_manifest_name(tmp_path):
    home, snap = _stub_home(tmp_path, mode="ok", name="Stub-Agent")
    layer, rendered = check_activation(home, snap, model=FLASH)
    assert layer.passed, [r for r in layer.results if r.status == "fail"]
    assert {r.rule for r in layer.results} >= {"A1", "A2", "A3", "A4", "A5"}
    assert rendered == {"model": FLASH, "provider": home.provider, "memory_backend": None}
    assert (home.path / "rendered.json").is_file()


def test_launcher_refusing_for_a_missing_key_is_environment_failure(tmp_path):
    home, snap = _stub_home(tmp_path, mode="ok", refuse="error: STUB_AGENT_API_KEY is not set")
    with pytest.raises(ValidatorEnvironmentError, match="API_KEY"):
        check_activation(home, snap, model=FLASH)


def test_engine_not_importable_fails_a1_with_reason(tmp_path):
    folder = make_stub_agent(tmp_path / "src", mode="ok")
    manifest = json.loads((folder / "subagent.json").read_text(encoding="utf-8"))
    manifest["engine"] = {"package": "no_such_engine_pkg_xyz", "wheel": "no-such-engine-xyz"}
    (folder / "subagent.json").write_text(json.dumps(manifest), encoding="utf-8")
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    home = build_isolated_home(tmp_path / "run", snap, host_config=_host_config(tmp_path / "host.json"), model=FLASH)
    layer, _ = check_activation(home, snap, model=FLASH)
    assert _failed(layer) == {"A1"}
    assert any("no-such-engine-xyz" in r.message or "no_such_engine_pkg_xyz" in r.message for r in layer.results)


def test_handshake_rejected_fails_a2(tmp_path):
    home, snap = _stub_home(tmp_path, mode="reject_init")
    layer, _ = check_activation(home, snap, model=FLASH)
    assert _failed(layer) == {"A2"}
    assert not _stub_pids(home.path)


def test_launcher_left_running_is_terminated(tmp_path):
    home, snap = _stub_home(tmp_path, mode="ok")
    check_activation(home, snap, model=FLASH)
    assert not _stub_pids(home.path)


@pytest.mark.parametrize(
    "rendered, rule_fails",
    [
        ({"agents": {"defaults": {"model": FLASH}}, "memory": {"backend": None}}, False),
        ({"agents": {"defaults": {"model": "anthropic/claude-opus-5"}}, "memory": {"backend": None}}, True),
        ({"agents": {"defaults": {"model": FLASH}}, "memory": {"backend": "everos"}}, True),
        ({"agents": {"defaults": {"model": FLASH}}}, True),
    ],
)
def test_check_rendered(rendered, rule_fails):
    results = check_rendered(rendered, model=FLASH, provider=None)
    assert any(r.status == "fail" for r in results) is rule_fails
    assert {r.rule for r in results} == {"A3"}


def _scaffold_rendered(tmp_path: Path, *, keep_slice: bool) -> tuple[Path, Path]:
    folder = scaffold_template(tmp_path / "scaffold")
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    if not keep_slice:
        config["plugins"]["config"].pop("demo-agent-flow")
    config["plugins"]["dirs"] = [str(folder / "plugins")]
    config.setdefault("agents", {}).setdefault("defaults", {})["model"] = FLASH
    config["memory"] = {"backend": None}
    path = tmp_path / "rendered.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, folder


DECLARED = {"demo-agent-flow": {"tools": ["demo_agent_hello"], "hooks": ["demo_agent_flow"]}}


def test_probe_reports_scaffold_contributions_registered(tmp_path):
    config, _ = _scaffold_rendered(tmp_path, keep_slice=True)
    probe = run_probe(config, ["demo-agent-flow"], env=dict(os.environ), workspace=tmp_path / "ws")
    assert probe["plugins"]["demo-agent-flow"]["tools"] == {"demo_agent_hello": "registered"}
    assert probe["plugins"]["demo-agent-flow"]["hooks"] == {"demo_agent_flow": "registered"}
    assert all(r.status == "pass" for r in judge_a4(probe, DECLARED))


def test_probe_with_slice_disabled_declines(tmp_path):
    config, _ = _scaffold_rendered(tmp_path, keep_slice=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["plugins"]["config"]["demo-agent-flow"]["enabled"] = False
    config.write_text(json.dumps(data), encoding="utf-8")
    probe = run_probe(config, ["demo-agent-flow"], env=dict(os.environ), workspace=tmp_path / "ws")
    assert probe["plugins"]["demo-agent-flow"]["tools"] == {"demo_agent_hello": "declined"}
    assert all(r.status == "pass" for r in judge_a5(probe, DECLARED))


def test_a4_fails_on_a_failed_or_missing_contribution():
    failed = {
        "plugins": {"demo-agent-flow": {"tools": {"demo_agent_hello": "failed: boom"}, "hooks": {}}},
        "activation_failures": [],
    }
    assert {r.rule for r in judge_a4(failed, DECLARED) if r.status == "fail"} == {"A4"}
    missing = {"plugins": {"demo-agent-flow": {"tools": {}, "hooks": {}}}, "activation_failures": []}
    assert any(r.status == "fail" and "not discovered" in r.message for r in judge_a4(missing, DECLARED))


def test_a5_fails_when_a_contribution_registers_without_its_slice():
    probe = {
        "plugins": {"demo-agent-flow": {"tools": {"demo_agent_hello": "registered"}, "hooks": {}}},
        "activation_failures": [],
    }
    assert {r.rule for r in judge_a5(probe, DECLARED) if r.status == "fail"} == {"A5"}


def test_check_rendered_requires_the_named_provider():
    rendered = {"agents": {"defaults": {"model": FLASH, "provider": "openrouter"}}, "memory": {"backend": None}}
    assert any(r.status == "fail" for r in check_rendered(rendered, model=FLASH, provider="deepseek"))
    assert not any(r.status == "fail" for r in check_rendered(rendered, model=FLASH, provider="openrouter"))
