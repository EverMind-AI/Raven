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
    monkeypatch.setattr(launcher, "MODES_DIR", tmp_path / "modes")
    for name in (
        "RESEARCH_API_KEY",
        "RESEARCH_SERPER_API_KEY",
        "RESEARCH_ANYSEARCH_API_KEY",
        "RESEARCH_SERPAPI_API_KEY",
        "RESEARCH_JINA_API_KEY",
        "RESEARCH_TAVILY_API_KEY",
        "RESEARCH_EXA_API_KEY",
        "RESEARCH_BRAVE_API_KEY",
        "RESEARCH_FIRECRAWL_API_KEY",
        "SERPER_API_KEY",
        "ANYSEARCH_API_KEY",
        "SERPAPI_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BRAVE_API_KEY",
        "FIRECRAWL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return launcher


@pytest.fixture
def searchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search key, for the tests that are about something else.

    The launcher refuses to render a config whose search backend has no key, so
    without this every unrelated case would exit on that instead of exercising
    what it names. The gate itself is tested below, from a bare fixture.
    """
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "k-serper")


def _source(tmp_path: Path, extra: dict | None = None) -> Path:
    config = {
        "providers": {"openrouter": {"apiBase": "https://example.invalid/v1"}},
        "agents": {"defaults": {"provider": "openrouter", "model": "own-model", "maxToolIterations": 150}},
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
    assert data["providers"]["openrouter"]["apiKey"] == "k-llm"
    assert data["tools"]["web"]["providers"]["serper"]["apiKey"] == "k-serper"
    assert "k-llm" not in source.read_text(encoding="utf-8")


def test_the_rendered_file_is_private_and_named_after_this_pid(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid in the name is the contract `sweep_stale_renders` reads: the
    exec hands this pid to the server, so liveness of the pid is liveness of
    the server that read the file."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    rendered = mod.render_config(_source(tmp_path))

    assert rendered.parent == mod.STATE_ROOT
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert (rendered.stat().st_mode & 0o777) == 0o600


def test_the_rendered_file_is_tightened_when_the_path_already_exists(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starts from a loose file rather than an empty directory, which is the
    only state that can tell `os.open`'s mode from `fchmod`: the mode argument
    applies where it creates, and this name can pre-exist because
    `sweep_stale_renders` keeps a render whose pid is alive - this process's
    own always is.
    """
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    stale = mod.STATE_ROOT / f".config.rendered.{os.getpid()}.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}", encoding="utf-8")
    stale.chmod(0o644)

    rendered = mod.render_config(_source(tmp_path))

    assert rendered == stale
    assert (rendered.stat().st_mode & 0o777) == 0o600
    assert "k-llm" in rendered.read_text(encoding="utf-8")


def test_the_workspace_is_pinned_under_the_state_root(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema default is the host raven's own ~/.raven/workspace, which
    this agent must not share."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    rendered = mod.render_config(_source(tmp_path))

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["workspace"] == str(mod.STATE_ROOT / "workspace")


def test_a_workspace_the_config_declares_is_left_alone(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = _source(tmp_path, extra={"workspace": "/elsewhere/ws"})

    rendered = mod.render_config(source)

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["agents"]["defaults"]["workspace"] == "/elsewhere/ws"


def test_no_key_anywhere_refuses_to_launch(mod, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="RESEARCH_API_KEY"):
        mod.render_config(_source(tmp_path))


def test_the_hosts_whole_provider_block_is_inherited_without_its_limits(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_a_missing_search_key_refuses_to_launch(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Search is what this agent is for. Withheld, its tool is simply absent and
    the run answers from the model's own memory, which reads as an ordinary
    run -- so the absence has to be loud at launch or it is never noticed."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    with pytest.raises(SystemExit, match="RESEARCH_SERPER_API_KEY"):
        mod.render_config(_source(tmp_path))


def test_the_llm_key_is_reported_before_the_search_key(mod, tmp_path: Path) -> None:
    """A deployment missing both should be sent to the more basic one first."""
    with pytest.raises(SystemExit, match="RESEARCH_API_KEY"):
        mod.render_config(_source(tmp_path))


def test_the_bare_environment_variable_counts_as_configured(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool resolves its key at call time from the config value *or* its
    provider's variable, so refusing here would block a runtime that works."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    monkeypatch.setenv("SERPER_API_KEY", "from-env")

    assert mod.render_config(_source(tmp_path)).exists()


def test_a_corpus_endpoint_needs_no_search_key(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It IS the search source on a fixed-corpus benchmark; refusing there
    would break every BrowseComp-Plus run."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "openrouter", "model": "own-model"}},
                "tools": {"web": {"corpusEndpoint": "http://127.0.0.1:8765"}},
            }
        ),
        encoding="utf-8",
    )

    assert mod.render_config(source).exists()


def test_another_providers_key_does_not_satisfy_the_selected_one(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Serper key is not an AnySearch key. AnySearch's anonymous tier is not
    accepted either: it would trade a loud launch failure for a run that is
    silently rate-limited part way through a batch."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "k-serper")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "openrouter", "model": "own-model"}},
                "tools": {"web": {"search": {"provider": "anysearch"}}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="RESEARCH_ANYSEARCH_API_KEY"):
        mod.render_config(source)


def _checkout_specs(kind: str) -> dict[str, dict[str, str]]:
    """The literal fields of one provider table, read off the checkout's source.

    Read rather than imported: ``web.py`` pulls in the whole Raven-X package,
    which is a separate environment this suite does not run under.
    """
    web = _LAUNCHER.parent / "Raven-X" / "raven" / "agent" / "tools" / "web.py"
    text = web.read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    for block in text.split(f": {kind}ProviderSpec(")[1:]:
        body = block.split("),", 1)[0]
        fields = {}
        for field in ("vendor", "env_var", "extractor"):
            if f'{field}="' in body:
                fields[field] = body.split(f'{field}="', 1)[1].split('"', 1)[0]
        fields["needs_key"] = "needs_key=True" in body
        out[fields["vendor"]] = fields
    return out


def test_the_launchers_provider_tables_match_the_checkouts(mod) -> None:
    """The launcher imports nothing from the checkout it launches, so it keeps
    its own copy of both provider tables. Nothing but this stops the two drifting
    -- and a drifted copy would refuse a configured deploy, or wave through an
    unconfigured one, with no other symptom."""
    search = _checkout_specs("Search")
    assert set(mod.SEARCH_PROVIDERS) == set(search)
    for name, (env_var, slot) in mod.SEARCH_PROVIDERS.items():
        assert env_var == search[name]["env_var"]
        assert slot in mod.SECRET_SLOTS

    fetch = _checkout_specs("Fetch")
    assert set(mod.FETCH_PROVIDERS) == set(fetch)
    for name, (env_var, slot, needs_key) in mod.FETCH_PROVIDERS.items():
        assert env_var == fetch[name]["env_var"]
        assert needs_key == fetch[name]["needs_key"]
        assert slot in mod.SECRET_SLOTS


def test_every_secret_slot_writes_under_its_own_vendor(mod) -> None:
    """The credential is keyed by vendor, so the slot's path and the provider
    name it serves must agree. They are written in two places -- SECRET_SLOTS and
    the provider tables -- and nothing else ties them together."""
    for table in (mod.SEARCH_PROVIDERS, mod.FETCH_PROVIDERS):
        for name, entry in table.items():
            slot = entry[1]
            assert mod.SECRET_SLOTS[slot] == ("tools", "web", "providers", name, "apiKey")


def test_the_hosts_pre_vendor_key_is_still_inherited(mod, tmp_path: Path, monkeypatch) -> None:
    """The host raven is a separate checkout that holds web keys per tool, so
    its keys keep arriving in the old shape. Reading only the new path would
    silently stop inheriting them and refuse to launch a deploy that worked."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    (tmp_path / "host-config.json").write_text(
        json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}, "jinaApiKey": "host-jina"}}}),
        encoding="utf-8",
    )

    rendered = mod.render_config(_source(tmp_path))

    web = json.loads(rendered.read_text(encoding="utf-8"))["tools"]["web"]
    assert web["providers"]["serper"]["apiKey"] == "host-serper"
    assert web["providers"]["jina"]["apiKey"] == "host-jina"


def test_the_hosts_vendor_choice_is_inherited_when_the_folder_names_none(mod, tmp_path: Path, monkeypatch):
    """The host wizard is where a vendor gets picked, and a folder config that
    says nothing about vendors runs the way its host does. A folder that does
    name one keeps its own, and a vendor this checkout cannot serve is ignored."""
    src = tmp_path / "config.json"
    src.write_text(json.dumps({"tools": {"web": {"search": {"maxResults": 5}}}}), encoding="utf-8")
    host = tmp_path / "host.json"
    host.write_text(
        json.dumps(
            {
                "tools": {
                    "web": {
                        "search": {"provider": "tavily"},
                        "fetch": {"provider": "firecrawl"},
                        "providers": {"tavily": {"apiKey": "host-tv"}, "firecrawl": {"apiKey": "host-fc"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "HOST_CONFIG", host)
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-own")

    web = json.loads(mod.render_config(src).read_text(encoding="utf-8"))["tools"]["web"]
    assert web["search"]["provider"] == "tavily" and web["fetch"]["provider"] == "firecrawl"
    assert web["providers"]["tavily"]["apiKey"] == "host-tv"

    src.write_text(json.dumps({"tools": {"web": {"search": {"provider": "serper"}}}}), encoding="utf-8")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "own-serper")
    web = json.loads(mod.render_config(src).read_text(encoding="utf-8"))["tools"]["web"]
    assert web["search"]["provider"] == "serper", "the folder's own choice wins"

    host.write_text(json.dumps({"tools": {"web": {"search": {"provider": "not-a-vendor"}}}}), encoding="utf-8")
    src.write_text(json.dumps({"tools": {"web": {}}}), encoding="utf-8")
    web = json.loads(mod.render_config(src).read_text(encoding="utf-8"))["tools"]["web"]
    assert "provider" not in web["search"], "an unknown host vendor is not copied into a config that must load"


def test_a_host_vendor_without_a_key_here_is_not_inherited(mod, tmp_path: Path, monkeypatch):
    """The host wizard lets a keyed reader be picked with its key left blank and
    falls back to Jina at registration. The launcher's gate refuses that state,
    so copying the choice without the key would turn a host that runs into a
    sub-agent that exits; the choice is skipped and the default reader runs."""
    src = tmp_path / "config.json"
    src.write_text(json.dumps({"tools": {"web": {}}}), encoding="utf-8")
    host = tmp_path / "host.json"
    host.write_text(
        json.dumps({"tools": {"web": {"search": {"provider": "tavily"}, "fetch": {"provider": "firecrawl"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "HOST_CONFIG", host)
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-own")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "own-serper")

    web = json.loads(mod.render_config(src).read_text(encoding="utf-8"))["tools"]["web"]
    assert "provider" not in web["fetch"], "firecrawl needs a key none resolves; the default reader stays"
    assert "provider" not in web["search"], "tavily has no key here; the serper default the checkout can run stays"

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-env")
    web = json.loads(mod.render_config(src).read_text(encoding="utf-8"))["tools"]["web"]
    assert web["fetch"]["provider"] == "firecrawl", "a bare export counts, as it does for the gate itself"


def test_a_keyless_fetch_provider_refuses_to_launch(mod, searchable, tmp_path: Path, monkeypatch) -> None:
    """Jina reads pages without a key; AnySearch does not. Selecting it with no
    key would advertise a tool whose every call fails."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "openrouter", "model": "own-model"}},
                "tools": {"web": {"fetch": {"provider": "anysearch"}}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="selected fetch provider"):
        mod.render_config(source)


def test_a_keyless_fetch_fallback_refuses_to_launch(mod, searchable, tmp_path: Path, monkeypatch) -> None:
    """A fallback that cannot run is worse than none: it reads as insurance
    while being unreachable at the moment it is needed."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "openrouter", "model": "own-model"}},
                "tools": {"web": {"fetch": {"provider": "jina", "fallback": ["anysearch"]}}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="fallback fetch provider"):
        mod.render_config(source)


def test_the_default_fetch_provider_needs_no_key(mod, searchable, tmp_path: Path, monkeypatch) -> None:
    """Unauthenticated r.jina.ai works, and a dead key is worse than none, so a
    bare deploy must keep launching with page reading available."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    assert mod.render_config(_source(tmp_path)).exists()


def test_the_modes_are_declared_for_the_agent_to_compose(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlays used to be merged here, which fixed a connection's budget at
    launch. They are declared now: the agent composes a profile per session, so
    the same files become a catalogue `session/set_mode` picks from, and the
    source config stays the baseline every entry diffs against."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"custom": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "custom", "model": "own-model", "maxToolIterations": 40}},
                "drFlow": {"maxIterations": 20, "identityOverride": "the one prompt"},
            }
        ),
        encoding="utf-8",
    )
    mod.MODES_DIR.mkdir()
    (mod.MODES_DIR / "deep.json").write_text(
        json.dumps({"agents": {"defaults": {"maxToolIterations": 80}}, "drFlow": {"maxIterations": 40}}),
        encoding="utf-8",
    )
    (mod.MODES_DIR / "ultra.json").write_text(json.dumps({"drFlow": {"maxIterations": None}}), encoding="utf-8")

    rendered = mod.render_config(source, mode="deep")

    data = json.loads(rendered.read_text(encoding="utf-8"))
    assert data["acp"]["defaultMode"] == "deep"
    assert set(data["acp"]["modes"]) == {"fast", "deep", "ultra"}
    assert data["acp"]["modes"]["deep"]["drFlow"] == {"maxIterations": 40}
    assert data["acp"]["modes"]["deep"]["maxToolIterations"] == 80
    # The baseline is untouched, and IS the fast entry - which is why fast needs
    # no overlay file and carries an empty diff.
    assert data["drFlow"]["maxIterations"] == 20
    assert data["agents"]["defaults"]["maxToolIterations"] == 40
    assert data["acp"]["modes"]["fast"]["drFlow"] == {}


def test_an_explicit_null_in_an_overlay_survives_into_the_catalogue(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ultra clears the baseline's iteration cap by writing null, which the
    schema reads as "back to the built-in default" - so the null has to reach
    the agent rather than be dropped as an absent key on the way."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    mod.MODES_DIR.mkdir()
    (mod.MODES_DIR / "ultra.json").write_text(json.dumps({"drFlow": {"maxIterations": None}}), encoding="utf-8")

    rendered = mod.render_config(_source(tmp_path), mode="ultra")

    ultra = json.loads(rendered.read_text(encoding="utf-8"))["acp"]["modes"]["ultra"]["drFlow"]
    assert "maxIterations" in ultra and ultra["maxIterations"] is None


def test_the_identity_prompt_is_written_once_however_many_modes_there_are(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the catalogue carries diffs rather than merged blocks: a merged one
    would hold the whole identity prompt per mode, which is the drift the
    one-prompt-one-place rule exists to prevent."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "providers": {"custom": {"apiBase": "https://example.invalid/v1"}},
                "agents": {"defaults": {"provider": "custom", "model": "own-model"}},
                "drFlow": {"identityOverride": "UNIQUE-IDENTITY-MARKER"},
            }
        ),
        encoding="utf-8",
    )
    mod.MODES_DIR.mkdir()
    for name in ("deep", "ultra"):
        (mod.MODES_DIR / f"{name}.json").write_text(json.dumps({"drFlow": {"maxIterations": 40}}), encoding="utf-8")

    rendered = mod.render_config(source, mode=None)

    assert rendered.read_text(encoding="utf-8").count("UNIQUE-IDENTITY-MARKER") == 1


def test_a_folder_with_no_modes_directory_declares_none(
    mod, searchable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation path: no catalogue in the rendered config, which leaves
    the agent on the three built-in tiers trunk raven falls back to."""
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")

    rendered = mod.render_config(_source(tmp_path))

    assert "acp" not in json.loads(rendered.read_text(encoding="utf-8"))


def test_a_mode_without_an_overlay_file_refuses_to_launch(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_API_KEY", "k-llm")
    with pytest.raises(SystemExit, match="no overlay for mode"):
        mod.render_config(_source(tmp_path), mode="deep")


def test_the_shipped_overlays_carry_only_budget_knobs() -> None:
    """The real modes/ files must stay diffs: an overlay that re-declares the
    identity prompt or the provider block forks the agent, which is the drift
    the overlay design exists to prevent."""
    for name in ("deep", "ultra"):
        overlay = json.loads((_LAUNCHER.parent / "modes" / f"{name}.json").read_text(encoding="utf-8"))
        assert set(overlay) <= {"agents", "drFlow"}, name
        assert "identityOverride" not in overlay.get("drFlow", {}), name
        assert "toolsAllowlist" not in overlay.get("drFlow", {}), name


def test_the_report_template_is_the_baseline_s_and_no_overlay_moves_it() -> None:
    """`finalShape.reportDepth` selects the report the product ships, so it
    belongs to the baseline every mode inherits. An overlay that set it would
    make the deep entry differ from the default in the shape of its report
    rather than in how hard it looks for evidence - and the routing text sells
    the modes on the latter."""
    baseline = json.loads((_LAUNCHER.parent / "config.json").read_text(encoding="utf-8"))
    assert baseline["drFlow"]["finalShape"]["reportDepth"] is True
    for name in ("deep", "ultra"):
        overlay = json.loads((_LAUNCHER.parent / "modes" / f"{name}.json").read_text(encoding="utf-8"))
        assert "reportDepth" not in overlay.get("drFlow", {}).get("finalShape", {}), name


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
    negotiates them in the initialize handshake instead.

    One row, not one per mode: the effort level is a `session/set_mode` on the
    session the spawn opens, so three near-identical rows would be three names
    for one agent and three uncoordinated connection pools against one provider
    quota."""
    manifest = json.loads((_LAUNCHER.parent / "subagent.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "acp"
    assert manifest["command"].endswith("run.py")
    assert manifest["cwd"] == "{SUBAGENT_DIR}"
    assert manifest["readyTimeoutMs"] > 0
    assert manifest["timeout"] is None
    assert manifest["everos"]["agentId"] == "raven-research"
    for field in ("resumeCommand", "idSource", "transcriptFormat", "readsLocalFiles", "stateful"):
        assert field not in manifest
    # No sibling agents to route between -- and no mode instructions either. The
    # roster row exists as soon as the folder is installed, while the `mode`
    # property appears only once the probe has measured the agent's modes, so a
    # routing line naming them here would advertise, on a fresh install, an
    # argument the schema does not yet offer. The guidance rides on each mode's
    # own description instead, where it shares that predicate.
    # Positive first: an `owns` that lost its routing claim would satisfy both
    # `not in` checks below, and moving the mode guidance out of this field is
    # exactly the edit that could empty it.
    assert manifest["owns"].startswith("owns research:")
    assert "Do not research it yourself" in manifest["owns"]
    assert "Raven-Research-Deep" not in manifest["owns"]
    assert "mode=" not in manifest["owns"] and "`mode`" not in manifest["owns"]


def test_the_choice_guidance_travels_with_the_modes_that_offer_it() -> None:
    """One predicate behind the route and the advertisement. The launcher's mode
    blurbs reach a reader through the menu `subagents.instance.set_mode` answers
    with, and the clamp that fits a session tier onto this agent -- both built
    only from modes the probe actually measured, so guidance written here cannot
    outlive the rungs it describes. (The model is no longer among its readers:
    the spawn tool offers no mode.)

    Asserted on the rendered catalogue rather than on the source text: the block
    `mode_catalogue` emits is what lands in `acp.modes`, what the agent then
    advertises on every session response, and what the probe records. A test
    that greps MODE_LABELS passes even when nothing carries the text onward.

    Loaded without the `mod` fixture on purpose -- that fixture repoints
    MODES_DIR at a tmp dir, and the shipped overlays are the artefact here.
    """
    spec = importlib.util.spec_from_file_location("raven_research_launcher_real", _LAUNCHER)
    assert spec and spec.loader
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    catalogue = launcher.mode_catalogue()

    assert set(catalogue) == {"fast", "deep", "ultra"}
    blurbs = {mode: entry["description"] for mode, entry in catalogue.items()}
    # Each mode has to say when it is the right pick, not only what it does.
    assert "default" in blurbs["fast"].lower()
    assert "multi-faceted" in blurbs["deep"]
    assert "explicitly asked" in blurbs["ultra"]


def test_the_folder_ships_exactly_one_roster_manifest() -> None:
    """The collapse itself. A leftover `subagent.<mode>.json` would be
    rediscovered as its own row on every start and quietly restore the split."""
    assert sorted(p.name for p in _LAUNCHER.parent.glob("subagent*.json")) == ["subagent.json"]
