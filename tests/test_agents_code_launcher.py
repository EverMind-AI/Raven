"""The agents/ code launcher: rendering, refusals, the two hostings, the face.

The B-side product holds the same launch contract as its vendored twin while
consuming installed raven: secrets merge into a rendered 0600 config whose
parent decides the data dir, the workspace is pinned to the hosting's state
partition, the fork's TOOLS.md wording is seeded byte-for-byte, and the
first-write gate arms through the rendered code-flow slice -- the fork's
RAVEN_WORKSPACE_ALLOC_* env contract respelled as config (verdict D6). The
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
FORK = REPO / "subagents" / "raven-code"
FORK_TEMPLATE = FORK / "Raven-main" / "raven" / "templates" / "TOOLS.md"


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
    monkeypatch.setenv("CODE_API_KEY", "sk-own")
    monkeypatch.delenv("CODE_SERPER_API_KEY", raising=False)
    monkeypatch.delenv("CODE_JINA_API_KEY", raising=False)
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
        visible = {d["function"]["name"] for d in rt.loop.tools.get_definitions()}
        gates = [type(gate).__name__ for gate in rt.loop.tools.tool_gates]
    finally:
        rt.discard()
    return visible, gates


# --- byte parity: the one prompt asset and the roster identity --------------


#: The only bytes where the carried guide may differ from the fork template:
#: the search tool ships as find on trunk, and a guide teaching the fork's
#: glob spelling would never self-heal (Rank A audit, G3). The workbench-tool
#: mentions keep the fork's bytes -- their wave restores those tools.
GUIDE_RESPELLINGS = [
    (
        "## grep / glob — search, truncation, spill",
        "## grep / find — search, truncation, spill",
    ),
    (
        "- When `grep`/`glob` results overflow the cap",
        "- When `grep`/`find` results overflow the cap",
    ),
]


def _respelled_fork_template() -> bytes:
    text = FORK_TEMPLATE.read_text()
    for fork_spelling, product_spelling in GUIDE_RESPELLINGS:
        assert text.count(fork_spelling) == 1, fork_spelling
        text = text.replace(fork_spelling, product_spelling)
    return text.encode()


def test_the_carried_guide_is_the_forks_template_modulo_the_respellings():
    """The fork's TOOLS.md is a whole-file drift (exec sessions, background
    jobs, the 30k spill), not an appended section like oncall's; the product
    carries it as one asset, byte-equal after the enumerated respellings --
    pinned from both ends so neither side can drift silently."""
    carried = (RUN_PY.parent / "plugins" / "code-flow" / "prompts" / "TOOLS_CODE.md").read_bytes()
    assert carried == _respelled_fork_template()
    text = carried.decode()
    for fork_spelling, product_spelling in GUIDE_RESPELLINGS:
        assert fork_spelling not in text
        assert text.count(product_spelling) == 1


def test_the_seeded_guide_is_the_forks_wording(grounded, tmp_path):
    """What lands in the state partition equals what the fork engine wrote,
    modulo the same enumerated respellings the carried guide holds.

    Raven writes workspace templates only for files still missing, so seeding
    first keeps the fork's tool guidance in front of the model instead of the
    trunk template that would otherwise land there.
    """
    _render(grounded)
    seeded = (tmp_path / "state" / "acp" / "TOOLS.md").read_bytes()
    assert seeded == _respelled_fork_template()


def test_the_guide_is_seeded_once_and_never_overwritten(grounded, tmp_path):
    _render(grounded)
    guide = tmp_path / "state" / "acp" / "TOOLS.md"
    guide.write_text("operator tuned")
    _render(grounded)
    assert guide.read_text() == "operator tuned"


def test_the_roster_row_identity_is_the_vendored_twins():
    """The spawn-facing text the host router reads stays byte-identical."""
    ours = json.loads((RUN_PY.parent / "subagent.json").read_text())
    theirs = json.loads((FORK / "subagent.json").read_text())
    for field in (
        "name",
        "kind",
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
    # This product has no machine routing; mirroring the fork's absence is
    # what keeps the oncall routing red flag inapplicable here.
    assert "runsOnMachines" not in ours
    assert "runsOnMachines" not in theirs


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


# --- the render: secrets, pinning, the plugin, the gate slices ---------------


def test_the_render_merges_secrets_pins_workspace_and_arms_the_gate_slice(grounded, tmp_path):
    rendered = _render(grounded)
    data = json.loads(rendered.read_text())
    acp = (tmp_path / "state" / "acp").resolve()
    assert data["providers"]["custom"]["apiKey"] == "sk-own"
    assert data["agents"]["defaults"]["workspace"] == str(acp)
    flow = data["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is True
    assert flow["workspaceGate"] == {
        "allocBase": str(acp),
        "reposRoot": str(tmp_path / "state" / "repos"),
        "stateBucket": "acp",
    }
    assert rendered.parent == acp


def test_the_render_declares_the_plugin_dirs(grounded):
    """The swap's production-discovery line: the render names the plugin
    directory, so the code-flow gate, observer and hook board every serve.
    (Until the swap this was pinned NEGATIVE -- the fork loader forbade the
    key; that lane retired with the fork engine.)"""
    data = json.loads(_render(grounded).read_text())
    assert data["plugins"]["dirs"] == [str(RUN_PY.parent / "plugins")]


def test_a_config_without_the_flow_slice_still_gates(grounded, tmp_path, monkeypatch):
    """The launcher that arms the gate also flips it on: on a custom --config
    lacking the code-flow slice, the render carries enabled: true beside the
    arming, and the loop built from it casts the gate. The fork armed via env
    regardless of which config file it served, so the swapped product must
    not gate any narrower on a slice-less config. Pinned from both ends."""
    source = json.loads((RUN_PY.parent / "config.json").read_text())
    del source["plugins"]["config"]["code-flow"]
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(source))
    rendered = grounded.render_acp_config(custom)
    flow = json.loads(rendered.read_text())["plugins"]["config"]["code-flow"]
    assert flow["enabled"] is True
    _, gates = _hermetic_build(rendered, tmp_path, monkeypatch)
    assert gates == ["WorkspaceGate"], "an armed render must cast the gate even without a shipped slice"


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
    assert extensions.plugins.config["code-flow"]["workspaceGate"]["stateBucket"] == "acp"
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
    exec time; and the fork's env arming is gone -- the gate arms through the
    rendered slice, not the process environment."""
    calls = {}

    def fake_execv(binary, argv):
        calls["binary"] = binary
        calls["argv"] = list(argv)
        calls["rendered_alive"] = Path(argv[-1]).is_file()
        raise SystemExit(0)

    monkeypatch.setattr(grounded.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        grounded.serve(SimpleNamespace(config=str(RUN_PY.parent / "config.json")))

    acp = (tmp_path / "state" / "acp").resolve()
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


def test_the_cli_render_arms_the_one_conversation_layout(cli_run, tmp_path):
    """The CLI hosting arms the gate's other layout, the ruled config
    spelling of the fork's ALLOC_DIR/INSTANCE env pair: per-instance record
    beside the conversation's state, same repos root as the ACP hosting so
    the two hostings contend over the same checkouts."""
    invoke, calls = cli_run
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls["answer"] = "ok"
    invoke(_cli_args(workspace))
    state_dir = (tmp_path / "state" / "instance-conv-1").resolve()
    gate = calls["rendered"]["plugins"]["config"]["code-flow"]["workspaceGate"]
    assert gate == {
        "allocDir": str(state_dir),
        "instance": "instance-conv-1",
        "reposRoot": str(tmp_path / "state" / "repos"),
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
#: sessions, background jobs) and the todo list are FEASIBLE-TODAY product
#: waves that have not boarded the code-flow plugin. Ledgered, not lost: each
#: lands as a plugin contribution and leaves this set when it does.
PENDING_WAVE_TOOLS = {
    "exec_read",
    "exec_write",
    "job_cancel",
    "job_status",
    "job_wait",
    "todowrite",
}

#: One capability, two spellings: the fork's pathname-pattern tool answers to
#: ``glob``, trunk's to ``find``.
RESPELLED = {"glob": "find"}

#: The product's visible tool face, hermetically rebuilt from the render:
#: the fork's config intent minus the ledgered pending waves, respelled.
#: Trunk also grew six tools the fork never had, and every one must be
#: disabled by the product config, not by luck; the two playbook tools only
#: register outside this hermetic fixture, so their disable rows are the pin.
VENDORED_TOOL_FACE = {
    "ask_user",
    "edit_file",
    "exec",
    "find",
    "grep",
    "list_dir",
    "read_file",
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
    for real (its gate is cast over the registry), and the trunk-new six
    stay disabled by name."""
    visible, gates = _hermetic_build(_render(grounded), tmp_path, monkeypatch)
    assert visible == VENDORED_TOOL_FACE
    assert gates == ["WorkspaceGate"], "the write gate must be cast at assembly, from the rendered slice"
    disabled = set(json.loads((RUN_PY.parent / "config.json").read_text())["tools"]["disabledTools"])
    assert TRUNK_NEW_SIX <= disabled, "the trunk-new six stay disabled by config, not by luck"
    assert ACP_HOST_EXTRAS <= disabled, "the acp assembly extras stay disabled by config, not by luck"
    assert {"exec", "ask_user"} & disabled == set(), "the coding lane and the gate's asking channel stay open"
