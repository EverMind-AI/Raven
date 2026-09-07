"""The agents/ design launcher: rendering, refusals, the trunk exec, the pinned face.

The exec target is installed raven's own ``raven acp``; the visual capability
reaches it as the design-engine wheel through the entry-point group, never by
directory. The launch contract is still the fork launcher's render half: an
own key reaches every provider block, the optional Serper/Jina keys fall back
per slot, the image key resolves through the fork's waterfall, the rendered
file is an owner-only copy under the state root, and the engine's Agent home
is pinned in the raven data directory (w109). Three renders are the swap
wave's own (the dw2 debt triple): the wheel's corpus mounted through
skillForge.localDirs (path-keyed, append-if-absent), taskState.stateRoot
under the product state root, and the exec line itself.

The hermetic tool-face pin is live (the w96 method): the loop built from this
render advertises the fork's config-intent face -- eleven trunk-stock rows,
the three engine tools, image_generate through the key waterfall -- with
web_search joining only on a Serper key and every trunk-born name held out by
config rows, not by luck. No identity file ships: design serves the
host-generic identity, the fork's own ACP-lane behaviour (oncall/code
precedent -- an added identity file would change the prompt face). The live
acp lane adds one row the hermetic fixture cannot and need not see: the
session-namer's emit_session_title side-call schema (ppt-adjudicated host
gain, carried forward).
"""

import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-design" / "run.py"
FORK = REPO / "subagents" / "raven-design"

#: The fork compaction leaves with no trunk counterpart (verdict D2): they
#: must appear nowhere in the shipped compaction slice.
PHANTOM_COMPACTION_KNOBS = (
    "targetRatio",
    "minRecentIterations",
    "maxCompactionsPerTurn",
    "minSavingsRatio",
    "retryAfterIterations",
    "maxUserTokens",
    "summaryMinTokens",
    "summaryMaxTokens",
    "maxToolResultChars",
)


#: The fork face's trunk-stock rows (dw0 survey: the fork loop's own
#: registrations under this product's config, seven disable rows applied,
#: everos on): six filesystem tools and exec, web_fetch (web_search is
#: Serper-key-gated), the skill pair the selector's cards depend on, and
#: everos's understand_media.
FORK_CONFIG_INTENT = {
    "edit_file",
    "exec",
    "find",
    "grep",
    "list_dir",
    "read_file",
    "read_skill",
    "understand_media",
    "use_skill",
    "web_fetch",
    "write_file",
}

#: The engine wheel's three contributions, admitted by the rendered slice.
ENGINE_TOOLS = {"preview_file", "render_file", "update_task_state"}

#: image_generate rides the fork's key waterfall; under the grounded render
#: (own key, OpenRouter base) it is present, exactly as the fork face was.
VENDORED_TOOL_FACE = FORK_CONFIG_INTENT | ENGINE_TOOLS | {"image_generate"}
KEY_GATED = {"web_search"}

#: Trunk-born names the fork face never had; every one held out by a config
#: row (the w96 ledger discipline). tool_call/tool_search additionally keep
#: the fork's face by default (tools.toolSearch never set).
TRUNK_HELD_OUT = {
    "create_playbook",
    "deliver_files",
    "find_skill",
    "hub",
    "load_playbook",
    "plugin",
    "run_subagent_dag",
    "tool_call",
    "tool_search",
}


@pytest.fixture()
def launcher():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agents_design_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESIGN_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DESIGN_API_KEY", "sk-own")
    for name in ("DESIGN_ACP_HOME", "DESIGN_IMAGE_API_KEY", "DESIGN_SERPER_API_KEY", "DESIGN_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return launcher


def _host_config(tmp_path: Path, data: dict) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(data))


def _render(grounded) -> dict:
    return json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())


# --- identity: the roster row and the install shim ----------------------------


def test_the_roster_row_carries_the_forks_identity_verbatim():
    """The lane flips cli -> acp (the verdict's feature 10); everything the
    fork row said about WHO this agent is -- description, ownership, everos
    identity pair, the tuned model -- is carried byte-for-byte, the
    recommendedLlm's custom/openrouter spelling included (C5)."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text())
    fork = json.loads((FORK / "subagent.json").read_text())

    assert ours["kind"] == "acp" and fork["kind"] == "cli"
    for field in ("name", "description", "owns", "everos", "recommendedLlm", "maxOutputChars", "timeout"):
        assert ours[field] == fork[field], field
    assert ours["recommendedLlm"] == {
        "provider": "custom",
        "model": "openai/gpt-5.6-sol",
        "apiBase": "https://openrouter.ai/api/v1",
    }
    assert ours["command"] == "{PYTHON} {SUBAGENT_DIR}/run.py --acp"
    assert ours["cwd"] == "{SUBAGENT_DIR}"


def test_the_install_shim_is_the_house_family_byte_for_byte():
    ours = (RUN_PY.parent / "install.py").read_bytes()
    for sibling in ("raven-ppt", "raven-code", "raven-oncall", "raven-research"):
        assert ours == (REPO / "agents" / sibling / "install.py").read_bytes(), sibling


# --- the config: fork leaves carried, the D2 port, no phantoms -----------------


def test_the_config_ports_the_forks_leaves_onto_the_trunk_schema():
    ours = json.loads((RUN_PY.parent / "config.json").read_text())
    fork = json.loads((FORK / "config.json").read_text())

    for leaf in ("model", "provider", "reasoningEffort", "contextWindowTokens", "maxToolIterations", "llmCallTimeout"):
        assert ours["agents"]["defaults"][leaf] == fork["agents"]["defaults"][leaf], leaf
    assert ours["language"] == fork["language"] == "zh"
    assert ours["providers"]["custom"]["apiBase"] == fork["providers"]["custom"]["apiBase"]
    assert ours["providers"]["custom"]["models"] == fork["providers"]["custom"]["models"]
    assert "protocol" not in ours["providers"]["custom"]
    # The fork's seven disable rows survive whole; the nine extras are the
    # swap ledger's trunk-born names, held out of the face by config rather
    # than luck (the w96 discipline). emit_session_title is deliberately NOT
    # among them: it is the session-namer's side-call schema, not a loop
    # registration, so the hermetic face structurally cannot see it and need
    # not -- the live acp lane adds it beside the pinned face, one call per
    # new session (the same +1 ppt's A/B measured; owner adjudicated it a
    # host gain, and that ruling carries forward).
    assert set(fork["tools"]["disabledTools"]) <= set(ours["tools"]["disabledTools"])
    assert set(ours["tools"]["disabledTools"]) - set(fork["tools"]["disabledTools"]) == TRUNK_HELD_OUT
    assert ours["tools"]["web"] == fork["tools"]["web"]
    assert ours["memory"] == fork["memory"]

    image = ours["tools"]["media"]["image"]
    fork_image = fork["tools"]["media"]["image"]
    assert image["apiBase"] == fork_image["apiBase"] and image["model"] == fork_image["model"]
    for phantom in ("apiStyle", "allowModelOverride"):
        assert phantom not in image


def test_the_compaction_slice_is_the_four_knob_d2_port_and_nothing_else():
    ours = json.loads((RUN_PY.parent / "config.json").read_text())
    fork = json.loads((FORK / "config.json").read_text())
    fork_compaction = fork["agents"]["defaults"]["contextCompaction"]
    window = fork["agents"]["defaults"]["contextWindowTokens"]

    compaction = ours["agents"]["defaults"]["compaction"]
    assert compaction == {
        "enabled": fork_compaction["enabled"],
        "triggerRatio": fork_compaction["triggerRatio"],
        "reservedTokens": fork_compaction["safetyMarginTokens"],
        "preserveRecentTokens": round(fork_compaction["tailRatio"] * window),
    }
    assert "contextCompaction" not in ours["agents"]["defaults"]
    for knob in PHANTOM_COMPACTION_KNOBS:
        assert knob not in compaction, knob


def test_the_engine_slice_carries_the_selector_and_render_knobs():
    ours = json.loads((RUN_PY.parent / "config.json").read_text())
    fork = json.loads((FORK / "config.json").read_text())
    engine = ours["plugins"]["config"]["design-engine"]

    assert engine["visualDomainSelector"] == {"enabled": True, **fork["skillForge"]["visualDomainSelector"]}
    # The fork's five spelled render knobs, plus the wheel's fail-closed
    # workspace fence spelled open for this product (H2): the fork seat read
    # the HOST's tools.restrictToWorkspace (false here); the wheel cannot,
    # so the shipped slice says it outright.
    assert engine["render"] == {
        **fork["tools"]["render"],
        "restrictToWorkspace": False,
        "rasterDpi": 300,
        "maxSidePixels": 16384,
    }
    assert "skillForge" not in ours
    assert "render" not in ours["tools"]


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "openai/gpt-5.6-sol"
    compaction = config.agents.defaults.compaction
    assert compaction.enabled is True
    assert compaction.trigger_ratio == 0.8
    assert compaction.reserved_tokens == 8192
    assert compaction.preserve_recent_tokens == 52429
    extensions = load_raven_config(rendered)
    assert extensions.plugins.config["design-engine"]["visualDomainSelector"]["enabled"] is True
    mounts = extensions.skill_forge.local_dirs
    assert len(mounts) == 1 and mounts[0].always_enabled and mounts[0].path.endswith("skills")


def test_the_render_declares_three_effort_tiers_each_with_its_own_cap_and_effort(grounded):
    """The built-in tiers are no-ops for a worker: empty overlay, cap inherited.
    Design declares its own catalogue so the session tier moves what the worker
    does -- the reasoning effort and the iteration cap -- with config.json as
    the high baseline and the other two as diffs in modes/."""
    from raven.config.loader import load_config
    from raven.config.mode_catalogue import build_mode_catalogue

    data = _render(grounded)
    modes = data["acp"]["modes"]
    assert list(modes) == ["medium", "high", "max"]
    assert data["acp"]["defaultMode"] == "high"
    assert {m: (e["maxToolIterations"], e["reasoningEffort"]) for m, e in modes.items()} == {
        "medium": (60, "medium"),
        "high": (150, "high"),
        "max": (300, "max"),
    }
    # And the trunk reads them as the loop will enforce them.
    catalogue = build_mode_catalogue(load_config(grounded.render_config(RUN_PY.parent / "config.json")))
    assert catalogue.default == "high"
    assert (catalogue.get("max").max_iterations, catalogue.get("max").reasoning_effort) == (300, "max")
    assert (catalogue.get("medium").max_iterations, catalogue.get("medium").reasoning_effort) == (60, "medium")


# --- the render: keys, fallbacks, the image waterfall --------------------------


def test_an_own_key_reaches_every_provider_block(grounded, tmp_path):
    config = {
        "providers": {
            "custom": {"apiBase": "https://a.example/v1", "models": ["m1"]},
            "other": {"apiBase": "https://b.example/v1", "models": ["m2"]},
        },
        "agents": {"defaults": {"model": "m1", "provider": "custom"}},
    }
    source = tmp_path / "two.json"
    source.write_text(json.dumps(config))
    data = json.loads(grounded.render_config(source).read_text())
    assert data["providers"]["custom"]["apiKey"] == "sk-own"
    assert data["providers"]["other"]["apiKey"] == "sk-own"


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("DESIGN_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_optional_keys_fall_back_per_slot_to_the_host_config(grounded, tmp_path):
    _host_config(tmp_path, {"tools": {"web": {"search": {"apiKey": "host-serper"}}}})
    data = _render(grounded)
    assert data["tools"]["web"]["search"]["apiKey"] == "host-serper"


def test_the_image_key_waterfall_prefers_the_own_key(grounded, monkeypatch):
    monkeypatch.setenv("DESIGN_IMAGE_API_KEY", "sk-image")
    data = _render(grounded)
    assert data["tools"]["media"]["image"]["apiKey"] == "sk-image"
    assert data["tools"]["media"]["image"]["model"] == "openai/gpt-image-2"


def test_the_image_key_waterfall_borrows_compatible_host_media(grounded, tmp_path):
    """The host's media key rides along with the base and model it was
    configured for (the fork's borrow), but only when the endpoint is
    OpenRouter-shaped -- the one backend trunk's media tools speak."""
    _host_config(
        tmp_path,
        {
            "tools": {
                "media": {
                    "image": {
                        "apiKey": "sk-host-media",
                        "apiBase": "https://openrouter.ai/api/v1",
                        "model": "host/image-model",
                    }
                }
            }
        },
    )
    data = _render(grounded)
    image = data["tools"]["media"]["image"]
    assert image["apiKey"] == "sk-host-media"
    assert image["model"] == "host/image-model"


def test_the_image_key_waterfall_skips_an_incompatible_host_media(grounded, tmp_path):
    """A host media block on a non-OpenRouter endpoint is not borrowed; the
    waterfall falls through to the host's openrouter provider key."""
    _host_config(
        tmp_path,
        {
            "tools": {"media": {"image": {"apiKey": "sk-foreign", "apiBase": "https://images.example/v1"}}},
            "providers": {"openrouter": {"apiKey": "sk-host-or"}},
        },
    )
    data = _render(grounded)
    image = data["tools"]["media"]["image"]
    assert image["apiKey"] == "sk-host-or"
    assert image["model"] == "openai/gpt-image-2"


def test_the_image_key_waterfall_ends_at_the_own_llm_key_on_openrouter(grounded):
    data = _render(grounded)
    assert data["tools"]["media"]["image"]["apiKey"] == "sk-own"


def test_no_image_key_anywhere_withholds_the_tool(grounded, tmp_path, monkeypatch):
    """No key resolves to an empty key AND an empty model -- exactly how the
    trunk registrar declines to offer image_generate (the fork launcher's
    withhold, kept)."""
    source = tmp_path / "offbase.json"
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    config["tools"]["media"]["image"]["apiBase"] = "https://images.example/v1"
    source.write_text(json.dumps(config))
    data = json.loads(grounded.render_config(source).read_text())
    assert data["tools"]["media"]["image"]["apiKey"] == ""
    assert data["tools"]["media"]["image"]["model"] == ""


# --- the render: placement (state root, agent home, w109 containment) ----------


def test_the_rendered_file_is_owner_only_under_the_state_root(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert rendered.parent == tmp_path / "state"


def test_the_state_root_override_wins_and_the_default_sits_under_the_home(grounded, tmp_path, monkeypatch):
    assert grounded.state_root() == tmp_path / "state"
    monkeypatch.delenv("DESIGN_STATE_ROOT", raising=False)
    assert grounded.state_root() == tmp_path / "home" / "workspace" / "subagent_sessions" / "raven-design"


def test_the_render_pins_the_home_mounts_the_corpus_and_seats_task_state(grounded, tmp_path):
    """The w109 home pin plus the swap wave's two data renders: the wheel's
    corpus mounted through skillForge.localDirs (always-on, the engine's own
    row), and taskState.stateRoot under the product state root so the
    resident surface stops declining. plugins.dirs stays absent: the wheel
    arrives by entry point."""
    import raven_design

    data = _render(grounded)
    assert data["agents"]["defaults"]["workspace"] == str(
        tmp_path / "home" / "subagent_sessions" / "raven-design" / "acp"
    )
    mounts = data["skillForge"]["localDirs"]
    assert mounts == [
        {"path": str(Path(raven_design.__file__).parent / "skills"), "name": "design-engine", "alwaysEnabled": True}
    ]
    assert data["plugins"]["config"]["design-engine"]["taskState"]["stateRoot"] == str(tmp_path / "state")
    assert "dirs" not in data.get("plugins", {})


def test_an_operators_own_mounts_and_state_root_survive_the_render(grounded, tmp_path):
    """Both swap renders are defaults, never overrides: a mount list the
    operator wrote keeps every row plus the engine's (path-keyed append),
    and an explicit stateRoot wins outright."""
    import raven_design

    source = tmp_path / "carried.json"
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    config["skillForge"] = {"localDirs": [{"path": str(tmp_path / "mine"), "name": "mine"}]}
    config["plugins"]["config"]["design-engine"]["taskState"] = {"stateRoot": str(tmp_path / "elsewhere")}
    source.write_text(json.dumps(config))
    data = json.loads(grounded.render_config(source).read_text())
    paths = [row["path"] for row in data["skillForge"]["localDirs"]]
    assert paths == [str(tmp_path / "mine"), str(Path(raven_design.__file__).parent / "skills")]
    assert data["plugins"]["config"]["design-engine"]["taskState"]["stateRoot"] == str(tmp_path / "elsewhere")


def test_the_mounted_corpus_resolves_the_card_ids(grounded, tmp_path):
    """The selector cards instruct read_skill over local/<name>; the mounted
    rows must make those ids resolve through trunk's own lookup (the local
    namespace resolves to the layer-priority winner)."""
    from raven.agent.tools.skill_hub import lookup_on_disk
    from raven.memory_engine.skill_local.registry import SkillRegistry
    from raven_design.selector import VISUAL_DOMAIN_SKILL_NAMES

    data = _render(grounded)
    rows = [(Path(row["path"]), row["name"], bool(row["alwaysEnabled"])) for row in data["skillForge"]["localDirs"]]
    registry = SkillRegistry(tmp_path / "reg-workspace", builtin_skills_dir=tmp_path / "no-builtin", extra_dirs=rows)
    for name in (*VISUAL_DOMAIN_SKILL_NAMES, "visual-artifact-design"):
        meta = lookup_on_disk(registry, "local", name)
        assert meta is not None and meta.content.strip(), name


def test_the_engine_home_is_never_inside_the_configured_host_home(grounded, tmp_path):
    """The w109 containment pin, all three directions: the host Agent home is
    accepted as a session cwd against the engine home, the raven data
    directory that CONTAINS the engine home is refused, and the engine home
    itself is nobody's working directory."""
    from raven.agent.workdir import validate_override

    data = _render(grounded)
    engine_home = Path(data["agents"]["defaults"]["workspace"]).resolve()
    host_home = (tmp_path / "home" / "workspace").resolve()
    assert engine_home != host_home
    assert host_home not in engine_home.parents
    host_home.mkdir(parents=True, exist_ok=True)
    assert validate_override(str(host_home), agent_home=engine_home) == host_home
    with pytest.raises(ValueError):
        validate_override(str(tmp_path / "home"), agent_home=engine_home)
    with pytest.raises(ValueError):
        validate_override(str(engine_home), agent_home=engine_home)


def test_design_acp_home_override_wins_outright(grounded, tmp_path, monkeypatch):
    monkeypatch.setenv("DESIGN_ACP_HOME", str(tmp_path / "elsewhere" / "acp"))
    data = _render(grounded)
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "elsewhere" / "acp")


def test_an_operators_own_workspace_survives_the_render(grounded, tmp_path):
    source = tmp_path / "carried.json"
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    config["agents"]["defaults"]["workspace"] = str(tmp_path / "operator-home")
    source.write_text(json.dumps(config))
    data = json.loads(grounded.render_config(source).read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "operator-home")


# --- the exec lane: installed raven, by entry point ---------------------------


def test_a_missing_engine_wheel_refuses_before_rendering(grounded, tmp_path, monkeypatch):
    """The fork launcher's order, kept: the engine precheck answers first,
    names what to install, and no file holding merged secrets exists for a
    run that cannot start."""
    monkeypatch.setattr(grounded.importlib.util, "find_spec", lambda name: None)
    args = SimpleNamespace(config=str(RUN_PY.parent / "config.json"))
    with pytest.raises(SystemExit) as excinfo:
        grounded.serve(args)
    assert "design-engine" in str(excinfo.value)
    assert not (tmp_path / "state").exists()


def test_the_exec_lane_is_installed_ravens_acp_with_no_chdir(grounded, tmp_path, monkeypatch):
    """The served argv is this interpreter's ``python -m raven acp``, the
    rendered file exists when the exec takes over, and nothing chdirs: the
    fork engine resolved its corpus from its checkout, the wheel resolves it
    from its own package -- execv replaces the image, so the pid sweep is
    the only cleaner."""
    calls = {}

    def fake_chdir(path):
        calls["cwd"] = str(path)

    def fake_execv(binary, argv):
        calls["binary"] = binary
        calls["argv"] = list(argv)
        calls["rendered_alive"] = Path(argv[5]).is_file()
        raise RuntimeError("execv reached")

    monkeypatch.setattr(grounded.os, "chdir", fake_chdir)
    monkeypatch.setattr(grounded.os, "execv", fake_execv)
    with pytest.raises(RuntimeError, match="execv reached"):
        grounded.serve(SimpleNamespace(config=str(RUN_PY.parent / "config.json")))

    assert calls["binary"] == sys.executable
    assert calls["argv"][:5] == [sys.executable, "-m", "raven", "acp", "--config"]
    assert Path(calls["argv"][5]).parent == tmp_path / "state"
    assert calls["rendered_alive"]
    assert "cwd" not in calls


# --- identity: host-generic, empirically ---------------------------------------


def test_no_identity_file_ships_and_none_is_seeded():
    """The fork's ACP lane never seeded an identity (dw0 survey: stock
    SOUL.md, no soul.md in the wrapper); the product keeps that face -- an
    added identity file would change the very prompt the parity run pins
    (the oncall/code precedent)."""
    import raven_design

    assert not (RUN_PY.parent / "soul.md").exists()
    wheel = Path(raven_design.__file__).parent
    assert not (wheel / "prompts").exists()
    from raven_design.plugin.hook import DesignEngineHook

    assert not hasattr(DesignEngineHook, "seed_identity")


# --- the pinned tool face: fork config intent, said as plugin admission --------


def _hermetic_build(rendered, tmp_path, monkeypatch):
    """Build the runtime from a rendered config: plugin dirs pinched to
    nothing, the entry-point group live (this product's delivery lane), the
    render extra probed present (its absence path is the plugin family's
    pin), a stub provider, the config path pinned. Returns the visible tool
    names."""
    import raven_design.plugin as dplugin
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

    monkeypatch.setattr(dplugin, "_render_extra_missing", lambda: [])
    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "none",
            "project_dir": tmp_path / "none",
            "entry_points_group": "raven.plugins",
        },
    )
    import raven.home as home

    monkeypatch.setattr(home, "_current_config_path", rendered)
    config = load_config(rendered)
    ec_config = load_raven_config(rendered)
    rt = runtime.build_runtime(config, ec_config, provider=_StubProvider())
    try:
        visible = {d["function"]["name"] for d in rt.loop.tools.get_definitions()}
    finally:
        rt.discard()
    return visible


def test_the_products_tool_face_is_the_forks_config_intent_plus_the_engine(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the ledgered face and nothing more -- the design-engine plugin discovered
    through the live entry point, its three rows admitted by the rendered
    slice, image_generate through the key waterfall, and every trunk-born
    name held out through the config rows."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE
    disabled = set(json.loads((RUN_PY.parent / "config.json").read_text())["tools"]["disabledTools"])
    assert TRUNK_HELD_OUT <= disabled, "the trunk-born names stay disabled by config, not by luck"
    assert not (KEY_GATED | ENGINE_TOOLS) & disabled


def test_a_serper_key_admits_exactly_web_search(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setenv("DESIGN_SERPER_API_KEY", "sk-serper")
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE | KEY_GATED


def test_a_host_config_serper_key_admits_the_same_row(grounded, tmp_path, monkeypatch):
    """The key that arrives by the host-config fallback admits the same face
    an env render would -- the pw2b lesson pinned on this product too."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("DESIGN_SERPER_API_KEY", raising=False)
    _host_config(tmp_path, {"tools": {"web": {"search": {"apiKey": "host-serper"}}}})
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE | KEY_GATED


def test_without_an_image_key_the_waterfall_withholds_image_generate(grounded, tmp_path, monkeypatch):
    """The launcher's empty-key-and-model write-back is what withholds the
    tool -- the fork's own registration refusal, surviving the swap."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    source = tmp_path / "offbase.json"
    config = json.loads((RUN_PY.parent / "config.json").read_text())
    config["tools"]["media"]["image"]["apiBase"] = "https://images.example/v1"
    source.write_text(json.dumps(config))
    rendered = grounded.render_config(source)
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE - {"image_generate"}
