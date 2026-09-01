"""The agents/ ppt launcher: rendering, refusals, the fork exec, the carried assets.

The B-side product must hold the same launch contract as its vendored twin
while the exec target is still the fork engine: an own key reaches every
provider block, PPT_MODEL/PPT_API_BASE apply on the own-key branch only,
the context window recalibrates from the host's model catalog, secrets
merge into a rendered 0600 config whose parent decides the data dir, and
the served process is the fork checkout's own ``raven acp`` run from the
checkout. The render is double-audienced -- the fork engine consumes it
today, the trunk loader must already accept it for the exec-target swap
that follows the ppt-engine wheel -- so both loaders round-trip it here.
No hermetic tool-face pin yet: a trunk loop built from this render would
lack every ppt_* tool until the engine wheel lands, so no face equality
exists to pin (the code family reached its pin the same way, swap wave).

The fork's identity trio (SOUL.md/AGENTS.md/TOOLS.md, all three genuinely
drifted and delivered per ACP session by the fork engine's own workspace
sync) is carried byte-for-byte at the engine wheel's prompts home; this
launcher seeds none of them, because on this lane the engine still seeds
its own from inside the checkout.
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
RUN_PY = REPO / "agents" / "raven-ppt" / "run.py"
FORK = REPO / "subagents" / "raven-ppt"
ENGINE_HOME = REPO / "plugins-dist" / "ppt-engine"
CARRIED_PROMPTS = ("SOUL.md", "AGENTS.md", "TOOLS.md")


@pytest.fixture()
def launcher():
    spec = importlib.util.spec_from_file_location("agents_ppt_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PPT_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("PPT_API_KEY", "sk-own")
    for name in ("PPT_MODEL", "PPT_API_BASE", "PPT_SERPER_API_KEY", "PPT_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return launcher


# --- byte parity: the carried assets and the roster identity -----------------


def test_the_roster_row_is_the_vendored_twins_byte_for_byte():
    """The whole file, not a field list: this product changes nothing about
    how the host router sees it, runsOnMachines stays absent on both sides,
    and recommendedLlm.provider keeps the load-bearing name ``ppt`` (C2:
    gateway detection and prompt caching key on it)."""
    ours = (RUN_PY.parent / "subagent.json").read_bytes()
    theirs = (FORK / "subagent.json").read_bytes()
    assert ours == theirs
    row = json.loads(ours)
    assert "runsOnMachines" not in row
    assert row["recommendedLlm"]["provider"] == "ppt"


def test_the_config_is_the_forks_modulo_the_engine_slice():
    """One structural addition and only one: the ppt-engine plugin slice,
    pre-named for the engine wave (D4's keys), inert on both of today's
    loaders. Every other byte of shipped intent is the fork's."""
    ours = json.loads((RUN_PY.parent / "config.json").read_text())
    theirs = json.loads((FORK / "config.json").read_text())
    slice_ = ours["plugins"]["config"].pop("ppt-engine")
    assert ours == theirs
    assert slice_ == theirs["tools"]["ppt"]


def test_the_everos_identity_agrees_in_all_its_places():
    """ppt's shape: the memory block and the roster row carry the identity
    (the everos-memory slice holds only the endpoint), and the factory
    posture is everos ON -- the fork ships backend "everos" with no plugin
    opt-out, unlike code's double-off."""
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    row = json.loads((RUN_PY.parent / "subagent.json").read_text())
    ids = {
        config["memory"]["userId"],
        config["memory"]["agentId"],
        row["everos"]["userId"],
        row["everos"]["agentId"],
    }
    assert ids == {"raven-ppt"}
    assert config["memory"]["backend"] == "everos"
    assert "disabled" not in config["plugins"]
    assert config["plugins"]["config"]["everos-memory"] == {"base_url": "http://localhost:18791"}


def test_the_carried_identity_prompts_are_the_forks_byte_for_byte():
    """The fork genuinely drifted all three workspace templates (a deck
    identity, a deck workflow, a build-script guide) and its ACP engine
    seeds them per session; the product carries the bytes at the engine
    wheel's prompts home, unconsumed on this lane, delivery decided at the
    engine wave."""
    for name in CARRIED_PROMPTS:
        ours = (ENGINE_HOME / "raven_ppt" / "prompts" / name).read_bytes()
        theirs = (FORK / "Raven-PPT" / "raven" / "templates" / name).read_bytes()
        assert ours == theirs, name


def test_the_install_shim_is_the_house_family_byte_for_byte():
    ours = (RUN_PY.parent / "install.py").read_bytes()
    assert ours == (REPO / "agents" / "raven-oncall" / "install.py").read_bytes()
    assert ours == (REPO / "agents" / "raven-code" / "install.py").read_bytes()


# --- the render: keys, overrides, the window, the two loaders ----------------


def test_an_own_key_reaches_every_provider_block(grounded, tmp_path):
    """The fork writes the key to every keyless provider block rather than to
    one slot path; pinned over a two-provider config so the loop shape (not
    just the shipped single block) is what passes."""
    config = {
        "providers": {
            "ppt": {"apiBase": "https://a.example/v1", "models": ["m1"]},
            "other": {"apiBase": "https://b.example/v1", "models": ["m2"]},
        },
        "agents": {"defaults": {"model": "m1", "provider": "ppt"}},
    }
    source = tmp_path / "two.json"
    source.write_text(json.dumps(config))
    data = json.loads(grounded.render_config(source).read_text())
    assert data["providers"]["ppt"]["apiKey"] == "sk-own"
    assert data["providers"]["other"]["apiKey"] == "sk-own"


def test_own_key_model_and_base_overrides_apply(grounded, monkeypatch):
    monkeypatch.setenv("PPT_MODEL", "vendor/other-model")
    monkeypatch.setenv("PPT_API_BASE", "https://gateway.example/v1")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["model"] == "vendor/other-model"
    assert data["providers"]["ppt"]["models"] == ["vendor/other-model"]
    assert data["providers"]["ppt"]["apiBase"] == "https://gateway.example/v1"


def test_the_inherit_branch_ignores_the_own_key_overrides(grounded, tmp_path, monkeypatch):
    """PPT_MODEL/PPT_API_BASE on top of an inherited block would point the
    host's gateway at a model it may not serve; the fork ignores them there
    and so does the product."""
    monkeypatch.delenv("PPT_API_KEY", raising=False)
    monkeypatch.setenv("PPT_MODEL", "vendor/other-model")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "host": {"apiKey": "sk-host", "apiBase": "https://host.example/v1", "models": ["host/model"]}
                },
                "agents": {"defaults": {"provider": "host", "model": "host/model"}},
            }
        )
    )
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["model"] == "host/model"
    assert data["providers"]["host"]["apiKey"] == "sk-host"
    assert "ppt" not in data["providers"]


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("PPT_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_the_window_recalibrates_from_the_host_catalog(grounded, tmp_path):
    """The catalog's number lands verbatim on whichever model won; the fork
    reads the host's cache and so does the product."""
    cache = tmp_path / "home" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model-catalog.json").write_text(
        json.dumps({"models": {"anthropic/claude-sonnet-5": {"context_length": 200000}}})
    )
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["contextWindowTokens"] == 200000


def test_the_window_keeps_the_shipped_number_without_a_catalog(grounded):
    shipped = json.loads((RUN_PY.parent / "config.json").read_text())["agents"]["defaults"]["contextWindowTokens"]
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["contextWindowTokens"] == shipped


def test_optional_keys_fall_back_per_slot_to_the_host_config(grounded, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}}}}))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["search"]["apiKey"] == "host-serper"


def test_the_rendered_file_is_owner_only_under_the_state_root(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert rendered.parent == tmp_path / "state"


def test_the_render_pins_no_workspace_and_declares_no_plugin_dirs(grounded):
    """Two deliberate absences, both the fork's own shape: its launcher never
    pinned agents.defaults.workspace (the ACP engine fences per-session deck
    projects itself), and its plugins block forbids fields it does not know
    -- while the ppt-engine wheel arrives by entry point, never by dirs."""
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert "workspace" not in data["agents"]["defaults"]
    assert "dirs" not in data["plugins"]


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "anthropic/claude-sonnet-5"
    # The fork-only tools.ppt block passes through the trunk loader unknown
    # and unloaded -- the engine wave moves its keys into the plugin slice.
    assert getattr(config.tools, "ppt", None) is None
    extensions = load_raven_config(rendered)
    assert extensions.plugins.config["ppt-engine"]["profile"] == "script_author"
    assert extensions.plugins.disabled == []


def test_the_render_loads_through_the_forks_own_loader(grounded, tmp_path):
    """The render's live consumer this wave is the fork engine; its config
    stack is imported in a subprocess (PYTHONPATH at the fork tree, cwd off
    the repo so the fork wins the import) and must accept the render whole,
    tools.ppt included."""
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    code = (
        "import sys; from pathlib import Path;"
        "from raven.config.loader import load_config;"
        "from raven.config.raven import load_raven_config;"
        "cfg = load_config(Path(sys.argv[1]));"
        "extensions = load_raven_config(Path(sys.argv[1]));"
        "print(cfg.tools.ppt.profile, cfg.agents.defaults.provider, 'ppt-engine' in extensions.plugins.config)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(FORK / "Raven-PPT")
    proc = subprocess.run(
        [sys.executable, "-c", code, str(rendered)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.strip()[-800:]
    assert proc.stdout.split() == ["script_author", "ppt", "True"]


# --- the exec lane: the fork engine, from the checkout -----------------------


def test_a_missing_fork_venv_refuses_before_rendering(grounded, tmp_path):
    """The fork launcher's order: the engine precheck answers first, names
    the build command, and no file holding merged secrets exists for a run
    that cannot start."""
    args = SimpleNamespace(checkout=str(tmp_path / "empty"), config=str(RUN_PY.parent / "config.json"))
    with pytest.raises(SystemExit) as excinfo:
        grounded.serve(args)
    assert "uv sync --extra ppt" in str(excinfo.value)
    assert not (tmp_path / "state").exists()


def test_the_exec_lane_is_the_fork_engines_from_the_checkout(grounded, tmp_path, monkeypatch):
    """The served argv is the checkout venv's own ``raven acp``, the process
    chdirs into the checkout first (bundled templates and skills resolve
    from there), and the rendered file exists when the exec takes over --
    execv replaces the image, so the pid sweep is the only cleaner."""
    checkout = tmp_path / "checkout"
    raven_bin = checkout / ".venv" / "bin" / "raven"
    raven_bin.parent.mkdir(parents=True)
    raven_bin.write_text("#!/bin/sh\n")
    raven_bin.chmod(0o755)

    calls = {}

    def fake_chdir(path):
        calls["cwd"] = str(path)

    def fake_execv(binary, argv):
        calls["binary"] = binary
        calls["argv"] = list(argv)
        calls["rendered_alive"] = Path(argv[3]).is_file()
        raise RuntimeError("execv reached")

    monkeypatch.setattr(grounded.os, "chdir", fake_chdir)
    monkeypatch.setattr(grounded.os, "execv", fake_execv)
    with pytest.raises(RuntimeError, match="execv reached"):
        grounded.serve(SimpleNamespace(checkout=str(checkout), config=str(RUN_PY.parent / "config.json")))

    expected_bin = str(checkout.resolve() / ".venv" / "bin" / "raven")
    assert calls["binary"] == expected_bin
    assert calls["argv"][:3] == [expected_bin, "acp", "--config"]
    assert Path(calls["argv"][3]).parent == tmp_path / "state"
    assert calls["rendered_alive"]
    assert calls["cwd"] == str(checkout.resolve())


def test_the_default_checkout_is_the_vendored_tree(launcher):
    """Until the engine wheel lands, the engine this product hosts is the
    frozen fork checkout inside this repo -- read-only consumption, no
    modification (the freeze guard keeps that honest)."""
    assert launcher.DEFAULT_CHECKOUT == REPO / "subagents" / "raven-ppt" / "Raven-PPT"


def test_the_state_root_override_wins_and_the_default_sits_under_the_home(grounded, tmp_path, monkeypatch):
    assert grounded.state_root() == tmp_path / "state"
    monkeypatch.delenv("PPT_STATE_ROOT", raising=False)
    assert grounded.state_root() == tmp_path / "home" / "workspace" / "subagent_sessions" / "raven-ppt"
