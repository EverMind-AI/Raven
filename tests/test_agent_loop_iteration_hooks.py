"""The iteration phases fire inside the loop and their grants take effect.

A product that steers the loop -- budget notes, forced finalization, spin
breaking -- lives entirely on these seams, so what is pinned here is the
loop's side of the contract: where each phase fires, what a rollback does
to history and to the iteration budget, that injected messages reach the
re-sample and persist, that generation overrides reach the provider only
when allowlisted, that a withheld tool is withheld for one iteration only,
and that the terminal seam sees an answerless exit.
"""

from __future__ import annotations

import pytest

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.agent.loop.turn_path import _stamp_turn_observers
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.spine import ChatType, Origin, Source, TurnRequest


class _ScriptedProvider:
    """chat_with_retry answers from a script and records every call's kwargs."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        # Snapshot: the loop mutates-and-returns the same message list, so a
        # stored reference would show the turn's end state, not this call's.
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs.get("messages") or []]
        snapshot["tools"] = list(kwargs.get("tools") or [])
        self.calls.append(snapshot)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]

    def get_default_model(self) -> str:
        return "fake/model"


def _text(content: str, **extra) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop", **extra)


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)])


def _req(text: str = "hi") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


def _loop(tmp_path, provider, hooks, max_iterations: int = 6) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=max_iterations),
        host=HostWiring(hooks=hooks),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


@pytest.mark.asyncio
async def test_before_iteration_sees_the_turn_and_can_withhold_a_tool_for_one_iteration(tmp_path):
    seen: list[tuple[int, str, int]] = []

    class WithholdFirst(AgentHook):
        async def before_iteration(self, ctx):
            seen.append((ctx.iteration, ctx.turn_question, ctx.turn_base))
            if ctx.iteration == 1:
                return HookDecision(modified_tools=[t for t in ctx.tools if t["function"]["name"] != "list_dir"])
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider, [WithholdFirst()])

    out = await loop._process_message(_req("what is here"))

    assert out is not None
    assert seen[0][1] == "what is here"
    first_call_tools = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    second_call_tools = {t["function"]["name"] for t in provider.calls[1]["tools"]}
    assert "list_dir" not in first_call_tools, "withheld for the first iteration"
    assert "list_dir" in second_call_tools, "and offered again on the next -- the registry was untouched"


@pytest.mark.asyncio
async def test_before_execute_tools_rollback_discards_the_proposal_and_does_not_consume_an_iteration(tmp_path):
    class BounceOnce(AgentHook):
        def __init__(self) -> None:
            self.bounced = False

        async def before_execute_tools(self, ctx):
            if not self.bounced:
                self.bounced = True
                return HookDecision(
                    rollback=True,
                    rollback_inject=[{"role": "user", "content": "[gate] do not call tools; answer directly"}],
                    rollback_overrides={"temperature": 0.0, "bogus": 1},
                )
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("answered directly")])
    loop = _loop(tmp_path, provider, [BounceOnce()], max_iterations=1)

    out = await loop._process_message(_req())

    # Budget of one iteration, two provider calls: the rollback did not spend it.
    assert out is not None and "answered directly" in str(out)
    assert len(provider.calls) == 2
    resample = provider.calls[1]
    assert resample["temperature"] == 0.0, "allowlisted override reached the re-sample"
    assert "bogus" not in resample, "unknown override was dropped, not forwarded"
    assert resample["messages"][-1]["content"] == "[gate] do not call tools; answer directly"
    assert not any(m.get("tool_calls") for m in resample["messages"]), "the discarded proposal is gone"


@pytest.mark.asyncio
async def test_after_iteration_short_circuit_replaces_the_draft_without_persisting_it(tmp_path):
    class Replace(AgentHook):
        async def after_iteration(self, ctx):
            if ctx.response is not None and not ctx.response.has_tool_calls:
                return HookDecision(short_circuit_result="reviewed answer")
            return HookDecision()

    provider = _ScriptedProvider([_text("first draft")])
    loop = _loop(tmp_path, provider, [Replace()])

    out = await loop._process_message(_req())

    assert "reviewed answer" in str(out)
    session = loop.sessions.get_or_create("cli:c")
    texts = [m.get("content") for m in session.messages if m.get("role") == "assistant"]
    assert "first draft" not in texts
    assert "reviewed answer" in texts


@pytest.mark.asyncio
async def test_an_honoured_rollback_is_counted_where_the_next_iteration_can_read_it(tmp_path):
    """The re-sample carries the same iteration number, so a hook scoped to the
    turn boundary cannot tell it from the first sampling by ``ctx.iteration``
    alone. The loop records the honoured count beside the refused one."""

    class BounceOnce(AgentHook):
        def __init__(self) -> None:
            self.seen: list[tuple[int | None, int]] = []

        async def before_iteration(self, ctx):
            self.seen.append((ctx.iteration, ctx.metadata.get("hook_rollbacks", 0)))
            return HookDecision()

        async def after_iteration(self, ctx):
            if ctx.metadata.get("bounced"):
                return HookDecision()
            ctx.metadata["bounced"] = True
            return HookDecision(rollback=True)

    hook = BounceOnce()
    provider = _ScriptedProvider([_text("draft"), _text("final")])
    loop = _loop(tmp_path, provider, [hook], max_iterations=3)

    out = await loop._process_message(_req())

    assert out is not None
    # Iteration 1 twice: the first sampling with no rollback on record, the
    # re-sample with exactly one.
    assert hook.seen == [(1, 0), (1, 1)]


@pytest.mark.asyncio
async def test_the_rollback_cap_degrades_to_pass_through_and_is_counted(tmp_path):
    class AlwaysBounce(AgentHook):
        def __init__(self) -> None:
            self.refusals_seen = 0

        async def after_iteration(self, ctx):
            self.refusals_seen = ctx.metadata.get("rollbacks_refused", 0)
            return HookDecision(rollback=True)

    hook = AlwaysBounce()
    provider = _ScriptedProvider([_text("draft")])
    loop = _loop(tmp_path, provider, [hook], max_iterations=2)

    out = await loop._process_message(_req())

    assert out is not None
    assert len(provider.calls) == AgentLoop._MAX_HOOK_ROLLBACKS + 1, "the cap bounds the re-samples"


@pytest.mark.asyncio
async def test_terminal_answerless_can_commit_an_answer_after_a_provider_error(tmp_path):
    class Salvage(AgentHook):
        async def terminal_answerless(self, ctx):
            assert ctx.metadata["turn_end"]["status"] == "error"
            return HookDecision(short_circuit_result="salvaged answer")

    provider = _ScriptedProvider([LLMResponse(content="upstream failed", finish_reason="error")])
    loop = _loop(tmp_path, provider, [Salvage()])

    out = await loop._process_message(_req())

    assert "salvaged answer" in str(out)


@pytest.mark.asyncio
async def test_no_hooks_means_no_phase_work(tmp_path):
    provider = _ScriptedProvider([_text("plain")])
    loop = _loop(tmp_path, provider, None)

    out = await loop._process_message(_req())

    assert "plain" in str(out)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_session_policy_caps_iterations_and_reaches_the_hooks(tmp_path):
    seen: list[dict] = []

    class Observe(AgentHook):
        async def before_iteration(self, ctx):
            seen.append(dict(ctx.metadata))
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."})] * 5 + [_text("done")])
    loop = _loop(tmp_path, provider, [Observe()], max_iterations=6)
    loop.set_session_policy("cli:c", max_iterations=2, mode="deep", mode_overlay={"k": 10})

    out = await loop._process_message(_req())

    assert out is not None
    assert len(provider.calls) <= 3, "the session cap of 2 held (plus the exhaustion wrap-up call)"
    assert seen[0]["mode"] == "deep" and seen[0]["mode_overlay"] == {"k": 10}
    assert loop.session_policy("cli:other").mode == "", "another session runs on the defaults"


@pytest.mark.asyncio
async def test_an_after_iteration_note_lands_on_the_last_message_before_the_next_call(tmp_path):
    class Nudge(AgentHook):
        async def after_iteration(self, ctx):
            if getattr(ctx.response, "has_tool_calls", False):
                return HookDecision(append_note="[budget warning: most of the budget is spent]")
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider, [Nudge()])

    out = await loop._process_message(_req("what is here"))

    assert out is not None
    last_seen = provider.calls[1]["messages"][-1]
    assert last_seen["role"] == "tool"
    assert str(last_seen["content"]).endswith("\n\n[budget warning: most of the budget is spent]")
    assert "[budget warning" not in str(provider.calls[0]["messages"][-1]["content"]), "landed after, not before"


@pytest.mark.asyncio
async def test_before_user_inbound_can_rewrite_the_inbound_text(tmp_path):
    seeded: list[str | None] = []

    class Memo(AgentHook):
        async def before_user_inbound(self, ctx):
            seeded.append(ctx.inbound_content)
            return HookDecision(modified_content=f"[research memo]\n\n{ctx.inbound_content}")

    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider, [Memo()])

    out = await loop._process_message(_req("what changed since last time?"))

    assert out is not None
    assert seeded == ["what changed since last time?"]
    dispatched = str(provider.calls[0]["messages"][-1]["content"])
    assert "[research memo]" in dispatched and "what changed since last time?" in dispatched


@pytest.mark.asyncio
async def test_metadata_is_one_dict_across_all_three_phase_groups(tmp_path):
    seen: dict[str, object] = {}

    class Stash(AgentHook):
        async def before_user_inbound(self, ctx):
            ctx.metadata["stash"] = "from-the-door"
            seen["inbound_history"] = list(ctx.session_history or [])
            return HookDecision()

        async def before_iteration(self, ctx):
            seen["at_iteration"] = ctx.metadata.get("stash")
            return HookDecision()

        async def after_send(self, ctx):
            seen["at_send"] = ctx.metadata.get("stash")
            seen["send_history"] = len(ctx.session_history or [])
            return HookDecision()

    provider = _ScriptedProvider([_text("done"), _text("again")])
    loop = _loop(tmp_path, provider, [Stash()])

    out = await loop._process_message(_req("hello"))

    assert out is not None
    assert seen["at_iteration"] == "from-the-door"
    assert seen["at_send"] == "from-the-door"
    assert seen["inbound_history"] == [], "a fresh session has no record yet"
    assert seen["send_history"] == 0, "the send fires before this turn is filed"

    out2 = await loop._process_message(_req("and now?"))

    assert out2 is not None
    assert len(seen["inbound_history"]) >= 2, "the second turn's inbound sees the first turn's record"


@pytest.mark.asyncio
async def test_history_keeps_the_users_words_not_the_hooks_rewrite(tmp_path):
    class Memo(AgentHook):
        async def before_user_inbound(self, ctx):
            return HookDecision(modified_content=f"[research memo]\n\n{ctx.inbound_content}\n\n[reminder]")

    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider, [Memo()])

    out = await loop._process_message(_req("what changed?"))

    assert out is not None
    dispatched = str(provider.calls[0]["messages"][-1]["content"])
    assert "[research memo]" in dispatched, "the model saw the rewrite"
    session = loop.sessions.get_or_create("cli:c")
    users = [m for m in session.messages if m.get("role") == "user"]
    assert users and users[-1]["content"] == "what changed?", "history kept the user's own words"


@pytest.mark.asyncio
async def test_a_hooks_observers_dict_lands_on_the_turns_last_assistant_message(tmp_path):
    class Counter(AgentHook):
        async def after_iteration(self, ctx):
            ctx.metadata.setdefault("observers", {})["gate"] = {"fired": 1}
            return HookDecision()

    provider = _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")])
    loop = _loop(tmp_path, provider, [Counter()])

    out = await loop._process_message(_req("look around"))

    assert out is not None
    session = loop.sessions.get_or_create("cli:c")
    stamped = [m for m in session.messages if m.get("observers")]
    assert len(stamped) == 1 and stamped[0]["role"] == "assistant"
    assert stamped[0]["observers"] == {"gate": {"fired": 1}}


@pytest.mark.asyncio
async def test_iteration_phases_read_the_filed_record_which_still_ends_with_the_previous_turn(tmp_path):
    histories: list[list] = []

    class Reader(AgentHook):
        async def before_iteration(self, ctx):
            if ctx.iteration == 1:
                histories.append(list(ctx.session_history or []))
            return HookDecision()

    provider = _ScriptedProvider([_text("first answer"), _text("second answer")])
    loop = _loop(tmp_path, provider, [Reader()])

    await loop._process_message(_req("first"))
    await loop._process_message(_req("second"))

    assert histories[0] == [], "a fresh session has no filed record yet"
    assert histories[1], "the second turn's iterations see the first turn's record"
    assert any("first answer" in str(m.get("content")) for m in histories[1])
    assert all("second" != str(m.get("content")) for m in histories[1]), "the running turn is not filed yet"


@pytest.mark.asyncio
async def test_iteration_phases_carry_the_cap_the_loop_enforces_and_the_bindings_window(tmp_path):
    seen: dict[str, object] = {}

    class Reader(AgentHook):
        async def before_iteration(self, ctx):
            seen.setdefault("cap", ctx.max_iterations)
            seen.setdefault("window", ctx.context_window_tokens)
            return HookDecision()

        async def before_user_inbound(self, ctx):
            seen["inbound_cap"] = ctx.max_iterations
            return HookDecision()

    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider, [Reader()], max_iterations=6)
    loop.set_session_policy("cli:c", max_iterations=2)

    out = await loop._process_message(_req())

    assert out is not None
    assert seen["cap"] == 2, "the stamped cap is the session policy's -- the one the loop enforces"
    assert isinstance(seen["window"], int) and seen["window"] > 0
    assert seen["inbound_cap"] is None, "the inbound fire predates the loop's one policy read"


@pytest.mark.asyncio
async def test_an_observers_stash_written_at_after_send_reaches_the_persisted_message(tmp_path):
    class SendCounter(AgentHook):
        async def after_send(self, ctx):
            ctx.metadata.setdefault("observers", {})["send_gate"] = {"fired": 1}
            return HookDecision()

    provider = _ScriptedProvider([_text("done")])
    loop = _loop(tmp_path, provider, [SendCounter()])

    out = await loop._process_message(_req("hello"))

    assert out is not None
    session = loop.sessions.get_or_create("cli:c")
    stamped = [m for m in session.messages if m.get("observers")]
    assert len(stamped) == 1 and stamped[0]["role"] == "assistant"
    assert stamped[0]["observers"] == {"send_gate": {"fired": 1}}


def test_the_persist_stamp_never_crosses_the_turn_base_into_filed_history():
    """The stamp's bound: the paper files observers on THE TURN'S message.

    An answerless turn has no seat, and the assembled window can share dict
    objects with the session record (the curator candidate view), so an
    unbounded search would rewrite a previous turn's filed message in
    place -- this turn's measurements stamped onto last turn's answer."""

    prior_assistant = {"role": "assistant", "content": "earlier answer"}
    messages = [
        {"role": "user", "content": "earlier question"},
        prior_assistant,
        {"role": "user", "content": "now this"},
    ]

    _stamp_turn_observers(messages, {"observers": {"gate": {"fired": 1}}}, turn_base=2)

    assert "observers" not in prior_assistant, "filed history must not be rewritten in place"
    assert not any("observers" in m for m in messages), "an answerless turn stamps nothing"
