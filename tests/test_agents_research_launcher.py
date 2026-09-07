"""The agents/ pilot launcher: rendering, refusals, identity, and the exec.

The B-side product must hold the same launch contract as its vendored twin
while consuming installed raven: secrets merge into a rendered 0600 config
whose parent decides the data dir, the workspace is pinned and seeded with
the identity, refusals fire before a server that could not work starts, and
the exec targets ``python -m raven acp``. The strongest pin is the loader
round-trip: what the launcher renders, trunk raven's own loader loads.
"""

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

#: The vendored twin's visible tool face -- its config disables everything
#: else its fork registers. The B side must show exactly this face: trunk
#: grew tools the fork never had (the deep_research offer stub above all,
#: which would have a research agent offering to outsource research), and
#: every one of them must be disabled by the product config, not by luck.
VENDORED_TOOL_FACE = {
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep",
    "find",
    "web_search",
    "web_fetch",
    "ask_user",
}

REPO = Path(__file__).resolve().parent.parent
RUN_PY = REPO / "agents" / "raven-research" / "run.py"


@pytest.fixture()
def launcher():
    spec = importlib.util.spec_from_file_location("agents_research_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def grounded(launcher, tmp_path, monkeypatch):
    """A launcher pointed at a scratch home and state root, secrets set."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RESEARCH_NG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("RESEARCH_NG_ACP_HOME", raising=False)
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-own")
    monkeypatch.setenv("RESEARCH_SERPER_API_KEY", "serper-key")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_JINA_API_KEY", raising=False)
    return launcher


def test_the_engine_home_is_never_inside_the_configured_host_home(grounded, tmp_path):
    """The w109 containment pin, both ways, plus the refusal half: the rendered
    engine home sits outside the host Agent home; the runtime guard accepts
    the host home as a session cwd against it, and still refuses the raven
    data directory and the engine home itself."""
    from raven.agent.workdir import validate_override

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    engine_home = Path(data["agents"]["defaults"]["workspace"]).resolve()
    host_home = (tmp_path / "home" / "workspace").resolve()
    assert engine_home != host_home and host_home not in engine_home.parents
    host_home.mkdir(parents=True, exist_ok=True)
    assert validate_override(str(host_home), agent_home=engine_home) == host_home
    with pytest.raises(ValueError):
        validate_override(str(tmp_path / "home"), agent_home=engine_home)
    with pytest.raises(ValueError):
        validate_override(str(engine_home), agent_home=engine_home)


def test_research_acp_home_override_wins_over_the_default(grounded, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_NG_ACP_HOME", str(tmp_path / "elsewhere" / "acp"))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "elsewhere" / "acp")


def test_the_render_merges_secrets_and_pins_the_workspace(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["providers"]["openrouter"]["apiKey"] == "sk-own"
    assert data["tools"]["web"]["search"]["apiKey"] == "serper-key"
    assert data["agents"]["defaults"]["workspace"] == str(
        tmp_path / "home" / "subagent_sessions" / "raven-research-ng" / "acp"
    )
    assert rendered.parent == tmp_path / "state"


def test_the_render_boards_the_flow_plugin_and_the_modes(grounded, tmp_path):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["plugins"]["dirs"] == [str(RUN_PY.parent / "plugins")]
    flow = data["plugins"]["config"]["research-flow"]
    assert flow["enabled"] is True and flow["version"].startswith("dr@")
    assert flow["stateRoot"] == str(tmp_path / "state" / "research_flow")
    assert data["context"]["dropSegments"] == ["identity", "memory", "active_skills", "skills"]
    modes = data["acp"]["modes"]
    assert list(modes) == ["medium", "high", "max"] and data["acp"]["defaultMode"] == "medium"
    assert modes["medium"]["overlay"]["drFlow"] == {}
    assert modes["high"]["overlay"]["drFlow"]["maxIterations"] == 30
    assert modes["max"]["overlay"]["drFlow"]["sufficiency"] == {"enabled": False}


def test_every_mode_ships_one_iteration_budget_the_loop_and_the_flow_share(grounded):
    """The number the loop enforces and the number the model is told are one.

    The vendored twin's loop overwrote its own cap with ``drFlow.maxIterations``
    (``self.max_iterations = self._dr_flow.max_iterations``), so a single number
    bounded the ReAct loop AND was the denominator the budget note and the spin
    breaker divided by. Here the loop reads ``acp.modes[*].maxToolIterations``
    and the flow reads its own knob, and nothing joined them: medium told the
    model ``iteration N/20`` on a turn the loop would let run to 40, and the
    breaker's ``minBudgetRatio: 0.5`` tripped at iteration 10 of 40 instead of
    20 of 20. The launcher resolves the twin's rule once and ships it to the
    loop as the mode's cap; a turn's hooks read the same number back off the
    iteration context (``ctx.max_iterations``, hook surface v3), so the
    overlay carries no copy.
    """
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    modes = data["acp"]["modes"]
    assert {name: entry["maxToolIterations"] for name, entry in modes.items()} == {
        "medium": 20,
        "high": 30,
        "max": 150,
    }
    for name, entry in modes.items():
        assert "maxToolIterations" not in entry["overlay"], (
            f"{name}: the cap reaches hooks as ctx.max_iterations, not as an overlay copy"
        )
    # ``max`` is the mode that declines the override (``maxIterations: null``),
    # so its budget is its own ``maxToolIterations`` rather than the baseline 20.
    assert modes["max"]["overlay"]["drFlow"]["maxIterations"] is None


def test_the_resolved_context_window_reaches_the_plugin_slice(grounded):
    """Both observers that divide by the window are handed it by the assembly.

    The twin CALLED its assembly with the loop's resolved window, so the note
    quoted the model the turn actually ran on. A plugin factory sees its own
    slice and nothing else, and with no window there the budget note drops its
    ``context ~N%`` clause and the spin breaker loses its context arm - two
    features that read as present and measure nothing.
    """
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    window = data["agents"]["defaults"]["contextWindowTokens"]
    assert window == 65536
    assert data["plugins"]["config"]["research-flow"]["contextWindowTokens"] == window


def test_the_shipped_config_claims_no_tool_fence_it_does_not_own(grounded):
    """``toolsAllowlist`` was the twin's fence and is not one here.

    In the fork it unregistered every tool it did not name. On the trunk the
    fence is ``tools.disabledTools``, one config level up, and the flow reads
    nothing - so a list shipped under the flow's own key described a fence that
    was not there. The face that fence produces is pinned separately; this pins
    that the config no longer claims to be what produces it.
    """
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    assert "toolsAllowlist" not in source["plugins"]["config"]["research-flow"]
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert "toolsAllowlist" not in data["plugins"]["config"]["research-flow"]
    assert data["tools"]["disabledTools"], "the fence that IS enforced stays declared"


def test_the_modes_are_the_vendored_twins_overlays(tmp_path):
    ours = RUN_PY.parent / "modes"
    theirs = REPO / "subagents" / "raven-research" / "modes"
    for name in ("high.json", "max.json"):
        assert json.loads((ours / name).read_text()) == json.loads((theirs / name).read_text())


def test_the_identity_is_the_vendored_twins_override_verbatim():
    fork = json.loads((REPO / "subagents" / "raven-research" / "config.json").read_text())
    assert (RUN_PY.parent / "soul.md").read_text().rstrip("\n") == fork["drFlow"]["identityOverride"].rstrip("\n")


#: The two keys the twins carry differently BY DESIGN, and where each went.
#: Everything else in the fork's ``drFlow`` must appear in the trunk slice verbatim.
TWIN_DRFLOW_EXCEPTIONS = {
    "identityOverride": "soul.md, pinned equal by the test above",
    "toolsAllowlist": "tools.disabledTools; the trunk's FlowConfig retires the key",
}

#: Keys both twins write and deliberately disagree on, with the reason. Unlike the table
#: above these must be PRESENT in both and UNEQUAL, so an entry cannot outlive the
#: divergence it records: when the fork catches up, this test reddens and the row goes.
TWIN_DRFLOW_DIVERGED = {
    "version": (
        "the trunk product's flow now carries the numeric-discipline rule in its deep "
        "report clause and in its reviewer rubric, and the fork's does not. Two "
        "distributions may not wear one label, so the trunk takes the next suffix; the "
        "fork's own label stays correct for the fork. Nothing is retired - "
        "SUPERSEDED_PROFILES is pinned to the fork's table and the fork's label is live"
    ),
}


def test_the_flow_slice_is_the_vendored_twins_drflow_verbatim():
    """Two launchers, one product: the trunk slice must be the fork's ``drFlow``.

    Until 2026-09-02 the fork ran ``search.includeSnippets`` and
    ``snippetDedupByDocid`` on (the measured arm's setting, and what the
    ``-derive`` label claims) while this slice wrote neither and the trunk
    schema defaulted both off - two distributions under one label, caught by a
    reviewer rather than a test. The modes and the identity were already pinned
    equal; this pins the rest, so the next fork edit reddens here instead. The
    class defaults underneath the slice are pinned separately, in
    ``test_agents_research_flow_parity.py``.
    """
    fork = json.loads((REPO / "subagents" / "raven-research" / "config.json").read_text())["drFlow"]
    twin = json.loads((RUN_PY.parent / "config.json").read_text())["plugins"]["config"]["research-flow"]
    skip = set(TWIN_DRFLOW_EXCEPTIONS) | set(TWIN_DRFLOW_DIVERGED)
    expected = {k: v for k, v in fork.items() if k not in skip}
    for key in TWIN_DRFLOW_EXCEPTIONS:
        assert key in fork, f"{key} left the fork config; drop it from TWIN_DRFLOW_EXCEPTIONS"
        assert key not in twin, f"{key} is carried by {TWIN_DRFLOW_EXCEPTIONS[key]}, not by this slice"
    for key, reason in TWIN_DRFLOW_DIVERGED.items():
        assert key in fork and key in twin, f"{key} is not written by both twins; drop it from the table"
        assert twin[key] != fork[key], f"{key} agrees again: drop it from TWIN_DRFLOW_DIVERGED ({reason})"
    assert {k: v for k, v in twin.items() if k not in skip} == expected


def test_the_products_own_label_still_loads_on_this_build():
    """The label the product ships is a live one, not a refused one.

    ``FlowConfig`` rejects a superseded profile by its whole name and a superseded base by
    its prefix, and the product's label is hand-written in ``config.json`` rather than
    taken from the field default - so the one thing a suffix move can do is make the
    product refuse to launch. Read through ``from_slice``, the loader the plugin uses.
    """
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import FlowConfig

    slice_ = json.loads((RUN_PY.parent / "config.json").read_text())["plugins"]["config"]["research-flow"]
    cfg = FlowConfig.from_slice(slice_)
    assert cfg.version == slice_["version"]
    assert cfg.version.split("-", 1)[0] not in FlowConfig._SUPERSEDED_VERSIONS
    assert cfg.version not in FlowConfig._SUPERSEDED_PROFILES


def test_the_label_this_product_left_behind_is_refused_and_names_its_successor():
    """A suffix move only means something if the old label stops loading here.

    The mirrored table cannot carry this one: it carries the fork's retirements, and the
    fork must go on accepting `-derive` because the fork's own distribution did not change. So
    the retirement lives in the product's own table, and without it a config still stamped
    with the old suffix would load clean and run the new distribution under the old name -
    the mislabelling the whole-profile check exists to prevent.
    """
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import PRODUCT_SUPERSEDED_PROFILES, SUPERSEDED_PROFILES, FlowConfig

    retired = "dr@3.5-filetools-askuser-derive"
    assert retired in PRODUCT_SUPERSEDED_PROFILES
    # And not in the mirrored one, which the parity test holds to the fork's retirements
    # (the twin may add its own at newer rungs, never drop or rewrite the fork's).
    assert retired not in SUPERSEDED_PROFILES

    with pytest.raises(ValueError) as excinfo:
        FlowConfig(enabled=True, version=retired)
    # The END of the chain, not the first hop: this label has been retired past more than
    # once, and naming a middle label would send an operator somewhere that also refuses.
    assert FlowConfig._resolve_successor(retired) in str(excinfo.value)

    # A disabled flow still loads: the label describes a distribution nothing will run.
    assert FlowConfig(enabled=False, version=retired).version == retired


def test_the_shipped_label_moves_when_the_shipped_prompt_does():
    """The check that would have caught this MR before review did.

    ``FlowConfig.version`` stamps a distribution, and ``finalShape.reportDepth`` is on in
    the shipped config, so the deep report clause is the model input every shipped run
    receives. A change to that clause under an unchanged label leaves two distributions
    answering to one published name, and results from before and after become
    indistinguishable - which is the whole reason the label exists.

    Nothing can infer intent, so this ties the two together the only way a test can: the
    label and a digest of the prompt the shipped config renders are pinned side by side.
    Editing the clause reddens this test, and the fix is to advance the product label,
    retire the one left behind in ``PRODUCT_SUPERSEDED_PROFILES``, and re-stamp here.
    That is a deliberate three-line chore, which is the point - it was a silent omission
    before.
    """
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.prompts import render_parts

    shipped = json.loads((RUN_PY.parent / "config.json").read_text())["plugins"]["config"]["research-flow"]
    shape = shipped["finalShape"]
    assert shape["reportDepth"] is True, "if reportDepth ships off, this pin is measuring the wrong clause"

    segment = render_parts(
        require_answer_marker=shape["requireMarker"],
        report_structure=shape["reportStructure"],
        report_format_override=shape["reportFormatOverride"],
        report_depth=shape["reportDepth"],
    )[1]
    digest = hashlib.sha256(" ".join(segment.split()).encode("utf-8")).hexdigest()[:16]

    assert shipped["version"] == "dr@3.7-filetools-askuser-derive-numeric-cite-rank"
    assert digest == "baf5019c4141a463", f"the shipped prompt moved; advance the label and re-stamp to {digest}"


def test_every_retirement_this_product_declares_names_a_label_it_can_load():
    """A successor that is itself refused would leave an operator with nowhere to go."""
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import PRODUCT_SUPERSEDED_PROFILES, FlowConfig

    for retired, successor in PRODUCT_SUPERSEDED_PROFILES.items():
        assert successor != retired
        # An intermediate successor may itself be retired - the table has held a chain
        # since 2026-09-07 - so what must load is where the chain ends.
        end = FlowConfig._resolve_successor(retired)
        assert FlowConfig(enabled=True, version=end).version == end
        assert FlowConfig._resolve_successor(end) is None, "the end of a chain is not itself retired"


def test_the_contract_is_seeded_beside_the_identity_once(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    profile = tmp_path / "home" / "subagent_sessions" / "raven-research-ng" / "acp" / "agent_memory" / "profile"
    soul = (profile / "soul.md").read_text()
    assert "## What actually decides this task" in soul, "measured guidance rendered into the identity"
    assert soul.index("## What actually decides this task") < soul.index("## Reading")
    contract = profile / "agent.md"
    text = contract.read_text()
    assert "## Reading" not in text, "the identity is soul.md's; agent.md carries only the contract"
    assert "contract" in text.lower()
    contract.write_text("operator tuned")
    grounded.render_config(RUN_PY.parent / "config.json")
    assert contract.read_text() == "operator tuned"


def test_the_rendered_file_is_owner_only(grounded):
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    config = load_config(rendered)
    assert config.agents.defaults.model == "openai/gpt-5.6-sol-pro"
    assert config.tools.web.search.api_key == "serper-key"


def test_the_identity_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    grounded.render_config(RUN_PY.parent / "config.json")
    soul = (
        tmp_path / "home" / "subagent_sessions" / "raven-research-ng" / "acp" / "agent_memory" / "profile" / "soul.md"
    )
    assert "research agent" in soul.read_text()
    soul.write_text("operator tuned")
    grounded.render_config(RUN_PY.parent / "config.json")
    assert soul.read_text() == "operator tuned"


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_the_llm_inherits_from_the_host_config(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"anthropic": {"apiKey": "sk-host"}},
                "agents": {"defaults": {"provider": "anthropic", "model": "claude-sonnet-5"}},
            }
        )
    )
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    data = json.loads(rendered.read_text())
    assert data["providers"]["anthropic"]["apiKey"] == "sk-host"
    assert data["agents"]["defaults"]["model"] == "claude-sonnet-5"
    assert data["agents"]["defaults"]["maxToolIterations"] == 40


def test_missing_search_key_refuses(grounded, monkeypatch):
    monkeypatch.delenv("RESEARCH_SERPER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_the_web_keys_and_the_proxy_reach_the_plugin_slice(grounded, monkeypatch):
    """The plugin REPLACES both web tools and is handed only its own slice.

    A key that lands on ``tools.web`` alone therefore configures the built-ins
    the plugin shadows and nothing the model can call: the launch succeeds, the
    tool is advertised, and every search answers "API key not configured".
    """
    monkeypatch.setenv("RESEARCH_JINA_API_KEY", "jina-key")
    monkeypatch.setenv("RESEARCH_WEB_PROXY", "http://127.0.0.1:7890")
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    flow = data["plugins"]["config"]["research-flow"]
    assert flow["search"]["apiKey"] == "serper-key"
    assert flow["fetch"]["apiKey"] == "jina-key"
    assert flow["proxy"] == "http://127.0.0.1:7890"
    assert flow["search"]["saturation"]["k"] == 5, "the mirror joins the flow's own search block"
    # And stays on the trunk surface: that is what the kernel's own tools read,
    # and what require_search consults before letting the server start.
    assert data["tools"]["web"]["search"]["apiKey"] == "serper-key"
    assert data["tools"]["web"]["jinaApiKey"] == "jina-key"
    assert data["tools"]["web"]["proxy"] == "http://127.0.0.1:7890"


def test_the_proxy_falls_back_to_the_host_config(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_WEB_PROXY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"proxy": "socks5://127.0.0.1:1080"}}}))
    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    assert data["tools"]["web"]["proxy"] == "socks5://127.0.0.1:1080"
    assert data["plugins"]["config"]["research-flow"]["proxy"] == "socks5://127.0.0.1:1080"


def test_the_search_env_var_is_the_tools_own(launcher):
    """The degraded path's pin, not the product's: research-flow REPLACES
    web_search, so in a healthy launch this module never serves. It is what
    answers when the plugin fails to board (an unreadable plugins.dirs entry,
    an import error in research_flow) -- the launch proceeds on built-ins, and
    the bare export require_search accepted must still reach the tool that
    actually runs. The healthy-path pin is the sibling test below.
    """
    from raven.agent.tools.web import SEARCH_PROVIDERS

    # The tool reads its env var off the vendor spec table, so the pin is the
    # table row, not a literal in the source.
    assert launcher.SEARCH_ENV_VAR == SEARCH_PROVIDERS["serper"].env_var


def test_the_search_env_var_is_the_plugin_tools_own(launcher):
    """The launcher accepts a bare export in place of the rendered key, so the
    tool that replaces the built-in has to read the same variable."""
    import inspect

    sys.path.insert(0, str(launcher.FLOW_PLUGIN_DIR))
    from research_flow.tools import web

    assert launcher.SEARCH_ENV_VAR in inspect.getsource(web)


def test_the_exec_targets_installed_raven(grounded, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["run.py"])
    with pytest.raises(AssertionError):
        grounded.main()
    ((binary, argv),) = [(c[0], c[1]) for c in calls]
    assert binary == sys.executable
    assert argv[:4] == [sys.executable, "-m", "raven", "acp"]
    assert argv[4] == "--config"


def _quiet_plugins(tmp_path, monkeypatch):
    from raven.core import plugin_stack

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


def test_the_pilots_tool_face_equals_the_vendored_twins(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the fork's nine and nothing more."""
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config
    from raven.contracts.llm_provider import LLMResponse
    from raven.core import runtime
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
        ):
            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "fake/default"

    _quiet_plugins(tmp_path, monkeypatch)
    rendered = grounded.render_config(RUN_PY.parent / "config.json")
    # The acp entrance's first act is set_config_path(rendered) -- the live
    # config (which serves disabledTools) reads that path, so the test does
    # what the transport does.
    import raven.home as home

    monkeypatch.setattr(home, "_current_config_path", rendered)
    config = load_config(rendered)
    ec_config = load_raven_config(rendered)
    assert ec_config.plugins.dirs == [str(RUN_PY.parent / "plugins")]
    rt = runtime.build_runtime(config, ec_config, provider=_StubProvider())
    try:
        visible = {d["function"]["name"] for d in rt.loop.tools.get_definitions()}
        from research_flow.tools.ask_user import DRAskUserTool
        from research_flow.tools.web import WebFetchTool, WebSearchTool

        search = rt.loop.tools.get("web_search")
        assert isinstance(search, WebSearchTool), "the plugin's search replaced the built-in"
        # The whole launch path in one assertion: .env -> rendered config ->
        # plugin slice -> the tool the model actually calls. Without it the
        # tool is registered and every call it makes is an error string.
        assert search.api_key == "serper-key", "the rendered Serper key reaches the tool the model calls"
        assert isinstance(rt.loop.tools.get("web_fetch"), WebFetchTool)
        assert isinstance(rt.loop.tools.get("ask_user"), DRAskUserTool)
        assert any(h.name == "ResearchFlowHook" for h in rt.loop.hooks._hooks), "the flow hook boarded the chain"
        assert [b.name for b in rt.loop.context_engine._builders][:1] == ["bootstrap"], "identity dropped"
    finally:
        rt.discard()
    assert visible == VENDORED_TOOL_FACE


def test_the_budget_the_launcher_ships_is_the_one_both_observers_divide_by(grounded, tmp_path):
    """The whole path in one assertion: rendered config -> slice -> observers.

    Two observers divide by these numbers - the budget note writes the quotient
    into the model's own history, the spin breaker gates on it - and both take
    them from the chain assembly, which is handed the slice and the mode's
    overlay and nothing else. Asserted per mode rather than once, because the
    mode is where the two settings used to disagree.
    """
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import FlowConfig
    from research_flow.flow import ResearchFlowHook, ToolHandles
    from research_flow.gates.budget_note import BudgetNoteObserver
    from research_flow.gates.spin_breaker import SpinEntryBreaker
    from research_flow.state import SessionStore

    from raven.contracts.loop_hooks import AgentHookContext

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    slice_ = data["plugins"]["config"]["research-flow"]
    cfg = FlowConfig.from_slice(slice_)
    hook = ResearchFlowHook(
        cfg=cfg,
        provider=None,
        tools=ToolHandles(),
        store=SessionStore(tmp_path / "flow"),
        max_iterations=cfg.max_iterations or 40,
        context_window_tokens=cfg.context_window_tokens or 0,
    )

    for name, expected in (("medium", 20), ("high", 30), ("max", 150)):
        entry = data["acp"]["modes"][name]
        slot = hook._resolve(
            AgentHookContext(
                session_key=f"s-{name}",
                iteration=1,
                max_iterations=entry["maxToolIterations"],
                context_window_tokens=65536,
                metadata={"mode": name, "mode_overlay": entry["overlay"]},
            )
        )
        # Unwrapped: the product runs the conversation surface, so every
        # observer in the chain arrives inside a GatedHook.
        built = [getattr(h, "inner", h) for h in slot.composite._hooks]
        note = next(h for h in built if isinstance(h, BudgetNoteObserver))
        breaker = next(h for h in built if isinstance(h, SpinEntryBreaker))
        assert (note._max_iterations, breaker._max_iterations) == (expected, expected), name
        assert entry["maxToolIterations"] == expected, f"{name}: and the loop enforces the same number"
        assert (note._context_window_tokens, breaker._context_window_tokens) == (65536, 65536), name


def test_a_typo_in_a_mode_overlay_refuses_to_launch(grounded, tmp_path, monkeypatch):
    """The vendored twin fails at startup naming the mode; the plugin's base slice
    ignores unknown keys, so without this door a mis-typed high.json would render,
    launch, and run the base value under the high label."""
    modes = tmp_path / "modes"
    modes.mkdir()
    for name in ("high.json", "max.json"):
        modes.joinpath(name).write_text((RUN_PY.parent / "modes" / name).read_text())
    bad = json.loads((modes / "high.json").read_text())
    bad["drFlow"].setdefault("budgetNote", {})["enabledd"] = True
    (modes / "high.json").write_text(json.dumps(bad))
    monkeypatch.setattr(grounded, "MODES_DIR", modes)

    with pytest.raises(SystemExit, match=r"high\.json.*budgetNote\.enabledd"):
        grounded.render_config(RUN_PY.parent / "config.json")


def test_no_shipped_mode_overlay_touches_ask_user():
    """[latent] DRAskUserTool is built once from the base config while the gates
    are rebuilt per (session, mode): a mode overlay that changed askUser would
    apply to the gate and silently not to the tool. No shipped mode does --
    pinned here so the day one wants to, the split surfaces in CI instead of a
    live session (rebuild the tool per mode first)."""
    import json as _json

    for overlay_file in sorted((RUN_PY.parent / "modes").glob("*.json")):
        overlay = _json.loads(overlay_file.read_text(encoding="utf-8"))
        assert "askUser" not in (overlay.get("drFlow") or {}), (
            f"{overlay_file.name} touches askUser: make the tool consult the per-mode "
            "config the gates already resolve before shipping this overlay"
        )


# --------------------------------------------------------------------------
# The web vendor: which backend, whose key, and the two staying in step
# --------------------------------------------------------------------------


def test_a_vendor_with_no_leaf_of_its_own_takes_the_products_env(grounded, monkeypatch):
    """The six vendors the vendor table added have no pre-vendor leaf, so their
    key lands in the vendor slot trunk raven reads first."""
    monkeypatch.setenv("RESEARCH_TAVILY_API_KEY", "tavily-key")

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())

    assert data["tools"]["web"]["providers"]["tavily"]["apiKey"] == "tavily-key"


def test_a_vendor_key_the_product_leaves_unset_is_inherited_from_the_host(grounded, tmp_path, monkeypatch):
    """The point of the slot table: a deployment configures a vendor once in the
    host raven instead of pasting the key into every product folder."""
    monkeypatch.delenv("RESEARCH_EXA_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"tools": {"web": {"providers": {"exa": {"apiKey": "host-exa-key"}}}}})
    )

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())

    assert data["tools"]["web"]["providers"]["exa"]["apiKey"] == "host-exa-key"


def test_a_host_that_wrote_the_default_vendor_to_the_vendor_slot_is_inherited(grounded, tmp_path, monkeypatch):
    """Serper and Jina keep their pre-vendor leaf, but a host configured after
    the vendor table landed writes to the vendor slot -- and the leaf is the
    only path the slot table inherits for them. Without the vendor-slot read a
    deployment that configured Serper centrally on a current raven launches
    keyless, which ``require_search`` then refuses."""
    monkeypatch.delenv("RESEARCH_SERPER_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"tools": {"web": {"providers": {"serper": {"apiKey": "host-serper-key"}}}}})
    )

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())

    assert data["tools"]["web"]["providers"]["serper"]["apiKey"] == "host-serper-key"
    # And it reaches the tool the model actually calls, not just the built-in.
    assert data["plugins"]["config"]["research-flow"]["search"]["apiKey"] == "host-serper-key"


def test_the_selected_vendor_and_its_key_reach_the_slice_together(grounded, monkeypatch):
    """The slice has no vendor table to consult, so the pair must already agree.

    Mirroring a fixed path would hand a Tavily-configured run the leftover
    Serper key to send to Tavily's endpoint: the launch succeeds, the tool is
    advertised, and every search comes back unauthorized -- which reads like a
    dead key rather than a wiring fault.
    """
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("RESEARCH_TAVILY_API_KEY", "tavily-key")

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())
    flow = data["plugins"]["config"]["research-flow"]

    assert flow["search"]["provider"] == "tavily"
    assert flow["search"]["apiKey"] == "tavily-key"
    # Not vacuous: the Serper key is configured on this render and must not be
    # the one that travels.
    assert data["tools"]["web"]["search"]["apiKey"] == "serper-key"


def test_the_default_readers_key_travels_beside_a_keyed_fetch_vendor(grounded, monkeypatch):
    """A keyed reader with no key is replaced by the default one, and that
    replacement has to arrive with its own credential.

    The slice carries one key per role, so unless the fallback's key is put
    there too the plugin has nothing to hand the reader it degraded to: the
    host's configured Jina quota serves raven's built-in fetch and not this
    one. Mirrored whenever the selection is not already the default rather than
    only when the fallback will fire -- the rule that decides that lives in
    ``WebFetchTool.effective_provider``, not here.
    """
    monkeypatch.setenv("RESEARCH_WEB_FETCH_PROVIDER", "tavily")
    monkeypatch.setenv("RESEARCH_TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("RESEARCH_JINA_API_KEY", "jina-key")

    fetch = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())["plugins"]["config"][
        "research-flow"
    ]["fetch"]

    assert fetch["provider"] == "tavily"
    assert fetch["apiKey"] == "tavily-key"
    assert fetch["fallbackApiKey"] == "jina-key"


def test_the_default_fetch_vendor_carries_no_fallback_of_its_own(grounded, monkeypatch):
    """It IS the fallback, so a second copy of its key would be one more place
    for the pair to disagree."""
    monkeypatch.delenv("RESEARCH_WEB_FETCH_PROVIDER", raising=False)
    monkeypatch.setenv("RESEARCH_JINA_API_KEY", "jina-key")

    fetch = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())["plugins"]["config"][
        "research-flow"
    ]["fetch"]

    assert fetch["provider"] == "jina"
    assert fetch["apiKey"] == "jina-key"
    assert "fallbackApiKey" not in fetch


def test_the_provider_choice_falls_back_to_the_host_config(grounded, tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("RESEARCH_BRAVE_API_KEY", "brave-key")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"provider": "brave"}}}}))

    data = json.loads(grounded.render_config(RUN_PY.parent / "config.json").read_text())

    assert data["tools"]["web"]["search"]["provider"] == "brave"
    assert data["plugins"]["config"]["research-flow"]["search"]["apiKey"] == "brave-key"


def test_search_refuses_to_launch_when_the_SELECTED_vendor_has_no_key(grounded, monkeypatch):
    """``require_search`` follows the selection, not the default. A configured
    Serper key does not make a Brave-selected run able to search, and search is
    what this agent is for: withheld, a run answers from the model's memory and
    reads as an ordinary run."""
    monkeypatch.setenv("RESEARCH_WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.delenv("RESEARCH_BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    with pytest.raises(SystemExit) as caught:
        grounded.render_config(RUN_PY.parent / "config.json")

    assert "brave" in str(caught.value) and "BRAVE_API_KEY" in str(caught.value)


@pytest.mark.parametrize(
    ("env", "typo", "meant"),
    [("RESEARCH_WEB_SEARCH_PROVIDER", "sepper", "serper"), ("RESEARCH_WEB_FETCH_PROVIDER", "jinaa", "jina")],
)
def test_a_typo_in_a_vendor_name_refuses_to_launch_naming_the_name(grounded, monkeypatch, env, typo, meant):
    """The plugin degrades an unknown vendor to its default, which is right for
    a slice nothing validates. Carried through the launcher, the same typo had
    ``require_search`` demand a key for a vendor that does not exist while the
    working Serper key sat in the config -- a refusal that sent the operator to
    set a credential they already had. The launcher can say what is wrong."""
    monkeypatch.setenv(env, typo)

    with pytest.raises(SystemExit) as caught:
        grounded.render_config(RUN_PY.parent / "config.json")

    message = str(caught.value)
    assert typo in message and meant in message
    assert "no key" not in message


def test_the_vendor_slots_are_the_trunks_own_vendors(launcher):
    """Every vendor trunk raven serves has a slot here, and no slot names a
    vendor it does not: a product that offers a backend the kernel cannot route
    advertises a choice that fails at the first call."""
    from raven.agent.tools.web import FETCH_PROVIDERS, SEARCH_PROVIDERS

    trunk = set(SEARCH_PROVIDERS) | set(FETCH_PROVIDERS)
    slotted = {
        path[3]
        for path in launcher.SECRET_SLOTS.values()
        if len(path) == 5 and path[:3] == ("tools", "web", "providers")
    }

    assert slotted | set(launcher.LEGACY_VENDOR_SLOTS) == trunk
    # And the names the launcher lets through are exactly the ones the kernel
    # routes: accepting one the tool table lacks advertises a vendor that
    # fails at the first call, refusing one it has withholds a working one.
    assert launcher.SEARCH_VENDORS == set(SEARCH_PROVIDERS)
    assert launcher.FETCH_VENDORS == set(FETCH_PROVIDERS)


def test_a_label_two_retirements_old_is_sent_to_the_end_of_the_chain():
    """The fork's table sends `-askuser` to `-derive`; this product sends `-derive` on.

    Naming the first hop would tell an operator to move to a label that also refuses,
    which is a worse answer than the one they arrived with.
    """
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import PRODUCT_SUPERSEDED_PROFILES, SUPERSEDED_PROFILES, FlowConfig

    two_hops = [old for old, mid in SUPERSEDED_PROFILES.items() if mid in PRODUCT_SUPERSEDED_PROFILES]
    assert two_hops, "no label is retired twice; this test has nothing to hold"
    for old in two_hops:
        # Walked rather than indexed twice: the chain has been three hops long since
        # 2026-09-07, and a test that assumes its length stops testing the resolution.
        end = SUPERSEDED_PROFILES[old]
        while end in PRODUCT_SUPERSEDED_PROFILES:
            end = PRODUCT_SUPERSEDED_PROFILES[end]
        assert FlowConfig._resolve_successor(old) == end
        with pytest.raises(ValueError) as excinfo:
            FlowConfig(enabled=True, version=old)
        assert end in str(excinfo.value)


def test_a_table_that_points_back_at_itself_stops_rather_than_hangs():
    sys.path.insert(0, str(RUN_PY.parent / "plugins" / "research-flow"))
    from research_flow.config import FlowConfig

    class _Cyclic(FlowConfig):
        _SUPERSEDED_PROFILES = {"dr@9.0-a": "dr@9.0-b", "dr@9.0-b": "dr@9.0-a"}
        _PRODUCT_SUPERSEDED_PROFILES: dict[str, str] = {}

    assert _Cyclic._resolve_successor("dr@9.0-a") == "dr@9.0-b"
