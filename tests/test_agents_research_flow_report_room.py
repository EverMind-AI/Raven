"""Room for the report: a revision is sent back only into room, and a cut reply is asked for again.

One research run filled a 262k window before it wrote: the write step had 11.9k tokens
of output, each revision 4k, every one was cut at the limit, and the report shipped
stopping in its third section with no sources. Pinned here: the reviewer ships a
rejected draft rather than bounce it into a context with no room for the revision, a
full context offers no evidence round, and a reply cut at the output limit is set
aside and asked for again, shorter and without tools, a set number of times.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain  # noqa: E402
from research_flow.gates.cut_report import CutReportGate  # noqa: E402
from research_flow.gates.verify import _REVISION_PROMPT, DraftReviewerGate  # noqa: E402
from research_flow.gates.write_phase import WritePhaseGate  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402
from research_flow.support import ledger as ledger_mod  # noqa: E402
from research_flow.support.evidence_round import EvidenceRound  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402

DRAFT = "> Revenue grew 40%.\n\n## 1. Growth\n\nRevenue grew 40% in 2025.\n"


@pytest.fixture(autouse=True)
def _ledger_reset():
    yield
    ledger_mod.close_product_ledger()
    ledger_mod.set_ledger_dir(None)


class _Rejecter:
    async def chat_with_retry(self, **kwargs):
        return SimpleNamespace(content='{"pass": false, "unsupported_claims": ["Revenue grew 40%"], "issues": []}')


def _review(gate: DraftReviewerGate, prompt_tokens: int, metadata: dict | None = None):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 30
    ctx.response = SimpleNamespace(
        has_tool_calls=False, content=DRAFT, usage={"prompt_tokens": prompt_tokens, "completion_tokens": 2_000}
    )
    ctx.messages = [{"role": "user", "content": "task"}, {"role": "tool", "name": "web_fetch", "content": "body"}]
    ctx.metadata.update(metadata or {})
    return asyncio.run(gate.after_iteration(ctx)), ctx.metadata["verify_gate"]


def _reviewer(**kwargs) -> DraftReviewerGate:
    return DraftReviewerGate(_Rejecter(), max_revisions=3, context_window_tokens=100_000, **kwargs)


def test_a_rejected_draft_is_sent_back_only_into_room_for_the_revision():
    decision, state = _review(_reviewer(min_revision_room_tokens=20_000), 60_000)
    assert decision.rollback and state["revisions"] == 1
    decision, state = _review(_reviewer(min_revision_room_tokens=20_000), 85_000)
    assert not decision.rollback and decision.short_circuit_result is None
    assert state["revisions"] == 0 and state["rejects"] == 0 and state["rejected_no_room"] == 1
    # Off, it is sent back whatever is left.
    decision, _ = _review(_reviewer(), 97_000)
    assert decision.rollback


def test_a_full_context_gets_the_plain_revision_not_an_evidence_round():
    rounds = EvidenceRound(searches=2)
    decision, _ = _review(_reviewer(evidence_round=rounds), 60_000, {"budget_note": {"full": True}})
    assert decision.rollback and not rounds.active
    assert decision.rollback_inject[1]["content"].startswith(_REVISION_PROMPT.split("{")[0])
    decision, _ = _review(_reviewer(evidence_round=rounds), 60_000)
    assert rounds.active


def _reply(gate: CutReportGate, metadata: dict, *, cut: bool, content: str = DRAFT, tools: bool = False):
    ctx = AgentHookContext(session_key="t", iteration=40, messages=[], metadata=metadata)
    ctx.response = SimpleNamespace(
        has_tool_calls=tools, content=content, finish_reason="length" if cut else "stop", reasoning_content=None
    )
    return asyncio.run(gate.after_iteration(ctx))


def _offered(gate: CutReportGate, metadata: dict):
    ctx = AgentHookContext(session_key="t", iteration=41, messages=[], tools=[{"function": {"name": "web_fetch"}}])
    ctx.metadata.update(metadata)
    return asyncio.run(gate.before_iteration(ctx)).modified_tools


def test_a_cut_reply_is_asked_for_again_shorter_without_tools_then_ships():
    gate, metadata = CutReportGate(max_asks=2), {}
    assert not _reply(gate, metadata, cut=False).rollback
    assert not _reply(gate, metadata, cut=True, tools=True).rollback, "a cut tool call is the loop's to handle"
    decision = _reply(gate, metadata, cut=True, content="x" * 1_000)
    assert decision.rollback and decision.rollback_inject[0]["_recovery_synthetic"] is True
    assert "at most about 750 characters" in decision.rollback_inject[0]["content"]
    assert _offered(gate, metadata) == []
    assert _reply(gate, metadata, cut=True).rollback
    decision = _reply(gate, metadata, cut=True)
    assert not decision.rollback and metadata["cut_report"]["shipped_cut"] is True
    assert _offered(gate, metadata) is None


def test_the_cut_gate_is_off_by_default_and_sits_between_the_write_step_and_the_reviewer(tmp_path):
    def kinds(cfg):
        observers = build_chain(
            cfg,
            _Rejecter(),
            max_iterations=40,
            context_window_tokens=65536,
            tools=ToolHandles(),
            store=SessionStore(tmp_path),
        )
        out = []
        for hook in observers:
            while hasattr(hook, "inner"):
                hook = hook.inner
            out.append(type(hook))
        return out

    assert CutReportGate not in kinds(FlowConfig(enabled=True))
    cfg = FlowConfig(enabled=True)
    cfg.final_shape.write_phase = True
    cfg.final_shape.cut_rewrites = 2
    order = kinds(cfg)
    assert order.index(WritePhaseGate) < order.index(CutReportGate) < order.index(DraftReviewerGate)
