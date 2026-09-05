"""The agents/ pilot launcher: rendering, refusals, identity, and the exec.

The B-side product must hold the same launch contract as its vendored twin
while consuming installed raven: secrets merge into a rendered 0600 config
whose parent decides the data dir, the workspace is pinned and seeded with
the identity, refusals fire before a server that could not work starts, and
the exec targets ``python -m raven acp``. The strongest pin is the loader
round-trip: what the launcher renders, trunk raven's own loader loads.
"""

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
    assert list(modes) == ["fast", "deep", "ultra"] and data["acp"]["defaultMode"] == "fast"
    assert modes["fast"]["overlay"]["drFlow"] == {}
    assert modes["deep"]["overlay"]["drFlow"]["maxIterations"] == 30
    assert modes["ultra"]["overlay"]["drFlow"]["sufficiency"] == {"enabled": False}


def test_every_mode_ships_one_iteration_budget_the_loop_and_the_flow_share(grounded):
    """The number the loop enforces and the number the model is told are one.

    The vendored twin's loop overwrote its own cap with ``drFlow.maxIterations``
    (``self.max_iterations = self._dr_flow.max_iterations``), so a single number
    bounded the ReAct loop AND was the denominator the budget note and the spin
    breaker divided by. Here the loop reads ``acp.modes[*].maxToolIterations``
    and the flow reads its own knob, and nothing joined them: fast told the
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
        "fast": 20,
        "deep": 30,
        "ultra": 150,
    }
    for name, entry in modes.items():
        assert "maxToolIterations" not in entry["overlay"], (
            f"{name}: the cap reaches hooks as ctx.max_iterations, not as an overlay copy"
        )
    # ``ultra`` is the mode that declines the override (``maxIterations: null``),
    # so its budget is its own ``maxToolIterations`` rather than the baseline 20.
    assert modes["ultra"]["overlay"]["drFlow"]["maxIterations"] is None


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
    for name in ("deep.json", "ultra.json"):
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
    expected = {k: v for k, v in fork.items() if k not in TWIN_DRFLOW_EXCEPTIONS}
    for key in TWIN_DRFLOW_EXCEPTIONS:
        assert key in fork, f"{key} left the fork config; drop it from TWIN_DRFLOW_EXCEPTIONS"
        assert key not in twin, f"{key} is carried by {TWIN_DRFLOW_EXCEPTIONS[key]}, not by this slice"
    assert twin == expected


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

    for name, expected in (("fast", 20), ("deep", 30), ("ultra", 150)):
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
