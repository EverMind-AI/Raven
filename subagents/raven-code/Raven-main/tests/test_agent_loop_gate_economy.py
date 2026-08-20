"""Gate economy: deficit-triggered firing, priority, and the round budget.

Wave10 mechanism rework. Measured motivation (opus48 gates-on run, 492 tasks):
verify_complete fired unconditionally on 97% of tasks, test_gate had no
changed-code precondition, mean 1.44 extra rounds per task (max 4). A gate must
name an observable deficit; when it cannot, it stays silent.

The stale-claim challenge (G2) and the baseline-evidence red template (G1) are
pinned to real trajectories: django-11885 / sympy-15976 escaped the old red
gate through its one-sentence "stale test" exit while the round that abused it
was running with gates ON.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _init_repo(path: Path) -> None:
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"),
    ):
        subprocess.run(("git", *args), cwd=path, check=True, env=env, capture_output=True)


class _ScriptedProvider(LLMProvider):
    """Plays a fixed list of responses; repeats the last one when nudged again."""

    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self._script = list(script)
        self.calls = 0

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
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="Done. Nothing further.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _tool(name: str, call_id: str, **arguments) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=12,
        restrict_to_workspace=True,
        interactive=False,
    )


def _gates(monkeypatch, **switches: str) -> None:
    """Explicitly pin every gate switch; unspecified ones are turned off."""
    defaults = {
        "RAVEN_REQUIRE_REAL_TEST_EVIDENCE": "off",
        "RAVEN_VERIFY_BEFORE_COMPLETE": "off",
        "RAVEN_GATE_STALE": "off",
        "RAVEN_GATE_RED": "off",
        "RAVEN_GATE_EMPTY_DIFF": "off",
    }
    defaults.update(switches)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RAVEN_GATE_MAX_ROUNDS", raising=False)


FAKE_GREEN_TEST = 'echo "2 passed"  # pytest'
FAKE_RED_TEST = 'echo "1 failed"  # pytest'


@pytest.mark.asyncio
async def test_verify_nudge_skipped_when_real_tests_ran(workspace, monkeypatch):
    """M1: a turn that produced real test evidence has no re-check deficit."""
    _gates(monkeypatch, RAVEN_VERIFY_BEFORE_COMPLETE="1")
    provider = _ScriptedProvider([
        _tool("exec", "t1", command=FAKE_GREEN_TEST),
        _text("All requirements verified. Done."),
    ])
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "fix the parser"}],
    )

    assert "verify_complete" not in outcome.gate_triggers
    assert not any("Before finalizing" in str(m.get("content")) for m in messages if m.get("role") == "user")


@pytest.mark.asyncio
async def test_verify_nudge_still_fires_without_any_evidence(workspace, monkeypatch):
    """M1 keeps the TB-relevant case: no test-shaped evidence -> one nudge."""
    _gates(monkeypatch, RAVEN_VERIFY_BEFORE_COMPLETE="1")
    provider = _ScriptedProvider([_text("Done.")])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "write the exporter"}],
    )

    assert outcome.gate_triggers.get("verify_complete") == 1


@pytest.mark.asyncio
async def test_test_gate_silent_when_nothing_changed(workspace, monkeypatch):
    """M2: no tracked edit and a clean git tree -> no "go run tests" ritual."""
    _gates(monkeypatch, RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1")
    _init_repo(workspace)
    provider = _ScriptedProvider([_text("The repository already satisfies this.")])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "does the parser handle empty input?"}],
    )

    assert "test_evidence" not in outcome.gate_triggers
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_test_gate_fires_after_code_edit_without_tests(workspace, monkeypatch):
    """M2 keeps the true deficit: edited code, never ran the project's tests."""
    _gates(monkeypatch, RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1")
    _init_repo(workspace)
    provider = _ScriptedProvider([
        _tool("write_file", "w1", path=str(workspace / "app.py"), content="x = 2\n"),
        _text("Fixed. Done."),
    ])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "fix app.py"}],
    )

    assert outcome.gate_triggers.get("test_evidence", 0) >= 1


@pytest.mark.asyncio
async def test_test_gate_edit_ledger_reads_file_path_argument(workspace, monkeypatch):
    """The schema name is file_path ("path" only survives as a legacy alias);
    the edit ledger must recognize an edit sent under the new name."""
    _gates(monkeypatch, RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1")
    _init_repo(workspace)
    provider = _ScriptedProvider([
        _tool("write_file", "w1", file_path=str(workspace / "app.py"), content="x = 2\n"),
        _text("Fixed. Done."),
    ])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "fix app.py"}],
    )

    assert outcome.gate_triggers.get("test_evidence", 0) >= 1


@pytest.mark.asyncio
async def test_round_budget_caps_total_gate_rounds(workspace, monkeypatch):
    """M3: all gates armed, stubborn no-op turn -> at most GATE_MAX_ROUNDS."""
    _gates(
        monkeypatch,
        RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1",
        RAVEN_VERIFY_BEFORE_COMPLETE="1",
        RAVEN_GATE_STALE="1",
        RAVEN_GATE_RED="1",
        RAVEN_GATE_EMPTY_DIFF="1",
    )
    _init_repo(workspace)
    provider = _ScriptedProvider([_text("Nothing to do here.")])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "handle the edge case"}],
    )

    assert sum(outcome.gate_triggers.values()) <= 2


@pytest.mark.asyncio
async def test_round_budget_of_one_prefers_empty_diff_over_verify(workspace, monkeypatch):
    """M3 priority: with one round to spend, the concrete deficit (empty diff)
    outranks the generic re-check."""
    _gates(
        monkeypatch,
        RAVEN_VERIFY_BEFORE_COMPLETE="1",
        RAVEN_GATE_EMPTY_DIFF="1",
    )
    monkeypatch.setenv("RAVEN_GATE_MAX_ROUNDS", "1")
    _init_repo(workspace)
    provider = _ScriptedProvider([_text("Nothing to do here.")])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "handle the edge case"}],
    )

    assert outcome.gate_triggers == {"empty_diff": 1}


@pytest.mark.asyncio
async def test_stale_claim_challenge_fires_on_unbacked_rationalization(workspace, monkeypatch):
    """G2: finishing by declaring failures 'updated by the test patch' without
    any baseline comparison earns exactly one evidence request."""
    _gates(monkeypatch, RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1", RAVEN_GATE_RED="1")
    _init_repo(workspace)
    provider = _ScriptedProvider([
        _tool("write_file", "w1", path=str(workspace / "app.py"), content="x = 2\n"),
        _tool("exec", "t1", command=FAKE_GREEN_TEST),
        _text(
            "Done. The only failures assert the old behavior and are exactly "
            "the tests the PR's test patch updates."
        ),
        _text("Baseline checked as requested; judgement stands. Done."),
    ])
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "fix app.py"}],
    )

    assert outcome.gate_triggers.get("stale_claim") == 1
    nudges = [
        m for m in messages
        if m.get("role") == "user" and "baseline" in str(m.get("content")).lower()
    ]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_stale_claim_exempt_after_baseline_action(workspace, monkeypatch):
    """G2/M4: a model that already ran its own baseline comparison is not
    asked again (the evidence-in-hand exemption)."""
    _gates(monkeypatch, RAVEN_REQUIRE_REAL_TEST_EVIDENCE="1", RAVEN_GATE_RED="1")
    _init_repo(workspace)
    provider = _ScriptedProvider([
        _tool("write_file", "w1", path=str(workspace / "app.py"), content="x = 2\n"),
        _tool("exec", "s1", command="git stash list || true"),
        _tool("exec", "t1", command=FAKE_GREEN_TEST),
        _text(
            "Done. The only failures assert the old behavior and are exactly "
            "the tests the PR's test patch updates (baseline compared)."
        ),
    ])
    agent = _agent(workspace, provider)

    _f, _t, _messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "fix app.py"}],
    )

    assert "stale_claim" not in outcome.gate_triggers


class TestGateTexts:
    def test_red_template_demands_baseline_evidence_with_safe_sequence(self):
        text = AgentLoop._TEST_GATE_RED_NUDGE_TMPL
        assert "git stash -u" in text
        assert "/tmp/mypatch.diff" in text
        assert "git stash pop" in text

    def test_red_template_keeps_the_soft_exits(self):
        # Replay data: 10 legitimate finish-red tasks. Removing the exit turns
        # this back into the hard gate the replay data already vetoed.
        text = " ".join(AgentLoop._TEST_GATE_RED_NUDGE_TMPL.split())
        assert "cite that result" in text
        assert "say why" in text
        assert "do NOT weaken your fix" in text

    def test_stale_claim_nudge_carries_the_recall_scope_clause(self):
        # R0 proximal placement: the moment the rationalization appears is the
        # moment the "elsewhere is not evidence" sentence must appear.
        text = " ".join(AgentLoop._STALE_CLAIM_NUDGE.split())
        assert "fixed elsewhere is not evidence" in text
        assert "git stash -u" in text

    def test_verify_nudge_reports_unresolved_failures_without_demanding_rework(self):
        # G3 is a past-tense reporting duty. Phrasing it as "verify harder"
        # is the filter-js relapse button (an already-green solution pushed
        # into a rewrite). The 4ad7bee sentences must survive verbatim.
        text = " ".join(AgentLoop._VERIFY_COMPLETE_NUDGE.split())
        assert "names it explicitly" in text
        assert "only your own script saw it" in text
        assert "do not redo work" in text
        assert text.index("names it explicitly") < text.index("clean state")
