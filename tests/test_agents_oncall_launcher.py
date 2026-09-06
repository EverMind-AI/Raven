"""The agents/ oncall launcher: rendering, refusals, the seeded guide, the exec.

The B-side product must hold the same launch contract as its vendored twin
while consuming installed raven: secrets merge into a rendered 0600 config
whose parent decides the data dir, the workspace is pinned, the on-call
guide is seeded from the fork's section -- byte-equal modulo the enumerated
ops_exec respellings (part 2c) -- and the exec targets
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
PRODUCT_SECTION = RUN_PY.parent / "plugins" / "oncall-flow" / "prompts" / "TOOLS_ONCALL.md"


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
    monkeypatch.delenv("ONCALL_ACP_HOME", raising=False)
    monkeypatch.setenv("ONCALL_API_KEY", "sk-own")
    monkeypatch.delenv("ONCALL_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("ONCALL_JINA_API_KEY", raising=False)
    monkeypatch.delenv("RAVEN_CONNECTIONS", raising=False)
    return launcher


# --- byte parity: the one prompt asset and the roster identity --------------


# The five ops_exec respellings: the ONLY bytes where the product's guide may
# differ from the vendored twin's. The fork's exec(machine=...) face landed as
# its own contributed tool (ops_exec; the same-name exec shadow is an open
# ruling), so every machine-face mention teaches the name that exists. The
# local-exec mentions -- the one-off row, the do-not-reproduce-locally warning
# -- keep the fork's bytes, because plain exec still runs on this computer.
OPS_EXEC_RESPELLINGS = [
    (
        "\u2192 ops_connections, exec(machine=...), ops_declare, ops_submit",
        "\u2192 ops_connections, ops_exec(machine=...), ops_declare, ops_submit",
    ),
    (
        "never with\n`exec`. Looking through `exec` is fine -- doing it twice costs a round trip. "
        "Acting\nthrough `exec` leaves no record",
        "never with\n`ops_exec`. Looking through `ops_exec` is fine -- doing it twice costs a round trip. "
        "Acting\nthrough `ops_exec` leaves no record",
    ),
    (
        "**`exec` takes a `machine`**",
        "**`ops_exec` takes a `machine`**",
    ),
    (
        "a size or a hash. Without `machine` it runs here, which is why a path on someone",
        "a size or a hash. Plain `exec` runs here, which is why a path on someone",
    ),
    (
        "`exec` with a `machine` already reaches it for looking",
        "`ops_exec` with a `machine` already reaches it for looking",
    ),
]


def test_the_oncall_section_is_the_vendored_twins_modulo_the_ops_exec_respellings():
    """Byte parity with an enumerated exception list, pinned from both ends.

    Every fork sentence on the list must occur exactly once (a fork edit that
    moves one fails loudly here instead of silently un-pinning it), and the
    list applied to the fork's bytes must reproduce the product's copy
    exactly -- so no byte outside the list may drift."""
    expected = FORK_SECTION.read_text(encoding="utf-8")
    for theirs, ours in OPS_EXEC_RESPELLINGS:
        assert expected.count(theirs) == 1, f"fork sentence moved: {theirs[:40]!r}"
        expected = expected.replace(theirs, ours)
    assert PRODUCT_SECTION.read_text(encoding="utf-8") == expected


def test_the_seeded_guide_is_the_trunk_template_plus_the_section(grounded, tmp_path):
    """What lands in the workspace equals the fork's composed append.

    The fork appended the section to the synced TOOLS.md as
    ``current.rstrip("\\n") + "\\n\\n" + body``; the launcher seeds the same
    composition over the trunk template, so on a fresh workspace the
    model-visible text is byte-equal.
    """
    from raven import templates

    grounded.render_config(RUN_PY.parent / "config.json")
    seeded = (tmp_path / "home" / "subagent_sessions" / "raven-oncall" / "acp" / "TOOLS.md").read_bytes()
    base = (Path(templates.__file__).resolve().parent / "TOOLS.md").read_text(encoding="utf-8")
    section = PRODUCT_SECTION.read_text(encoding="utf-8")
    assert seeded == (base.rstrip("\n") + "\n\n" + section).encode("utf-8")


def test_the_guide_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    guide = tmp_path / "home" / "subagent_sessions" / "raven-oncall" / "acp" / "TOOLS.md"
    guide.write_text("operator tuned")
    grounded.render_config(RUN_PY.parent / "config.json")
    assert guide.read_text() == "operator tuned"


def test_the_engine_home_is_never_inside_the_configured_host_home(grounded, tmp_path):
    """The w109 containment pin: the shipped config's literal "workspace" is
    the default spelling, so the rendered engine home sits in the raven DATA
    directory, outside the host Agent home the surfaces hand over as a
    session cwd -- and the runtime's own guard accepts that cwd against it."""
    from raven.agent.workdir import validate_override

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    engine_home = Path(data["agents"]["defaults"]["workspace"]).resolve()
    assert engine_home == (tmp_path / "home" / "subagent_sessions" / "raven-oncall" / "acp").resolve()
    host_home = (tmp_path / "home" / "workspace").resolve()
    assert engine_home != host_home and host_home not in engine_home.parents
    host_home.mkdir(parents=True, exist_ok=True)
    assert validate_override(str(host_home), agent_home=engine_home) == host_home
    # And the other half of the fork's concern stays refused: the raven data
    # directory (config.json, oauth tokens) now CONTAINS the engine home, and
    # the engine home itself is nobody's working directory.
    with pytest.raises(ValueError):
        validate_override(str(tmp_path / "home"), agent_home=engine_home)
    with pytest.raises(ValueError):
        validate_override(str(engine_home), agent_home=engine_home)


def test_an_operators_own_workspace_survives_and_the_override_wins(grounded, tmp_path, monkeypatch):
    """An explicit operator value is not the shipped sentinel: it stays as
    written (relative under the state root, the documented shape); and
    ONCALL_ACP_HOME moves the default outright."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    source["agents"]["defaults"]["workspace"] = "my-own-seat"
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_config(custom).read_text())
    assert data["agents"]["defaults"]["workspace"] == str((tmp_path / "state" / "my-own-seat").resolve())

    monkeypatch.setenv("ONCALL_ACP_HOME", str(tmp_path / "elsewhere" / "acp"))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "elsewhere" / "acp")


def test_the_roster_row_identity_is_the_vendored_twins():
    """The spawn-facing text the host router reads stays byte-identical."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text())
    theirs = json.loads((FORK / "subagent.json").read_text())
    for field in (
        "name",
        "kind",
        "ownsWatchedWork",
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
    assert data["agents"]["defaults"]["workspace"] == str(
        tmp_path / "home" / "subagent_sessions" / "raven-oncall" / "acp"
    )
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


# --- the visible tool face ----------------------------------------------------


#: The vendored twin's visible tool face, hermetically rebuilt: the fork's
#: config-intent face plus the ledgered ops_exec addition. Intent, not leak --
#: the fork's ACP host never passed disabled_tools, so its live face showed
#: four tools its own config disables; trunk enforces the list. Trunk also
#: grew six tools the fork never had (create_playbook, deliver_files,
#: find_skill, load_playbook, plugin, run_subagent_dag), and every one must
#: be disabled by the product config, not by luck. The two playbook tools and
#: the everos understand_media only register outside this hermetic fixture;
#: their disable rows are pinned below instead.
VENDORED_TOOL_FACE = {
    "ask_user",
    "edit_file",
    "exec",
    "find",
    "grep",
    "list_dir",
    "message",
    "ops_ask_owner",
    "ops_campaigns",
    "ops_case_changes",
    "ops_check_later",
    "ops_connections",
    "ops_declare",
    "ops_edit_case_dict",
    "ops_exec",
    "ops_finish",
    "ops_kill",
    "ops_note",
    "ops_outputs",
    "ops_submit",
    "ops_tune_status",
    "read_file",
    "web_fetch",
    "write_file",
}

#: Tools trunk grew after the fork was cut; none may reach this product's face.
TRUNK_NEW_SIX = {
    "create_playbook",
    "deliver_files",
    "find_skill",
    "load_playbook",
    "plugin",
    "run_subagent_dag",
}


def test_the_products_tool_face_equals_the_forks_config_intent(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the fork's config intent and nothing more, pinned from both ends."""
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config
    from raven.contracts.llm_provider import LLMResponse
    from raven.core import plugin_stack, runtime
    from raven.providers.base import LLMProvider

    class _StubProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
            **kwargs,
        ):
            return LLMResponse(content="", tool_calls=[])

        def get_default_model(self):
            return "test-model"

    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "none",
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    import raven.home as home

    monkeypatch.setattr(home, "_current_config_path", rendered)
    config = load_config(rendered)
    ec_config = load_raven_config(rendered)
    rt = runtime.build_runtime(config, ec_config, provider=_StubProvider())
    try:
        visible = {d["function"]["name"] for d in rt.loop.tools.get_definitions()}
    finally:
        rt.discard()
    assert visible == VENDORED_TOOL_FACE
    disabled = set(json.loads((RUN_PY.parent / "config.json").read_text())["tools"]["disabledTools"])
    assert TRUNK_NEW_SIX <= disabled, "the trunk-new six stay disabled by config, not by luck"
    assert {"exec", "message"} & disabled == set(), "the ruled-open pair stays open"
