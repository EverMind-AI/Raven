"""The agents/ oncall launcher: rendering, refusals, the seeded guide, the exec.

The B-side product must hold the same launch contract as its vendored twin
while consuming installed raven: secrets merge into a rendered 0600 config
whose parent decides the data dir, the workspace is pinned, the on-call
guide is seeded byte-equal to what the fork composed, and the exec targets
``python -m raven acp``. The strongest pin is the loader round-trip: what
the launcher renders, trunk raven's own loader loads.
"""

import importlib.util
import json
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-oncall" / "run.py"
FORK = REPO / "subagents" / "raven-oncall"
FORK_SECTION = FORK / "Raven-Oncall" / "raven" / "templates" / "TOOLS_ONCALL.md"


@pytest.fixture()
def launcher():
    spec = importlib.util.spec_from_file_location("agents_oncall_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ONCALL_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("ONCALL_API_KEY", "sk-own")
    monkeypatch.delenv("ONCALL_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("ONCALL_JINA_API_KEY", raising=False)
    monkeypatch.delenv("RAVEN_CONNECTIONS", raising=False)
    return launcher


# --- byte parity: the one prompt asset and the roster identity --------------


def test_the_oncall_section_is_the_vendored_twins_verbatim():
    """The product's copy of TOOLS_ONCALL.md is the fork's, byte for byte."""
    assert (RUN_PY.parent / "TOOLS_ONCALL.md").read_bytes() == FORK_SECTION.read_bytes()


def test_the_seeded_guide_is_the_trunk_template_plus_the_section(grounded, tmp_path):
    """What lands in the workspace equals the fork's composed append.

    The fork appended the section to the synced TOOLS.md as
    ``current.rstrip("\\n") + "\\n\\n" + body``; the launcher seeds the same
    composition over the trunk template, so on a fresh workspace the
    model-visible text is byte-equal.
    """
    from raven import templates

    grounded.render_config(RUN_PY.parent / "config.json")
    seeded = (tmp_path / "state" / "workspace" / "TOOLS.md").read_bytes()
    base = (Path(templates.__file__).resolve().parent / "TOOLS.md").read_text(encoding="utf-8")
    section = (RUN_PY.parent / "TOOLS_ONCALL.md").read_text(encoding="utf-8")
    assert seeded == (base.rstrip("\n") + "\n\n" + section).encode("utf-8")


def test_the_guide_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    guide = tmp_path / "state" / "workspace" / "TOOLS.md"
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
        "runsOnMachines",
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
    assert ids == {"raven-oncall"}


# --- the render: secrets, pinning, the plugin, the gate ---------------------


def test_the_render_merges_secrets_pins_workspace_and_boards_the_plugin(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["providers"]["custom"]["apiKey"] == "sk-own"
    assert data["agents"]["defaults"]["workspace"] == str((tmp_path / "state" / "workspace").resolve())
    assert data["plugins"]["dirs"] == [str(RUN_PY.parent / "plugins")]
    flow = data["plugins"]["config"]["oncall-flow"]
    assert flow["enabled"] is True
    assert flow["stateRoot"] == str(tmp_path / "state" / "oncall_flow")
    assert rendered.parent == tmp_path / "state"


def test_the_fork_schema_key_never_reaches_trunks_loader(grounded):
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert "oncall" not in data["tools"], "the gate arms via the plugin slice, not tools.oncall"
    assert "acp" not in data, "oncall ships no modes; session/set_mode stays method-not-found"


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

    config = load_config(grounded.render_config(RUN_PY.parent / "config.json"))
    assert config.agents.defaults.model == "anthropic/claude-opus-5"


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("ONCALL_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


# --- the connections pointer -------------------------------------------------


def test_the_connections_env_name_matches_the_trunk_module(launcher):
    from raven.ops.connections import CONNECTIONS_ENV

    assert launcher.CONNECTIONS_ENV == CONNECTIONS_ENV


def test_the_registry_is_a_pointer_at_the_owners_file(grounded, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "connections.json").write_text("[]")
    assert grounded.connections_registry() == home / "connections.json"


def test_a_preexisting_own_registry_stays(grounded, tmp_path):
    own = tmp_path / "state" / "connections.json"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("[]")
    assert grounded.connections_registry() == own
