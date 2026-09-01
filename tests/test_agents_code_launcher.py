"""The agents/ code launcher: rendering, refusals, the seeded guide, the fork exec.

The B-side product must hold the same launch contract as its vendored twin
while the exec target is still the fork engine: secrets merge into a
rendered 0600 config whose parent decides the data dir, the workspace is
pinned to the acp partition, the fork's first-write gate is armed through
the environment, the fork's TOOLS.md wording is seeded byte-for-byte, and
the served process is the fork checkout's own ``raven acp``. The render is
double-audienced -- the fork engine consumes it today, the trunk loader
must already accept it for the later exec-target swap -- so both loaders
round-trip it here. The hermetic tool-face pin (the oncall family's
VENDORED_TOOL_FACE) boards with the swap wave: a trunk loop built from this
render would still lack the code-flow plugin's contributed tools, so no
face equality exists yet to pin.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-code" / "run.py"
FORK = REPO / "subagents" / "raven-code"
FORK_TEMPLATE = FORK / "Raven-main" / "raven" / "templates" / "TOOLS.md"


@pytest.fixture()
def launcher():
    spec = importlib.util.spec_from_file_location("agents_code_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CODE_API_KEY", "sk-own")
    monkeypatch.delenv("CODE_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("CODE_JINA_API_KEY", raising=False)
    monkeypatch.delenv("RAVEN_WORKSPACE_ALLOC_BASE", raising=False)
    monkeypatch.delenv("RAVEN_WORKSPACE_ALLOC_REPOS", raising=False)
    monkeypatch.delenv("RAVEN_WORKSPACE_STATE_BUCKET", raising=False)
    return launcher


# --- byte parity: the one prompt asset and the roster identity --------------


def test_the_carried_guide_is_the_forks_template_byte_for_byte():
    """The fork's TOOLS.md is a whole-file drift (exec sessions, background
    jobs, the 30k spill), not an appended section like oncall's; the product
    carries it as one asset, byte-equal, no respelling list."""
    assert (RUN_PY.parent / "TOOLS_CODE.md").read_bytes() == FORK_TEMPLATE.read_bytes()


def test_the_seeded_guide_is_the_forks_wording(grounded, tmp_path):
    """What lands in the workspace equals what the fork engine writes there.

    Both engines write workspace templates only for files still missing, so
    on a fresh workspace the model-visible text is byte-equal to a fork
    first run today, and stays the fork's after the exec-target swap.
    """
    grounded.render_config(RUN_PY.parent / "config.json")
    seeded = (tmp_path / "state" / "acp" / "TOOLS.md").read_bytes()
    assert seeded == FORK_TEMPLATE.read_bytes()


def test_the_guide_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    guide = tmp_path / "state" / "acp" / "TOOLS.md"
    guide.write_text("operator tuned")
    grounded.render_config(RUN_PY.parent / "config.json")
    assert guide.read_text() == "operator tuned"


def test_the_roster_row_identity_is_the_vendored_twins():
    """The spawn-facing text the host router reads stays byte-identical."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text())
    theirs = json.loads((FORK / "subagent.json").read_text())
    for field in (
        "name",
        "kind",
        "description",
        "owns",
        "command",
        "cwd",
        "readyTimeoutMs",
        "timeout",
        "maxOutputChars",
        "everos",
        "recommendedLlm",
    ):
        assert ours[field] == theirs[field], field
    # This product has no machine routing; mirroring the fork's absence is
    # what keeps the oncall routing red flag inapplicable here.
    assert "runsOnMachines" not in ours
    assert "runsOnMachines" not in theirs


def test_the_everos_identity_agrees_in_all_three_places():
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    row = json.loads((RUN_PY.parent / "subagent.json").read_text())
    slice_ = config["plugins"]["config"]["everos-memory"]
    ids = {
        config["memory"]["userId"],
        config["memory"]["agentId"],
        slice_["user_id"],
        slice_["agent_id"],
        row["everos"]["userId"],
        row["everos"]["agentId"],
    }
    assert ids == {"raven-code"}


# --- the render: secrets, pinning, the gate slice, the two loaders ----------


def test_the_render_merges_secrets_pins_workspace_and_arms_the_gate_slice(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    acp = (tmp_path / "state" / "acp").resolve()
    assert data["providers"]["custom"]["apiKey"] == "sk-own"
    assert data["agents"]["defaults"]["workspace"] == str(acp)
    flow = data["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is True
    assert flow["workspaceGate"] == {
        "allocBase": str(acp),
        "reposRoot": str(tmp_path / "state" / "repos"),
        "stateBucket": "acp",
    }
    assert rendered.parent == acp


def test_the_render_declares_no_plugin_dirs_for_the_fork_engine(grounded):
    """The fork's plugins block forbids fields it does not know, ``dirs``
    included, so declaring the not-yet-existing plugin directory would stop
    the engine at load. The declaration boards with the exec-target swap."""
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert "dirs" not in data["plugins"]


def test_everos_stays_factory_off_through_the_render(grounded):
    """The vendored twin ships everos double-off (backend null plus the
    plugin opt-out); the migrated factory state is the shipped state."""
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["memory"]["backend"] is None
    assert "everos-memory" in data["plugins"]["disabled"]


def test_the_product_config_ships_compaction_enabled(grounded):
    """The trunk factory default is off; this product's slice turns it on,
    which is the fork's shipped posture (its knob defaults on). Pinned from
    the published file and through the trunk loader's reading of the render."""
    from raven.config.loader import load_config

    published = json.loads((RUN_PY.parent / "config.json").read_text())
    assert published["agents"]["defaults"]["compaction"] == {"enabled": True}
    config = load_config(grounded.render_config(RUN_PY.parent / "config.json"))
    assert config.agents.defaults.compaction.enabled is True


def test_optional_keys_fall_back_per_slot_to_the_host_config(grounded, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}}}}))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["search"]["apiKey"] == "host-serper"


def test_the_rendered_file_is_owner_only(grounded):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "anthropic/claude-opus-5"
    extensions = load_raven_config(rendered)
    assert extensions.plugins.disabled == ["everos-memory"]
    assert extensions.plugins.config["code-flow"]["workspaceGate"]["stateBucket"] == "acp"
    assert extensions.skill_forge.rewrite_enabled is False
    assert extensions.skill_forge.llm_gate_enabled is False


def test_the_render_loads_through_the_forks_own_loader(grounded, tmp_path):
    """The render's live consumer this wave is the fork engine; its config
    stack is imported in a subprocess (PYTHONPATH at the fork tree, cwd off
    the repo so the fork wins the import) and must accept the render whole
    -- the pin that keeps trunk-lane groundwork from breaking today's lane.
    """
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    code = (
        "import sys; from pathlib import Path;"
        "from raven.config.loader import load_config;"
        "from raven.config.raven import load_raven_config;"
        "cfg = load_config(Path(sys.argv[1]));"
        "extensions = load_raven_config(Path(sys.argv[1]));"
        "print(cfg.agents.defaults.model, extensions.plugins.disabled[0])"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(FORK / "Raven-main")
    proc = subprocess.run(
        [sys.executable, "-c", code, str(rendered)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.strip()[-800:]
    assert proc.stdout.split() == ["anthropic/claude-opus-5", "everos-memory"]


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("CODE_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


# --- the exec lane: the fork engine, armed ------------------------------------


def test_a_missing_fork_venv_refuses_before_rendering(grounded, tmp_path):
    """The fork launcher's order: the engine precheck answers first, and no
    file holding merged secrets exists for a run that cannot start."""
    args = SimpleNamespace(checkout=str(tmp_path / "empty"), config=str(RUN_PY.parent / "config.json"))
    with pytest.raises(SystemExit) as excinfo:
        grounded.serve(args)
    assert "uv sync" in str(excinfo.value)
    assert not (tmp_path / "state").exists()


def test_the_exec_lane_is_the_fork_engines(grounded, tmp_path, monkeypatch):
    """The served argv, cwd and gate-arming env are the fork launcher's, the
    rendered file exists while the engine runs and is gone afterwards, and
    stdio passes through untouched."""
    checkout = tmp_path / "checkout"
    raven_bin = checkout / ".venv" / "bin" / "raven"
    raven_bin.parent.mkdir(parents=True)
    raven_bin.write_text("#!/bin/sh\n")

    calls = {}

    class _Proc:
        def wait(self):
            return 0

    def fake_popen(argv, cwd=None, env=None, **kwargs):
        calls["argv"] = list(argv)
        calls["cwd"] = cwd
        calls["env"] = env
        calls["extra"] = kwargs
        calls["rendered_alive"] = Path(argv[3]).is_file()
        return _Proc()

    monkeypatch.setattr(grounded.subprocess, "Popen", fake_popen)
    rc = grounded.serve(SimpleNamespace(checkout=str(checkout), config=str(RUN_PY.parent / "config.json")))
    assert rc == 0

    acp = (tmp_path / "state" / "acp").resolve()
    assert calls["argv"][:3] == [str(checkout.resolve() / ".venv" / "bin" / "raven"), "acp", "--config"]
    rendered = Path(calls["argv"][3])
    assert rendered.parent == acp
    assert calls["rendered_alive"]
    assert not rendered.exists()
    assert calls["cwd"] == str(checkout.resolve())
    assert calls["extra"] == {}
    env = calls["env"]
    assert env["RAVEN_WORKSPACE_ALLOC_BASE"] == str(acp)
    assert env["RAVEN_WORKSPACE_ALLOC_REPOS"] == str(tmp_path / "state" / "repos")
    assert env["RAVEN_WORKSPACE_STATE_BUCKET"] == "acp"


def test_a_callers_state_bucket_is_honoured_not_clobbered(grounded, tmp_path, monkeypatch):
    """setdefault, the fork's spelling: a caller who partitioned the runtime
    state keeps the partition."""
    checkout = tmp_path / "checkout"
    raven_bin = checkout / ".venv" / "bin" / "raven"
    raven_bin.parent.mkdir(parents=True)
    raven_bin.write_text("#!/bin/sh\n")
    monkeypatch.setenv("RAVEN_WORKSPACE_STATE_BUCKET", "caller-owned")

    seen = {}

    class _Proc:
        def wait(self):
            return 0

    def fake_popen(argv, cwd=None, env=None, **kwargs):
        seen["bucket"] = env["RAVEN_WORKSPACE_STATE_BUCKET"]
        return _Proc()

    monkeypatch.setattr(grounded.subprocess, "Popen", fake_popen)
    grounded.serve(SimpleNamespace(checkout=str(checkout), config=str(RUN_PY.parent / "config.json")))
    assert seen["bucket"] == "caller-owned"


# --- the state root: host-config-aware, the fork's derivation -----------------


def test_the_state_root_honours_the_hosts_configured_agent_home(grounded, tmp_path, monkeypatch):
    """The fork derives the state root from the host config's Agent home, so
    an operator-moved workspace moves this product's state with it; the
    schema-default spelling resolves to the raven home it means."""
    monkeypatch.delenv("CODE_STATE_ROOT", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    moved = tmp_path / "moved-workspace"
    (home / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": str(moved)}}}))
    assert grounded.state_root() == moved / "subagent_sessions" / "raven-code"
    (home / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": "~/.raven/workspace"}}}))
    assert grounded.state_root() == home / "workspace" / "subagent_sessions" / "raven-code"


def test_a_relative_state_root_resolves_under_the_host_agent_home(grounded, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_STATE_ROOT", "nested/code")
    assert grounded.state_root() == tmp_path / "home" / "workspace" / "nested" / "code"
