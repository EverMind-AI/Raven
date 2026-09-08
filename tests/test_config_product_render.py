"""The launcher library: the product-agnostic machines behind agents/ launchers.

``tests/test_agents_research_launcher.py`` stays the behavioral pin for the
composed render; these pin each machine alone, with product-neutral tables,
so the next product inherits tested parts rather than a copy of run.py.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from raven.config import product_render as render


def test_env_wins_over_the_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nKEY=from-file\nOTHER = spaced \n", encoding="utf-8")
    monkeypatch.setenv("KEY", "from-env")
    assert render.env_value("KEY", env_file=f) == "from-env"
    monkeypatch.delenv("KEY")
    assert render.env_value("KEY", env_file=f) == "from-file"
    assert render.env_value("OTHER", env_file=f) == "spaced"
    assert render.env_value("ABSENT", env_file=f) is None
    assert render.env_value("KEY") is None, "no file, no env, no value"


def test_dig_and_put_roundtrip():
    data: dict = {}
    render.put(data, ("a", "b", "c"), "v")
    assert data == {"a": {"b": {"c": "v"}}}
    assert render.dig(data, ("a", "b", "c")) == "v"
    assert render.dig(data, ("a", "missing", "c")) == ""
    assert render.dig({"a": "leaf"}, ("a", "b")) == "", "a non-dict on the way is not an error"


def test_secret_slots_fall_back_to_the_host_except_required():
    slots = {"P_KEY": ("providers", "x", "apiKey"), "P_EXTRA": ("tools", "extra")}
    host = {"providers": {"x": {"apiKey": "host-key"}}, "tools": {"extra": "host-extra"}}

    config: dict = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": None, "P_EXTRA": None}.get
    )
    assert render.dig(config, ("tools", "extra")) == "host-extra"
    assert render.dig(config, ("providers", "x", "apiKey")) == "", "a required secret never inherits per-slot"

    config = {}
    render.apply_secret_slots(
        config, host, slots=slots, required=("P_KEY",), lookup={"P_KEY": "own", "P_EXTRA": "mine"}.get
    )
    assert render.dig(config, ("providers", "x", "apiKey")) == "own"
    assert render.dig(config, ("tools", "extra")) == "mine", "an own value beats the host fallback"


def test_inherit_llm_takes_the_hosts_brains_not_its_limits():
    host = {
        "providers": {"open": {"apiKey": "k"}},
        "routing": {"rules": []},
        "agents": {"defaults": {"provider": "open", "model": "m", "maxToolIterations": 40}},
    }
    config = {"agents": {"defaults": {"maxToolIterations": 20}}}
    taken = render.inherit_llm(config, host)
    assert "provider=open" in taken and "model=m" in taken
    assert config["providers"] == host["providers"]
    assert config["routing"] == {"rules": []}
    assert config["agents"]["defaults"]["provider"] == "open"
    assert config["agents"]["defaults"]["maxToolIterations"] == 20, "operating limits stay the product's"


def test_inherit_llm_declines_without_a_host_key():
    assert render.inherit_llm({}, {"providers": {"open": {"baseUrl": "u"}}}) == ""


def test_inherit_llm_honours_the_parent_riders_on_the_inheritance_branch(monkeypatch):
    """The fork launchers' riders, kept at the shared seat (G1): the cli
    dispatcher injects RAVEN_PARENT_MODEL / RAVEN_PARENT_REASONING_EFFORT per
    spawn; on the inheritance branch the model rider wins, the provider is
    re-inferred by models-list membership (the fork's back-query), and the
    effort rider overrides the host's copied reasoningEffort. Launch-time
    semantics -- weaker than the fork's per-turn form, ledgered in D3."""
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "open/parent-model")
    monkeypatch.setenv("RAVEN_PARENT_REASONING_EFFORT", "low")
    host = {
        "providers": {
            "open": {"apiKey": "k", "models": ["open/parent-model"]},
            "other": {"apiKey": "k2", "models": ["other/m"]},
        },
        "agents": {"defaults": {"provider": "other", "model": "other/m", "reasoningEffort": "high"}},
    }
    config = {}
    taken = render.inherit_llm(config, host)
    defaults = config["agents"]["defaults"]
    assert defaults["model"] == "open/parent-model"
    assert defaults["provider"] == "open", "provider re-inferred from the models list, not the host default"
    assert defaults["reasoningEffort"] == "low"
    assert "reasoning_effort=low" in taken


def test_inherit_llm_honours_parent_provider_and_protocol(monkeypatch):
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/openai/gpt-5.6-sol")
    monkeypatch.setenv("RAVEN_PARENT_PROVIDER", "openrouter")
    monkeypatch.setenv("RAVEN_PARENT_PROTOCOL", "responses")
    host = {
        "providers": {
            "openrouter": {"apiKey": "k", "models": ["openrouter/openai/gpt-5.6-sol"]},
            "custom": {"apiKey": "placeholder", "models": ["openai/gpt-5.6-sol"]},
        },
        "agents": {"defaults": {"provider": "custom", "model": "openai/gpt-5.6-sol"}},
    }
    config: dict = {}

    render.inherit_llm(config, host)

    defaults = config["agents"]["defaults"]
    assert defaults["provider"] == "openrouter"
    assert defaults["model"] == "openrouter/openai/gpt-5.6-sol"
    assert config["providers"]["openrouter"]["modelProtocols"] == {"openrouter/openai/gpt-5.6-sol": "responses"}


def test_inherit_llm_names_the_provider_from_the_model_prefix(monkeypatch):
    """A LiteLLM-backed parent sends no RAVEN_PARENT_PROVIDER, and a model the
    page typed in is listed under no block: the stored id's own prefix names
    the provider, and the host default stands in when even that fails."""
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "openrouter/some-new-model")
    monkeypatch.delenv("RAVEN_PARENT_PROVIDER", raising=False)
    host = {
        "providers": {"openrouter": {"apiKey": "k", "models": []}, "custom": {"apiKey": "k", "models": []}},
        "agents": {"defaults": {"provider": "custom", "model": "openai/x"}},
    }
    config: dict = {}
    render.inherit_llm(config, host)
    assert config["agents"]["defaults"]["provider"] == "openrouter"

    monkeypatch.setenv("RAVEN_PARENT_MODEL", "bare-model")
    config = {}
    render.inherit_llm(config, host)
    assert config["agents"]["defaults"]["provider"] == "custom", "host default rather than an empty provider"


def test_the_own_key_branch_never_reads_the_parent_riders(monkeypatch, tmp_path):
    """The riders belong to inheritance alone: a product paying with its own
    key keeps its own tuned model whatever the spawning parent runs -- the
    fork's own division, pinned at the launcher that exercises it."""
    import importlib.util

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESIGN_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DESIGN_API_KEY", "sk-own")
    monkeypatch.setenv("RAVEN_PARENT_MODEL", "vendor/parent-model")
    monkeypatch.setenv("RAVEN_PARENT_REASONING_EFFORT", "low")
    for name in ("DESIGN_ACP_HOME", "DESIGN_IMAGE_API_KEY", "DESIGN_SERPER_API_KEY", "DESIGN_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    launcher_path = Path(render.__file__).resolve().parents[2] / "agents" / "raven-design" / "run.py"
    spec = importlib.util.spec_from_file_location("design_run_riders", launcher_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = json.loads(mod.render_config(launcher_path.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["model"] == "openai/gpt-5.6-sol"
    assert data["agents"]["defaults"]["reasoningEffort"] == "high"


def test_state_root_prefers_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert render.product_state_root("prod-x", override=str(tmp_path / "s")) == tmp_path / "s"
    assert render.product_state_root("prod-x") == tmp_path / "home" / "workspace" / "subagent_sessions" / "prod-x"


def test_seed_once_writes_once(tmp_path):
    target = tmp_path / "deep" / "soul.md"
    calls: list[int] = []
    assert render.seed_once(target, lambda: calls.append(1) or "first") is True
    assert target.read_text() == "first"
    assert render.seed_once(target, lambda: calls.append(1) or "second") is False
    assert target.read_text() == "first", "a product update must not overwrite what an operator tuned"
    assert calls == [1], "the render is not evaluated when the target exists"


def test_mode_catalogue_assembles_the_acp_modes_contract(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"flow": {"maxIterations": 60}}), encoding="utf-8")
    labels = {"fast": ("Fast", "the default"), "deep": ("Deep", "longer"), "ghost": ("Ghost", "no file")}
    caps = {False: 20, True: 60}

    def resolve(overlay):
        return caps[bool(overlay)], {"flow": overlay.get("flow", {})}

    catalogue = render.mode_catalogue(
        tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=resolve
    )
    assert set(catalogue) == {"fast", "deep"}, "a labeled mode without its overlay file is skipped"
    assert catalogue["fast"]["overlay"] == {"flow": {}}, (
        "the overlay is the product's diff alone; the loop hands hooks the cap as ctx.max_iterations"
    )
    assert catalogue["deep"] == {
        "name": "Deep",
        "description": "longer",
        "maxToolIterations": 60,
        "overlay": {"flow": {"maxIterations": 60}},
    }


def test_mode_catalogue_lifts_a_modes_reasoning_effort_onto_the_entry(tmp_path):
    """The effort a mode asks for is the trunk's knob (`AcpModeConfig.reasoningEffort`),
    so it leaves the overlay and rides the entry, where the trunk dispenses it to
    every call a session in that mode makes. The overlay itself is untouched."""
    (tmp_path / "medium.json").write_text(
        json.dumps({"agents": {"defaults": {"reasoningEffort": "none"}}}), encoding="utf-8"
    )
    labels = {"high": ("High", "the baseline"), "medium": ("Medium", "no thinking")}

    catalogue = render.mode_catalogue(
        tmp_path, labels, baseline="high", overlay_keys=frozenset({"agents"}), resolve=lambda o: (150, o)
    )

    assert catalogue["medium"]["reasoningEffort"] == "none"
    assert catalogue["medium"]["overlay"] == {"agents": {"defaults": {"reasoningEffort": "none"}}}
    assert "reasoningEffort" not in catalogue["high"], "the baseline inherits agents.defaults"


def test_mode_catalogue_refuses_an_unknown_overlay_key(tmp_path):
    (tmp_path / "deep.json").write_text(json.dumps({"surprise": 1}), encoding="utf-8")
    labels = {"fast": ("Fast", "d"), "deep": ("Deep", "d")}
    with pytest.raises(SystemExit, match="surprise"):
        render.mode_catalogue(
            tmp_path, labels, baseline="fast", overlay_keys=frozenset({"flow"}), resolve=lambda o: (None, {})
        )


def test_mode_catalogue_is_empty_without_a_modes_dir(tmp_path):
    out = render.mode_catalogue(
        tmp_path / "absent",
        {"fast": ("F", "d")},
        baseline="fast",
        overlay_keys=frozenset(),
        resolve=lambda o: (None, {}),
    )
    assert out == {}


def test_sweep_removes_only_the_dead(tmp_path):
    own = tmp_path / f".config.rendered.{os.getpid()}.json"
    own.write_text("{}")
    dead = tmp_path / ".config.rendered.999999.json"
    dead.write_text("{}")
    malformed = tmp_path / ".config.rendered.notapid.json"
    malformed.write_text("{}")

    render.sweep_stale_renders(tmp_path)

    assert own.exists(), "a live server's render survives the sweep"
    assert not dead.exists()
    assert not malformed.exists()


def test_write_rendered_is_owner_only_and_pid_named(tmp_path):
    rendered = render.write_rendered({"a": 1}, tmp_path)
    assert rendered.name == f".config.rendered.{os.getpid()}.json"
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert json.loads(rendered.read_text()) == {"a": 1}


# --- product_acp_home: the engine home never inside the host's (w109) ---------


@pytest.fixture()
def homed(tmp_path, monkeypatch):
    """A scratch RAVEN_HOME with a host config the helper reads."""
    home = tmp_path / "rhome"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


def test_acp_home_defaults_into_the_data_dir_outside_the_host_home(homed):
    got = render.product_acp_home("raven-ppt")
    assert got == homed / "subagent_sessions" / "raven-ppt" / "acp"
    host = render.host_agent_home()
    assert host == homed / "workspace"
    assert got != host and host not in got.parents


def test_acp_home_falls_beside_a_host_home_that_swallows_the_default(homed):
    """An operator who points agents.defaults.workspace at RAVEN_HOME (or any
    ancestor) puts the data directory inside the very tree being handed over;
    the seat then falls beside that home, tagged per instance."""
    (homed / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(homed)}}}), encoding="utf-8"
    )
    got = render.product_acp_home("raven-ppt")
    assert got.parent.parent == homed.parent
    assert got.name == "acp"
    assert got.parent.name.startswith(".raven-ppt-")
    assert got != homed and homed not in got.parents


def test_acp_home_refuses_loud_when_no_outside_placement_exists(homed, monkeypatch):
    """A host home at the filesystem root leaves nowhere beside it; the
    refusal names the product's own override variable."""
    (homed / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": "/"}}}), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        render.product_acp_home("raven-ppt")
    assert "PPT_ACP_HOME" in str(caught.value)


def test_acp_home_refuses_an_uncreatable_placement_actionably(homed, monkeypatch):
    """Outside is not enough: a seat whose nearest existing ancestor is not
    writable is refused naming the override, not with a bare PermissionError."""
    monkeypatch.setattr(render.os, "access", lambda *_a, **_k: False)
    with pytest.raises(SystemExit) as caught:
        render.product_acp_home("raven-code")
    assert "CODE_ACP_HOME" in str(caught.value)
    assert "cannot be created" in str(caught.value)


def test_acp_home_override_wins_outright(homed):
    got = render.product_acp_home("raven-oncall", override="~/elsewhere/acp")
    assert got == render.Path("~/elsewhere/acp").expanduser()


def test_acp_home_var_derivation_matches_the_state_root_family():
    assert render._acp_home_var("raven-code") == "CODE_ACP_HOME"
    assert render._acp_home_var("raven-research-ng") == "RESEARCH_NG_ACP_HOME"


def _case_insensitive_fs(tmp_path) -> bool:
    """Probe, don't assume: APFS defaults one way, CI filesystems the other."""
    probe = tmp_path / "CaseProbe"
    probe.touch()
    return (tmp_path / "caseprobe").exists()


def test_acp_home_sees_through_a_case_variant_host_home(tmp_path, monkeypatch):
    """The HIGH-1 regression pin: a workspace spelled as a case variant of
    RAVEN_HOME names the same physical tree on a case-insensitive volume;
    resolve() keeps the given spelling, so a string comparison calls the
    default seat "outside" while it sits physically INSIDE the tree the host
    hands over. The guard asks the filesystem (samefile over existing
    ancestors) and must fall beside instead."""
    if not _case_insensitive_fs(tmp_path):
        pytest.skip("filesystem is case-sensitive; the alias cannot be built here")
    home = tmp_path / "rhome"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    variant = tmp_path / "RHOME"
    (home / "config.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(variant)}}}), encoding="utf-8"
    )
    got = render.product_acp_home("raven-ppt")
    assert os.path.samefile(variant, home), "probe premise: the two spellings are one directory"
    got_existing = next(a for a in (got, *got.parents) if a.exists())
    assert not os.path.samefile(got_existing, home)
    assert got.parent.name.startswith(".raven-ppt-")
    assert got.name == "acp"


def test_acp_home_instance_tag_tells_same_named_homes_apart(tmp_path, monkeypatch):
    """Two instances named alike under different parents must not share the
    sibling seat (the isolation RAVEN_HOME exists to give)."""
    tags = []
    for parent in ("one", "two"):
        home = tmp_path / parent / "rhome"
        home.mkdir(parents=True)
        monkeypatch.setenv("RAVEN_HOME", str(home))
        tags.append(render._instance_tag())
    assert tags[0] != tags[1]
    assert all(tag.startswith("rhome-") for tag in tags)
