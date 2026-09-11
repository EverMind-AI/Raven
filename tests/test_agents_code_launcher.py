"""The agents/ code launcher: rendering, refusals, the two hostings, the face.

The B-side product holds the same launch contract as its vendored twin while
consuming installed raven: secrets merge into a rendered 0600 config whose
parent decides the data dir, the workspace is pinned to the hosting's state
partition, the tool guide and coding conduct refresh untouched seeds, and the code-flow
and its tool face are switched on through the rendered slice (verdict D6; the
write gate and its worktree isolation retired, so nothing arms). The
ACP hosting execs ``python -m raven acp``; the CLI hosting runs one
adjudicated ``raven agent -m`` turn and owes the fork launcher's five
commitments (preamble, transcript verdict, changes footer, workspace pin,
exit 124 vs 1). The strongest pins are the trunk-loader round-trip and the
hermetic tool face. The fork-loader round-trip retired with the swap: the
render now names ``plugins.dirs``, which the fork's own loader forbids by
design -- the fork engine is no longer a consumer of this render, and the
vendored twin keeps its own untouched launcher and config.
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
# The vendored twin retired with its directory; what survives of it is the
# fixture the roster-identity pin still reads.
FORK = REPO / "tests" / "fixtures" / "vendored_fork" / "raven-code"


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
    monkeypatch.delenv("CODE_ACP_HOME", raising=False)
    monkeypatch.setenv("CODE_API_KEY", "sk-own")
    monkeypatch.delenv("CODE_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("CODE_JINA_API_KEY", raising=False)
    monkeypatch.delenv("CODE_PROJECT_FILES", raising=False)
    for retired in (
        "RAVEN_WORKSPACE_ALLOC_BASE",
        "RAVEN_WORKSPACE_ALLOC_REPOS",
        "RAVEN_WORKSPACE_STATE_BUCKET",
    ):
        monkeypatch.delenv(retired, raising=False)
    return launcher


def _render(grounded):
    return grounded.render_acp_config(RUN_PY.parent / "config.json")


def _hermetic_build(rendered, tmp_path, monkeypatch):
    """Build the runtime from a rendered config, hermetically: plugin
    discovery pinched to nothing (the render's plugins.dirs row is the only
    lane in), a stub provider, the config path pinned. Returns the visible
    tool names and the cast gates' class names."""
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
    import raven.home as home

    monkeypatch.setattr(home, "_current_config_path", rendered)
    config = load_config(rendered)
    ec_config = load_raven_config(rendered)
    rt = runtime.build_runtime(config, ec_config, provider=_StubProvider())
    try:
        definitions = [dict(d) for d in rt.loop.tools.get_definitions()]
        visible = {d["function"]["name"] for d in definitions}
        gates = [type(gate).__name__ for gate in rt.loop.tools.tool_gates]
    finally:
        rt.discard()
    return visible, gates, definitions


# --- the prompt assets: the guide and the conduct ---------------------------
#: The guide documents the product's real face and nothing the face lacks.
#: The fork template taught exec sessions and the job workbench; those tools
#: have not boarded (PENDING_WAVE_TOOLS below), so a guide still teaching them
#: would promise what the model cannot call. When their wave lands, the
#: sections come back with the tools.
GUIDE_MUST_NAME = ("exec", "run_in_background", "read_file", "write_file", "edit_file", "grep", "glob", "todo")
GUIDE_MUST_NOT_NAME = ("exec_write", "exec_read", "job_status", "job_wait", "job_cancel", "cron", "todowrite")
CARRIED_GUIDE = RUN_PY.parent / "plugins" / "code-flow" / "prompts" / "TOOLS_CODE.md"
CONDUCTS = {
    "default": RUN_PY.parent / "plugins" / "code-flow" / "prompts" / "CODE_CONDUCT_default.md",
    "anthropic": RUN_PY.parent / "plugins" / "code-flow" / "prompts" / "CODE_CONDUCT_anthropic.md",
}
#: Words that would tie the conduct to one evaluation harness rather than to
#: engineering; the prompt-wording rule (generic engineering only) bans them.
CONDUCT_MUST_NOT_NAME = ("TASK_COMPLETE", "SWE-bench", "benchmark", "harness", "grader")
#: The discipline, held apart from the conduct that carries it (the fork's own
#: shape) so it can be measured -- and dropped -- on its own.
DISCIPLINE = RUN_PY.parent / "plugins" / "code-flow" / "prompts" / "CODE_DISCIPLINE.md"


def test_the_carried_guide_teaches_the_face_it_ships():
    text = CARRIED_GUIDE.read_text()
    for name in GUIDE_MUST_NAME:
        assert name in text, f"the guide is silent about {name}"
    for name in GUIDE_MUST_NOT_NAME:
        assert name not in text, f"the guide promises {name}, which the face does not carry"


def test_the_seeded_guide_is_the_carried_asset(grounded, tmp_path):
    """What lands in the state partition equals the carried guide byte for
    byte. Raven writes workspace templates only for files still missing, so
    seeding first keeps this guidance in front of the model instead of the
    trunk template that would otherwise land there."""
    _render(grounded)
    seeded = (tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp" / "TOOLS.md").read_bytes()
    assert seeded == CARRIED_GUIDE.read_bytes()


def test_the_conduct_is_seeded_as_agent_md_for_the_configured_models_family(grounded, tmp_path):
    """config.json routes an anthropic model, so the anthropic variant lands
    as ``agent_memory/profile/agent.md`` -- the bootstrap file rendered right
    after the host identity, which stays (the runtime facts live there)."""
    _render(grounded)
    partition = tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp"
    seeded = (partition / "agent_memory" / "profile" / "agent.md").read_text()
    assert seeded == grounded.render_conduct("anthropic/claude-opus-5")
    assert "## Software Engineering Discipline" in seeded, "the discipline is spliced in, not referenced"
    assert "{{" not in seeded, "no sentinel survives the render"
    soul = partition / "agent_memory" / "profile" / "soul.md"
    assert soul.read_text() == grounded.SOUL.read_text(), "the product identity is seeded beside the conduct"
    assert "personal AI assistant" not in soul.read_text()


def test_the_soul_survives_the_cli_hostings_template_sync(grounded, tmp_path):
    """The one-turn CLI hosting runs ``raven agent``, whose startup syncs the
    workspace templates -- and trunk's SOUL.md template is a personal assistant
    with a personality. The sync only creates what is missing, so the soul this
    product seeds first is the one bootstrap reads, on this hosting as on ACP."""
    from raven.utils.workspace import sync_workspace_templates

    rendered = _render(grounded)
    partition = rendered.parent
    soul = partition / "agent_memory" / "profile" / "soul.md"
    conduct = partition / "agent_memory" / "profile" / "agent.md"
    before = (soul.read_bytes(), conduct.read_bytes())

    sync_workspace_templates(partition, silent=True)

    assert (soul.read_bytes(), conduct.read_bytes()) == before
    assert soul.read_text().startswith("# Soul\n\nI am Raven-Code, a coding agent.")
    assert "personal AI assistant" not in soul.read_text()


def test_the_soul_speaks_engineering_not_evaluation(grounded):
    text = grounded.SOUL.read_text()
    for word in CONDUCT_MUST_NOT_NAME:
        assert word not in text, f"the soul names {word}"


def test_the_published_slice_names_the_checkouts_instruction_files(grounded):
    """The coding set rides the flow slice, from the published file through
    the render; the launcher's setting empties it without a config edit."""
    published = json.loads((RUN_PY.parent / "config.json").read_text())
    assert published["plugins"]["config"]["code-flow"]["projectFiles"] == ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"]
    data = json.loads(_render(grounded).read_text())
    assert data["plugins"]["config"]["code-flow"]["projectFiles"] == ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"]


def test_the_checkouts_files_go_off_from_the_products_own_settings(grounded, monkeypatch):
    monkeypatch.setenv("CODE_PROJECT_FILES", "off")
    data = json.loads(_render(grounded).read_text())
    assert data["plugins"]["config"]["code-flow"]["projectFiles"] == []


def test_a_custom_config_without_the_names_still_gets_the_coding_set(grounded, tmp_path):
    """Same shape as the flow and tool switches: the launcher fills the names
    in for its own renders; an explicit empty list in a custom config wins."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    del source["plugins"]["config"]["code-flow"]["projectFiles"]
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_acp_config(custom).read_text())
    assert data["plugins"]["config"]["code-flow"]["projectFiles"] == ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"]

    source["plugins"]["config"]["code-flow"]["projectFiles"] = []
    custom.write_text(json.dumps(source))
    data = json.loads(grounded.render_acp_config(custom).read_text())
    assert data["plugins"]["config"]["code-flow"]["projectFiles"] == []


def test_the_conduct_variant_follows_the_model_family(launcher):
    assert launcher.conduct_source("anthropic/claude-opus-5") == CONDUCTS["anthropic"]
    assert launcher.conduct_source("claude-sonnet-4") == CONDUCTS["anthropic"]
    assert launcher.conduct_source("openai/gpt-5") == CONDUCTS["default"]
    assert launcher.conduct_source("deepseek/deepseek-v4") == CONDUCTS["default"]
    assert launcher.conduct_source(None) == CONDUCTS["default"]


def test_the_conduct_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    _render(grounded)
    conduct = tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp" / "agent_memory" / "profile" / "agent.md"
    conduct.write_text("operator tuned")
    _render(grounded)
    assert conduct.read_text() == "operator tuned"


def _launcher_render_conduct(variant: str) -> str:
    """The conduct as the launcher composes it, imported the way a test can."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("agents_code_run_render", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    model = "anthropic/claude-opus-5" if variant == "anthropic" else "deepseek/deepseek-v4"
    return mod.render_conduct(model)


@pytest.mark.parametrize("variant", sorted(CONDUCTS))
def test_the_conduct_speaks_engineering_not_evaluation(variant):
    """The wording rule: generic engineering only. No harness token, no
    benchmark name -- the conduct reads the same to a model on a real task and
    to one under measurement. And the stale-test rule carries its own baseline
    requirement, so a gate asking for that baseline enforces the prompt rather
    than contradicting it."""
    # The rendered composition, not the carried file: the carried one holds the
    # discipline sentinel, and what the wording rule binds is what the model
    # actually reads.
    text = _launcher_render_conduct(variant)
    for word in CONDUCT_MUST_NOT_NAME:
        assert word not in text, f"{variant}: {word!r} ties the conduct to a harness"
    assert "git stash -u" in text, "the stale-test exception names the baseline evidence it needs"
    for tool in ("glob", "grep", "read_file", "edit_file", "write_file", "run_in_background"):
        assert tool in text, f"{variant}: the tool policy is silent about {tool}"
    for absent in ("exec_write", "exec_read", "job_status", "session:"):
        assert absent not in text, f"{variant}: the conduct teaches {absent}, which the face lacks"
    assert "{{" not in text, "no unrendered sentinel"


def test_the_guide_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    _render(grounded)
    guide = tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp" / "TOOLS.md"
    guide.write_text("operator tuned")
    _render(grounded)
    assert guide.read_text() == "operator tuned"


def test_the_roster_row_identity_is_the_vendored_twins():
    """The spawn-facing text the host router reads stays byte-identical --
    except the description: the frozen A side still promises the worktree
    authorization the write gate used to ask for, and this side, which
    retired that gate, tells the orchestrator about parallel sessions
    instead."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text())
    theirs = json.loads((FORK / "subagent.json").read_text())
    for field in (
        "name",
        "kind",
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
    assert "worktree" not in ours["description"].lower(), "no retired promise in the roster line"
    assert "at the same time" in ours["description"], "the parallel-sessions guidance replaces it"
    # This product does not own run-and-watch work; mirroring the fork's
    # absence is what keeps the oncall steering inapplicable here.
    assert "ownsWatchedWork" not in ours
    assert "ownsWatchedWork" not in theirs


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


# --- the render: secrets, pinning, the plugin, the flow slice ----------------


def test_the_render_merges_secrets_pins_workspace_and_enables_the_flow(grounded, tmp_path):
    """The acp partition (the engine home, the rendered file's parent) sits in
    the raven DATA directory, outside the host Agent home the
    surfaces hand over as a session cwd (w109); the work -- repos, the state
    bucket -- stays under CODE_STATE_ROOT."""
    rendered = _render(grounded)
    data = json.loads(rendered.read_text())
    acp = (tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp").resolve()
    assert data["providers"]["custom"]["apiKey"] == "sk-own"
    assert data["agents"]["defaults"]["workspace"] == str(acp)
    flow = data["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is True
    assert "workspaceGate" not in flow, "the write gate retired; the render arms nothing"
    assert rendered.parent == acp


def test_the_engine_home_is_never_inside_the_configured_host_home(grounded, tmp_path):
    """The w109 containment pin, both ways: the rendered engine home is outside
    the host Agent home, and the runtime's own guard accepts the host home as a
    session working directory against that engine home -- the exact dispatch
    the web surface performs, which used to refuse."""
    from raven.agent.workdir import validate_override

    data = json.loads(_render(grounded).read_text())
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


def test_code_acp_home_override_wins_outright(grounded, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_ACP_HOME", str(tmp_path / "elsewhere" / "acp"))
    data = json.loads(_render(grounded).read_text())
    assert data["agents"]["defaults"]["workspace"] == str(tmp_path / "elsewhere" / "acp")


def test_the_render_declares_the_plugin_dirs(grounded):
    """The swap's production-discovery line: the render names the plugin
    directory, so the code-flow hook boards every serve.
    (Until the swap this was pinned NEGATIVE -- the fork loader forbade the
    key; that lane retired with the fork engine.)"""
    data = json.loads(_render(grounded).read_text())
    assert data["plugins"]["dirs"] == [str(RUN_PY.parent / "plugins")]


def test_a_config_without_the_flow_slice_still_enables_the_flow(grounded, tmp_path, monkeypatch):
    """The launcher flips the flow on: on a custom --config lacking the
    code-flow slice, the render still carries enabled: true, and the loop
    built from it casts no tool gate (the write gate retired). The fork armed
    via env regardless of which config file it served, so the swapped product
    must not behave any differently on a slice-less config. Pinned from both
    ends."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    del source["plugins"]["config"]["code-flow"]
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    rendered = grounded.render_acp_config(custom)
    flow = json.loads(rendered.read_text())["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is True
    _, gates, _defs = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert gates == [], "the flow casts no tool gate: the write gate retired with worktree isolation"


def test_an_operators_explicit_opt_out_survives_the_render(grounded, tmp_path):
    """setdefault, not assignment: D6's opt-out stays an operator's to make."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    source["plugins"]["config"]["code-flow"] = {"enabled": False}
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    flow = json.loads(grounded.render_acp_config(custom).read_text())["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is False


def test_everos_stays_factory_off_through_the_render(grounded):
    """The vendored twin ships everos double-off (backend null plus the
    plugin opt-out); the migrated factory state is the shipped state."""
    data = json.loads(_render(grounded).read_text())
    assert data["memory"]["backend"] is None
    assert "everos-memory" in data["plugins"]["disabled"]


def test_the_product_config_ships_compaction_enabled(grounded):
    """The trunk factory default is off; this product's slice turns it on,
    which is the fork's shipped posture (its knob defaults on). Pinned from
    the published file and through the trunk loader's reading of the render."""
    from raven.config.loader import load_config

    published = json.loads((RUN_PY.parent / "config.json").read_text())
    assert published["agents"]["defaults"]["compaction"] == {"enabled": True}
    config = load_config(_render(grounded))
    assert config.agents.defaults.compaction.enabled is True


def test_optional_keys_fall_back_per_slot_to_the_host_config(grounded, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"tools": {"web": {"search": {"apiKey": "host-serper"}}}}))
    data = json.loads(_render(grounded).read_text())
    assert data["tools"]["web"]["search"]["apiKey"] == "host-serper"


def test_the_rendered_file_is_owner_only(grounded):
    assert stat.S_IMODE(_render(grounded).stat().st_mode) == 0o600


def test_the_render_loads_through_trunks_own_loader(grounded):
    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    rendered = _render(grounded)
    config = load_config(rendered)
    assert config.agents.defaults.model == "anthropic/claude-opus-5"
    extensions = load_raven_config(rendered)
    assert extensions.plugins.disabled == ["everos-memory"]
    assert extensions.plugins.config["code-flow"] == {
        "enabled": True,
        "tools": {
            "enabled": True,
            "restrictToWorkspace": False,
            "exec": {"maxTimeout": 1200, "timeout": 600, "pathAppend": "", "sandboxBackend": "none"},
        },
        "projectFiles": ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"],
    }
    assert extensions.skill_forge.rewrite_enabled is False
    assert extensions.skill_forge.llm_gate_enabled is False


def test_no_llm_key_anywhere_refuses_before_serving(grounded, monkeypatch):
    """Refusal-before-serve survives the swap: the key check still precedes
    the exec, so no process starts and no secret-holding render survives."""
    monkeypatch.delenv("CODE_API_KEY", raising=False)
    monkeypatch.setattr(grounded.os, "execv", lambda *a: pytest.fail("execv must not be reached"))
    with pytest.raises(SystemExit):
        grounded.serve(SimpleNamespace(config=str(RUN_PY.parent / "config.json")))


# --- the acp exec lane: installed raven ---------------------------------------


def test_the_acp_exec_lane_is_installed_ravens(grounded, tmp_path, monkeypatch):
    """The served process is ``python -m raven acp`` on this interpreter,
    execed so it inherits this pid and stdio; the rendered file is on disk at
    exec time; and the fork's env arming is gone -- the flow is switched on
    through the rendered slice, not the process environment."""
    calls = {}

    def fake_execv(binary, argv):
        calls["binary"] = binary
        calls["argv"] = list(argv)
        calls["rendered_alive"] = Path(argv[-1]).is_file()
        raise SystemExit(0)

    monkeypatch.setattr(grounded.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        grounded.serve(SimpleNamespace(config=str(RUN_PY.parent / "config.json")))

    acp = (tmp_path / "home" / "subagent_sessions" / "raven-code" / "acp").resolve()
    assert calls["binary"] == sys.executable
    assert calls["argv"][:5] == [sys.executable, "-m", "raven", "acp", "--config"]
    rendered = Path(calls["argv"][5])
    assert rendered.parent == acp
    assert calls["rendered_alive"]
    for retired in (
        "RAVEN_WORKSPACE_ALLOC_BASE",
        "RAVEN_WORKSPACE_ALLOC_REPOS",
        "RAVEN_WORKSPACE_STATE_BUCKET",
    ):
        assert retired not in os.environ


# --- the cli hosting: one adjudicated turn ------------------------------------


def _cli_args(workspace: Path, session: str = "conv-1", **overrides):
    base = {
        "task": "fix the bug",
        "prompt_file": None,
        "session": session,
        "job": None,
        "workspace": str(workspace),
        "config": str(RUN_PY.parent / "config.json"),
        "timeout": 0,
        "keep_going": False,
        "verbose": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def cli_run(grounded, tmp_path, monkeypatch):
    """Run the CLI hosting with a fake engine; returns (invoke, calls).

    The fake stands in for the ``raven agent`` subprocess only -- git
    invocations (describe_changes) pass through to the real subprocess.run.
    ``calls['answer']`` seeds the transcript row the fake commits;
    ``calls['raise_timeout']`` kills the run the way a deadline does.
    """
    real_run = subprocess.run
    calls = {"answer": None, "raise_timeout": False, "rc": 0, "argv": None}

    def fake_run(argv, cwd=None, timeout=None, capture_output=False, text=False, **kwargs):
        if not (argv and argv[0] == sys.executable and argv[1:4] == ["-m", "raven", "agent"]):
            return real_run(argv, cwd=cwd, timeout=timeout, capture_output=capture_output, text=text, **kwargs)
        calls["argv"] = list(argv)
        calls["cwd"] = cwd
        calls["timeout"] = timeout
        calls["rendered"] = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
        session = argv[argv.index("--session") + 1]
        workspace = Path(argv[argv.index("--workspace") + 1])
        if calls["answer"] is not None:
            transcript = grounded.session_file(
                Path(calls["rendered"]["agents"]["defaults"]["workspace"]), workspace, session.partition(":")[2]
            )
            transcript.parent.mkdir(parents=True, exist_ok=True)
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"role": "assistant", "content": calls["answer"]}) + "\n")
        if calls["raise_timeout"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout or 0)
        return SimpleNamespace(returncode=calls["rc"], stderr="")

    monkeypatch.setattr(grounded.subprocess, "run", fake_run)

    def invoke(args):
        return grounded.run_task(args)

    return invoke, calls


def test_the_cli_lane_is_installed_ravens_agent_turn(cli_run, grounded, tmp_path, capsys):
    """The turn is ``python -m raven agent`` under the rendered config, run
    from the workspace (raven groups sessions by launch directory), on the
    full ``cli:<id>`` session form, markdown off, the task preambled -- and
    without the fork's two skill flags (verdict D5: installed raven has
    neither, and this product ships skillForge off)."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "done: the bug is fixed"
    rc = invoke(_cli_args(workspace))
    assert rc == 0
    assert calls["cwd"] == str(workspace.resolve())
    assert calls["argv"][:4] == [sys.executable, "-m", "raven", "agent"]
    assert calls["argv"][calls["argv"].index("--session") + 1] == "cli:conv-1"
    assert "--no-markdown" in calls["argv"]
    assert "--wait-skill-extract" not in calls["argv"]
    assert "--flush-skill-buffer" not in calls["argv"]
    assert calls["argv"][-2] == "-m"
    sent = calls["argv"][-1]
    assert sent.startswith("fix the bug\n\n---\nEnvironment: You are working in")
    assert capsys.readouterr().out.strip().startswith("done: the bug is fixed")
    rendered = Path(calls["argv"][calls["argv"].index("--config") + 1])
    assert not rendered.exists(), "the secret-holding render must not outlive the turn"


def test_the_cli_render_enables_the_flow_beside_the_conversation_state(cli_run, tmp_path):
    """The CLI hosting renders the same flow slice as the ACP hosting, and
    pins the workspace to the conversation's own state partition."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "ok"
    invoke(_cli_args(workspace))
    state_dir = (tmp_path / "state" / "instance-conv-1").resolve()
    assert calls["rendered"]["plugins"]["config"]["code-flow"] == {
        "enabled": True,
        "tools": {
            "enabled": True,
            "restrictToWorkspace": False,
            "exec": {"maxTimeout": 1200, "timeout": 600, "pathAppend": "", "sandboxBackend": "none"},
        },
        "projectFiles": ["AGENTS.md", "CLAUDE.md", "CONTEXT.md"],
    }
    assert calls["rendered"]["plugins"]["dirs"] == [str(RUN_PY.parent / "plugins")]
    assert calls["rendered"]["agents"]["defaults"]["workspace"] == str(state_dir)


def test_the_transcript_verdict_takes_the_last_committed_answer(grounded, tmp_path):
    """A row carrying tool_calls is a step, metadata is not a message, and
    only rows past the resume snapshot count."""
    transcript = tmp_path / "t.jsonl"
    rows = [
        {"_type": "metadata", "metadata": {}},
        {"role": "assistant", "content": "stale answer from the previous turn"},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "working", "tool_calls": [{"id": "1"}]},
        {"role": "assistant", "content": "the committed answer"},
    ]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert grounded.extract_answer(transcript) == "the committed answer"
    assert grounded.extract_answer(transcript, skip_lines=5) is None


def test_a_resumed_turn_cannot_replay_the_previous_answer(cli_run, tmp_path, capsys):
    """The second turn snapshots the transcript length first; producing
    nothing new is a failure, not a success carrying stale text."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "first turn answer"
    assert invoke(_cli_args(workspace)) == 0
    capsys.readouterr()
    calls["answer"] = None
    rc = invoke(_cli_args(workspace))
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_the_workspace_pointer_pins_the_conversation(grounded, tmp_path):
    """The first turn's workspace is recorded and replayed; a contradicting
    --workspace is refused rather than silently starting a new history."""
    state_dir = tmp_path / "instance"
    state_dir.mkdir()
    first = tmp_path / "repo-a"
    first.mkdir()
    assert grounded.resolve_workspace(state_dir, str(first)) == first.resolve()
    assert grounded.resolve_workspace(state_dir, None) == first.resolve()
    with pytest.raises(SystemExit) as excinfo:
        grounded.resolve_workspace(state_dir, str(tmp_path / "repo-b"))
    assert "already bound" in str(excinfo.value)


def test_the_task_preamble_is_task_first_and_resume_aware(grounded, tmp_path):
    """Task first (everos summarises the head of the message), environment
    after; a resumed turn says what is still there instead of re-introducing
    the directory as if it were fresh."""
    fresh = grounded.build_task("do the thing", tmp_path, resuming=False)
    assert fresh.startswith("do the thing\n\n---\nEnvironment: You are working in")
    resumed = grounded.build_task("do the thing", tmp_path, resuming=True)
    assert "continuing in" in resumed and "still present" in resumed


def test_a_session_flag_cannot_escape_the_state_root(grounded):
    assert "/" not in grounded.safe_name("../../elsewhere")
    assert grounded.safe_name("../../elsewhere") == "_.._elsewhere"


def test_the_session_transcript_path_is_ravens_own(grounded, tmp_path):
    """Computed by SessionManager, not reproduced: the grouping and the id
    escaping stay raven's functions, so they cannot drift apart."""
    from raven.session.manager import SessionManager
    from raven.utils.paths import project_slug

    partition = tmp_path / "part"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    expected = SessionManager(partition, project_slug=project_slug(workspace), project_dir=workspace).session_path(
        "cli:abc"
    )
    assert grounded.session_file(partition, workspace, "abc") == expected


def test_a_timeout_kill_exits_124_with_nothing_committed(cli_run, tmp_path, capsys):
    """GNU timeout's code, kept distinct from 1: a kill says nothing about
    the work, and the caller decides whether to rerun with a longer deadline."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["raise_timeout"] = True
    rc = invoke(_cli_args(workspace, timeout=5))
    assert rc == 124
    assert "TIMEOUT" in capsys.readouterr().out


def test_a_committed_answer_survives_the_timeout_kill(cli_run, tmp_path, capsys):
    """An answer that reached the transcript before the kill is the agent's
    own; the reply carries it plus the truth that the run was cut short."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "committed before the kill"
    calls["raise_timeout"] = True
    rc = invoke(_cli_args(workspace, timeout=5))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("committed before the kill")
    assert "timed out: killed after 5s, after this answer was committed" in out


def test_exit_1_is_a_config_or_credential_error(cli_run, tmp_path, capsys):
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["rc"] = 1
    rc = invoke(_cli_args(workspace))
    assert rc == 1
    assert "config or credential error" in capsys.readouterr().out


def test_keep_going_reports_instead_of_failing(cli_run, tmp_path, capsys):
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    rc = invoke(_cli_args(workspace, keep_going=True))
    assert rc == 0
    assert "(no answer committed)" in capsys.readouterr().out


def test_the_changes_footer_reports_the_working_tree(cli_run, tmp_path, capsys):
    """When the workspace is a checkout, the reply ends with the run's
    working-tree footprint -- where to look, not a pasted patch."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "new.txt").write_text("hi\n")
    calls["answer"] = "done"
    rc = invoke(_cli_args(workspace))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"--- working tree of {workspace.resolve()} after this run:" in out
    assert "?? new.txt" in out


def test_cli_diagnostics_go_to_the_log_not_the_reply(cli_run, grounded, tmp_path, capsys):
    """The caller's backend folds non-empty stderr into the conversation, so
    the CLI hosting logs to launcher.log; nothing but the reply on stdout,
    nothing at all on stderr."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "quiet"
    invoke(_cli_args(workspace))
    captured = capsys.readouterr()
    assert captured.err == ""
    log_text = (tmp_path / "state" / "instance-conv-1" / "launcher.log").read_text()
    assert "[run] workspace=" in log_text


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


# --- the visible tool face ----------------------------------------------------


#: The fork engine's config-intent face, measured: its AgentLoop built under
#: this product's published config (web on, no Serper key, the 11 fork
#: disable rows applied) advertises exactly these 15 tools.
FORK_CONFIG_INTENT = {
    "ask_user",
    "edit_file",
    "exec",
    "exec_read",
    "exec_write",
    "glob",
    "grep",
    "job_cancel",
    "job_status",
    "job_wait",
    "list_dir",
    "read_file",
    "todowrite",
    "web_fetch",
    "write_file",
}

#: The fork face the trunk lane does not show yet: the exec workbench (PTY
#: sessions, background jobs) needs the sandbox executor's session and job
#: seams, an engine-level wave. Ledgered, not lost: each lands as a plugin
#: contribution and leaves this set when it does. The todo list boarded as
#: code-flow's own ``todo`` contribution (code_flow/tools/), respelled below.
PENDING_WAVE_TOOLS = {
    "exec_read",
    "exec_write",
    "job_cancel",
    "job_status",
    "job_wait",
}

#: One capability, two spellings: the fork's pathname-pattern tool answers to
#: ``glob``, trunk's to ``find``. code-flow's tool face contributes ``glob``
#: and the product config withholds ``find``, so that pair is not respelled.
#: The checklist is: the fork itself renamed ``todowrite`` to ``todo`` (one
#: read/write tool, Raven-X 40a035cc) and kept the old name as a hidden
#: alias; the trunk registry has no hidden aliases, so the face carries the
#: new name alone and ``cast_params`` still accepts the old call shape.
RESPELLED: dict[str, str] = {"todowrite": "todo"}

#: The product's visible tool face, hermetically rebuilt from the render:
#: the fork's config intent minus the ledgered pending waves.
#: Trunk also grew six tools the fork never had, and every one must be
#: disabled by the product config, not by luck; the two playbook tools only
#: register outside this hermetic fixture, so their disable rows are the pin.
VENDORED_TOOL_FACE = {
    "ask_user",
    "edit_file",
    "exec",
    "glob",
    "grep",
    "list_dir",
    "read_file",
    "todo",
    "web_fetch",
    "write_file",
}

#: Tools trunk's acp assembly wires that the fork's never registered; they
#: board the live face past the hermetic fixture, so only their disable rows
#: hold the line (Rank A audit, G1).
ACP_HOST_EXTRAS = {"cron"}

#: Tools trunk grew after the fork was cut; none may reach this product's face.
TRUNK_NEW_SIX = {
    "create_playbook",
    "deliver_files",
    "find_skill",
    "load_playbook",
    "plugin",
    "run_subagent_dag",
}


def test_the_face_arithmetic_is_the_ledger():
    """The literal above is not free-standing: it is the measured fork intent
    minus the ledgered pending waves, respelled -- so a tool can only leave
    or join the face by moving on this ledger."""
    expected = (FORK_CONFIG_INTENT - PENDING_WAVE_TOOLS - set(RESPELLED)) | set(RESPELLED.values())
    assert VENDORED_TOOL_FACE == expected


def test_the_products_tool_face_is_the_forks_config_intent_minus_the_ledger(grounded, tmp_path, monkeypatch):
    """Build the loop from the rendered config; the model-visible tool set is
    the ledgered face and nothing more, the code-flow plugin is discovered
    for real (and casts no tool gate), and the trunk-new six stay disabled
    by name."""
    visible, gates, definitions = _hermetic_build(_render(grounded), tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE
    assert gates == [], "no tool gate is cast: the write gate retired with worktree isolation"
    # The names alone are not the face: the plugin's tools replace the built-ins
    # BY NAME, so a change that registers them before the built-ins instead of
    # after leaves this set identical while every schema silently reverts to the
    # host's spelling -- and the guide and the conduct teach the fork's.
    schemas = {d["function"]["name"]: d["function"]["parameters"] for d in definitions}
    assert schemas["read_file"]["required"] == ["file_path"]
    assert schemas["write_file"]["required"] == ["file_path", "content"]
    assert schemas["edit_file"]["required"] == ["file_path", "old_string", "new_string"]
    assert "occurrence" in schemas["edit_file"]["properties"]
    assert "path" not in schemas["read_file"]["properties"]
    disabled = set(json.loads((RUN_PY.parent / "config.json").read_text())["tools"]["disabledTools"])
    assert TRUNK_NEW_SIX <= disabled, "the trunk-new six stay disabled by config, not by luck"
    rendered_disabled = set(json.loads(_render(grounded).read_text())["tools"]["disabledTools"])
    superseded = grounded.superseded_host_tools()
    assert set(superseded) <= rendered_disabled, "a host name this face replaces is withheld while it serves"
    assert not set(superseded) & disabled, "and the withholding is rendered, not written into the product config"
    assert set(superseded.values()) <= VENDORED_TOOL_FACE, "every replacement in the table is really served"
    assert set(superseded) & VENDORED_TOOL_FACE == set(), "a name we serve ourselves needs no entry"
    assert ACP_HOST_EXTRAS <= disabled, "the acp assembly extras stay disabled by config, not by luck"
    assert {"exec", "ask_user"} & disabled == set(), "the coding lane and the asking channel stay open"


# --- the permission gate: only the hosting with nobody to ask opens it ------------


def test_the_acp_render_leaves_the_ask_tier_alone(grounded):
    """Trunk's permission gate (permissions.mode, default ``ask``) prompts a
    person before a write or a command, and refuses outright when the turn is
    not interactive. The ACP hosting keeps that default on purpose: raven
    dispatching a sub-agent answers those prompts itself
    (``raven/acp_client/permissions.py`` approves every one), and a person in
    an editor should still be asked -- opening the tier product-wide would
    take their prompt away for good."""
    from raven.config.loader import load_config

    config = load_config(_render(grounded))
    assert config.permissions.mode == "ask"
    assert "permissions" not in json.loads((RUN_PY.parent / "config.json").read_text())


def test_the_one_shot_render_opens_the_ask_tier_because_nobody_can_answer(grounded, tmp_path):
    """One CLI turn prints a reply and exits, so there is no channel to ask on
    and the gate refuses every write instead of prompting (measured
    2026-09-08: the model could not edit one line and reported the task
    incomplete). Trunk's own one-shot spine names this the operator's call.
    Builtin refusals -- the catastrophic-command list -- hold in every mode."""
    from raven.config.loader import load_config

    rendered = grounded.render_config(RUN_PY.parent / "config.json", tmp_path / "cli", unattended=True)
    assert load_config(rendered).permissions.mode == "full"


def test_an_explicit_permissions_block_wins_over_the_hosting_default(grounded, tmp_path):
    source = tmp_path / "tight.json"
    base = json.loads((RUN_PY.parent / "config.json").read_text())
    base["permissions"] = {"mode": "smart"}
    source.write_text(json.dumps(base))
    from raven.config.loader import load_config

    rendered = grounded.render_config(source, tmp_path / "cli2", unattended=True)
    assert load_config(rendered).permissions.mode == "smart"


# --- the effort tiers: the host's own ladder, declared for the agent to compose ---


def test_the_tool_slice_carries_the_products_workspace_fence(grounded, tmp_path):
    """The replacements are fenced exactly when the originals would have been.

    The host grants its own file tools the workspace root only when
    ``tools.restrictToWorkspace`` is on, and a plugin factory cannot read that
    field (``ServiceLocator`` does not grant it), so the
    launcher renders the answer into the slice. Without it a product that asked
    for the fence got replacements with no fence at all, and no warning."""
    data = json.loads(_render(grounded).read_text())
    assert data["plugins"]["config"]["code-flow"]["tools"]["restrictToWorkspace"] is False
    fenced = tmp_path / "fenced.json"
    base = json.loads((RUN_PY.parent / "config.json").read_text())
    base["tools"]["restrictToWorkspace"] = True
    fenced.write_text(json.dumps(base))
    rendered = json.loads(grounded.render_config(fenced, tmp_path / "part").read_text())
    assert rendered["plugins"]["config"]["code-flow"]["tools"]["restrictToWorkspace"] is True


def test_switching_the_tool_face_off_leaves_a_working_face(grounded, tmp_path):
    """``find`` is withheld only while the plugin is serving ``glob``. Written
    into the product config it would outlive the switch, and a product with the
    tool face off had no way to find a file at all."""
    off = tmp_path / "off.json"
    base = json.loads((RUN_PY.parent / "config.json").read_text())
    base["plugins"]["config"]["code-flow"] = {"enabled": True, "tools": {"enabled": False}}
    off.write_text(json.dumps(base))
    rendered = json.loads(grounded.render_config(off, tmp_path / "off-part").read_text())
    withheld = set(rendered["tools"]["disabledTools"])
    assert not set(grounded.superseded_host_tools()) & withheld, "the host's own names come straight back"


def test_the_conduct_is_reseeded_when_the_model_family_changes(grounded, tmp_path):
    """A partition is reused across renders, and seed-once alone froze whichever
    variant the first render picked: pointing the product at another family
    changed the rendered config and left the conduct behind, silently. A
    pristine seed of the other variant is replaced; anything an operator
    tuned in place is not."""
    partition = tmp_path / "part"
    grounded.seed_conduct(partition, "anthropic/claude-opus-5")
    conduct = partition / "agent_memory" / "profile" / "agent.md"
    assert conduct.read_text() == grounded.render_conduct("anthropic/claude-opus-5")
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    assert conduct.read_text() == grounded.render_conduct("deepseek/deepseek-v4"), "the family switch is followed"
    conduct.write_text("operator tuned")
    grounded.seed_conduct(partition, "anthropic/claude-opus-5")
    assert conduct.read_text() == "operator tuned", "an edited conduct is never overwritten"


def test_the_render_declares_the_effort_tiers_on_the_hosts_ladder(grounded):
    """The ids ARE the host's tier ladder: the host clamps its session tier onto
    the rungs an agent offers by name, and a rung it cannot rank would leave the
    agent on its default rather than guess. Each tier moves the reasoning
    effort and nothing else; the baseline inherits config.json's."""
    from raven.config.schema import DEFAULT_TIER, TIER_LADDER

    data = json.loads(_render(grounded).read_text())
    modes = data["acp"]["modes"]
    assert list(modes) == list(TIER_LADDER)
    assert data["acp"]["defaultMode"] == DEFAULT_TIER == grounded.BASELINE_MODE
    assert modes["medium"]["reasoningEffort"] == "medium"
    assert modes["max"]["reasoningEffort"] == "max"
    assert "reasoningEffort" not in modes["high"], "the baseline inherits config.json's effort"
    for entry in modes.values():
        assert entry["maxToolIterations"] is None, "a tier moves the effort, not the iteration cap"
        assert entry["overlay"] == {}, "nothing for the hooks to read"
        assert entry["name"] and entry["description"]


def test_the_shipped_overlays_carry_exactly_the_effort():
    for name in ("medium", "max"):
        overlay = json.loads((RUN_PY.parent / "modes" / f"{name}.json").read_text(encoding="utf-8"))
        assert overlay == {"agents": {"defaults": {"reasoningEffort": name}}}, name


def test_the_rendered_tiers_load_through_trunks_own_schema(grounded):
    from raven.config.loader import load_config

    config = load_config(_render(grounded))
    assert config.acp.modes["medium"].reasoning_effort == "medium"
    assert config.acp.modes["max"].reasoning_effort == "max"
    assert config.acp.modes["high"].reasoning_effort is None
    assert config.acp.effective_default_mode == "high"
    assert config.agents.defaults.reasoning_effort == "high"


def _ship_overlays(grounded, tmp_path, monkeypatch, **bodies):
    modes = tmp_path / "modes"
    modes.mkdir()
    for name, body in bodies.items():
        (modes / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(grounded, "MODES_DIR", modes)


def test_an_overlay_moving_anything_but_the_effort_refuses_to_launch(grounded, tmp_path, monkeypatch):
    """A knob the engine's mode profile does not carry would be declared and
    then silently ignored -- the launcher refuses instead."""
    _ship_overlays(
        grounded,
        tmp_path,
        monkeypatch,
        medium={"agents": {"defaults": {"reasoningEffort": "medium", "maxToolIterations": 60}}},
        max={"agents": {"defaults": {"reasoningEffort": "max"}}},
    )
    with pytest.raises(SystemExit, match="maxToolIterations"):
        _render(grounded)


def test_an_overlay_that_moves_nothing_refuses_to_launch(grounded, tmp_path, monkeypatch):
    _ship_overlays(grounded, tmp_path, monkeypatch, medium={"agents": {"defaults": {}}})
    with pytest.raises(SystemExit, match="reasoningEffort"):
        _render(grounded)


def test_a_mode_flag_starts_sessions_on_that_tier(grounded, monkeypatch):
    calls = {}

    def fake_execv(binary, argv):
        calls["rendered"] = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        raise SystemExit(0)

    monkeypatch.setattr(grounded.os, "execv", fake_execv)
    monkeypatch.setattr(sys, "argv", ["run.py", "--acp", "--mode", "max"])
    with pytest.raises(SystemExit):
        grounded.main()
    assert calls["rendered"]["acp"]["defaultMode"] == "max"
    assert list(calls["rendered"]["acp"]["modes"]) == ["medium", "high", "max"]


def test_the_mode_flag_is_refused_off_the_acp_path(grounded, monkeypatch):
    """A tier is a session-level choice the ACP client makes; the one-turn CLI
    path has no session to put it on, and accepting it there would be a flag
    that silently does nothing."""
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "x", "--mode", "medium"])
    with pytest.raises(SystemExit, match="--mode"):
        grounded.main()


# --- the product's own settings: the supersede table and the conduct switches ----


def test_the_supersede_table_lives_beside_the_tools_it_names(grounded):
    """Adding a tool is a line beside the tool, not an edit in the launcher.

    The table is the plugin's, and this pins the two ways it can go stale: an
    entry whose replacement this face does not actually serve, and a host name
    we serve ourselves (which needs no entry -- registering over it IS the
    replacement)."""
    table = grounded.superseded_host_tools()
    assert table == {"find": "glob"}, "the one capability whose name differs from trunk's"
    manifest = (RUN_PY.parent / "plugins" / "code-flow" / "raven-plugin.toml").read_text()
    for replacement in table.values():
        assert f'name = "{replacement}"' in manifest, replacement


def test_the_conduct_and_the_discipline_are_each_switchable(grounded, tmp_path, monkeypatch):
    """Two switches, because they answer different questions: whether this
    product speaks its own working rules at all, and whether the phased
    discipline for changing code is one of them. The fork held the discipline
    apart for the same reason -- it is the section worth measuring alone."""
    whole = grounded.render_conduct("deepseek/deepseek-v4")
    without = grounded.render_conduct("deepseek/deepseek-v4", discipline=False)
    assert "## Software Engineering Discipline" in whole and "## Software Engineering Discipline" not in without
    assert "## Tool usage policy" in without, "dropping the discipline keeps everything else"
    assert "{{" not in without and len(without) < len(whole)

    partition = tmp_path / "p"
    monkeypatch.setenv("CODE_CONDUCT_DISCIPLINE", "off")
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    conduct = partition / "agent_memory" / "profile" / "agent.md"
    assert conduct.read_text() == without

    # Back on: the composition changes, and a pristine copy follows it.
    monkeypatch.delenv("CODE_CONDUCT_DISCIPLINE")
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    assert conduct.read_text() == whole


def test_switching_the_conduct_off_removes_a_copy_this_product_wrote(grounded, tmp_path, monkeypatch):
    """Off has to mean gone: a workspace asset an earlier render wrote would
    keep reaching the model long after the switch said no."""
    partition = tmp_path / "p"
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    conduct = partition / "agent_memory" / "profile" / "agent.md"
    assert conduct.exists()
    monkeypatch.setenv("CODE_CONDUCT", "off")
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    assert not conduct.exists()
    # And off means nothing is written on a fresh partition either.
    fresh = tmp_path / "fresh"
    grounded.seed_conduct(fresh, "deepseek/deepseek-v4")
    assert not (fresh / "agent_memory" / "profile" / "agent.md").exists()


def test_an_edited_conduct_is_never_removed_by_the_switch(grounded, tmp_path, monkeypatch):
    partition = tmp_path / "p"
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    conduct = partition / "agent_memory" / "profile" / "agent.md"
    conduct.write_text("operator tuned")
    monkeypatch.setenv("CODE_CONDUCT", "off")
    grounded.seed_conduct(partition, "deepseek/deepseek-v4")
    assert conduct.read_text() == "operator tuned"


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [True, False])
async def test_workspace_restriction_reaches_real_product_file_tools(grounded, tmp_path, restricted):
    from raven.agent import workdir
    from raven.contracts.loop_hooks import AgentHookContext
    from raven.plugins.context import PluginContext, ServiceLocator

    source = tmp_path / "source.json"
    base = json.loads((RUN_PY.parent / "config.json").read_text())
    base["tools"]["restrictToWorkspace"] = restricted
    base["plugins"]["config"]["code-flow"]["tools"]["restrictToWorkspace"] = not restricted
    source.write_text(json.dumps(base))
    home, repo = tmp_path / "part", tmp_path / "repo"
    repo.mkdir()
    rendered = json.loads(grounded.render_config(source, home).read_text())
    from code_flow.flow import make_flow_hook
    from code_flow.tools import plugin

    ctx = PluginContext(
        config=rendered["plugins"]["config"]["code-flow"],
        services=ServiceLocator(workspace=home, user_id="u", agent_id="a"),
    )
    read, write, edit = plugin.make_read_file(ctx), plugin.make_write_file(ctx), plugin.make_edit_file(ctx)

    def text(result):
        return result.model_text if hasattr(result, "model_text") else str(result)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside original")
    with workdir.bind(repo):
        await make_flow_hook(ctx).before_iteration(AgentHookContext(session_key="cli:files", messages=[]))
        await write.execute(file_path="inside.txt", content="inside original")
        assert "inside original" in text(await read.execute(file_path="inside.txt"))
        await edit.execute(file_path="inside.txt", old_string="original", new_string="updated")
        assert (repo / "inside.txt").read_text() == "inside updated"
        result = text(await read.execute(file_path=str(outside)))
        if restricted:
            assert "outside allowed" in result.lower()
            assert "outside allowed" in text(await write.execute(file_path=str(outside), content="changed")).lower()
            assert (
                "outside allowed"
                in text(await edit.execute(file_path=str(outside), old_string="original", new_string="changed")).lower()
            )
            assert outside.read_text() == "outside original"
        else:
            assert "outside original" in result
            await edit.execute(file_path=str(outside), old_string="original", new_string="updated")
            assert outside.read_text() == "outside updated"
            await write.execute(file_path=str(outside), content="allowed write")
            assert outside.read_text() == "allowed write"


def test_rerendered_exec_settings_follow_the_host_and_preserve_its_sandbox(grounded, tmp_path):
    first = grounded.render_config(RUN_PY.parent / "config.json", tmp_path / "first")
    config = json.loads(first.read_text())
    config["tools"]["sandbox"]["backend"] = "auto"
    config["tools"]["exec"].update(timeout=23, pathAppend="/test/tools")
    source = tmp_path / "custom.json"
    source.write_text(json.dumps(config))
    rendered = json.loads(grounded.render_config(source, tmp_path / "second").read_text())
    section = rendered["plugins"]["config"]["code-flow"]
    from code_flow.tools import plugin

    from raven.plugins.context import PluginContext, ServiceLocator

    ctx = PluginContext(config=section, services=ServiceLocator(tmp_path / "second", "u", "a"))
    assert plugin.make_exec(ctx) is None, "a sandboxed shell must not be replaced with a host executor"
    assert section["tools"]["exec"]["timeout"] == 23
    assert section["tools"]["exec"]["pathAppend"] == "/test/tools"
    assert section["tools"]["exec"]["maxTimeout"] == 1200


def test_a_pristine_legacy_tool_guide_is_migrated_in_existing_partitions(grounded, tmp_path):
    from raven.agent.context import ContextBuilder

    partition = tmp_path / "existing"
    partition.mkdir()
    old = FORK / "Raven-main" / "raven" / "templates" / "TOOLS.md"
    (partition / "TOOLS.md").write_bytes(old.read_bytes())
    grounded.seed_guide(partition)
    system = ContextBuilder(partition).build_system_prompt()
    assert "run_in_background" in system
    assert "exec_write" not in system
    assert (partition / "TOOLS.md").read_text() == grounded.GUIDE.read_text()


def test_a_managed_tool_guide_refreshes_but_an_operator_edit_is_preserved(grounded, tmp_path, monkeypatch):
    partition = tmp_path / "part"
    grounded.seed_guide(partition)
    newer = tmp_path / "new-guide.md"
    newer.write_text("Updated tool guidance")
    monkeypatch.setattr(grounded, "GUIDE", newer)
    grounded.seed_guide(partition)
    assert (partition / "TOOLS.md").read_text() == "Updated tool guidance"
    (partition / "TOOLS.md").write_text("operator guidance")
    newer.write_text("Another product update")
    grounded.seed_guide(partition)
    assert (partition / "TOOLS.md").read_text() == "operator guidance"


@pytest.mark.parametrize("tools_enabled", [True, False])
def test_rendered_guidance_conditions_product_only_tools_on_the_actual_tool_list(
    grounded, tmp_path, monkeypatch, tools_enabled
):
    from raven.agent.context import ContextBuilder

    config = json.loads((RUN_PY.parent / "config.json").read_text())
    config["plugins"]["config"]["code-flow"]["tools"]["enabled"] = tools_enabled
    source = tmp_path / "config.json"
    source.write_text(json.dumps(config))
    partition = tmp_path / "part"
    rendered = grounded.render_config(source, partition)
    names, _gates, _definitions = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert ("todo" in names) is tools_enabled
    assert ("glob" in names) is tools_enabled
    assert ("find" in names) is not tools_enabled
    system = ContextBuilder(partition).build_system_prompt()
    assert "If the todo tool is available" in system
    assert "You have access to the todo tool" not in system
    assert "Only use tools and parameters exposed in the current function schemas" in system
    assert "otherwise use find" in system
