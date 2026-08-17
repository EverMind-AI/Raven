"""LoopscanObserver: density-fingerprint spin detection + intervention.

Detection cases are ported from the offline calibration goldens: the
density criterion must catch real degenerate spins (zh / en, content or
reasoning side) while passing the known false-positive families — quoted
constraint re-reads in multi-hop reasoning, parallel tool-call XML
scaffolding, markdown table rules, plain long prose.

Intervention: escalating-temperature rollbacks bounded per turn, then
pass-through with an ``exhausted`` tag (never fabricate a reply).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.hook import AgentHookContext
from raven.agent.hook.observers import LoopscanObserver, terminal_state
from raven.agent.hook.observers.loopscan import is_spin, spin_stats
from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse

ZH_SPIN = ("这道题需要重新从头分析一遍:条件一是该人物出生在十九世纪末的欧洲,条件二是曾经获得过国际奖项,") * 12
EN_SPIN = "I need to re-examine all the riddle constraints once again. " * 14

QUOTE = "该获奖者曾与鲁克米尼一起拯救了濒临失传的婆罗多舞并创办了音乐学院,"
REQUOTE = "".join(
    f'现在核对约束{i}:题面说"{QUOTE}"——检索到证据 EV-{i},与假设'
    f"H{i} 交叉验证:{'成立,记录来源页面并继续下一条线索' if i % 2 else '不成立,换一个候选人再查其生平与创办机构的年份'}。"
    for i in range(6)
)
TABLE = "| 指标 | 数值 | 备注 |\n|-------|-------|-------|\n" + "".join(
    f"| item_{i} | {i * 37} | 各行内容互异说明{i} |\n" for i in range(30)
)
NORMAL = "".join(f"第{i}步:检索到新证据 evidence-{i},与前一步交叉验证通过。" for i in range(80))
PARALLEL_CALLS = "先并行搜四路。\n" + "".join(
    f"<tool_call>\n<function=web_search>\n<parameter=query>\n角度{i}的检索词组合"
    f"\n</parameter>\n<parameter=count>\n8\n</parameter>\n</function>\n</tool_call>\n"
    for i in range(5)
)


@pytest.mark.parametrize(
    "text,expected",
    [
        (ZH_SPIN, True),
        (EN_SPIN, True),
        (REQUOTE, False),
        (TABLE, False),
        (NORMAL, False),
        ("short text", False),
    ],
)
def test_density_criterion(text, expected):
    assert is_spin(spin_stats(text)) is expected


def _ctx(content=None, reasoning=None, has_tool_calls=False, iteration=1):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = iteration
    ctx.response = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        has_tool_calls=has_tool_calls,
    )
    return ctx


@pytest.mark.asyncio
async def test_parallel_toolcall_scaffolding_not_flagged():
    observer = LoopscanObserver()
    decision = await observer.before_execute_tools(_ctx(content=PARALLEL_CALLS, has_tool_calls=True))
    assert decision.rollback is False


@pytest.mark.asyncio
async def test_spin_in_reasoning_is_caught():
    observer = LoopscanObserver()
    decision = await observer.after_iteration(_ctx(reasoning=EN_SPIN))
    assert decision.rollback is True


@pytest.mark.asyncio
async def test_temperature_ladder_then_exhaustion():
    observer = LoopscanObserver(max_rollbacks=2, retry_temperatures=(1.0, 1.2))
    ctx = _ctx(content=ZH_SPIN)

    d1 = await observer.after_iteration(ctx)
    assert d1.rollback is True
    assert d1.rollback_overrides == {"temperature": 1.0}

    d2 = await observer.after_iteration(ctx)
    assert d2.rollback is True
    assert d2.rollback_overrides == {"temperature": 1.2}

    d3 = await observer.after_iteration(ctx)
    assert d3.rollback is False
    assert ctx.metadata["loopscan"]["exhausted"] is True
    assert len(ctx.metadata["loopscan"]["hits"]) == 3


@pytest.mark.asyncio
async def test_after_iteration_skips_tool_call_responses():
    observer = LoopscanObserver()
    decision = await observer.after_iteration(_ctx(content=ZH_SPIN, has_tool_calls=True))
    assert decision.rollback is False
    assert "loopscan" not in _ctx().metadata


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _SpinOnceProvider(LLMProvider):
    def __init__(self):
        super().__init__(api_key="test")
        self.temps: list = []

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
        self.temps.append(temperature)
        if len(self.temps) == 1:
            return LLMResponse(content=EN_SPIN, finish_reason="stop")
        return LLMResponse(content="clean final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_default_wiring_rerolls_spin_final(workspace):
    provider = _SpinOnceProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=1,
        restrict_to_workspace=True,
    )

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == "clean final answer"
    assert provider.temps == [provider.temps[0], 1.0]
    assert not any("re-examine" in str(m.get("content")) for m in messages)


def test_terminal_state_summarizes_fired_namespaces():
    metadata = {
        "loopscan": {
            "rollbacks": 2,
            "exhausted": True,
            "hits": [
                {"iteration": 3, "share": 0.4, "windows": 12, "frag": "x"},
                {"iteration": 3, "share": 0.95, "windows": 40, "frag": "y"},
            ],
        },
        "dup_query": {"seen": {("web_search", '{"q": "a"}')}, "rollbacks": 1},
        "empty_search": {"streak": 3, "watermark": 9, "tagged": True},
        "refusal": {"rollbacks": 1},
        "verify_gate": {
            "revisions": 1,
            "reviews": 3,
            "fail_open": 1,
            "passes": 1,
            "rejects": 1,
            "verdict": {"pass": False, "issues": ["missing source"]},
            "accepted_after_revision": True,
        },
        "budget": {"max_context_used": 51200, "iterations_warned_at": 32},
    }

    state = terminal_state(metadata)

    assert state["loopscan"] == {
        "hits": 2,
        "rollbacks": 2,
        "exhausted": True,
        "last_share": 0.95,
        "last_windows": 40,
        "hit_log": metadata["loopscan"]["hits"],
    }
    assert state["dup_query"] == {"rollbacks": 1}
    assert state["empty_search"] == {"streak": 3, "tagged": True}
    assert state["refusal"] == {"rollbacks": 1}
    assert state["verify_gate"] == {
        "reviews": 3,
        "fail_open": 1,
        "passes": 1,
        "rejects": 1,
        "revisions": 1,
        "accepted_after_revision": True,
    }
    assert state["budget"] == {"max_context_used": 51200, "iterations_warned_at": 32}
    json.dumps(state)


def test_terminal_state_quiet_turn_is_empty():
    assert terminal_state({}) == {}
    assert (
        terminal_state(
            {
                "loopscan": {"rollbacks": 0, "hits": []},
                "dup_query": {"seen": set(), "rollbacks": 0},
                "empty_search": {"streak": 1, "watermark": 4},
            }
        )
        == {}
    )


@pytest.mark.asyncio
async def test_reroll_stamps_terminal_state_and_journals_notes(workspace):
    from raven.agent.loop.journal import TurnJournal

    provider = _SpinOnceProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=1,
        restrict_to_workspace=True,
    )
    partial = workspace / "t.partial.jsonl"
    journal = TurnJournal(partial, base=0)

    final, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}], journal=journal)

    assert final == "clean final answer"
    stamp = messages[-1].get("observers")
    assert stamp is not None
    assert stamp["loopscan"]["hits"] == 1
    assert stamp["loopscan"]["rollbacks"] == 1
    assert stamp["loopscan"]["exhausted"] is False
    assert len(stamp["loopscan"]["hit_log"]) == 1
    assert "observers" not in messages[0]
    json.dumps(stamp)

    lines = [json.loads(line) for line in partial.read_text(encoding="utf-8").splitlines() if line.strip()]
    rollback = next(r for r in lines if r.get("event") == "rollback")
    assert rollback["notes"] and rollback["notes"][0].startswith("loopscan_spin")


@pytest.mark.asyncio
async def test_loopscan_can_be_disabled(workspace):
    provider = _SpinOnceProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        loop_observers=[],
    )

    final, _, _, _ = await agent._run_agent_loop([{"role": "user", "content": "go"}])

    assert final == EN_SPIN.strip()
    assert len(provider.temps) == 1


class _Call:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


def _tool_ctx(calls, iteration=1):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = iteration
    ctx.response = SimpleNamespace(tool_calls=calls, has_tool_calls=bool(calls))
    return ctx


@pytest.mark.asyncio
async def test_budget_iteration_watermark_warns_once():
    from raven.agent.hook.observers import BudgetObserver

    observer = BudgetObserver(max_iterations=10, context_window_tokens=0, warn_ratio=0.8)
    ctx = AgentHookContext(session_key="cli:test")

    ctx.iteration = 7
    assert (await observer.before_iteration(ctx)).notes == []
    ctx.iteration = 8
    assert (await observer.before_iteration(ctx)).notes == ["budget_iterations 8/10"]
    ctx.iteration = 9
    assert (await observer.before_iteration(ctx)).notes == []
    assert ctx.metadata["budget"]["iterations_warned_at"] == 8


@pytest.mark.asyncio
async def test_budget_context_watermark_and_high_water_mark():
    from raven.agent.hook.observers import BudgetObserver

    observer = BudgetObserver(max_iterations=0, context_window_tokens=1000, warn_ratio=0.8)
    ctx = AgentHookContext(session_key="cli:test")

    ctx.response = SimpleNamespace(usage={"prompt_tokens": 500, "completion_tokens": 100})
    assert (await observer.after_iteration(ctx)).notes == []
    ctx.response = SimpleNamespace(usage={"prompt_tokens": 700, "completion_tokens": 200})
    assert (await observer.after_iteration(ctx)).notes == ["budget_context 900/1000"]
    ctx.response = SimpleNamespace(usage={"prompt_tokens": 800, "completion_tokens": 150})
    assert (await observer.after_iteration(ctx)).notes == []
    assert ctx.metadata["budget"]["max_context_used"] == 950
    assert ctx.metadata["budget"]["context_warned_at"] == 900


@pytest.mark.asyncio
async def test_duplicate_query_rollback_and_cap():
    from raven.agent.hook.observers import DuplicateQueryObserver

    observer = DuplicateQueryObserver(max_rollbacks=2)
    ctx = AgentHookContext(session_key="cli:test")
    search = [_Call("web_search", {"query": "who won 1998"})]

    first = await observer.before_execute_tools(_swap(ctx, search))
    assert first.rollback is False

    second = await observer.before_execute_tools(_swap(ctx, search))
    assert second.rollback is True
    assert second.rollback_overrides == {"temperature": 1.0}

    third = await observer.before_execute_tools(_swap(ctx, search))
    assert third.rollback is True

    fourth = await observer.before_execute_tools(_swap(ctx, search))
    assert fourth.rollback is False


@pytest.mark.asyncio
async def test_duplicate_query_mixed_calls_pass():
    from raven.agent.hook.observers import DuplicateQueryObserver

    observer = DuplicateQueryObserver()
    ctx = AgentHookContext(session_key="cli:test")

    await observer.before_execute_tools(_swap(ctx, [_Call("web_search", {"query": "a"})]))
    mixed = [_Call("web_search", {"query": "a"}), _Call("web_search", {"query": "b"})]
    decision = await observer.before_execute_tools(_swap(ctx, mixed))
    assert decision.rollback is False

    unwatched = [_Call("web_search", {"query": "a"}), _Call("exec", {"cmd": "ls"})]
    decision = await observer.before_execute_tools(_swap(ctx, unwatched))
    assert decision.rollback is False


def _swap(ctx, calls):
    ctx.response = SimpleNamespace(tool_calls=calls, has_tool_calls=True)
    return ctx


@pytest.mark.asyncio
async def test_empty_search_streak_tags_at_threshold():
    from raven.agent.hook.observers import EmptySearchObserver

    observer = EmptySearchObserver(streak_threshold=3)
    ctx = AgentHookContext(session_key="cli:test")
    ctx.messages = []

    def iteration(content):
        ctx.messages.append({"role": "assistant", "content": ""})
        ctx.messages.append({"role": "tool", "name": "web_search", "content": content})
        return observer.after_iteration(ctx)

    assert (await iteration("No results for: q1")).notes == []
    assert (await iteration("No results for: q2")).notes == []
    decision = await iteration("No results for: q3")
    assert decision.notes == ["empty_search_streak 3"]
    assert ctx.metadata["empty_search"]["tagged"] is True

    assert (await iteration("1. Real result — example.com")).notes == []
    assert ctx.metadata["empty_search"]["streak"] == 0


@pytest.mark.asyncio
async def test_refusal_rerolls_once_then_stands():
    from raven.agent.hook.observers import RefusalObserver

    observer = RefusalObserver(max_rollbacks=1)
    ctx = AgentHookContext(session_key="cli:test")
    ctx.response = SimpleNamespace(
        content="<think>hmm</think>I cannot help with that request.",
        has_tool_calls=False,
    )

    first = await observer.after_iteration(ctx)
    assert first.rollback is True
    assert first.rollback_overrides == {"temperature": 1.0}

    second = await observer.after_iteration(ctx)
    assert second.rollback is False
    assert second.notes == ["refusal_stands"]


@pytest.mark.asyncio
async def test_refusal_ignores_long_answers_and_tool_calls():
    from raven.agent.hook.observers import RefusalObserver

    observer = RefusalObserver()
    ctx = AgentHookContext(session_key="cli:test")

    long_answer = "I cannot confirm the 1998 claim, but the evidence shows... " * 20
    ctx.response = SimpleNamespace(content=long_answer, has_tool_calls=False)
    assert (await observer.after_iteration(ctx)).rollback is False

    ctx.response = SimpleNamespace(content="我无法直接回答,先搜索。", has_tool_calls=True)
    assert (await observer.after_iteration(ctx)).rollback is False


def test_terminal_state_reports_a_terminal_only_salvage():
    """A salvage from the turn-end seam records ``terminal_hits`` and no
    ``empty_hits`` — the namespace must still be emitted, because the train-side
    render exemption keys off ``force_finalize.synthesized``."""
    state = terminal_state(
        {"force_finalize": {"terminal_hits": 1, "nudges": 0, "synth_failed": 0, "synthesized": True}}
    )
    assert state["force_finalize"] == {
        "terminal_hits": 1,
        "nudges": 0,
        "synth_failed": 0,
        "synthesized": True,
    }


def test_terminal_state_exports_the_salvage_commit_counters():
    """The scorer's closing-tag exemption keys off ``salvage_committed``.

    A committed salvage carries no closing think tag, so it is indistinguishable
    on the trajectory from a turn that never reached its answer; this counter is
    the only thing that separates them. It was dropped once by a key-name
    whitelist here, which made an entire arm measure the previous version's
    scoring behaviour under the new version's label.
    """
    state = terminal_state(
        {
            "force_finalize": {
                "empty_hits": 0,
                "nudges": 0,
                "synth_failed": 0,
                "terminal_hits": 12,
                "synthesized": True,
                "salvage_committed": 12,
                "salvage_seam": "terminal",
                "salvage_answer_chars": 771,
            }
        }
    )

    assert state["force_finalize"]["salvage_committed"] == 12
    assert state["force_finalize"]["salvage_seam"] == "terminal"
    assert state["force_finalize"]["salvage_answer_chars"] == 771
    json.dumps(state)


@pytest.mark.parametrize(
    "namespace,gate_key",
    [("force_finalize", "terminal_hits"), ("verify_gate", "reviews"), ("fetch_floor", "notes")],
)
def test_terminal_state_exports_a_counter_nobody_enumerated(namespace, gate_key):
    """Export is by value type, so a counter added later still reaches the
    trajectory. Asserting the mechanism rather than one key is the point: the
    original defect was invisible from the writing side and only showed up as a
    downstream consumer that never fired."""
    state = terminal_state({namespace: {gate_key: 1, "a_counter_added_next_quarter": 7}})

    assert state[namespace]["a_counter_added_next_quarter"] == 7


def test_terminal_state_still_drops_unbounded_values():
    state = terminal_state(
        {
            "force_finalize": {
                "terminal_hits": 1,
                "attempt_log": [{"i": n} for n in range(1000)],
                "seen": {"a", "b"},
                "long_reason": "x" * 5000,
            }
        }
    )

    assert "attempt_log" not in state["force_finalize"]
    assert "seen" not in state["force_finalize"]
    assert len(state["force_finalize"]["long_reason"]) == 200
    json.dumps(state)


def test_terminal_state_stamps_how_the_turn_ended():
    state = terminal_state(
        {
            "turn_end": {
                "status": "error",
                "answerless": True,
                "overflows": 1,
                "clamps": 1,
                "elisions": 0,
                "final_completion_cap": 13894,
                "unserializable": {"nested": 1},
            }
        }
    )
    assert state["turn_end"]["status"] == "error"
    assert state["turn_end"]["answerless"] is True
    assert state["turn_end"]["overflows"] == 1
    assert "unserializable" not in state["turn_end"]


def test_terminal_state_exports_the_salvage_exempt_counter():
    """dr@2.2's corrected counter has to survive the persistence layer.

    This is the shape that already burned one release: three new keys were written
    into metadata but dropped by a per-key whitelist in terminal_state(), so a
    version shipped, was measured, and reported while behaving exactly like its
    predecessor. The export is by value type now, and this pins that - a bool key
    added upstream must come out the other side.

    It does not replace counting the key on traj_raw.jsonl at batch acceptance;
    that check lives in pipeline/check_dr21_fingerprints.py, because a hook-level
    assertion cannot prove what actually landed on disk.
    """
    state = terminal_state(
        {
            "turn_end": {
                "status": "ok",
                "answerless": False,
                "answerless_shape": True,  # raw: no closing tag
                "answerless_shape_exempt": False,  # corrected: salvage committed
                "salvage_committed_at_terminal": True,
            }
        }
    )
    assert state["turn_end"]["answerless_shape"] is True
    assert state["turn_end"]["answerless_shape_exempt"] is False
    assert state["turn_end"]["salvage_committed_at_terminal"] is True
