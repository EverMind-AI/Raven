"""Deep-research flow (dr@1): assembly, budget notes, verify-gate, e2e.

The DR flow must be strictly additive: disabled config -> no assembly,
chat behavior byte-identical. Enabled: budget lines land in persisted
tool results (the train-serve-safe channel), the draft reviewer bounces
a failing draft back exactly once with feedback injected into history,
and the trajectory stamp carries the flow version prefix.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.context import TOOL_OUTPUT_ELIDED
from raven.agent.flow import BudgetNoteObserver, DraftReviewerGate, build_dr_flow
from raven.agent.flow.answer_text import visible_answer
from raven.agent.flow.dr import _make_digest_fn
from raven.agent.hook import AgentHookContext
from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.config.raven import DRFlowConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _StubProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def test_build_dr_flow_disabled_returns_none():
    assert build_dr_flow(DRFlowConfig(), _StubProvider(), max_iterations=40, context_window_tokens=65536) is None


@pytest.mark.parametrize("required", [True, False])
def test_force_finalize_gate_inherits_the_closing_tag_caliber(required):
    """The salvage reply must be held to the same bar as the answer it replaces;
    a gate left on the permissive default commits a tagless chain of thought."""
    config = DRFlowConfig(
        enabled=True,
        think_closing_tag_required=required,
        force_finalize={"enabled": True},
        verify={"enabled": False},
    )
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)
    gate = next(o for o in assembly.observers if type(o).__name__ == "ForcedFinalizeGate")

    assert gate._closing_tag_required is required


def test_build_dr_flow_assembly_shape():
    config = DRFlowConfig(enabled=True, max_iterations=48)
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)

    # Read the default rather than a literal. This file is the flow/anchor contract and
    # AGENTS.md 5.3 requires it to survive every change untouched - but it hardcoded
    # drFlow.version, so each version bump violated 5.3 by construction. The assertion
    # that carries meaning is "the assembly carries the configured version", not "the
    # version is exactly X"; the exact-label rules live in test_config_raven_loader.py.
    assert assembly.version == DRFlowConfig().version
    assert assembly.max_iterations == 48
    assert [type(o).__name__ for o in assembly.observers] == ["BudgetNoteObserver", "DraftReviewerGate"]
    assert assembly.web_search_kwargs == {
        "include_answer_box": False,
        "include_knowledge_graph": False,
        "include_snippets": False,
        "snippet_dedup_by_docid": False,
        # dr@2.8, both off by default. False and None here are the anchor contract:
        # a profile that only turns the flow on gets a search tool whose request
        # depth and result selection are identical to dr@2.7's.
        "cross_query_dedup": False,
        "search_depth": 20,
        # dr@3.2. 5 is what ``WebSearchTool``'s constructor already defaulted to and
        # what every arm ever run - anchor included - actually rendered, because
        # ``WebSearchConfig.max_results`` is read by nothing. Threading it through the
        # flow makes the width settable PER ARM; the value stays 5 so a bare flow-on
        # profile is byte-identical to dr@3.1. If this ever reads anything but 5, the
        # anchor and the DR arm are no longer receiving the same search tool by
        # default - which is the dr@2.7 "no gate ⇒ the anchor changes code too" shape.
        "max_results": 5,
        "repeat_notice": True,
        "evidence_round": None,
        # dr@3.0, off by default like the two above it. Spelled out rather than
        # omitted-when-None so this assertion keeps failing if the saturation rule
        # ever defaults on: the search tool a bare flow-on profile receives must still
        # issue exactly the requests dr@2.7 issued, or every arm's request volume
        # changes without a single config saying so.
        "saturation": None,
    }
    assert "digest_fn" in assembly.web_fetch_kwargs
    assert assembly.web_fetch_kwargs["max_chars"] == 14_000
    assert assembly.segment_builder is not None
    assert assembly.segment_builder.order == 2


@pytest.mark.asyncio
async def test_dr_segment_contains_flow_contract():
    from raven.agent.flow import DRModeSegmentBuilder

    seg = await DRModeSegmentBuilder().build(None)
    assert "Deep Research Mode" in seg.text
    assert "info_to_extract" in seg.text
    assert "[budget:" in seg.text


@pytest.mark.asyncio
async def test_prompt_section_override_replaces_the_contract():
    """A profile may replace the contract; the default must stay byte-identical.

    The built-in contract ends with "answer first, then evidence", which
    contradicts a grader that parses a token at the end of the output.
    """
    from raven.agent.flow import DRModeSegmentBuilder

    default = await DRModeSegmentBuilder().build(None)
    assert (await DRModeSegmentBuilder(None).build(None)).text == default.text

    base = build_dr_flow(
        DRFlowConfig(enabled=True), _StubProvider(), max_iterations=40, context_window_tokens=65536
    )
    assert (await base.segment_builder.build(None)).text == default.text

    custom = build_dr_flow(
        DRFlowConfig(enabled=True, version=f"{DRFlowConfig().version}-profile", prompt_section_override="# Custom\n\nEnd with a box."),
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
    )
    seg = await custom.segment_builder.build(None)
    # The override replaces the CONTRACT only; the DR identity is always prepended
    # so an override cannot silently delete the tool-surface and untrusted-content
    # rules the dropped product identity used to carry.
    assert seg.text.endswith("# Custom\n\nEnd with a box.")
    assert seg.text.startswith("# Raven — Deep Research")
    assert "[BEGIN UNTRUSTED" in seg.text
    assert "Deep Research Mode" not in seg.text


def _tool_iteration_ctx(iteration, content="tool output", usage=None):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = iteration
    ctx.messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "name": "web_search", "content": content},
    ]
    ctx.response = SimpleNamespace(has_tool_calls=True, usage=usage or {})
    return ctx


@pytest.mark.asyncio
async def test_budget_note_appended_to_tool_result():
    observer = BudgetNoteObserver(max_iterations=10, context_window_tokens=1000)
    ctx = _tool_iteration_ctx(3, usage={"prompt_tokens": 400, "completion_tokens": 100})

    await observer.after_iteration(ctx)

    assert ctx.messages[-1]["content"].endswith("[budget: iteration 3/10 | context ~50%]")


@pytest.mark.asyncio
async def test_budget_note_converge_warning_fires_once():
    observer = BudgetNoteObserver(max_iterations=10, warn_ratio=0.8)
    ctx = _tool_iteration_ctx(8)

    await observer.after_iteration(ctx)
    assert "budget warning" in ctx.messages[-1]["content"]

    ctx.messages.append({"role": "tool", "name": "web_search", "content": "more"})
    ctx.iteration = 9
    await observer.after_iteration(ctx)
    assert "budget warning" not in ctx.messages[-1]["content"]


class _ReviewerProvider:
    """chat_with_retry stub scripted per review call."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0
        self.kwargs = []

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        verdict = self._verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        if isinstance(verdict, LLMResponse):
            return verdict
        return LLMResponse(content=verdict, finish_reason="stop")


def _final_ctx(draft="The answer is X because Y.", messages=None):
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 2
    ctx.messages = messages or [
        {"role": "user", "content": "who is X?"},
        {"role": "tool", "name": "web_fetch", "content": "evidence about X"},
    ]
    ctx.response = SimpleNamespace(has_tool_calls=False, content=f"<think>hm</think>{draft}")
    return ctx


@pytest.mark.asyncio
async def test_gate_passes_clean_draft():
    provider = _ReviewerProvider([json.dumps({"pass": True, "unresolved_claims": 0})])
    gate = DraftReviewerGate(provider)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is False
    assert decision.short_circuit_result is None
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_gate_bounces_failing_draft_once_with_feedback():
    provider = _ReviewerProvider(
        [json.dumps({"pass": False, "unsupported_claims": ["X founded in 1990"], "issues": ["no source for the date"]})]
    )
    gate = DraftReviewerGate(provider, max_revisions=1)
    ctx = _final_ctx()

    first = await gate.after_iteration(ctx)
    assert first.rollback is True
    assert first.rollback_inject[0]["role"] == "assistant"
    assert first.rollback_inject[0]["content"] == "The answer is X because Y."
    assert first.rollback_inject[1]["role"] == "user"
    assert "X founded in 1990" in first.rollback_inject[1]["content"]

    second = await gate.after_iteration(ctx)
    assert second.rollback is False
    assert ctx.metadata["verify_gate"]["accepted_after_revision"] is True
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [RuntimeError("reviewer down"), "not json at all {{{", json.dumps({"no_pass_key": 1})],
)
async def test_gate_fails_open(verdict):
    provider = _ReviewerProvider([verdict])
    gate = DraftReviewerGate(provider)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is False
    assert decision.short_circuit_result is None


@pytest.mark.asyncio
async def test_gate_parses_verdict_wrapped_in_think_and_fences():
    wrapped = (
        "<think>let me check the claims one by one...</think>\n"
        'Here is my verdict:\n```json\n{"pass": false, "unresolved_claims": 1, '
        '"unsupported_claims": ["Z founded in 1990"], "issues": []}\n```'
    )
    provider = _ReviewerProvider([wrapped])
    gate = DraftReviewerGate(provider)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is True
    assert "Z founded in 1990" in decision.rollback_inject[1]["content"]


@pytest.mark.asyncio
async def test_gate_coerces_string_pass_boolean():
    provider = _ReviewerProvider([json.dumps({"pass": "false", "issues": ["missing source"]})])
    gate = DraftReviewerGate(provider)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, []), ("the 1998 date is unsupported", ["the 1998 date is unsupported"]), ({"a": 1}, [])],
)
async def test_gate_normalises_non_list_claim_fields(value, expected):
    gate = DraftReviewerGate(_StubProvider())
    verdict = gate._parse_verdict(json.dumps({"pass": False, "unsupported_claims": value}))
    assert verdict["unsupported_claims"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        LLMResponse(content="<think>reasoning that got cut mid-sente", finish_reason="stop"),
        LLMResponse(content='<think>very long thinking</think>{"pass": tru', finish_reason="length"),
    ],
)
async def test_gate_fails_open_on_truncated_reviewer_output(response):
    provider = _ReviewerProvider([response])
    gate = DraftReviewerGate(provider)
    ctx = _final_ctx()
    decision = await gate.after_iteration(ctx)
    assert decision.rollback is False
    assert ctx.metadata["verify_gate"]["fail_open"] == 1


@pytest.mark.asyncio
async def test_gate_passes_max_tokens_to_reviewer_call():
    provider = _ReviewerProvider([json.dumps({"pass": True})])
    gate = DraftReviewerGate(provider, max_tokens=2048)
    await gate.after_iteration(_final_ctx())
    assert provider.kwargs[0]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_gate_counters_distinguish_failopen_from_verdicts():
    provider = _ReviewerProvider(["not json at all", json.dumps({"pass": True})])
    gate = DraftReviewerGate(provider)
    ctx = _final_ctx()

    await gate.after_iteration(ctx)
    await gate.after_iteration(ctx)

    state = ctx.metadata["verify_gate"]
    assert state["reviews"] == 2
    assert state["fail_open"] == 1
    assert state["passes"] == 1
    assert state["rejects"] == 0
    assert state["revisions"] == 0


class _StallingProvider:
    """First N chat_with_retry calls stall (dead-connection shape), rest answer."""

    def __init__(self, stalls, verdict):
        self._stalls = stalls
        self._verdict = verdict
        self.calls = 0

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        if self.calls <= self._stalls:
            await asyncio.sleep(30)
        return LLMResponse(content=self._verdict, finish_reason="stop")


@pytest.mark.asyncio
async def test_gate_retries_past_stalled_attempt():
    provider = _StallingProvider(stalls=1, verdict=json.dumps({"pass": True}))
    gate = DraftReviewerGate(provider, timeout_seconds=5, attempt_timeout_seconds=0.05)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is False
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_gate_fails_open_when_budget_spent_on_stalls():
    provider = _StallingProvider(stalls=99, verdict=json.dumps({"pass": True}))
    gate = DraftReviewerGate(provider, timeout_seconds=0.2, attempt_timeout_seconds=0.05)
    decision = await gate.after_iteration(_final_ctx())
    assert decision.rollback is False
    assert decision.short_circuit_result is None
    assert provider.calls >= 2


class _DRScriptProvider(LLMProvider):
    """One tool iteration, then a draft; reviewer rejects once; revised final.

    Reviewer calls are recognized by their system prompt (fresh context).
    """

    def __init__(self):
        super().__init__(api_key="test")
        self.reviewer_calls = 0
        self.solve_calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        system = messages[0].get("content", "") if messages else ""
        if "draft reviewer" in system:
            self.reviewer_calls += 1
            return LLMResponse(
                content=json.dumps({"pass": False, "unsupported_claims": ["the 1990 date"], "issues": []}),
                finish_reason="stop",
            )
        self.solve_calls += 1
        if self.solve_calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="no_such_tool", arguments={})],
                finish_reason="tool_calls",
            )
        if self.solve_calls == 2:
            return LLMResponse(content="weighing sources</think>Draft: founded in 1990.", finish_reason="stop")
        return LLMResponse(
            content="fixing the date</think>Revised: founded in 1992, per two sources.",
            finish_reason="stop",
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_dr_flow_end_to_end_review_loop(workspace):
    provider = _DRScriptProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(enabled=True),
    )

    assert agent._flow_version.startswith(f"{DRFlowConfig().version}/raven-")

    final, _, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "when was X founded?"}])

    # The loop returns the turn verbatim; only visible_answer folds the reasoning away.
    assert final == "fixing the date</think>Revised: founded in 1992, per two sources."
    assert visible_answer(final) == "Revised: founded in 1992, per two sources."
    assert outcome.status == "completed"
    assert provider.reviewer_calls == 1

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert any("[budget: iteration 1/2]" in str(m.get("content")) for m in tool_msgs)

    drafts = [m for m in messages if m.get("role") == "assistant" and str(m.get("content", "")).startswith("Draft:")]
    assert len(drafts) == 1
    feedback = [m for m in messages if m.get("role") == "user" and "reviewer rejected" in str(m.get("content", ""))]
    assert len(feedback) == 1
    assert "the 1990 date" in feedback[0]["content"]
    assert messages.index(drafts[0]) < messages.index(feedback[0])


@pytest.mark.asyncio
async def test_dr_flow_disabled_keeps_chat_defaults(workspace):
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(),
    )
    assert agent._dr_flow is None
    assert agent._flow_version.startswith("raven-")
    assert len(list(agent.hooks)) == 3


def test_visible_answer_fourth_shape_needs_the_flag():
    prefilled_cut = "reasoning that never reached an answer"
    assert visible_answer(prefilled_cut) == prefilled_cut
    assert visible_answer(prefilled_cut, closing_tag_required=True) == ""
    complete = "reasoning</think>the answer"
    assert visible_answer(complete, closing_tag_required=True) == "the answer"
    paired = "<think>reasoning</think>the answer"
    assert visible_answer(paired, closing_tag_required=True) == "the answer"
    assert visible_answer("", closing_tag_required=True) == ""


@pytest.mark.asyncio
async def test_unclosed_think_terminal_counts_as_answerless(workspace):
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(enabled=True),
    )
    assert agent._think_closing_tag_required is True
    chat = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
    )
    assert chat._think_closing_tag_required is False


def test_assembly_carries_slimming_config():
    config = DRFlowConfig(enabled=True)
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)
    assert assembly.tools_allowlist == ("web_search", "web_fetch")
    assert assembly.drop_segments == frozenset({"identity", "bootstrap", "memory", "active_skills", "skills"})

    keep_all = DRFlowConfig(enabled=True, minimal_context=False, tools_allowlist=())
    assembly = build_dr_flow(keep_all, _StubProvider(), max_iterations=40, context_window_tokens=65536)
    assert assembly.tools_allowlist == ()
    assert assembly.drop_segments == frozenset()


@pytest.mark.asyncio
async def test_dr_mode_slims_tools_and_segments(workspace):
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(enabled=True),
        # web_search is withheld without a key; this asserts the tool surface,
        # not configuredness.
        brave_api_key="test-key",
    )
    assert sorted(agent.tools.names()) == ["web_fetch", "web_search"]
    # DR mode drops the product identity: eight of its instructions named tools no
    # DR arm has, and one line was the phrase the restart detector reads.
    # DRModeSegmentBuilder carries the replacement, so ``identity`` is absent here.
    assert [b.name for b in agent.context_engine._builders] == ["dr_mode", "curator"]


@pytest.mark.asyncio
async def test_corpus_endpoint_rejects_escape_tool(workspace):
    with pytest.raises(ValueError, match="voids the containment"):
        AgentLoop(
            provider=_DRScriptProvider(),
            workspace=workspace,
            model="stub",
            restrict_to_workspace=True,
            web_corpus_endpoint="http://127.0.0.1:8765",
            dr_flow=DRFlowConfig(enabled=True, tools_allowlist=("web_search", "web_fetch", "exec")),
        )


@pytest.mark.asyncio
async def test_corpus_endpoint_accepts_contained_surface(workspace):
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
        web_corpus_endpoint="http://127.0.0.1:8765",
        dr_flow=DRFlowConfig(enabled=True),
    )
    assert sorted(agent.tools.names()) == ["web_fetch", "web_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_enabled, expected", [(True, True), (False, False)])
async def test_repeat_notice_follows_the_flow_switch(workspace, flow_enabled, expected):
    """The shaping must reach the registered tool, not just the assembly.

    web_search_kwargs is spread into the constructor, so a config field that is
    never threaded through ``build_dr_flow`` fails silently: the assembly reads
    correct and the tool keeps its own default.
    """
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(enabled=flow_enabled),
        # web_search is withheld without a key; this asserts the tool surface,
        # not configuredness.
        brave_api_key="test-key",
    )
    assert agent.tools.get("web_search").repeat_notice is expected


class _BigResultTool(Tool):
    """Stand-in web_search returning an oversized result."""

    @property
    def name(self):
        return "web_search"

    @property
    def description(self):
        return "stub"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "x" * 40_000


class _OneSearchProvider(LLMProvider):
    """One big search, then a final answer; reviewer always passes."""

    def __init__(self):
        super().__init__(api_key="test")
        self.solve_calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        system = messages[0].get("content", "") if messages else ""
        if "draft reviewer" in system:
            return LLMResponse(content=json.dumps({"pass": True}), finish_reason="stop")
        self.solve_calls += 1
        if self.solve_calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="web_search", arguments={"query": "q"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="Answer with sources.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_dr_ingestion_cap_keeps_fence_and_budget_note(workspace):
    """Oversized tool results are capped when they enter history, so the
    persist-time cap never fires and cannot chop off the untrusted fence
    or the appended budget note (wire == persisted trajectory)."""
    agent = AgentLoop(
        provider=_OneSearchProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=4,
        restrict_to_workspace=True,
        dr_flow=DRFlowConfig(enabled=True),
    )
    agent.tools.unregister("web_search")
    agent.tools.register(_BigResultTool())

    _, _, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "q"}])

    assert outcome.status == "completed"
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    content = str(tool_msg["content"])
    assert len(content) <= AgentLoop._TOOL_RESULT_MAX_CHARS
    assert "... (truncated)" in content
    assert "[END UNTRUSTED" in content
    assert "[budget: iteration 1/4]" in content
    assert content.index("[END UNTRUSTED") < content.index("[budget:")


@pytest.mark.asyncio
async def test_tool_results_capped_at_ingestion_in_every_mode(workspace):
    """The ingestion cap is not DR-only: it was, and uncapped chat-mode runs
    killed one turn in seven on a context-window error a 50k-char fetch caused,
    while the persisted trajectory kept the 16k-char version the model never
    saw (a train-serve homomorphism break on top of the lost turn)."""
    agent = AgentLoop(
        provider=_OneSearchProvider(),
        workspace=workspace,
        model="stub",
        max_iterations=4,
        restrict_to_workspace=True,
    )
    assert agent._dr_flow is None
    agent.tools.unregister("web_search")
    agent.tools.register(_BigResultTool())

    _, _, messages, _ = await agent._run_agent_loop([{"role": "user", "content": "q"}])

    tool_msg = next(m for m in messages if m.get("role") == "tool")
    content = str(tool_msg["content"])
    # Capped at ingestion, then the untrusted fence is appended — so the result
    # lands inside the persist-time cap thanks to the headroom, and the
    # persisted text is byte-identical to what the model saw.
    assert "... (truncated)" in content
    assert len(content) <= AgentLoop._TOOL_RESULT_MAX_CHARS


@pytest.mark.asyncio
async def test_chat_mode_keeps_full_surface(workspace):
    agent = AgentLoop(
        provider=_DRScriptProvider(),
        workspace=workspace,
        model="stub",
        restrict_to_workspace=True,
        # web_search is withheld without a key; this asserts the tool surface,
        # not configuredness.
        brave_api_key="test-key",
    )
    names = set(agent.tools.names())
    assert {"message", "spawn", "read_file", "web_search"} <= names
    segment_names = [b.name for b in agent.context_engine._builders]
    assert "bootstrap" in segment_names and "skills" in segment_names


def test_dr_flow_version_label_must_match_build():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DRFlowConfig(enabled=True, version="dr@1.1")
    assert DRFlowConfig(version="dr@1.1").version == "dr@1.1"


def test_assembly_all_mechanisms_ordered():
    config = DRFlowConfig(
        enabled=True,
        force_finalize={"enabled": True},
        spin_breaker={"enabled": True},
        fetch_floor={"enabled": True},
    )
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)

    assert [type(o).__name__ for o in assembly.observers] == [
        "BudgetNoteObserver",
        "FetchFloorObserver",
        "SpinEntryBreaker",
        "ForcedFinalizeGate",
        "DraftReviewerGate",
    ]


@pytest.mark.asyncio
async def test_digest_verbatim_head_appended():
    config = DRFlowConfig(enabled=True, digest={"verbatim_head_chars": 20})
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)

    digested = await assembly.web_fetch_kwargs["digest_fn"]("PAGE TEXT " * 10, "founding year")
    assert digested.startswith("ok")
    assert "[page head, verbatim]\nPAGE TEXT PAGE TEXT " in digested


@pytest.mark.asyncio
async def test_gate_reviews_visible_answer_of_prefilled_think():
    provider = _ReviewerProvider([json.dumps({"pass": True, "unresolved_claims": 0})])
    gate = DraftReviewerGate(provider)
    ctx = _final_ctx()
    ctx.response = SimpleNamespace(
        has_tool_calls=False,
        content="I should check the sources once more.</think>The answer is X.",
    )

    await gate.after_iteration(ctx)

    reviewer_user = provider.kwargs[0]["messages"][1]["content"]
    assert "Draft answer:\nThe answer is X." in reviewer_user
    assert "check the sources once more" not in reviewer_user


@pytest.mark.asyncio
async def test_gate_skips_all_think_terminal():
    provider = _ReviewerProvider([])
    gate = DraftReviewerGate(provider)
    ctx = _final_ctx()
    ctx.response = SimpleNamespace(has_tool_calls=False, content="Let me search for more leads.</think>  ")

    decision = await gate.after_iteration(ctx)

    assert provider.calls == 0
    assert not decision.rollback and decision.short_circuit_result is None


@pytest.mark.asyncio
async def test_strict_reject_only_degrades_unnamed_reject_to_pass():
    provider = _ReviewerProvider(
        [json.dumps({"pass": False, "unresolved_claims": 1, "unsupported_claims": [], "issues": ["too thin"]})]
    )
    gate = DraftReviewerGate(provider, strict_reject_only=True)
    ctx = _final_ctx()

    decision = await gate.after_iteration(ctx)

    assert not decision.rollback
    assert ctx.metadata["verify_gate"]["strict_overrides"] == 1


@pytest.mark.asyncio
async def test_strict_reject_with_named_claim_still_bounces():
    provider = _ReviewerProvider(
        [json.dumps({"pass": False, "unresolved_claims": 1, "unsupported_claims": ["X is unsupported"], "issues": []})]
    )
    gate = DraftReviewerGate(provider, strict_reject_only=True)
    ctx = _final_ctx()

    decision = await gate.after_iteration(ctx)

    assert decision.rollback
    assert decision.rollback_inject[1]["content"].count("X is unsupported") == 1


@pytest.mark.asyncio
async def test_rubric_flags_extend_reviewer_system_prompt():
    provider = _ReviewerProvider([json.dumps({"pass": True, "unresolved_claims": 0})])
    gate = DraftReviewerGate(provider, constraint_rubric=True, strict_reject_only=True)

    await gate.after_iteration(_final_ctx())

    system = provider.kwargs[0]["messages"][0]["content"]
    assert "EACH constraint one by one" in system
    assert "Reject ONLY" in system


@pytest.mark.asyncio
async def test_evidence_pack_skips_elided_tool_bodies():
    """An emergency shrink keeps fewer tool bodies than the pack asks for.

    A blind tail would hand the reviewer the elision placeholder, and the
    tail-truncation inside the pack cannot recover a body that is gone.
    """
    provider = _ReviewerProvider([json.dumps({"pass": True, "unresolved_claims": 0})])
    gate = DraftReviewerGate(provider, evidence_items=3)
    messages = [
        {"role": "user", "content": "who is X?"},
        {"role": "tool", "name": "web_fetch", "content": "the decisive fact about X"},
        {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED},
        {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED},
        {"role": "tool", "name": "web_search", "content": "a later search result"},
    ]

    await gate.after_iteration(_final_ctx(messages=messages))

    evidence = provider.kwargs[0]["messages"][1]["content"]
    assert "the decisive fact about X" in evidence
    assert "a later search result" in evidence
    assert TOOL_OUTPUT_ELIDED not in evidence


@pytest.mark.asyncio
async def test_evidence_pack_counts_skipped_elisions():
    provider = _ReviewerProvider([json.dumps({"pass": True, "unresolved_claims": 0})])
    gate = DraftReviewerGate(provider)
    ctx = _final_ctx(
        messages=[
            {"role": "user", "content": "who is X?"},
            {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED},
            {"role": "tool", "name": "web_fetch", "content": "real evidence"},
        ]
    )

    await gate.after_iteration(ctx)

    assert ctx.metadata["verify_gate"]["evidence_elided_skipped"] == 1


@pytest.mark.asyncio
async def test_digest_folds_closing_tag_only_reasoning():
    """The digest model is the served student, whose template prefills the
    opening think tag, so its reply carries a bare closing tag."""
    provider = _ReviewerProvider(
        [LLMResponse(content="long chain of thought</think>Paris is the capital.", finish_reason="stop")]
    )
    digest = _make_digest_fn(provider, None)

    out = await digest("page text", "the capital")

    assert out == "Paris is the capital."
    assert "chain of thought" not in out


@pytest.mark.asyncio
async def test_digest_appends_verbatim_head_after_folding():
    provider = _ReviewerProvider(
        [LLMResponse(content="reasoning</think>extracted fact", finish_reason="stop")]
    )
    digest = _make_digest_fn(provider, None, verbatim_head_chars=8)

    out = await digest("PAGEHEAD rest of the page", "fact")

    assert out.startswith("extracted fact")
    assert "PAGEHEAD" in out
    assert "reasoning" not in out


@pytest.mark.parametrize("stale", ["dr@1", "dr@1.1", "dr@1.2", "dr@1.3"])
def test_superseded_version_labels_are_rejected(stale):
    with pytest.raises(ValueError, match="predates this build"):
        DRFlowConfig(enabled=True, version=stale)


def test_superseded_label_allowed_while_flow_is_off():
    assert DRFlowConfig(enabled=False, version="dr@1.3").version == "dr@1.3"
