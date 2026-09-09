"""The agents/ ppt launcher: rendering, refusals, the trunk exec, the pinned face.

The exec target is installed raven's own ``raven acp``; the deck capability
reaches it as the ppt-engine wheel through the entry-point group, never by
directory. The launch contract is still the fork launcher's ACP half: an own
key reaches every provider block, PPT_MODEL/PPT_API_BASE apply on the own-key
branch only, the context window recalibrates from the host's model catalog,
secrets merge into a rendered 0600 config whose parent decides the data dir.
Three renders are this hosting's own trunk seats: the agent home pinned under
the state root (a pooled loop must not share the host's), the engine skill
directory mounted through skillForge.localDirs, and the retired tools.ppt
block dropped from a carried config with the successor named (D4's floor).

The hermetic tool-face pin is live now (the code family's w96 shape): the
loop built from this render advertises the fork's config intent respelled to
plugin admission -- ten deck tools plus the fork's base face -- with every
trunk-new tool the fork never registered held out by config, not by luck.
The key-gated pair (web_search, ppt_image_search) joins only with a Serper
key, the fork's own registration refusal.

The fork's identity trio (SOUL.md/AGENTS.md/TOOLS.md) is carried
byte-for-byte at the engine wheel's prompts home; the engine plugin's hook
seeds it into the pinned home at first turn (tests in the plugin family),
so this launcher still seeds none of them.
"""

import importlib.util
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-ppt" / "run.py"
FORK = REPO / "tests" / "fixtures" / "vendored_fork" / "raven-ppt"
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
    monkeypatch.delenv("PPT_ACP_HOME", raising=False)
    monkeypatch.setenv("PPT_API_KEY", "sk-own")
    for name in ("PPT_MODEL", "PPT_API_BASE", "PPT_SERPER_API_KEY", "PPT_JINA_API_KEY", "PPT_IMAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # The launcher reads the host's proxy out of the environment; a developer box
    # that exports one would otherwise decide what "no proxy configured" renders.
    for name in ("PPT_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    return launcher


# --- byte parity: the carried assets and the roster identity -----------------


#: The model each side recommends and defaults to, in the roster row, in
#: agents.defaults and as the provider's one listed model. The sealed fork names
#: the model it shipped with; this product names the one the recent hosted deck
#: runs were measured on. The fork is the frozen A side of the comparison, so the
#: swap is ledgered here rather than written into its config.
FORK_MODEL = "anthropic/claude-sonnet-5"
TRUNK_MODEL = "z-ai/glm-5.3-flash"


def test_the_roster_row_is_the_vendored_twins_modulo_two_ledgered_deltas():
    """The whole row, not a field list, with exactly two ledgered deltas. The
    ``engine`` declaration product discovery probes readiness with (the fork
    twin's venv gate has no counterpart here, so the wheel probe is what keeps
    an engineless install listed-but-disabled instead of failing at dispatch),
    and the model this product recommends. Everything the host router reads
    stays the twin's: ownsWatchedWork stays absent on both sides, and
    recommendedLlm.provider keeps the load-bearing name ``ppt`` (C2: gateway
    detection and prompt caching key on it)."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text(encoding="utf-8"))
    theirs = json.loads((FORK / "subagent.json").read_text(encoding="utf-8"))
    assert ours.pop("engine") == {"package": "raven_ppt", "wheel": "ppt-engine"}
    assert ours["recommendedLlm"].pop("model") == TRUNK_MODEL
    assert theirs["recommendedLlm"].pop("model") == FORK_MODEL
    assert ours == theirs
    assert "ownsWatchedWork" not in ours
    assert ours["recommendedLlm"]["provider"] == "ppt"


#: Trunk tools the fork loop never registered under this product's config;
#: every one is held out of the face by a config row, not by luck (the code
#: family's ledger discipline). tool_call/tool_search additionally keep the
#: fork's face: its meta-pair registers only under tools.toolSearch.enabled,
#: default False and never set by this config (the threshold only folds).
TRUNK_HELD_OUT = {
    "create_playbook",
    "cron",
    "deliver_files",
    "find_skill",
    "hub",
    "load_playbook",
    "plugin",
    "read_skill",
    "run_subagent_dag",
    "tool_call",
    "tool_search",
}


#: agents.defaults rows the sealed fork's config does not carry: the retry of a
#: streamed call that failed after output, for a measured run that died on a
#: mid-stream disconnect after two hours, and the second retry ladder, in
#: minutes, that waits out a gateway serving error pages (one measured build had
#: 62 minutes behind it when a 40-second outage ended its turn).
TRUNK_ONLY_DEFAULTS = {
    "llmRetryAfterOutput": True,
    "llmErrorRetryDelays": [30, 60, 120, 240, 300, 300, 300, 300],
}

#: agents.defaults rows both sides carry with different values, as (fork, trunk).
#: The call timeout is applied per streamed chunk, and a reasoning model that
#: thinks silently for longer than the fork's 600s is what a hosted run met: one
#: iteration failed six times in a row at exactly 600s while a standalone run of
#: the same deck survived a 20-minute call under 1800.
TRUNK_OVERRIDDEN_DEFAULTS = {"llmCallTimeout": (600, 1800)}


# Engine-slice keys the fork's tools.ppt schema never had. The second reader's own
# reasoning effort: the fork read every page at the author's setting, which litellm
# dropped for this model anyway, so a GLM thought at its default for 100 to 350 seconds
# a page; the gateway's own low effort reads one in 13 to 29.
TRUNK_ONLY_SLICE = {"readerEffort": "low"}


def test_the_config_is_the_forks_modulo_the_swap_ledger():
    """Every delta against the sealed fork wrapper's config is a ledgered row:
    the engine slice carries the retired tools.ppt knobs verbatim (D4),
    tools.ppt itself is gone (the slice is the only reading), disabledTools
    grows exactly the trunk-new hold-out set, agents.defaults grows the
    trunk-only rows and re-values the overridden ones, and the model is the
    trunk's wherever the fork names its own. Every other byte of shipped intent
    is the fork's."""
    ours = json.loads((RUN_PY.parent / "config.json").read_text())
    theirs = json.loads((FORK / "config.json").read_text())
    slice_ = ours["plugins"]["config"].pop("ppt-engine")
    for key, value in TRUNK_ONLY_SLICE.items():
        assert slice_.pop(key) == value
    assert slice_ == theirs["tools"]["ppt"]
    fork_tools = dict(theirs["tools"])
    retired = fork_tools.pop("ppt")
    assert retired == slice_
    ours_disabled = set(ours["tools"].pop("disabledTools"))
    fork_disabled = set(fork_tools.pop("disabledTools"))
    assert ours_disabled - fork_disabled == TRUNK_HELD_OUT
    assert fork_disabled <= ours_disabled, "no fork disable row may be quietly re-enabled"
    assert ours["tools"] == fork_tools
    ours.pop("tools")
    theirs.pop("tools")
    for key, value in TRUNK_ONLY_DEFAULTS.items():
        assert ours["agents"]["defaults"].pop(key) == value, key
        assert key not in theirs["agents"]["defaults"], f"{key} is no longer trunk-only"
    for key, (fork_value, trunk_value) in TRUNK_OVERRIDDEN_DEFAULTS.items():
        assert ours["agents"]["defaults"].pop(key) == trunk_value, key
        assert theirs["agents"]["defaults"].pop(key) == fork_value, key
    assert ours["agents"]["defaults"].pop("model") == TRUNK_MODEL
    assert theirs["agents"]["defaults"].pop("model") == FORK_MODEL
    assert ours["providers"]["ppt"].pop("models") == [TRUNK_MODEL]
    assert theirs["providers"]["ppt"].pop("models") == [FORK_MODEL]
    assert ours == theirs


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


def test_the_own_key_also_pays_for_the_image_generator_on_openrouter(grounded, monkeypatch):
    """GPT Image 2 is an OpenRouter model: on the own-key branch against OpenRouter
    the same key lands in tools.media.image.apiKey, so a deck can generate its
    backdrops with nothing else set. A different gateway gets nothing written there,
    and an explicit PPT_IMAGE_API_KEY wins."""
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["media"]["image"]["apiKey"] == "sk-own"

    monkeypatch.setenv("PPT_API_BASE", "https://gateway.example/v1")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert not data.get("tools", {}).get("media", {}).get("image", {}).get("apiKey")

    monkeypatch.setenv("PPT_IMAGE_API_KEY", "sk-pictures")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["media"]["image"]["apiKey"] == "sk-pictures"


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
        json.dumps({"models": {"z-ai/glm-5.3-flash": {"context_length": 200000}}})
    )
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["contextWindowTokens"] == 200000


def test_a_configured_window_under_the_catalogs_is_a_cap(grounded, tmp_path):
    """The catalog's number is the model's ceiling and the configured one is what the
    run is willing to carry: a run that took a 1.3M ceiling as its window grew to 450k
    tokens a call, since nothing compacted short of a ceiling it never reached."""
    cache = tmp_path / "home" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model-catalog.json").write_text(
        json.dumps({"models": {"z-ai/glm-5.3-flash": {"context_length": 1310720}}})
    )
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    shipped = json.loads((RUN_PY.parent / "config.json").read_text())["agents"]["defaults"]["contextWindowTokens"]
    assert data["agents"]["defaults"]["contextWindowTokens"] == shipped == 1000000


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


def test_the_render_pins_the_home_mounts_the_skill_and_declares_no_plugin_dirs(grounded, tmp_path):
    """Three renders of the swap, one absence kept. The agent home is pinned
    in the raven DATA directory, outside the host Agent home the surfaces
    hand over as a session cwd (w109) -- a pooled loop reads identity,
    sessions and skills from ONE home, and unpinned it would share the
    host's. The engine's skill directory is mounted through
    skillForge.localDirs with always-on semantics (the verdict's feature-14
    collapse). plugins.dirs stays absent: the wheel arrives by entry point."""
    import raven_ppt

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "home" / "subagent_sessions" / "raven-ppt" / "acp")
    mounts = data["skillForge"]["localDirs"]
    assert mounts == [
        {"path": str(Path(raven_ppt.__file__).parent / "skill"), "name": "ppt-engine", "alwaysEnabled": True}
    ]
    assert "dirs" not in data["plugins"]


def test_the_engine_home_is_never_inside_the_configured_host_home(grounded, tmp_path):
    """The w109 containment pin, both ways: the rendered engine home is outside
    the host Agent home, and the runtime's own guard accepts the host home as a
    session working directory against that engine home -- the exact dispatch
    the web surface performs, which used to refuse."""
    from raven.agent.workdir import validate_override

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    engine_home = Path(data["agents"]["defaults"]["workspace"]).resolve()
    host_home = (tmp_path / "home" / "workspace").resolve()
    assert engine_home != host_home
    assert host_home not in engine_home.parents
    host_home.mkdir(parents=True, exist_ok=True)
    assert validate_override(str(host_home), agent_home=engine_home) == host_home
    # And the other half of the fork's concern stays refused: the raven data
    # directory (config.json, oauth tokens) now CONTAINS the engine home, and
    # the engine home itself is nobody's working directory.
    with pytest.raises(ValueError):
        validate_override(str(tmp_path / "home"), agent_home=engine_home)
    with pytest.raises(ValueError):
        validate_override(str(engine_home), agent_home=engine_home)


def test_ppt_acp_home_override_wins_outright(grounded, tmp_path, monkeypatch):
    monkeypatch.setenv("PPT_ACP_HOME", str(tmp_path / "elsewhere" / "acp"))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "elsewhere" / "acp")


def test_an_operators_own_workspace_and_skill_mounts_survive_the_render(grounded, tmp_path):
    """The home pin is a default, never an override; the skill mount is a
    per-entry merge, never a whole-list default -- an operator who mounts a
    directory of their own keeps it AND keeps the deck skill, whose
    site-packages path is nothing they could re-spell by hand."""
    import raven_ppt

    source = json.loads((RUN_PY.parent / "config.json").read_text())
    source.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(tmp_path / "mine")
    source["skillForge"] = {"localDirs": [{"path": str(tmp_path / "skills")}]}
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_config(custom).read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "mine")
    engine_row = {"path": str(Path(raven_ppt.__file__).parent / "skill"), "name": "ppt-engine", "alwaysEnabled": True}
    assert data["skillForge"]["localDirs"] == [{"path": str(tmp_path / "skills")}, engine_row]

    # And a render of an already-rendered config stacks no duplicate row.
    again = tmp_path / "again.json"
    again.write_text(json.dumps(data))
    twice = json.loads(grounded.render_config(again).read_text())
    assert twice["skillForge"]["localDirs"].count(engine_row) == 1


def test_a_carried_tools_ppt_block_is_dropped_with_the_successor_named(grounded, tmp_path, capsys):
    """D4's migration floor: the trunk loader would ignore the retired fork
    key without a word -- the knobs would look honoured and be dead. The
    render drops it and says where the same knobs live now."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    source["tools"]["ppt"] = {"enabled": True, "renderDpi": 300}
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_config(custom).read_text())
    assert "ppt" not in data["tools"]
    assert 'plugins.config["ppt-engine"]' in capsys.readouterr().err


def test_the_serper_key_reaches_both_search_consumers(grounded, tmp_path, monkeypatch):
    """One key, two readers, ONE source of truth: the slice key is copied from
    the tools.web slot after the secret merge, so every admission source --
    the product env var here, the host config's own key below -- reaches
    trunk's web_search and the engine's ppt_image_search together. The
    hosting never exports $SERPER_API_KEY to the child."""
    monkeypatch.setenv("PPT_SERPER_API_KEY", "sk-serper")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["search"]["apiKey"] == "sk-serper"
    assert data["plugins"]["config"]["ppt-engine"]["imageSearch"]["apiKey"] == "sk-serper"


def test_a_web_proxy_reaches_both_tool_families(grounded, tmp_path):
    """The fork's one tools.web.proxy fed the web tools AND every deck tool;
    landed, the deck tools read the slice's webProxy, so the render bridges
    the config's own proxy value across (G3, the Serper bridge's shape).
    ppt_fetch is trust_env=False on purpose -- an environment proxy cannot
    stand in, so an unbridged render would proxy web_search while the deck
    tools dialled bare, without a sound. setdefault: a slice that shipped
    its own webProxy keeps it."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    source["tools"]["web"]["proxy"] = "http://proxy.example:3128"
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_config(custom).read_text())
    assert data["tools"]["web"]["proxy"] == "http://proxy.example:3128"
    assert data["plugins"]["config"]["ppt-engine"]["webProxy"] == "http://proxy.example:3128"

    shipped = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert "webProxy" not in shipped["plugins"]["config"]["ppt-engine"], "no proxy configured renders no row"


def test_an_environment_proxy_is_written_into_the_config_once(grounded, monkeypatch):
    """The engine's fetch is trust_env=False on purpose, so an exported HTTPS_PROXY
    reached nothing and every download on a proxied host failed as unreachable. The
    launcher translates it into tools.web.proxy -- once, in the open -- and the
    bridge below carries it to the deck tools; a proxy the config states itself wins."""
    monkeypatch.setenv("HTTPS_PROXY", "http://corp.example:15002")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["proxy"] == "http://corp.example:15002"
    assert data["plugins"]["config"]["ppt-engine"]["webProxy"] == "http://corp.example:15002"

    monkeypatch.setenv("PPT_PROXY", "http://own.example:8080")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["proxy"] == "http://own.example:8080", "PPT_PROXY outranks the generic names"


def test_a_host_config_serper_key_reaches_both_search_consumers(grounded, tmp_path, monkeypatch):
    """The per-slot host fallback is a supported admission source (pinned
    above for the slot); rendered from the env var alone, a host-keyed deploy
    would register web_search while the deck's own image search silently
    declined -- the fork on that same deploy had working image search."""
    monkeypatch.delenv("PPT_SERPER_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}}}}))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["search"]["apiKey"] == "host-serper"
    assert data["plugins"]["config"]["ppt-engine"]["imageSearch"]["apiKey"] == "host-serper"


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "z-ai/glm-5.3-flash"
    # tools.ppt is retired from the shipped config; the loader must see none.
    assert getattr(config.tools, "ppt", None) is None
    extensions = load_raven_config(rendered)
    assert extensions.plugins.config["ppt-engine"]["profile"] == "script_author"
    assert extensions.plugins.disabled == []
    mounts = extensions.skill_forge.local_dirs
    assert len(mounts) == 1 and mounts[0].always_enabled and mounts[0].path.endswith("skill")


# --- the exec lane: installed raven, by entry point ---------------------------


def test_a_missing_engine_wheel_refuses_before_rendering(grounded, tmp_path, monkeypatch):
    """The fork launcher's order, kept: the engine precheck answers first,
    names what to install, and no file holding merged secrets exists for a
    run that cannot start."""
    monkeypatch.setattr(grounded.importlib.util, "find_spec", lambda name: None)
    args = SimpleNamespace(config=str(RUN_PY.parent / "config.json"))
    with pytest.raises(SystemExit) as excinfo:
        grounded.serve(args)
    assert "ppt-engine" in str(excinfo.value)
    assert not (tmp_path / "state").exists()


def test_the_exec_lane_is_installed_ravens_acp_with_no_chdir(grounded, tmp_path, monkeypatch):
    """The served argv is this interpreter's ``python -m raven acp`` (the
    roster row's {PYTHON} resolves at install to one that imports raven), the
    rendered file exists when the exec takes over, and nothing chdirs: the
    fork engine resolved assets from its checkout, the wheel resolves them
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


def test_the_state_root_override_wins_and_the_default_sits_under_the_home(grounded, tmp_path, monkeypatch):
    assert grounded.state_root() == tmp_path / "state"
    monkeypatch.delenv("PPT_STATE_ROOT", raising=False)
    assert grounded.state_root() == tmp_path / "home" / "workspace" / "subagent_sessions" / "raven-ppt"


# --- the pinned tool face: fork config intent, said as plugin admission -------

#: The fork engine's config-intent face, measured: its AgentLoop built under
#: this product's published config (the four media/deep-research disable rows
#: applied, no Serper key, everos on) registers exactly these -- the six
#: filesystem tools and exec, the two web tools (search key-gated, so absent
#: hermetically), message/spawn/ask_user (the question rides the ACP
#: `ask_user_request` update to whoever is driving the host), use_skill (registry reachable;
#: read_skill needs a Hub endpoint the config never names), everos's
#: understand_media, and the ten deck tools. Its tool_search meta-pair
#: registers only under tools.toolSearch.enabled, default False and never
#: set by this config.
FORK_CONFIG_INTENT = {
    "ask_user",
    "edit_file",
    "exec",
    "find",
    "grep",
    "list_dir",
    "message",
    "read_file",
    "spawn",
    "understand_media",
    "use_skill",
    "web_fetch",
    "write_file",
}

DECK_TOOLS = {
    "ppt_prepare",
    "ppt_brief",
    "ppt_fetch",
    "ppt_generate_image",
    "ppt_ingest",
    "ppt_figure_inspect",
    "ppt_outline",
    "ppt_template",
    "ppt_build",
    "ppt_review",
}

#: The product's visible tool face, hermetically rebuilt from the render: the
#: fork's config intent plus the deck tools as plugin contributions. The
#: key-gated pair (web_search from the merged tools.web slot, ppt_image_search
#: from the rendered slice key) joins only when a Serper key is present -- the
#: fork's own refusal to register keyless search. Every trunk-new name is held
#: out by the TRUNK_HELD_OUT config rows pinned above.
VENDORED_TOOL_FACE = FORK_CONFIG_INTENT | DECK_TOOLS
KEY_GATED = {"web_search", "ppt_image_search"}


def _hermetic_build(rendered, tmp_path, monkeypatch):
    """Build the runtime from a rendered config: user/project plugin dirs
    pinched to nothing, the entry-point group kept live (that is this
    product's delivery lane), a stub provider, the config path pinned.
    Returns the visible tool names."""
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


def test_the_products_tool_face_is_the_forks_config_intent_plus_the_deck(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the ledgered face and nothing more -- the ppt-engine plugin is discovered
    through the live entry point, its eleven rows admitted by the rendered
    slice, and every trunk-new name stays out through the config rows."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE
    disabled = set(json.loads((RUN_PY.parent / "config.json").read_text())["tools"]["disabledTools"])
    assert TRUNK_HELD_OUT <= disabled, "the trunk-new names stay disabled by config, not by luck"
    assert not (KEY_GATED | DECK_TOOLS) & disabled


def test_a_serper_key_admits_exactly_the_gated_pair(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setenv("PPT_SERPER_API_KEY", "sk-serper")
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE | KEY_GATED


def test_a_host_config_serper_key_admits_the_same_pair(grounded, tmp_path, monkeypatch):
    """The pair joins and leaves together on EVERY admission source: a key
    that arrives by the host-config fallback must not split the face the way
    an env-only slice render would have."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("PPT_SERPER_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}}}}))
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    visible = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE | KEY_GATED


def test_copying_a_published_deck_is_not_denied():
    """A deny rule on `cp ... .pptx` once stopped a model that copied an unpublished build
    into out/ and called it delivered. It also stopped the one copy a delegating agent
    legitimately asks for -- "save the file to the working directory" -- and the run ended
    with the refusal as its answer. What defends delivery now is the publish record:
    the hook announces only decks whose sha256 the publish step wrote (see
    test_ppt_engine_plugin), so a copy is harmless and the exec policy is the trunk's own."""
    from raven.agent.tools.shell_policy import CommandDecision, ShellCommandPolicy

    config = json.loads((RUN_PY.parent / "config.json").read_text())
    assert "extraDenyPatterns" not in config["tools"]["exec"]
    policy = ShellCommandPolicy(deny_patterns=config["tools"]["exec"].get("extraDenyPatterns", []))
    assert (
        policy.evaluate('cp out/deck.pptx "/work/community elderly care operations plan.pptx"')
        is not CommandDecision.HARD_DENY
    )
