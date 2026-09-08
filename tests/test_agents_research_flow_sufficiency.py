"""The first-round sufficiency gate: when it asks, and what it does with the answer.

The gate spends a model call inside every turn that opens a page, and the action it
can take - telling the turn its research is finished - is the one direction this repo
has measured going wrong: a four-line guidance block saying "correct runs are short"
was read as "commit early", and two same-config replicates differed by 5 items fixed
and 9 broken, 5 of the 9 previously correct. So the properties worth pinning are not
"does it parse a verdict" but the three that bound that risk:

  * it asks **once**, and only after the contract's grounding floor is paid;
  * every failure of the judge leaves the turn researching (fail-open direction);
  * the action is an appended note, so the draft still reaches ``verify``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.sufficiency import SufficiencyGate  # noqa: E402
from research_flow.support.harness_text import (  # noqa: E402
    SUFFICIENCY_PREFIX,
    TOOL_OUTPUT_ELIDED,
    sufficiency_notice,
)

from raven.contracts.loop_hooks import AgentHookContext, HookDecision  # noqa: E402

_OK_PAGE = json.dumps({"url": "https://example.com/pricing", "content": "the plan costs $50/mo"})
_FAILED_PAGE = json.dumps({"error": "403 forbidden"})
_SERP = json.dumps({"results": [{"title": "Pricing", "url": "https://example.com/pricing"}]})


class _Response:
    has_tool_calls = True
    finish_reason = "stop"

    def __init__(self, content: str) -> None:
        self.content = content


class _Provider:
    """Records every judge call so a test can assert the gate did not ask."""

    def __init__(self, *replies: object, sleep_s: float = 0.0) -> None:
        self._replies = list(replies)
        self._sleep_s = sleep_s
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> _Response:
        self.calls.append(kwargs)
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        reply = self._replies.pop(0) if self._replies else '{"sufficient": false}'
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, _Response):
            return reply
        return _Response(str(reply))


def _round(*, page: str = _OK_PAGE, searched: bool = True) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "what does it cost?"}]
    if searched:
        messages.append({"role": "tool", "name": "web_search", "content": _SERP})
    messages.append({"role": "tool", "name": "web_fetch", "content": page})
    return messages


def _run(
    gate: SufficiencyGate,
    messages: list[dict],
    *,
    metadata: dict | None = None,
    turn_base: int = 0,
    iteration: int = 2,
    has_tool_calls: bool = True,
) -> tuple[dict, HookDecision]:
    meta = {} if metadata is None else metadata
    response = _Response("")
    response.has_tool_calls = has_tool_calls
    ctx = AgentHookContext(
        session_key="t",
        iteration=iteration,
        messages=messages,
        response=response,
        metadata=meta,
        turn_base=turn_base,
    )
    decision = asyncio.run(gate.after_iteration(ctx))
    return meta.get("sufficiency_gate", {}), decision


def _fired(decision: HookDecision) -> bool:
    return SUFFICIENCY_PREFIX in (decision.append_note or "")


def test_a_sufficient_verdict_appends_the_note_to_the_newest_tool_result():
    """The action is a note on the body the model is already reading, not a tool removal.

    In-history is the only train-serve-safe channel (the runtime-context block is
    stripped on persist), and appending rather than replacing is what keeps the real
    page in front of the model while it drafts.
    """
    provider = _Provider('{"sufficient": true, "reason": "price is stated on the page"}')
    messages = _round()
    state, decision = _run(SufficiencyGate(provider), messages)

    assert _fired(decision)
    assert decision.append_note == sufficiency_notice()
    assert messages[-1]["content"] == _OK_PAGE
    assert state["outcome"] == "sufficient" and state["fired"] is True
    assert state["reason"] == "price is stated on the page"
    assert state["searches"] == 1 and state["fetches_ok"] == 1


def test_the_judge_is_not_asked_before_a_page_has_been_opened():
    """A search listing is titles and links; there is nothing to call sufficient yet.

    Asserted on the provider rather than on the outcome: the cost this gate is judged
    on is calls made, so "did not fire" and "did not ask" are different claims.
    """
    provider = _Provider('{"sufficient": true}')
    messages = [{"role": "user", "content": "q"}, {"role": "tool", "name": "web_search", "content": _SERP}]
    state, decision = _run(SufficiencyGate(provider), messages)

    assert provider.calls == []
    assert not _fired(decision)
    assert state == {"searches": 1, "fetches_ok": 0}


def test_a_failed_fetch_is_not_a_page_opened():
    """``fetch_result_ok`` is the predicate, so a 403 leaves the floor unpaid."""
    provider = _Provider('{"sufficient": true}')
    messages = _round(page=_FAILED_PAGE)
    state, _ = _run(SufficiencyGate(provider), messages)

    assert provider.calls == []
    assert state["fetches_ok"] == 0


def test_a_pasted_url_can_be_judged_when_the_search_floor_is_zero():
    """``min_searches: 0`` is the surface where the user brought the page themselves."""
    provider = _Provider('{"sufficient": true, "reason": "the page answers it"}')
    messages = _round(searched=False)
    state, decision = _run(SufficiencyGate(provider, min_searches=0), messages)

    assert len(provider.calls) == 1
    assert state["outcome"] == "sufficient" and _fired(decision)


def test_an_insufficient_verdict_writes_nothing():
    provider = _Provider('{"sufficient": false, "reason": "no second source"}')
    messages = _round()
    state, decision = _run(SufficiencyGate(provider), messages)

    assert not _fired(decision)
    assert state["outcome"] == "insufficient" and state["fired"] is False


def test_every_judge_failure_leaves_the_turn_researching():
    """Fail-open direction, asserted per failure mode rather than in aggregate.

    A transport error, a truncated generation, empty content and an unparsed verdict
    reach the turn as the same thing - nothing written - and each is a separate path
    in ``_judge``, so a regression in one is invisible in a test that only covers
    another.
    """
    truncated = _Response('{"sufficient": true}')
    truncated.finish_reason = "length"
    errored = _Response('{"sufficient": true}')
    errored.finish_reason = "error"
    for reply in (
        RuntimeError("connection reset"),
        truncated,
        errored,
        _Response(""),
        "not json at all",
        '{"sufficient": "maybe"}',
    ):
        provider = _Provider(reply)
        messages = _round()
        state, decision = _run(SufficiencyGate(provider, timeout_seconds=0.5, attempt_timeout_seconds=0.2), messages)

        assert not _fired(decision), reply
        assert state["outcome"] == "fail_open", reply


def test_a_stalled_attempt_retries_but_an_exhausted_budget_fails_open():
    """The two halves of ``verify``'s timeout shape, which this gate copies: a call
    stalled on a dead pooled connection never errors and never returns, and a fresh
    attempt completes in seconds - so a stall costs an attempt, not the verdict. Only
    running out of total budget fails open."""
    provider = _Provider(asyncio.TimeoutError(), '{"sufficient": true}')
    messages = _round()
    state, decision = _run(SufficiencyGate(provider), messages)
    assert len(provider.calls) == 2
    assert state["outcome"] == "sufficient" and _fired(decision)

    stalling = _Provider(sleep_s=0.2)
    messages = _round()
    state, decision = _run(SufficiencyGate(stalling, timeout_seconds=0.1, attempt_timeout_seconds=0.05), messages)
    assert len(stalling.calls) >= 1
    assert state["outcome"] == "fail_open" and not _fired(decision)


def test_the_judge_is_asked_once_per_turn():
    """The trigger is a floor, so it stays true; without the latch the gate re-asks
    on every later iteration of the same turn - the cost shape ``fetch_gate``'s
    once-per-firing notice exists to avoid."""
    provider = _Provider('{"sufficient": false}', '{"sufficient": true}')
    messages = _round()
    metadata: dict = {}
    _, first = _run(SufficiencyGate(provider), messages, metadata=metadata)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    _, second = _run(SufficiencyGate(provider), messages, metadata=metadata, iteration=3)

    assert len(provider.calls) == 1
    assert not _fired(first) and not _fired(second)


def test_a_fail_open_leaves_a_retry_for_the_next_qualifying_iteration():
    """Only a real verdict latches. The latch used to be set before the judge ran,
    so a truncated first-round judge consumed the turn's only judgement - observed
    live as a six-source second round nobody judged. A fail-open leaves one retry."""
    truncated = _Response('{"sufficient": true}')
    truncated.finish_reason = "length"
    provider = _Provider(truncated, '{"sufficient": true, "reason": "r"}')
    messages = _round()
    metadata: dict = {}
    gate = SufficiencyGate(provider)
    state, decision = _run(gate, messages, metadata=metadata)
    assert state["outcome"] == "fail_open" and not _fired(decision)

    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state, decision = _run(gate, messages, metadata=metadata, iteration=3)
    assert len(provider.calls) == 2
    assert state["outcome"] == "sufficient" and _fired(decision)


def test_the_retry_is_capped_so_a_flaky_upstream_cannot_tax_every_iteration():
    """The other half of the latch change: without a cap, a permanently broken
    judge would be re-asked on every remaining iteration of the turn."""
    provider = _Provider("not json", "still not json", '{"sufficient": true}')
    messages = _round()
    metadata: dict = {}
    gate = SufficiencyGate(provider)
    _run(gate, messages, metadata=metadata)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    _run(gate, messages, metadata=metadata, iteration=3)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state, decision = _run(gate, messages, metadata=metadata, iteration=4)

    assert len(provider.calls) == 2
    assert state["attempts"] == 2
    assert not _fired(decision)


def test_an_earlier_turns_research_does_not_pay_this_turns_floor():
    """Scope is ``turn_base``, not the session.

    A conversation whose previous turn searched and fetched would otherwise satisfy
    the floor on this turn's first iteration, and the note it writes - "the pages you
    have opened" - would be about pages this turn never saw.
    """
    provider = _Provider('{"sufficient": true}')
    messages = _round() + [
        {"role": "user", "content": "and the other one?"},
        {"role": "tool", "name": "web_search", "content": _SERP},
    ]
    state, _ = _run(SufficiencyGate(provider), messages, turn_base=3)

    assert provider.calls == []
    assert state == {"searches": 1, "fetches_ok": 0}


def test_the_judge_reads_this_turns_evidence_fenced_as_untrusted():
    """The judge's verdict decides control flow and its input is page content, so the
    pack is fenced. Elided bodies are skipped rather than packed: a judge shown a
    placeholder is being asked whether the harness's own sentence answers the task."""
    provider = _Provider('{"sufficient": true}')
    messages = _round()
    messages.insert(1, {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED})
    _run(SufficiencyGate(provider), messages)

    user = provider.calls[0]["messages"][-1]["content"]
    assert "BEGIN UNTRUSTED" in user
    assert "the plan costs $50/mo" in user
    assert TOOL_OUTPUT_ELIDED not in user
    assert "what does it cost?" in user


def test_a_turn_that_is_already_writing_is_not_taxed():
    """No tool calls means the answer is being drafted; there is nothing to release."""
    provider = _Provider('{"sufficient": true}')
    messages = _round()
    _, decision = _run(SufficiencyGate(provider), messages, has_tool_calls=False)

    assert provider.calls == []
    assert not _fired(decision)


def test_the_judge_call_is_pinned_to_a_deterministic_low_effort_shape():
    """temperature 0 and the effort knob, for the reason the conversation gate's
    ``gate_max_tokens`` docstring records: a reasoning model spends the budget on its
    think block first and returns empty content, which reads as an unparsed verdict."""
    provider = _Provider('{"sufficient": true}')
    _run(SufficiencyGate(provider, model="small", max_tokens=256, reasoning_effort="low"), _round())

    call = provider.calls[0]
    assert call["model"] == "small"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 256
    assert call["reasoning_effort"] == "low"
    # No transport deadline unless one is configured. This line asserted 25.0 --
    # five seconds inside the default attempt slice, so a hang would raise
    # something classifiable -- and the arithmetic was right while the kwarg was
    # not: the trunk provider takes no ``timeout``, so every judge call raised
    # TypeError and the gate failed open having sent nothing. A permissive
    # ``**kwargs`` stub is what let both the gate and this assertion agree with
    # each other and with nothing else.
    assert "timeout" not in call


def test_an_unset_effort_knob_sends_no_effort_parameter_at_all():
    """``None`` means "leave the provider default", and only OMITTING the argument
    does that: ``chat_with_retry`` resolves an absent value through its sentinel to
    ``self.generation.reasoning_effort``, while an explicit ``None`` suppresses the
    parameter on the wire. Passing it unconditionally would make the documented
    default unreachable - the same distinction the verify gate carries."""
    provider = _Provider('{"sufficient": true}')
    _run(SufficiencyGate(provider, reasoning_effort=None), _round())

    assert "reasoning_effort" not in provider.calls[0]


# --------------------------------------------------------------------------
# Wiring: default-off, and unreachable from the anchor by construction
# --------------------------------------------------------------------------


def test_the_gate_is_absent_until_its_knob_is_set(tmp_path):
    """Flow-on alone must not install it. The knob is the only thing that does, which
    is what lets an arm's config say whether its trajectories could have been
    released early."""
    from research_flow.config import FlowConfig
    from research_flow.flow import ToolHandles, build_chain
    from research_flow.state import SessionStore

    chain = build_chain(
        FlowConfig(enabled=True),
        _Provider(),
        max_iterations=40,
        context_window_tokens=65_536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    assert not any(isinstance(o, SufficiencyGate) for o in chain)

    config = FlowConfig(enabled=True)
    config.sufficiency.enabled = True
    config.sufficiency.min_fetches = 2
    chain = build_chain(
        config,
        _Provider(),
        max_iterations=40,
        context_window_tokens=65_536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    gate = next(o for o in chain if isinstance(o, SufficiencyGate))
    assert gate._min_fetches == 2


def test_the_anchor_cannot_reach_the_gate_however_the_knob_is_written(tmp_path):
    """``make_hook`` returns None with the flow off, so the flow-off arm builds no
    hook at all - the construction-level isolation AGENTS.md 0.4 requires of any
    change that would otherwise flatter the treated arm."""
    from types import SimpleNamespace

    from research_flow.plugin import make_hook
    from research_flow.support.ledger import set_ledger_dir

    ctx = SimpleNamespace(
        config={"enabled": False, "sufficiency": {"enabled": True}},
        services=SimpleNamespace(workspace=tmp_path, provider=None),
    )
    try:
        assert make_hook(ctx) is None
    finally:
        set_ledger_dir(None)


# --------------------------------------------------------------------------
# The judge's call shape against the trunk provider
# --------------------------------------------------------------------------


class _SignatureBoundProvider:
    """``chat_with_retry`` with the trunk protocol's parameters, kwarg for kwarg.

    ``raven.contracts.llm_provider.LLMProvider.chat_with_retry`` takes no
    ``timeout``, so a gate that passes one raises TypeError before any request
    leaves the process -- and this gate's fail-open contract then records that
    as an ordinary unavailable judge. The stub refuses unknown kwargs rather
    than swallowing them: ``_Provider`` above takes ``**kwargs``, which is why
    every verdict test here passed while the shipped gate never once reached a
    provider.
    """

    def __init__(self, reply: str = '{"sufficient": true, "reason": "the pages answer it"}') -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def chat_with_retry(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=None,
        temperature=None,
        reasoning_effort=None,
        tool_choice=None,
        fallback_models=None,
    ) -> _Response:
        self.calls.append({"model": model, "max_tokens": max_tokens, "temperature": temperature})
        return _Response(self._reply)


def test_the_judge_call_carries_no_transport_timeout_by_default():
    """Unset, the transport deadline stays out of the call entirely.

    The gate passed ``timeout`` unconditionally until 2026-09-07, so on the
    trunk provider it failed open on every turn of every mode in about a
    millisecond, having sent nothing. Measured on a smoke turn: ``outcome:
    fail_open``, ``latency_s: 0.001``, and a TypeError in the log naming the
    kwarg. The mode whose entire stop rule is this gate ran without one.
    """
    provider = _SignatureBoundProvider()
    gate = SufficiencyGate(provider, min_searches=1, min_fetches=1)
    state, decision = _run(gate, _round())
    assert provider.calls, "the judge never reached the provider"
    assert state.get("outcome") == "sufficient", state
    assert _fired(decision)


def test_the_gate_declares_no_transport_deadline_knob():
    """There is no knob to turn this back on, and that is deliberate.

    ``verify`` carries ``attemptHttpTimeoutSeconds`` because the fork's reviewer
    did; the fork's judge had no such field, and the twin's models are held field
    for field to the fork's by the parity suite. So the deadline comes back only
    when the trunk provider accepts one -- not through a knob this product invents.
    """
    from research_flow.config import SufficiencyConfig

    assert "attempt_http_timeout_seconds" not in SufficiencyConfig.model_fields


# --------------------------------------------------------------------------
# The listing stage: a second judgement one round earlier, product-only
# --------------------------------------------------------------------------

from research_flow.support.harness_text import (  # noqa: E402
    harness_body_kind,
    is_harness_echo,
    sufficiency_listing_notice,
)

_LONG_SERP = json.dumps(
    {
        "results": [
            {"title": f"Result {i}", "url": f"https://s{i}.example/", "snippet": "the capital is Canberra " * 20}
            for i in range(12)
        ]
    }
)


def _listing_only() -> list[dict]:
    return [
        {"role": "user", "content": "capital of Australia?"},
        {"role": "tool", "name": "web_search", "content": _SERP},
    ]


def test_off_by_default_the_listing_is_never_judged():
    """The measured arms ran without this stage, and their flow must be byte-identical:
    a search-only round asks nothing, exactly as before the knob existed."""
    provider = _Provider('{"sufficient": true}')
    state, decision = _run(SufficiencyGate(provider, min_fetches=2), _listing_only())
    assert provider.calls == [] and not _fired(decision) and "stage" not in state


def test_with_the_knob_on_the_listing_is_judged_before_any_page():
    """One search, no page: the judge is asked over the snippets with the listing rubric,
    and a yes releases with the listing variant of the note - the one that waives
    contract rule 1 and says to cite result URLs."""
    provider = _Provider('{"sufficient": true, "reason": "snippets agree"}')
    state, decision = _run(SufficiencyGate(provider, min_fetches=2, judge_listing=True), _listing_only())
    assert len(provider.calls) == 1
    system = provider.calls[0]["messages"][0]["content"]
    assert "no page opened yet" in system and "When in doubt, insufficient" in system
    assert decision.append_note == sufficiency_listing_notice()
    assert (state["stage"], state["released_on"], state["listing_outcome"]) == ("listing", "listing", "sufficient")
    assert "evaluated" not in state, "the page-floor latch is untouched by a listing verdict"


def test_an_insufficient_listing_verdict_leaves_the_page_judgement_intact():
    """The stage is a second latch, not a lower floor. A research question fails the
    listing judge and must still get its page-floor judgement once two pages are open
    - otherwise the knob would silently take medium's release door away."""
    provider = _Provider(
        '{"sufficient": false, "reason": "only implied"}', '{"sufficient": true, "reason": "page states it"}'
    )
    gate = SufficiencyGate(provider, min_fetches=2, judge_listing=True)
    messages = _listing_only()
    metadata: dict = {}
    state, first = _run(gate, messages, metadata=metadata)
    assert not _fired(first) and state["listing_outcome"] == "insufficient" and state["listing_evaluated"] is True

    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state, second = _run(gate, messages, metadata=metadata, iteration=3)
    assert len(provider.calls) == 1, "one page is below the floor of two: no judgement yet"

    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state, third = _run(gate, messages, metadata=metadata, iteration=4)
    assert len(provider.calls) == 2
    assert "no page opened yet" not in provider.calls[1]["messages"][0]["content"]
    assert third.append_note == sufficiency_notice()
    assert (state["stage"], state["released_on"], state["evaluated"]) == ("pages", "pages", True)


def test_a_listing_release_is_still_a_release_after_the_page_judge_says_no():
    """The record is per stage, and ``fired`` is sticky.

    A listing release leaves the tools in place, so the model may open pages anyway;
    the page stage then gets its own judgement, and an "insufficient" there must not
    overwrite the fact that this turn was released - ``turn_observers`` publishes the
    whole dict, and a reader counting releases reads ``fired``.
    """
    provider = _Provider(
        '{"sufficient": true, "reason": "the snippets state it"}', '{"sufficient": false, "reason": "pages disagree"}'
    )
    gate = SufficiencyGate(provider, min_fetches=2, judge_listing=True)
    messages = _listing_only()
    metadata: dict = {}
    state, first = _run(gate, messages, metadata=metadata)
    assert first.append_note == sufficiency_listing_notice() and state["fired"] is True

    messages.extend([{"role": "tool", "name": "web_fetch", "content": _OK_PAGE}] * 2)
    state, second = _run(gate, messages, metadata=metadata, iteration=3)
    assert len(provider.calls) == 2 and second.append_note is None
    assert (state["stage"], state["outcome"]) == ("pages", "insufficient"), "the newest judgement is the page stage's"
    assert (state["listing_outcome"], state["pages_outcome"]) == ("sufficient", "insufficient")
    assert state["fired"] is True and state["released_on"] == "listing"


def test_the_listing_is_asked_once_and_a_fail_open_keeps_its_own_retry():
    truncated = _Response('{"sufficient": true}')
    truncated.finish_reason = "length"
    provider = _Provider(truncated, '{"sufficient": false}', '{"sufficient": true}')
    gate = SufficiencyGate(provider, min_fetches=2, judge_listing=True)
    messages = _listing_only()
    metadata: dict = {}
    _run(gate, messages, metadata=metadata)
    messages.append({"role": "tool", "name": "web_search", "content": _SERP})
    _run(gate, messages, metadata=metadata, iteration=3)
    messages.append({"role": "tool", "name": "web_search", "content": _SERP})
    state, decision = _run(gate, messages, metadata=metadata, iteration=4)
    assert len(provider.calls) == 2 and state["listing_attempts"] == 2
    assert not _fired(decision)


def test_the_listing_judge_reads_the_top_of_the_listing_not_its_tail():
    """Ranking puts the deciding results first; the page pack keeps a body's tail
    (where the extracted fact lands) and would have shown the judge results 9-12."""
    provider = _Provider('{"sufficient": false}')
    gate = SufficiencyGate(provider, min_fetches=2, judge_listing=True, evidence_item_chars=600)
    messages = [{"role": "user", "content": "q"}, {"role": "tool", "name": "web_search", "content": _LONG_SERP}]
    _run(gate, messages)
    user = provider.calls[0]["messages"][1]["content"]
    assert "Result 0" in user and "Result 11" not in user


def test_a_listing_release_is_a_harness_sentence_on_the_same_terms_as_the_page_one():
    assert sufficiency_listing_notice().startswith(SUFFICIENCY_PREFIX)
    assert sufficiency_listing_notice() != sufficiency_notice()
    assert is_harness_echo(sufficiency_listing_notice())
    assert harness_body_kind(sufficiency_listing_notice()) is harness_body_kind(sufficiency_notice()) is None


def test_the_listing_knob_reaches_the_gate_from_config(tmp_path):
    from research_flow.config import FlowConfig
    from research_flow.flow import ToolHandles, build_chain
    from research_flow.state import SessionStore

    config = FlowConfig(enabled=True)
    config.sufficiency.enabled = True
    assert config.sufficiency.judge_listing is False
    config = config.with_overlay({"sufficiency": {"judgeListing": True}})
    chain = build_chain(
        config,
        _Provider(),
        max_iterations=40,
        context_window_tokens=65_536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    gate = next(o for o in chain if isinstance(o, SufficiencyGate))
    assert gate._judge_listing is True
