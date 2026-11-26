"""The write step: a research turn's first draft is set aside and the report written again, tools withdrawn.

Pinned here: which drafts it asks again for (researched ones, once a turn, on an
iteration that offered the web tools), that the re-sample carries no tools and ends on
the brief, that the brief is the report rule the system prompt carries, numbered as
there, and where the gate sits in the chain. One test drives the real loop, since the
rollback, the withheld tools and the dropped brief are the loop's to honour.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow import prompts  # noqa: E402
from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain  # noqa: E402
from research_flow.gates import write_phase  # noqa: E402
from research_flow.gates.budget_note import BudgetNoteObserver  # noqa: E402
from research_flow.gates.evidence_floor import EvidenceFloorGate  # noqa: E402
from research_flow.gates.verify import DraftReviewerGate  # noqa: E402
from research_flow.gates.write_phase import WritePhaseGate  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402

from raven.agent.hook import AgentHook  # noqa: E402
from raven.agent.hook.composite import CompositeHook  # noqa: E402
from raven.agent.loop import AgentLoop  # noqa: E402
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy  # noqa: E402
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.spine import ChatType, Origin, Source, TurnRequest  # noqa: E402

BRIEF = "[brief] write the report now"
DRAFT = "> The answer.\n\n## What was found\nIt holds.\n\n## Limitations\nNone."
WEB_TOOLS = [{"type": "function", "function": {"name": n}} for n in ("web_search", "web_fetch")]
SEARCHED = [{"role": "user", "content": "q"}, {"role": "tool", "name": "web_search", "content": "results"}]


class _Response:
    def __init__(self, content: str, has_tool_calls: bool = False) -> None:
        self.content = content
        self.has_tool_calls = has_tool_calls
        self.reasoning_content = "thought it through"


def _ctx(meta: dict, *, messages=None, response=None, tools=None) -> AgentHookContext:
    return AgentHookContext(
        session_key="t",
        iteration=3,
        messages=list(SEARCHED if messages is None else messages),
        response=response,
        tools=tools,
        metadata=meta,
    )


def _step(gate: WritePhaseGate, meta: dict, response: _Response, *, messages=None, tools=WEB_TOOLS):
    before = asyncio.run(gate.before_iteration(_ctx(meta, messages=messages, tools=list(tools))))
    after = asyncio.run(gate.after_iteration(_ctx(meta, messages=messages, response=response)))
    return before, after


def test_a_researched_draft_is_set_aside_and_the_report_asked_for_without_tools():
    gate, meta = WritePhaseGate(BRIEF), {}
    _, decision = _step(gate, meta, _Response(DRAFT))
    assert decision.rollback
    assert decision.rollback_inject == [{"role": "user", "content": BRIEF, "_recovery_synthetic": True}]

    before, after = _step(gate, meta, _Response(DRAFT + " Rewritten."))
    assert before.modified_tools == []
    assert not after.rollback
    assert meta["write_phase"]["written_chars"] == len(DRAFT + " Rewritten.")


def test_once_a_turn_a_later_draft_ships_as_written():
    gate, meta = WritePhaseGate(BRIEF), {}
    _step(gate, meta, _Response(DRAFT))
    _step(gate, meta, _Response(DRAFT))
    # A revision after a rejection, the tools back on offer.
    before, after = _step(gate, meta, _Response(DRAFT))
    assert before.modified_tools is None and not after.rollback


@pytest.mark.parametrize(
    "case",
    ["no_research", "tool_calls", "no_web_tools", "empty"],
)
def test_what_is_passed_through(case):
    gate, meta = WritePhaseGate(BRIEF), {}
    response = _Response("" if case == "empty" else DRAFT, has_tool_calls=case == "tool_calls")
    messages = [{"role": "user", "content": "hi"}] if case == "no_research" else None
    tools = [] if case == "no_web_tools" else WEB_TOOLS
    before, after = _step(gate, meta, response, messages=messages, tools=tools)
    assert before.modified_tools is None and not after.rollback
    assert not meta["write_phase"].get("asked")


def test_research_from_an_earlier_turn_does_not_count():
    gate, meta = WritePhaseGate(BRIEF), {}
    ctx = AgentHookContext(
        session_key="t",
        messages=[*SEARCHED, {"role": "user", "content": "thanks"}],
        response=_Response(DRAFT),
        metadata=meta,
        turn_base=len(SEARCHED),
    )
    assert not asyncio.run(gate.after_iteration(ctx)).rollback


_LINE = "Averaging the weights of several fine-tuned models lifts accuracy at no inference cost"


def _report(*paragraphs: str) -> str:
    body = "\n\n".join(paragraphs)
    return f"# Merging\n\n> **Model Soups** wins.\n\n## 1. Methods\n\n{body}\n\n## Sources\n- **A page**: https://a.example/x\n"


def test_a_round_the_budget_closed_is_still_the_research_end():
    """The budget note runs first and strips the web tools; the write step still asks for the report."""
    chain = CompositeHook([BudgetNoteObserver(max_iterations=60, close_after=6), WritePhaseGate(BRIEF)])
    meta = {"budget_note": {"warned": True, "closed": True}}
    before = asyncio.run(chain.before_iteration(_ctx(meta, tools=list(WEB_TOOLS))))
    assert before.modified_tools == []
    after = asyncio.run(chain.after_iteration(_ctx(meta, response=_Response(DRAFT))))
    assert after.rollback and meta["write_phase"]["asked"]


def test_the_skim_count_reads_names_and_figures_not_sentences_or_sources():
    named = f"{_LINE}, as **Model Soups** shows with **81.03%** top-1 ([paper](https://arxiv.org/abs/2203.05482))."
    sentence = f"**{_LINE}.** It needs no extra pass."
    assert write_phase.skim_count(_report(named, sentence, _LINE, "Too short to count.")) == (1, 3)
    listed = f"- {_LINE}, as **DARE** does.\n- {_LINE}, plainly.\n\n| a | b |\n|--|--|\n| **x** | y |"
    assert write_phase.skim_count(_report(listed)) == (1, 2)
    # Only the closing section is the list of sources: one on data sources mid-body is body.
    data = _report(_LINE + ".").replace("## 1. Methods", "## 1. Data sources")
    assert write_phase.skim_count(data) == (0, 1)


def test_a_paragraph_bolded_past_the_cap_is_counted_as_dense():
    crowded = _LINE + ", " + ", ".join(f"**{n}%**" for n in range(7)) + "."
    assert write_phase.skim_dense(_report(crowded, f"{_LINE}, as **DARE** shows.")) == 1
    assert "none carries more\nthan four or five" in prompts.skim_brief(1, 2)


def test_a_report_short_of_bold_is_handed_back_once_for_its_bold_alone():
    gate, meta = WritePhaseGate(BRIEF, skim_floor=0.4), {}
    _step(gate, meta, _Response(DRAFT))
    plain = _report(*[_LINE + "."] * 4)
    _, after = _step(gate, meta, _Response(plain))
    assert after.rollback
    report, ask = after.rollback_inject
    assert report == {"role": "assistant", "content": plain.strip(), "_recovery_synthetic": True}
    assert ask == {"role": "user", "content": prompts.skim_brief(4, 4), "_recovery_synthetic": True}
    assert "4 of the 4 paragraphs" in ask["content"] and "do not mention it" in ask["content"]
    assert meta["write_phase"]["skim_held"] == 0 and meta["write_phase"]["skim_total"] == 4

    # The revision is written with the tools still withdrawn, and ships whatever its bold.
    revised = _report(*[f"{_LINE}, as **DARE** shows."] * 4)
    before, after = _step(gate, meta, _Response(revised))
    assert before.modified_tools == [] and not after.rollback
    record = meta["write_phase"]
    assert record["skim_chars"] == len(revised) and record["skim_held_after"] == 4 and record["skim_dense_after"] == 0
    before, _ = _step(gate, meta, _Response(DRAFT))
    assert before.modified_tools is None


@pytest.mark.parametrize("floor, bolded", [(0.4, 2), (0.0, 0)])
def test_a_bolded_report_or_an_unset_floor_passes(floor, bolded):
    gate, meta = WritePhaseGate(BRIEF, skim_floor=floor), {}
    _step(gate, meta, _Response(DRAFT))
    paragraphs = [f"{_LINE}, as **DARE** shows."] * bolded + [_LINE + "."] * (4 - bolded)
    _, after = _step(gate, meta, _Response(_report(*paragraphs)))
    assert not after.rollback and not meta["write_phase"].get("skim_asked")


def test_the_brief_is_the_report_rule_of_the_system_prompt_numbered_as_there():
    cfg = FlowConfig(enabled=True)
    cfg.final_shape.report_depth = True
    cfg.final_shape.report_layout = "reader"
    cfg.final_shape.report_style = "outline"
    cfg.report_page.enabled = True
    brief = prompts.write_brief(cfg)
    _, contract = prompts.render_identity_and_contract(cfg)
    head, rule = brief.split("\n\n", 1)
    assert "tools are withdrawn" in head and "do not mention it" in head
    number = rule.split(".", 1)[0]
    assert f"Rule {number} of" in head
    assert rule in contract
    clause = prompts._DR_REPORT_STRUCTURE_CLAUSE_READER_PAGE_OUTLINE.replace("{n}", number)
    assert rule.startswith(clause.split("{format_override}")[0].strip("\n"))


def _chain(tmp_path, cfg):
    class _Stub:
        async def chat_with_retry(self, **kwargs):
            raise AssertionError("assembly calls no model")

    observers = build_chain(
        cfg, _Stub(), max_iterations=40, context_window_tokens=65536, tools=ToolHandles(), store=SessionStore(tmp_path)
    )

    def inner(hook):
        while hasattr(hook, "inner"):
            hook = hook.inner
        return hook

    return [type(inner(o)) for o in observers], [inner(o) for o in observers]


def test_off_by_default_and_on_after_the_floor_before_the_reviewer(tmp_path):
    assert WritePhaseGate not in _chain(tmp_path, FlowConfig(enabled=True))[0]
    cfg = FlowConfig(enabled=True)
    cfg.final_shape.write_phase = True
    cfg.evidence_floor.enabled = True
    kinds, gates = _chain(tmp_path, cfg)
    assert kinds.index(EvidenceFloorGate) < kinds.index(WritePhaseGate) < kinds.index(DraftReviewerGate)
    assert gates[kinds.index(WritePhaseGate)]._brief == prompts.write_brief(cfg)
    assert gates[kinds.index(WritePhaseGate)]._skim_floor == 0.0
    cfg.final_shape.skim_floor = 0.4
    kinds, gates = _chain(tmp_path, cfg)
    assert gates[kinds.index(WritePhaseGate)]._skim_floor == 0.4


class _Provider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls.append(
            {
                **kwargs,
                "messages": [dict(m) for m in kwargs.get("messages") or []],
                "tools": list(kwargs.get("tools") or []),
            }
        )
        return self._responses[min(len(self.calls), len(self._responses)) - 1]

    def get_default_model(self) -> str:
        return "fake/model"


class _Seat(AgentHook):
    """The gate on the loop's own seams, as the research participant puts it there."""

    def __init__(self, gate: WritePhaseGate) -> None:
        self.gate = gate

    async def before_iteration(self, ctx):
        return await self.gate.before_iteration(ctx)

    async def after_iteration(self, ctx):
        return await self.gate.after_iteration(ctx)


@pytest.mark.asyncio
async def test_the_loop_writes_the_report_with_no_tools_and_keeps_it_not_the_brief(tmp_path, monkeypatch):
    # list_dir stands in for the web tools: the loop under test has no web tools registered.
    monkeypatch.setattr(write_phase, "_WEB_TOOLS", frozenset({"list_dir"}))
    provider = _Provider(
        [
            LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})]),
            LLMResponse(content="first draft", finish_reason="stop"),
            LLMResponse(content="the report", finish_reason="stop"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=3),
        host=HostWiring(hooks=[_Seat(WritePhaseGate(BRIEF))]),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    request = TurnRequest(
        origin=Origin.USER, source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM), text="q"
    )

    out = await loop._process_message(request)

    assert "the report" in str(out) and "first draft" not in str(out)
    assert len(provider.calls) == 3
    assert provider.calls[1]["tools"], "research offered tools"
    write = provider.calls[2]
    assert write["tools"] == [] and write["messages"][-1]["content"] == BRIEF
    assert not any(m.get("content") == "first draft" for m in write["messages"]), "the draft was set aside"
    kept = loop.sessions.get_or_create("cli:c").messages
    assert not any(m.get("content") == BRIEF for m in kept)
    assert [m.get("content") for m in kept if m.get("role") == "assistant"][-1] == "the report"
