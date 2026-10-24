"""The checkpoint -- the request that writes it, its shape, and its chain.

Three claims are worth holding here, and only the first needs a whole assembled
turn to see:

**The summarising request is a verbatim prefix of the conversation.** That is
what makes the call cheap: a provider serves the shared part from its cache and
bills only the instruction and the reply. Asserted structurally rather than
from a cache counter -- the counter is what the property *causes*, on a
provider that reports one, and the property itself is checkable here and now:
every message of the compaction request except its last must be, byte for byte,
the message at that same position in the request the turn before it sent.

**A checkpoint supersedes the one before it, and the chain is walkable.** A
summary that grew by appending would eventually be the thing overflowing the
window, so each generation folds the last one in and names it.

**Every summary line can be cashed.** A line whose refs went missing is an
assertion nobody can check against the log, so what the model does not carry is
attached by the code -- and what comes out has to name this revision of the
log, at ordinals the log still holds.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.config.raven import (
    ContextCacheConfig,
    ContextCompactionConfig,
    ContextConfig,
    ContextHistoryConfig,
    ContextPruneConfig,
    ContextSummaryConfig,
)
from raven.context_engine import headroom
from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.history.compacting import CompactingHistoryBuilder
from raven.context_engine.history.compacting.checkpoint import (
    SECTION_GUIDE,
    SECTIONS,
    CompactionCheckpoint,
    SummaryEntry,
    ground_refs,
    merge_refs,
    parse_sections,
)
from raven.context_engine.history.compacting.store import CHECKPOINT_DIR, CheckpointStore
from raven.context_engine.history.compacting.writer import (
    COMPACTION_INSTRUCTION,
    PARTIAL_REPLAY_NOTE,
    SYSTEM_KEY,
    CheckpointWriter,
    RequestRecord,
    ReusedPrefix,
    SummaryFailure,
    prefix_tokens,
    reuse_prefix,
    session_message_key,
    shrink_prefix,
)
from raven.context_engine.history.markers import HISTORY_PRIVATE_KEYS
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.contracts.assembled import TokenBudget
from raven.contracts.context import AssembledPrefix, AssemblyContext, SessionRevision
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.session.refs import belongs_to
from raven.token_wise.cache_optimizer import CacheOptimizer
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_prompt_tokens

WINDOW = 32_768
FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod " * 20
LOG_ID = "0123456789abcdef" * 2

# Exchange counts either side of the 28672 trigger this window produces, so a
# session can be stepped over it without the test guessing where the line is.
UNDER = 60
OVER = 70
FAR_OVER = 130

SUMMARY_REPLY = """## Goal
- Fix the 401 that follows a refresh
## User Constraints
- The user said: "don't touch middleware, that's another team's"
## Decisions
- Start from the token rotation order, leave middleware.ts alone
## Artifacts
- auth.ts: refresh() calls out of order at line 214
## Findings
- The first test failed with AssertionError: expected 200, got 401
## Open Threads
[None]
## Next Step
- Re-run auth.test.ts after reordering auth.ts:214
## Pointers
[None]"""

# The template answered back rather than filled in: it parses, every heading is
# present, and nothing is under any of them. This is what a model replies when
# the request is past what it can hold together, and it is indistinguishable
# from a conversation that genuinely had nothing to report.
HEADINGS_ONLY = "\n".join(f"## {name}\n- [{guide}]" for name, guide in SECTION_GUIDE.items())


class _RecordingProvider:
    """Answers whatever it is asked, and keeps every request verbatim."""

    api_key = "test"

    def __init__(
        self,
        reply: str = SUMMARY_REPLY,
        *,
        tool_call: bool = False,
        hang: bool = False,
        finish_reason: str = "stop",
        error_classification: object | None = None,
        usage: dict | None = None,
        raises: bool = False,
    ) -> None:
        self.raises = raises
        self.reply = reply
        self.tool_call = tool_call
        self.hang = hang
        self.finish_reason = finish_reason
        self.error_classification = error_classification
        self.usage = usage
        self.sent: list[list[dict]] = []
        self.tools: list[list[dict] | None] = []
        self.models: list[str | None] = []

    async def chat_with_retry(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.sent.append(messages)
        self.tools.append(tools)
        self.models.append(model)
        if self.hang:
            await asyncio.sleep(5)
        if self.raises:
            raise RuntimeError("401 invalid api key")
        if self.tool_call:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="t1", name="read_file", arguments={"path": "a"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content=self.reply,
            finish_reason=self.finish_reason,
            usage=self.usage or {},
            error_classification=self.error_classification,
        )

    def get_default_model(self) -> str:
        return "stub"

    def wire_model_id(self, model: str) -> str:
        return model


class _RelentingProvider(_RecordingProvider):
    """Refuses to answer the first request, then answers any shorter one.

    The relenting is keyed on the request's own length rather than on a call
    counter, so what the test asserts is that *shortening* rescued the summary
    -- a provider that simply succeeded on the second call would pass whether
    or not anything had changed about the request.
    """

    async def chat_with_retry(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        first = len(self.sent[0]) if self.sent else None
        self.reply = SUMMARY_REPLY if first is not None and len(messages) < first else HEADINGS_ONLY
        return await super().chat_with_retry(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )


class _SlowAtFullSizeProvider(_RecordingProvider):
    """Never comes back from the first request; answers any shorter one.

    A summary is the longest request this conversation makes, so a timeout on
    one is size-shaped in a way a timeout elsewhere is not -- which is why it
    is on the list of failures a shorter attempt is allowed to follow.
    """

    async def chat_with_retry(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        first = len(self.sent[0]) if self.sent else None
        self.hang = first is None or len(messages) >= first
        return await super().chat_with_retry(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )


@pytest.fixture(autouse=True)
def _forget_learned_headroom():
    headroom.reset()
    yield
    headroom.reset()


def _session(exchanges: int) -> list[dict]:
    """A conversation whose first ``2 * n`` messages never change with ``n``."""
    messages: list[dict] = []
    for index in range(exchanges):
        messages.append({"role": "user", "content": f"question {index} {FILLER}"})
        messages.append({"role": "assistant", "content": f"answer {index} {FILLER}"})
    return messages


def _more(count: int, *, start: int) -> list[dict]:
    """``count`` further exchanges, numbered on from ``start``."""
    messages: list[dict] = []
    for index in range(start, start + count):
        messages.append({"role": "user", "content": f"question {index} {FILLER}"})
        messages.append({"role": "assistant", "content": f"answer {index} {FILLER}"})
    return messages


def _tool_exchange(call_id: str, tool: str, result: str, arguments: str = "{}") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool, "arguments": arguments}}],
        },
        {"role": "tool", "tool_call_id": call_id, "name": tool, "content": result},
    ]


def _budget(model: str = "stub", window: int = WINDOW) -> TokenBudget:
    reserved = headroom.reply_headroom(model, window=window)
    return TokenBudget(
        context_length=window,
        reserved_output=reserved,
        reserved_tools=0,
        reserved_system=200,
        available_history=window - reserved - 200,
    )


def _summary_config(**kwargs) -> ContextConfig:
    return ContextConfig(
        history=ContextHistoryConfig(compacting=ContextCompactionConfig(summary=ContextSummaryConfig(**kwargs)))
    )


def _builder(
    tmp_path: Path,
    provider: object,
    *,
    config: ContextConfig | None = None,
    tools: list[dict] | None = None,
    model: str = "stub",
) -> CompactingHistoryBuilder:
    return CompactingHistoryBuilder(
        tmp_path,
        config or ContextConfig(),
        provider,  # type: ignore[arg-type]
        model,
        WINDOW,
        lambda: list(tools or []),
    )


def _revision(session: list[dict], *, log_id: str = LOG_ID) -> SessionRevision:
    return SessionRevision(log_id=log_id, generation=0)


def _ctx(
    session: list[dict],
    *,
    session_key: str = "s1",
    tools: list[dict] | None = None,
    revision: SessionRevision | None = None,
) -> AssemblyContext:
    return AssemblyContext(
        session_key=session_key,
        current_message="what now",
        media=None,
        channel=None,
        chat_id=None,
        session_messages=session,
        budget=_budget(),
        prefix=AssembledPrefix(
            system_prefix="system prompt",
            user_message={"role": "user", "content": "what now"},
            tool_defs=list(tools or []),
        ),
        session_revision=revision or _revision(session),
    )


def _request(history: list[dict]) -> list[dict]:
    """What a loop sends for this history: markers stripped, user message last."""
    clean = [{k: v for k, v in m.items() if k not in HISTORY_PRIVATE_KEYS} for m in history]
    return [{"role": "system", "content": "system prompt"}, *clean, {"role": "user", "content": "what now"}]


async def _turn(
    builder: CompactingHistoryBuilder,
    session: list[dict],
    *,
    session_key: str = "s1",
    model: str = "stub",
):
    """One turn as the loop runs it: assemble, then report the request it sent."""
    segment = await builder.build(_ctx(session, session_key=session_key))
    request = _request(segment.history)
    builder.note_request(session_key, request, model)
    return segment, request


def _stored(tmp_path: Path) -> list[CompactionCheckpoint]:
    return CheckpointStore(tmp_path / CHECKPOINT_DIR).chain("s1")


def _checkpoint_message(history: list[dict]) -> dict:
    found = [m for m in history if isinstance(m.get("content"), str) and "<compacted-" in m["content"]]
    assert len(found) == 1, f"expected exactly one checkpoint in the history, found {len(found)}"
    return found[0]


# ---------------------------------------------------------------------------
# The request: a verbatim prefix of the conversation
# ---------------------------------------------------------------------------


class TestPrefixReuse:
    """The property a cache read is evidence *of*.

    A provider counter can only be read on a wire that reports one, and it
    answers after the fact. The structural claim -- these bytes are the bytes
    already sent -- is what causes the counter to move, and it is decidable
    offline, on every run, without a provider that keeps books.
    """

    async def _two_turns(self, tmp_path: Path, provider: object, *, config=None):
        """One turn under the line, then one over it, on the same session."""
        builder = _builder(tmp_path, provider, config=config)
        _first, first_request = await _turn(builder, _session(UNDER))
        second, _ = await _turn(builder, _session(OVER))
        return first_request, second

    async def test_the_summarising_request_is_a_byte_exact_prefix_of_the_last_one(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        first_request, second = await self._two_turns(tmp_path, provider)

        assert second.meta["compaction_action"] == "compact"
        assert len(provider.sent) == 1, "one summarising call, and no other"
        request = provider.sent[0]

        assert request[-1]["content"] == COMPACTION_INSTRUCTION
        assert len(request) - 1 < len(first_request)
        assert request[:-1] == first_request[: len(request) - 1]
        assert second.meta["byte_prefix_reused"] is True

    async def test_reuse_holds_across_every_compaction_of_a_growing_session(self, tmp_path: Path) -> None:
        """The production shape, stepped: a session grows by an exchange at a
        time, so the request recorded last turn always reaches past the range
        this turn removes. Both compactions reuse their whole prefix."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        seen: set[str] = set()
        reused: list[bool] = []
        for exchanges in range(UNDER, 140, 2):
            segment, _ = await _turn(builder, _session(exchanges))
            checkpoint_id = segment.meta.get("checkpoint_id", "")
            if checkpoint_id and checkpoint_id not in seen:
                seen.add(checkpoint_id)
                reused.append(segment.meta["byte_prefix_reused"])

        assert len(reused) >= 2, "the session has to cross the trigger twice for this to say anything"
        assert all(reused)

    async def test_it_stays_a_prefix_when_the_last_request_carried_a_cache_breakpoint(self, tmp_path: Path) -> None:
        """A cache breakpoint is marked on the way out, so the sent bytes are
        not the builder's own. Reusing what was sent -- rather than
        re-deriving it -- is what keeps the two equal."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        first = await builder.build(_ctx(_session(UNDER)))
        request = _request(first.history)
        marked = dict(request[-2])
        marked["content"] = [{"type": "text", "text": marked["content"], "cache_control": {"type": "ephemeral"}}]
        request[-2] = marked
        builder.note_request("s1", request, "stub")

        second = await builder.build(_ctx(_session(OVER)))

        assert second.meta["compaction_action"] == "compact"
        sent = provider.sent[0]
        assert sent[:-1] == request[: len(sent) - 1]
        assert second.meta["byte_prefix_reused"] is True

    async def test_every_summary_replays_a_breakpoint_the_turn_before_put_inside_its_cut(self, tmp_path: Path) -> None:
        """The cut sits behind every rolling mark, so the only breakpoint a
        summary can carry past the system head is one an ordinary request
        placed there ahead of time -- and a breakpoint-style cache reads
        nothing without one. Stepped the way production grows, with the real
        cache optimizer marking what the loop sends: each summary has to carry
        that mark, deep enough that most of what it replays is cacheable."""
        provider = _RecordingProvider()
        builder = _builder(
            tmp_path, provider, config=ContextConfig(cache=ContextCacheConfig(compaction_breakpoint=True))
        )
        optimizer = CacheOptimizer(supports_caching=lambda _model: True)
        for exchanges in range(UNDER, 140, 2):
            segment = await builder.build(_ctx(_session(exchanges)))
            request, _tools, _model = await optimizer.before_llm_call(_request(segment.history), None, "stub")
            builder.note_request("s1", request, "stub")

        assert len(provider.sent) >= 2, "the session has to cross the trigger twice for this to say anything"
        for sent in provider.sent:
            replayed = [i for i, m in enumerate(sent[1:-1], 1) if "cache_control" in json.dumps(m)]
            assert len(replayed) == 1
            covered = estimate_prompt_tokens(sent[: replayed[0] + 1])
            assert covered / estimate_prompt_tokens(sent[:-1]) > 0.9

    async def test_a_request_whose_history_was_rewritten_is_not_kept(self, tmp_path: Path) -> None:
        """In-turn window management may elide a body before the request goes
        out. Reusing those bytes would summarise the elision, so the record is
        refused and the summary is built from the log."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        first = await builder.build(_ctx(_session(UNDER)))
        request = _request(first.history)
        request[3] = {**request[3], "content": "[earlier output elided]"}
        builder.note_request("s1", request, "stub")

        second = await builder.build(_ctx(_session(OVER)))

        assert second.meta["compaction_action"] == "compact"
        assert "[earlier output elided]" not in json.dumps(provider.sent[0])
        assert second.meta["byte_prefix_reused"] is False

    async def test_a_record_taken_under_another_model_is_not_reused(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        await _turn(builder, _session(UNDER), model="some/other-model")
        second = await builder.build(_ctx(_session(OVER)))

        assert second.meta["checkpoint_kind"] == "summary"
        assert second.meta["byte_prefix_reused"] is False

    async def test_it_carries_the_bodies_the_provider_cached_not_the_pruned_rewrites(self, tmp_path: Path) -> None:
        """The pruner fires on the compacting turn, so a re-derived request
        would differ from the cached one at the first shortened result -- which
        is the oldest large one, near the front, where a cache is worth most."""
        big = ("Traceback (most recent call last):\n  File auth.ts, line 214\n" * 200)[:12_000]
        base = _session(50)
        base[10:10] = _tool_exchange("big-1", "exec", big, '{"command": "pytest -x"}')
        # Three later exchanges, because ``keepRecentPairs`` holds the last
        # three whole and the big one has to be older than all of them.
        for recent in range(3):
            base += _tool_exchange(f"r{recent}", "read_file", f"line {recent}")
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)

        _first, first_request = await _turn(builder, base)
        grown = [*base, *_more(16, start=50)]
        second, _ = await _turn(builder, grown)

        assert second.meta["pruned_messages"] >= 1
        request = provider.sent[0]
        assert request[:-1] == first_request[: len(request) - 1]
        assert any(big in str(message.get("content")) for message in request)

    async def test_it_goes_out_on_the_turns_own_model_with_the_turns_own_tools(self, tmp_path: Path) -> None:
        """Prefix reuse and a second model are mutually exclusive, and reuse
        wins -- so the summarising call is made by whoever is already holding
        the conversation, tools and all."""
        provider = _RecordingProvider()
        tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        builder = _builder(tmp_path, provider, tools=tools)
        await builder.build(_ctx(_session(UNDER), tools=tools))
        await builder.build(_ctx(_session(OVER), tools=tools))

        assert provider.models == ["stub"]
        assert provider.tools == [tools]

    async def test_it_goes_out_with_the_tool_list_the_turn_sent_not_the_registrys(self, tmp_path: Path) -> None:
        """Tools head a breakpoint-style cache key, and tool search drops
        ``tool_search`` from a small catalogue on the way out: a summary sent
        with the registry's list is a prefix of nothing the turn cached."""
        provider = _RecordingProvider()
        sent_tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        registry = [*sent_tools, {"type": "function", "function": {"name": "tool_search", "parameters": {}}}]
        builder = _builder(tmp_path, provider, tools=registry)
        segment = await builder.build(_ctx(_session(UNDER), tools=registry))
        builder.note_request("s1", _request(segment.history), "stub", tools=sent_tools)
        await builder.build(_ctx(_session(OVER), tools=registry))

        assert provider.tools == [sent_tools]

    async def test_the_engine_hands_the_turns_tool_list_through_to_the_summary(self, tmp_path: Path) -> None:
        """The loop reports to the engine, not to the strategy; the list has to survive that hop."""
        provider = _RecordingProvider()
        sent_tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        registry = [*sent_tools, {"type": "function", "function": {"name": "tool_search", "parameters": {}}}]
        builder = _builder(tmp_path, provider, tools=registry)
        engine = ContextAssembler([builder], get_tool_definitions=lambda: registry)
        segment = await builder.build(_ctx(_session(UNDER), tools=registry))
        engine.note_request("s1", _request(segment.history), model="stub", tools=sent_tools)
        await builder.build(_ctx(_session(OVER), tools=registry))

        assert provider.tools == [sent_tools]

    async def test_a_record_that_names_no_model_lends_its_messages_and_its_tools_together(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        sent_tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        registry = [*sent_tools, {"type": "function", "function": {"name": "tool_search", "parameters": {}}}]
        builder = _builder(tmp_path, provider, tools=registry)
        segment = await builder.build(_ctx(_session(UNDER), tools=registry))
        builder.note_request("s1", _request(segment.history), "", tools=sent_tools)
        second = await builder.build(_ctx(_session(OVER), tools=registry))

        assert second.meta["byte_prefix_reused"] is True
        assert provider.tools == [sent_tools]

    async def test_a_record_without_a_tool_list_falls_back_to_the_registrys(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        registry = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        builder = _builder(tmp_path, provider, tools=registry)
        segment = await builder.build(_ctx(_session(UNDER), tools=registry))
        builder.note_request("s1", _request(segment.history), "stub")
        await builder.build(_ctx(_session(OVER), tools=registry))

        assert provider.tools == [registry]

    async def test_a_record_another_model_wrote_lends_neither_its_messages_nor_its_tools(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        sent_tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
        registry = [*sent_tools, {"type": "function", "function": {"name": "tool_search", "parameters": {}}}]
        builder = _builder(tmp_path, provider, config=_summary_config(model="some/other-model"), tools=registry)
        segment = await builder.build(_ctx(_session(UNDER), tools=registry))
        builder.note_request("s1", _request(segment.history), "stub", tools=sent_tools)
        second = await builder.build(_ctx(_session(OVER), tools=registry))

        assert second.meta["byte_prefix_reused"] is False
        assert provider.tools == [registry]

    async def test_a_second_model_turns_prefix_reuse_off_and_says_so(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(model="some/other-model"))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert provider.models == ["some/other-model"]
        assert segment.meta["byte_prefix_reused"] is False

    async def test_with_nothing_recorded_it_still_summarises_and_says_reuse_failed(self, tmp_path: Path) -> None:
        """A process that restarts mid-session has no record of what the
        provider holds. The summary still gets written -- from freshly derived
        messages -- and reports that it paid full price for the input."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "summary"
        assert segment.meta["byte_prefix_reused"] is False


class TestReusePrefix:
    """The substitution itself, away from a turn."""

    def test_a_matching_key_takes_the_recorded_bytes(self) -> None:
        record = RequestRecord(
            messages=({"role": "system", "content": "S"}, {"role": "user", "content": "original"}),
            keys=(("system", ""), ("session", 0)),
        )
        prefix = reuse_prefix(
            (("system", ""), ("session", 0)),
            ({"role": "system", "content": "S"}, {"role": "user", "content": "rewritten"}),
            record,
        )
        assert prefix.messages[1]["content"] == "original"
        assert prefix.whole is True

    def test_a_key_the_record_does_not_have_falls_back_to_the_fresh_one(self) -> None:
        record = RequestRecord(messages=({"role": "system", "content": "S"},), keys=(("system", ""),))
        prefix = reuse_prefix(
            (("system", ""), ("session", 0)),
            ({"role": "system", "content": "S"}, {"role": "user", "content": "fresh"}),
            record,
        )
        assert prefix.messages[1]["content"] == "fresh"
        assert (prefix.reused, prefix.whole) == (1, False)

    def test_a_record_whose_keys_do_not_line_up_is_not_used_at_all(self) -> None:
        """The assembler may merge two adjacent assistant messages on the way
        out. Reusing bytes under the wrong name is worse than reusing none."""
        record = RequestRecord(messages=({"role": "system", "content": "S"},), keys=())
        prefix = reuse_prefix((("system", ""),), ({"role": "system", "content": "T"},), record)
        assert prefix.messages[0]["content"] == "T"
        assert prefix.whole is False


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------


class TestTheTemplate:
    def test_the_instruction_names_all_eight_sections_and_forbids_tools(self) -> None:
        for name in SECTIONS:
            assert f"## {name}" in COMPACTION_INSTRUCTION
        assert "Do not call any tool" in COMPACTION_INSTRUCTION

    def test_every_heading_carries_the_line_that_says_what_belongs_under_it(self) -> None:
        """The one shape claim worth asserting about the request.

        What a model does with a heading is not decidable here, but what it is
        shown is: a heading followed by nothing is answered from whatever the
        surrounding text makes it mean, and each of these is followed by a
        sentence naming its content at the point where it gets answered.
        """
        for name, guide in SECTION_GUIDE.items():
            assert f"## {name}\n- [{guide}]" in COMPACTION_INSTRUCTION
            assert guide.strip()

    def test_no_heading_is_left_bare_for_the_instructions_own_rules_to_fill(self) -> None:
        """Observed against a local Qwen3-4B: a bare ``## Constraints`` under a
        numbered "Rules:" block came back holding those rules, verbatim -- even
        though a rule one line below already said whose constraints it meant.
        The heading now names its owner and says what belongs under it. The
        older spelling still parses, because a reply is read permissively even
        where the template is written strictly.
        """
        echoed = "\n".join(
            [
                "## Goal",
                "- Fix the 401 that follows a refresh",
                "## Constraints",
                '- Every section must appear. Write "(none)" for a section with no content.',
                "- Do not mention this request, and do not mention that the conversation was compacted.",
            ]
        )
        sections = parse_sections(echoed)

        assert sections is not None
        assert len(sections["User Constraints"]) == 2, "the old spelling still reaches the section"
        assert "## Constraints\n" not in COMPACTION_INSTRUCTION
        assert "## User Constraints\n- [" in COMPACTION_INSTRUCTION

    @pytest.mark.parametrize("marker", ["[None]", "(none)", "none", "N/A", "- [None]", "  * (none)"])
    def test_a_section_marked_empty_parses_as_empty_however_it_was_spelled(self, marker: str) -> None:
        """``[None]`` is what the template asks for and what the renderer
        writes; the read side stays permissive, since a model that answers a
        bulleted template with a bulleted "nothing here" is not wrong."""
        sections = parse_sections(f"## Goal\n- a goal\n## Open Threads\n{marker}")
        assert sections is not None
        assert sections["Open Threads"] == ()
        assert sections["Goal"] == (SummaryEntry(text="a goal"),)

    def test_a_section_handed_its_own_description_back_holds_nothing(self) -> None:
        """Saying what belongs under a heading is what stops the heading being
        answered from its surroundings, and it is one more line to copy instead
        of answer. An echo is content-free, so it reads as an empty section."""
        echoed = f"## Goal\n- a goal\n## Decisions\n- [{SECTION_GUIDE['Decisions']}]"
        sections = parse_sections(echoed)
        assert sections is not None
        assert sections["Decisions"] == ()
        assert sections["Goal"] == (SummaryEntry(text="a goal"),)

    def test_a_reply_parses_into_the_eight_sections(self) -> None:
        sections = parse_sections(SUMMARY_REPLY)
        assert sections is not None
        assert list(sections) == list(SECTIONS)
        assert sections["Goal"][0].text == "Fix the 401 that follows a refresh"
        assert sections["Open Threads"] == ()

    def test_the_users_own_words_survive_the_round_trip(self) -> None:
        sections = parse_sections(SUMMARY_REPLY)
        assert sections is not None
        assert (
            sections["User Constraints"][0].text == "The user said: \"don't touch middleware, that's another team's\""
        )

    def test_a_missing_section_reads_as_empty_rather_than_as_absent(self) -> None:
        sections = parse_sections("## Goal\n- only this one")
        assert sections is not None
        assert list(sections) == list(SECTIONS)
        assert all(sections[name] == () for name in SECTIONS if name != "Goal")

    def test_a_wrapped_line_stays_one_entry(self) -> None:
        """Providers hard-wrap prose, and starting a new entry at each newline
        would cut a path or an error string in half."""
        sections = parse_sections("## Findings\n- the first half\n  and the second half")
        assert sections is not None
        assert sections["Findings"] == (SummaryEntry(text="the first half and the second half"),)

    def test_a_section_the_template_never_named_is_dropped(self) -> None:
        sections = parse_sections("## Goal\n- keep\n## Wild Idea\n- drop")
        assert sections is not None
        assert list(sections) == list(SECTIONS)
        assert sections["Goal"] == (SummaryEntry(text="keep"),)

    def test_a_reply_that_is_not_a_checkpoint_parses_to_nothing(self) -> None:
        """``None`` is the answer a caller has to fall back on; an empty summary
        would look like a conversation with nothing in it."""
        assert parse_sections("Sure! Here is a summary of our chat so far.") is None

    def test_every_section_is_rendered_in_order_and_an_empty_one_says_so(self) -> None:
        checkpoint = _a_checkpoint()
        rendered = checkpoint.render()
        assert rendered.startswith('<compacted-summary generation="1">')
        assert rendered.endswith("</compacted-summary>")
        headings = [line for line in rendered.splitlines() if line.startswith("## ")]
        assert headings == [f"## {name}" for name in SECTIONS]
        assert "## Open Threads\n[None]" in rendered

    def test_refs_can_be_kept_out_of_the_window(self) -> None:
        checkpoint = _a_checkpoint()
        assert "[[ref " in checkpoint.render()
        assert "[[ref " not in checkpoint.render(inline_refs=False)

    def test_a_ref_the_model_already_wrote_is_not_repeated(self) -> None:
        ref = SourceRef("session", "s1@abcd", 3, 3)
        entry = SummaryEntry(text=f"a finding {ref.render()}", refs=(ref,))
        assert entry.render().count("[[ref ") == 1


def _a_checkpoint(*, generation: int = 1, supersedes: str | None = None) -> CompactionCheckpoint:
    ref = SourceRef("session", "s1@abcd", 0, 40)
    sections = {name: () for name in SECTIONS}
    sections["Goal"] = (SummaryEntry(text="fix the 401", refs=(ref,)),)
    return CompactionCheckpoint(
        checkpoint_id=f"cp-{generation:03d}-test",
        session_key="s1",
        created_at="2026-08-20T00:00:00+00:00",
        generation=generation,
        covers=ref,
        supersedes=supersedes,
        sections=sections,
        model="stub",
        byte_prefix_reused=True,
    )


# ---------------------------------------------------------------------------
# Merging, and the chain
# ---------------------------------------------------------------------------


class TestMerging:
    def test_a_new_entry_inherits_the_refs_of_the_old_one_it_overlaps(self) -> None:
        """The template asks the model to carry every ref it saw, and that ask
        is probabilistic -- rewriting a sentence is exactly when a tag goes
        missing. The union makes it structural."""
        old = _a_checkpoint()
        narrow = SourceRef("session", "s1@abcd", 12, 12)
        new = {name: () for name in SECTIONS}
        new["Goal"] = (SummaryEntry(text="still the 401", refs=(narrow,)),)

        merged = merge_refs(new, old)

        assert merged["Goal"][0].text == "still the 401"
        assert set(merged["Goal"][0].refs) == {narrow, old.covers}

    def test_an_entry_about_something_else_inherits_nothing(self) -> None:
        old = _a_checkpoint()
        elsewhere = SourceRef("session", "s1@ffff", 1, 2)
        new = {name: () for name in SECTIONS}
        new["Goal"] = (SummaryEntry(text="a different session", refs=(elsewhere,)),)
        assert merge_refs(new, old)["Goal"][0].refs == (elsewhere,)

    def test_the_first_generation_has_nothing_to_merge(self) -> None:
        new = {name: () for name in SECTIONS}
        new["Goal"] = (SummaryEntry(text="first"),)
        assert merge_refs(new, None)["Goal"] == (SummaryEntry(text="first"),)

    def test_a_line_with_no_ref_of_its_own_gets_the_covering_span(self) -> None:
        """Nothing in a verbatim-prefix request labels a message with its
        ordinal, so most lines come back uncited. The covering span is coarse
        and it is retrievable, which is the promise a rendered ref makes."""
        covers = SourceRef("session", "s1@abcd", 0, 40)
        sections = {name: () for name in SECTIONS}
        sections["Findings"] = (SummaryEntry(text="uncited"),)
        assert ground_refs(sections, covers)["Findings"][0].refs == (covers,)

    def test_a_line_that_kept_its_own_ref_is_left_alone(self) -> None:
        covers = SourceRef("session", "s1@abcd", 0, 40)
        own = SourceRef("session", "s1@abcd", 7, 7)
        sections = {name: () for name in SECTIONS}
        sections["Findings"] = (SummaryEntry(text="cited", refs=(own,)),)
        assert ground_refs(sections, covers)["Findings"][0].refs == (own,)

    def test_a_ref_into_another_log_is_taken_back_out(self) -> None:
        """Asked to preserve tags it has barely seen, a model writes one that
        looks right. Rendering it would promise bytes nobody can produce."""
        covers = SourceRef("session", "s1@abcd", 0, 40)
        invented = SourceRef("session", "s1@deadbeef", 12, 12)
        sections = {name: () for name in SECTIONS}
        sections["Findings"] = (SummaryEntry(text=f"a claim {invented.render()}", refs=(invented,)),)

        entry = ground_refs(sections, covers)["Findings"][0]

        assert entry.refs == (covers,)
        assert "deadbeef" not in entry.render()
        assert entry.text == "a claim"

    def test_text_that_only_looks_like_a_ref_is_cut_out_of_the_line(self) -> None:
        """``[[ref auth:retry:1]]`` is not the grammar, so nothing downstream
        reads it as a ref -- not the retrieval tool, and not the pass above,
        which only ever sees tokens that parsed. Left in, it reaches the next
        turn as prose indistinguishable from a pointer, and the turn spends a
        tool call finding out it names nothing.
        """
        covers = SourceRef("session", "s1@abcd", 0, 40)
        real = SourceRef("session", "s1@abcd", 7, 7)
        sections = {name: () for name in SECTIONS}
        sections["Findings"] = (
            SummaryEntry(text=f"retry is off by one {real.render()} [[ref auth:retry:1]]", refs=(real,)),
        )

        entry = ground_refs(sections, covers)["Findings"][0]

        assert "auth:retry:1" not in entry.render()
        assert entry.text == f"retry is off by one {real.render()}"
        assert entry.refs == (real,), "the real one in the same line survives"

    def test_a_line_that_was_nothing_but_an_invented_pointer_is_dropped(self) -> None:
        """A pointer to nowhere is worse than no line: attaching the covering
        span to it would turn a fabrication into a plausible-looking one."""
        covers = SourceRef("session", "s1@abcd", 0, 40)
        sections = {name: () for name in SECTIONS}
        sections["Pointers"] = (
            SummaryEntry(text="[[ref billing:idempotency:1]]"),
            SummaryEntry(text="the failing span", refs=(SourceRef("session", "s1@abcd", 3, 9),)),
        )

        entries = ground_refs(sections, covers)["Pointers"]

        assert [entry.text for entry in entries] == ["the failing span"]

    def test_a_doubled_bracket_that_is_not_a_ref_at_all_is_left_alone(self) -> None:
        """Only ref-shaped text goes; ``[[`` in a preserved command does not."""
        covers = SourceRef("session", "s1@abcd", 0, 40)
        command = "ran: if [[ -f auth.ts ]]; then pytest -x; fi"
        sections = {name: () for name in SECTIONS}
        sections["Artifacts"] = (SummaryEntry(text=command),)
        assert ground_refs(sections, covers)["Artifacts"][0].text == command

    def test_a_ref_into_another_store_is_left_where_it_is(self) -> None:
        """Another store owns what its scopes mean; this one has no standing to
        judge them, and the shared-memory workflow renders its own."""
        covers = SourceRef("session", "s1@abcd", 0, 40)
        elsewhere = SourceRef("shared_memory", "raven-code", 7, 9)
        sections = {name: () for name in SECTIONS}
        sections["Pointers"] = (SummaryEntry(text="deposited", refs=(elsewhere,)),)
        assert ground_refs(sections, covers)["Pointers"][0].refs == (elsewhere,)


class TestTheChain:
    async def _two_compactions(self, tmp_path: Path, provider: object):
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        first = await builder.build(_ctx(_session(OVER)))
        second = await builder.build(_ctx(_session(FAR_OVER)))
        return builder, first, second

    async def test_the_second_checkpoint_supersedes_the_first(self, tmp_path: Path) -> None:
        _, first, second = await self._two_compactions(tmp_path, _RecordingProvider())
        assert first.meta["checkpoint_id"]
        assert second.meta["checkpoint_supersedes"] == first.meta["checkpoint_id"]
        assert second.meta["compaction_generation"] == 2

    async def test_the_span_it_covers_only_ever_grows(self, tmp_path: Path) -> None:
        _, first, second = await self._two_compactions(tmp_path, _RecordingProvider())
        earlier = SourceRef.parse(first.meta["checkpoint_covers"])
        later = SourceRef.parse(second.meta["checkpoint_covers"])
        assert earlier is not None and later is not None
        assert later.lo <= earlier.lo and later.hi > earlier.hi

    async def test_only_one_checkpoint_is_ever_in_the_window(self, tmp_path: Path) -> None:
        """Generation two folds generation one in; leaving both would make the
        summary the thing that grows without bound."""
        _, _, second = await self._two_compactions(tmp_path, _RecordingProvider())
        assert 'generation="2"' in _checkpoint_message(second.history)["content"]

    async def test_the_earlier_checkpoint_is_in_the_request_that_replaces_it(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        await self._two_compactions(tmp_path, provider)
        assert len(provider.sent) == 2
        assert any('<compacted-summary generation="1">' in str(m.get("content")) for m in provider.sent[1])

    async def test_the_chain_is_walkable_from_disk(self, tmp_path: Path) -> None:
        """The in-process chain answers what is standing in the window now.
        This answers what stood there three summaries ago, from outside."""
        await self._two_compactions(tmp_path, _RecordingProvider())
        chain = _stored(tmp_path)

        assert [cp.generation for cp in chain] == [1, 2]
        assert chain[1].supersedes == chain[0].checkpoint_id
        assert chain[0].supersedes is None
        assert chain[1].sections["Goal"][0].text

    async def test_the_checkpoint_is_the_same_bytes_on_every_later_turn(self, tmp_path: Path) -> None:
        """It is spliced into every later turn's history, so re-rendering it
        each time -- recounting the tokens it folded, say -- would move the
        prefix a provider cached on the turn it was written."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        first = await builder.build(_ctx(_session(OVER)))
        second = await builder.build(_ctx(_session(OVER)))

        assert _checkpoint_message(second.history) == _checkpoint_message(first.history)
        assert len(provider.sent) == 1

    async def test_clearing_the_session_forgets_what_was_retired(self, tmp_path: Path) -> None:
        """``/new`` renumbers the log from zero, at which point a retired
        ordinal names a different message. The revision -- the same one refs
        fold into their scope -- is what says the numbering restarted."""
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        compacted = await builder.build(_ctx(_session(OVER)))
        assert compacted.meta["compaction_generation"] == 1

        fresh = [{"role": "user", "content": "starting over"}, {"role": "assistant", "content": "sure"}]
        segment = await builder.build(_ctx(fresh, revision=_revision(fresh, log_id="f" * 32)))

        assert segment.meta["compaction_generation"] == 0
        assert segment.history == fresh

    async def test_the_standing_checkpoint_survives_an_overflow_reclaim(self, tmp_path: Path) -> None:
        """The overflow path works on an assembled prompt, where no position is
        a session ordinal -- so it writes no checkpoint, and has to recognise
        the one already there by its bytes. Dropping it would take the only
        account of everything already compacted with it."""
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))
        standing = _checkpoint_message(segment.history)["content"]

        messages = [{"role": "system", "content": "system prompt"}, *segment.history, *_more(40, start=500)]
        headroom.learn_from_overflow("stub", window=WINDOW, prompt_tokens=estimate_prompt_tokens(messages), step=8_192)
        outcome = await builder.on_context_overflow("s1", messages, attempt=1, window=WINDOW)

        assert outcome is not None
        assert len(outcome.messages) < len(messages)
        assert any(message.get("content") == standing for message in outcome.messages)

    async def test_nothing_is_written_before_the_first_checkpoint(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(4)))
        assert not (tmp_path / "memory").exists()


# ---------------------------------------------------------------------------
# What the window gets, and whether it can be cashed
# ---------------------------------------------------------------------------


class TestWhatTheWindowGets:
    async def test_the_summary_stands_where_the_messages_stood(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        message = _checkpoint_message(segment.history)
        assert message["role"] == "user"
        assert segment.history.index(message) < len(segment.history) - 1
        assert HistoryTrimmer.structural_errors(segment.history) == []

    async def test_it_never_reaches_the_session_log(self, tmp_path: Path) -> None:
        """The strategy chooses a view of an append-only log and never edits it."""
        session = _session(OVER)
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(session))
        assert not any("<compacted-" in str(m.get("content")) for m in session)

    async def test_every_summary_line_can_be_cashed(self, tmp_path: Path) -> None:
        """The whole point of a ref in the window: what the line says is
        checkable against the messages it came from -- every ref names this
        revision of the log, at ordinals it still holds."""
        session = _session(OVER)
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(session))

        rendered = _checkpoint_message(segment.history)["content"]
        lines = [line for line in rendered.splitlines() if line.startswith("- ")]
        assert lines and all("[[ref " in line for line in lines)

        revision = _revision(session)
        for line in lines:
            for ref in SourceRef.find_all(line):
                assert belongs_to(ref, revision)
                assert 0 <= ref.lo <= ref.hi < len(session)

    async def test_the_refs_it_renders_name_this_session(self, tmp_path: Path) -> None:
        session = _session(OVER)
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(session))

        covers = SourceRef.parse(segment.meta["checkpoint_covers"])
        assert covers is not None
        assert (covers.store, covers.scope) == ("session", _revision(session).scope)


# ---------------------------------------------------------------------------
# The rungs below a summary
# ---------------------------------------------------------------------------


class TestTheFallbacks:
    @pytest.mark.parametrize(
        ("provider", "why"),
        [
            (_RecordingProvider(reply="Sure, here is a recap!"), "an unreadable reply"),
            (_RecordingProvider(tool_call=True), "a model that reached for a tool"),
        ],
    )
    async def test_a_summary_that_cannot_be_read_folds_instead(
        self, tmp_path: Path, provider: object, why: str
    ) -> None:
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold", why
        assert "[[folded " in _checkpoint_message(segment.history)["content"]

    async def test_a_reply_that_is_only_headings_folds_instead(self, tmp_path: Path) -> None:
        """The template copied back rather than answered. It parses -- the
        headings are all there -- and would store as a checkpoint asserting the
        removed span held nothing, with no line to hang a ref on and so not
        even the way back that the fold carries.
        """
        template = "\n".join(f"## {name}\n- [{guide}]" for name, guide in SECTION_GUIDE.items())
        builder = _builder(tmp_path, _RecordingProvider(reply=template))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"
        assert "[[folded " in _checkpoint_message(segment.history)["content"]

    async def test_a_reply_cut_off_at_the_ceiling_folds_instead(self, tmp_path: Path) -> None:
        """The failure that looks like a success. A reply stopped at
        ``summary.maxTokens`` parses -- the headings it reached are there, and
        the ones it never got to read as empty -- so nothing downstream can
        tell it apart from a summary of a conversation that had no next step.
        The fold says less and claims nothing.
        """
        cut = SUMMARY_REPLY.split("## Findings")[0]
        builder = _builder(tmp_path, _RecordingProvider(reply=cut, finish_reason="length"))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"
        assert "[[folded " in _checkpoint_message(segment.history)["content"]
        stored = _stored(tmp_path)
        assert [checkpoint.kind for checkpoint in stored] == ["fold"]

    async def test_a_reply_the_transport_called_an_error_never_becomes_a_checkpoint(self, tmp_path: Path) -> None:
        """A failed route can hand back a fragment with no finish reason.

        The provider layer defaults an absent finish reason to ``stop``, so the
        text arrives here looking like a completed reply. Given a whole
        eight-section reply, every other check passes and the span is filed as a
        checkpoint; the classification the transport already made is the only
        thing separating the two, which is why the reply here is complete.
        """
        builder = _builder(
            tmp_path,
            _RecordingProvider(reply=SUMMARY_REPLY, error_classification="upstream_error"),
        )
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"
        stored = _stored(tmp_path)
        assert [checkpoint.kind for checkpoint in stored] == ["fold"]

    async def test_a_reply_that_generated_no_tokens_never_becomes_a_checkpoint(self, tmp_path: Path) -> None:
        """Text with a zero completion count did not come from a completion.

        Judged only when usage is reported: a provider that reports none is
        saying "unknown", and unknown is not evidence of failure -- which is why
        every other double here, reporting nothing, still gets its summary.

        The reply is a whole one, so the token count is the only thing that can
        turn it away.
        """
        builder = _builder(
            tmp_path,
            _RecordingProvider(reply=SUMMARY_REPLY, usage={"prompt_tokens": 1200, "completion_tokens": 0}),
        )
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"

    async def test_byte_reuse_and_a_cache_miss_are_recorded_together(self, tmp_path: Path) -> None:
        """The two are separate facts, and the checkpoint has to be able to say so.

        Sending the recorded request's own bytes is what makes a cache read
        possible; whether one happened is the provider's answer, not ours. A
        checkpoint that can only report the first was read as evidence of the
        second, which is the reading this pairing exists to make impossible.
        """
        provider = _RecordingProvider(
            usage={"prompt_tokens": 2581, "completion_tokens": 199, "cache_read_input_tokens": 0},
        )
        builder = _builder(tmp_path, provider)
        await _turn(builder, _session(UNDER))
        second, _ = await _turn(builder, _session(OVER))

        assert second.meta["byte_prefix_reused"] is True
        assert second.meta["cache_status"] == "miss"
        assert second.meta["cache_read_tokens"] == 0
        assert (second.meta["summary_input_tokens"], second.meta["summary_output_tokens"]) == (2581, 199)

    async def test_a_provider_that_never_mentions_caching_reads_as_unobservable(self, tmp_path: Path) -> None:
        """Silence is not a miss.

        A miss is a measurement -- the provider looked and found nothing cached
        -- and reporting one where no count arrived would put a number on
        something never measured.
        """
        builder = _builder(
            tmp_path,
            _RecordingProvider(reply=SUMMARY_REPLY, usage={"prompt_tokens": 2581, "completion_tokens": 199}),
        )
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        stored = _stored(tmp_path)
        assert stored[0].cache_status == "unobservable"

    async def test_a_real_cache_read_is_the_only_thing_reported_as_a_hit(self, tmp_path: Path) -> None:
        builder = _builder(
            tmp_path,
            _RecordingProvider(
                reply=SUMMARY_REPLY,
                usage={"prompt_tokens": 4501, "completion_tokens": 137, "cache_read_input_tokens": 4501},
            ),
        )
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        stored = _stored(tmp_path)
        assert stored[0].cache_status == "hit"
        assert stored[0].cache_read_tokens == 4501

    async def test_a_reply_missing_sections_never_becomes_a_checkpoint(self, tmp_path: Path) -> None:
        """A reply that stopped early carries no other sign that it did.

        The instruction tells the model to write the emptiness marker rather
        than drop a section, so an absent heading is the one mark a half-written
        reply leaves once the transport reports no error, the finish reason says
        ``stop``, and the tokens it did spend look ordinary. Parsed, its missing
        sections would read as "nothing to report" -- an account of the removed
        span that is confidently wrong, where the fold is merely uninformative.
        """
        fragment = SUMMARY_REPLY.split("## Findings")[0]
        builder = _builder(tmp_path, _RecordingProvider(reply=fragment))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"
        stored = _stored(tmp_path)
        assert [checkpoint.kind for checkpoint in stored] == ["fold"]

    async def test_a_reply_answering_every_section_with_the_marker_is_still_a_checkpoint(self, tmp_path: Path) -> None:
        """Emptiness is an answer; absence is not.

        The guard above turns away a reply that never reached a heading, and
        this is the case it must not catch: a conversation with little to report
        is summarised as eight headings of ``[None]`` plus whatever it does
        have, which is a complete reply and has to survive.
        """
        sparse = "\n\n".join(
            f"## {name}\n- [None]" if name != "Goal" else f"## {name}\n- Ship the parser" for name in SECTIONS
        )
        builder = _builder(tmp_path, _RecordingProvider(reply=sparse))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "summary"

    async def test_a_call_that_does_not_come_back_folds_instead(self, tmp_path: Path) -> None:
        builder = _builder(
            tmp_path,
            _RecordingProvider(hang=True),
            config=_summary_config(timeout_seconds=0.05),
        )
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))
        assert segment.meta["checkpoint_kind"] == "fold"

    async def test_a_provider_that_cannot_be_asked_folds_instead(self, tmp_path: Path) -> None:
        class _NoChat:
            api_key = "test"

            def get_default_model(self) -> str:
                return "stub"

        builder = _builder(tmp_path, _NoChat())  # type: ignore[arg-type]
        segment = await builder.build(_ctx(_session(OVER)))
        assert segment.meta["checkpoint_kind"] == "fold"

    async def test_the_fold_says_how_much_went_and_where_it_is(self, tmp_path: Path) -> None:
        session = _session(OVER)
        builder = _builder(tmp_path, _RecordingProvider(), config=_summary_config(mode="off"))
        segment = await builder.build(_ctx(session))

        rendered = _checkpoint_message(segment.history)["content"]
        assert rendered.startswith('<compacted-fold generation="1">')
        assert "Use context_retrieve to read any of it." in rendered
        ref = SourceRef.parse(rendered)
        assert ref is not None and ref.scope == _revision(session).scope

    async def test_off_never_calls_a_model(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(mode="off"))
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))
        assert provider.sent == []

    async def test_off_with_no_fold_is_the_behaviour_from_before_summaries(self, tmp_path: Path) -> None:
        """The rollback lever, all the way down: messages leave the window and
        nothing at all stands in for them, which is what the strategy did before
        this rung existed.

        Including the part that is easy to miss. Retirement is what a checkpoint
        buys -- these are spoken for, so stop re-deciding about them -- so with
        no checkpoint there is none, and every turn selects from the whole log
        again exactly as it used to.
        """
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(mode="off", fold=False))
        first = await builder.build(_ctx(_session(OVER)))
        second = await builder.build(_ctx(_session(OVER)))

        assert provider.sent == []
        assert first.meta["compacted_away"] > 0
        assert not any("<compacted-" in str(m.get("content")) for m in first.history)
        assert HistoryTrimmer.structural_errors(first.history) == []
        assert second.meta["compaction_action"] == "compact"
        assert second.history[0]["content"] != first.history[0]["content"], "the head decays, as it always did"

    async def test_eager_summarises_a_removal_a_fold_would_have_covered(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(mode="eager", fold_max_messages=10_000))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))
        assert segment.meta["checkpoint_kind"] == "summary"

    async def test_lazy_folds_a_removal_small_enough_to_point_at(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(fold_max_messages=10_000))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert segment.meta["checkpoint_kind"] == "fold"
        assert provider.sent == []

    async def test_shortening_a_tool_result_still_avoids_the_whole_ladder(self, tmp_path: Path) -> None:
        """The rung order the design turns on: a turn the pruner brings back
        under the line keeps every message and never asks a model anything."""
        big = ("Traceback (most recent call last):\n  File auth.ts, line 214\n" * 600)[:40_000]
        session = [{"role": "user", "content": "why is auth failing?"}]
        session += _tool_exchange("big-1", "exec", big, '{"command": "pytest -x"}')
        session += _more(50, start=0)
        for recent in range(3):
            session += _tool_exchange(f"r{recent}", "read_file", f"line {recent}")

        provider = _RecordingProvider()
        segment = await _builder(tmp_path, provider).build(_ctx(session))

        assert segment.meta["compaction_action"] == "pass"
        assert segment.meta["pruned_messages"] >= 1
        assert provider.sent == []
        assert len(segment.history) == len(session)

    async def test_turning_the_pruner_off_does_not_turn_the_summary_off(self, tmp_path: Path) -> None:
        config = ContextConfig(
            history=ContextHistoryConfig(
                compacting=ContextCompactionConfig(
                    prune=ContextPruneConfig(enabled=False),
                    summary=ContextSummaryConfig(),
                )
            )
        )
        builder = _builder(tmp_path, _RecordingProvider(), config=config)
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))
        assert segment.meta["pruned_messages"] == 0
        assert segment.meta["checkpoint_kind"] in ("summary", "fold")


# ---------------------------------------------------------------------------
# The shorter retry, and the cap that can pre-empt it
# ---------------------------------------------------------------------------


def _instruction_of(request: list[dict]) -> str:
    return str(request[-1]["content"])


# Taken from the note rather than retyped, so it cannot drift out of agreement
# with the text the model is actually sent.
_NOTE_OPENING = PARTIAL_REPLAY_NOTE.split("{dropped}")[0].strip()


class TestTheShorterRetry:
    """Measured at 128K: a 216,610-token summarising request came back with all
    eight headings and nothing under any of them, and every compaction on that
    session folded. The request was inside the window and the provider raised
    nothing -- the model simply could not hold that much together. The rung
    above the fold is only reachable if something makes the request smaller.
    """

    async def test_a_reply_that_filled_nothing_gets_one_shorter_attempt(self, tmp_path: Path) -> None:
        provider = _RelentingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 2, "one attempt at full size, one shorter"
        assert segment.meta["checkpoint_kind"] == "summary"
        assert "<compacted-summary" in _checkpoint_message(segment.history)["content"]

    async def test_the_second_request_is_shorter_than_the_first(self, tmp_path: Path) -> None:
        provider = _RelentingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        first, second = provider.sent
        assert len(second) < len(first)
        assert estimate_prompt_tokens(second) < estimate_prompt_tokens(first)

    async def test_the_shorter_request_is_still_a_conversation(self, tmp_path: Path) -> None:
        """What is dropped comes off the front, so the system message stays and
        no tool result is left without the call that produced it."""
        provider = _RelentingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        second = provider.sent[1]
        assert second[0]["role"] == "system"
        assert second[0] == provider.sent[0][0]
        assert HistoryTrimmer.structural_errors(second) == []

    async def test_the_shorter_request_says_what_it_could_not_see(self, tmp_path: Path) -> None:
        """A model asked to summarise a conversation whose opening it never saw
        will still answer ## Goal, and answer it confidently. Naming the gap is
        what keeps the reply narrower instead of wrong.
        """
        provider = _RelentingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        first, second = provider.sent
        assert _instruction_of(first) == COMPACTION_INSTRUCTION
        assert _NOTE_OPENING in _instruction_of(second)
        assert "no claim that a list is complete" in _instruction_of(second)

    @pytest.mark.parametrize(
        ("provider", "expected_kind", "expected_reuse"),
        [
            (_RecordingProvider(), "summary", True),
            (_RelentingProvider(), "summary", False),
        ],
        ids=["answered at full size", "rescued by the shorter retry"],
    )
    async def test_the_reuse_flag_follows_the_attempt_that_answered(
        self, tmp_path: Path, provider: object, expected_kind: str, expected_reuse: bool
    ) -> None:
        """The retry drops the recorded bytes by construction, so the flag has
        to describe the request that produced the reply -- not the one that was
        available before the first attempt failed.

        Driven at the writer rather than through a turn: only a record that
        covers every key makes the untrimmed prefix whole in the first place,
        which is the condition under which the two answers differ at all.
        """
        messages = [{"role": "system", "content": "system prompt"}, *_session(20)]
        keys = [SYSTEM_KEY, *(session_message_key(index) for index in range(len(messages) - 1))]
        writer = CheckpointWriter(ContextSummaryConfig())

        checkpoint = await writer.compact(
            provider=provider,
            model="stub",
            session_key="s1",
            generation=1,
            covers=SourceRef(store="session", scope=f"{LOG_ID}@0", lo=0, hi=39),
            previous=None,
            keys=keys,
            fresh=messages,
            record=RequestRecord(messages=tuple(messages), keys=tuple(keys)),
            tool_defs=[],
            folded_messages=40,
            folded_tokens=4_000,
        )

        assert checkpoint.kind == expected_kind
        assert checkpoint.byte_prefix_reused is expected_reuse

    @pytest.mark.parametrize(
        ("provider", "why"),
        [
            (_RecordingProvider(tool_call=True), "a tool call is a choice about the tools, not the size"),
            (
                _RecordingProvider(reply=SUMMARY_REPLY.split("## Findings")[0], finish_reason="length"),
                "an output ceiling is not bought off with less input",
            ),
        ],
    )
    async def test_a_failure_a_shorter_request_cannot_fix_is_not_retried(
        self, tmp_path: Path, provider: object, why: str
    ) -> None:
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 1, why
        assert segment.meta["checkpoint_kind"] == "fold"

    async def test_a_call_that_never_came_back_gets_the_shorter_attempt_too(self, tmp_path: Path) -> None:
        provider = _SlowAtFullSizeProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(timeout_seconds=0.05))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 2
        assert segment.meta["checkpoint_kind"] == "summary"

    async def test_the_retry_can_be_turned_off(self, tmp_path: Path) -> None:
        provider = _RelentingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(retry_on_empty=False))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 1
        assert segment.meta["checkpoint_kind"] == "fold"

    async def test_a_reply_that_can_be_read_costs_one_call(self, tmp_path: Path) -> None:
        """The good path pays nothing for the fallback existing."""
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 1
        assert segment.meta["checkpoint_kind"] == "summary"
        assert _instruction_of(provider.sent[0]) == COMPACTION_INSTRUCTION


class TestTheInputCap:
    async def test_a_request_over_the_cap_goes_out_short(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(max_input_tokens=4_000))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(_session(OVER)))

        assert len(provider.sent) == 1, "the first attempt already fits, so there is nothing to retry"
        assert estimate_prompt_tokens(provider.sent[0][:-1]) <= 4_000
        assert "does not start at" in _instruction_of(provider.sent[0])
        assert segment.meta["checkpoint_kind"] == "summary"

    async def test_a_request_under_the_cap_is_untouched(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider, config=_summary_config(max_input_tokens=10_000_000))
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))

        assert _instruction_of(provider.sent[0]) == COMPACTION_INSTRUCTION

    async def test_the_cap_gives_up_prefix_reuse(self, tmp_path: Path) -> None:
        """Not a regression -- the trade the knob exists to make. A shortened
        request is not a prefix of the recorded one, and saying it was would
        put a number in the checkpoint that no cache will ever match.

        At the writer, for the same reason as the retry's twin: only a record
        covering every key makes the untrimmed prefix whole, so only there does
        the cap have a True to turn into a False.
        """
        messages = [{"role": "system", "content": "system prompt"}, *_session(20)]
        keys = [SYSTEM_KEY, *(session_message_key(index) for index in range(len(messages) - 1))]
        record = RequestRecord(messages=tuple(messages), keys=tuple(keys))
        covers = SourceRef(store="session", scope=f"{LOG_ID}@0", lo=0, hi=39)
        kwargs = dict(
            session_key="s1",
            generation=1,
            covers=covers,
            previous=None,
            keys=keys,
            fresh=messages,
            record=record,
            tool_defs=[],
            folded_messages=40,
            folded_tokens=4_000,
        )

        uncapped = await CheckpointWriter(ContextSummaryConfig()).compact(
            provider=_RecordingProvider(), model="stub", **kwargs
        )
        capped = await CheckpointWriter(ContextSummaryConfig(max_input_tokens=2_000)).compact(
            provider=_RecordingProvider(), model="stub", **kwargs
        )

        assert (uncapped.kind, capped.kind) == ("summary", "summary")
        assert uncapped.byte_prefix_reused is True
        assert capped.byte_prefix_reused is False


class TestShrinking:
    """``shrink_prefix`` on its own -- the rules a shortened request obeys."""

    @staticmethod
    def _prefix(messages: list[dict]) -> ReusedPrefix:
        return ReusedPrefix(messages=tuple(messages), reused=len(messages))

    def test_it_leaves_a_prefix_that_already_fits(self) -> None:
        prefix = self._prefix([{"role": "system", "content": "s"}, *_session(3)])
        shorter, dropped = shrink_prefix(prefix, budget_tokens=1_000_000)
        assert (shorter, dropped) == (prefix, 0)
        assert shorter.reused == prefix.reused, "an untouched prefix keeps its reuse"

    def test_it_never_drops_the_system_message(self) -> None:
        prefix = self._prefix([{"role": "system", "content": "s"}, *_session(20)])
        shorter, dropped = shrink_prefix(prefix, budget_tokens=prefix_tokens(prefix, []) // 4)
        assert dropped > 0
        assert shorter.messages[0] == prefix.messages[0]

    def test_it_never_splits_a_tool_exchange(self) -> None:
        messages = [{"role": "system", "content": "s"}]
        for index in range(12):
            messages += _tool_exchange(f"c{index}", "read_file", f"body {index} {FILLER}")
        prefix = self._prefix(messages)

        for divisor in (2, 3, 4, 6, 8):
            shorter, _ = shrink_prefix(prefix, budget_tokens=prefix_tokens(prefix, []) // divisor)
            assert HistoryTrimmer.structural_errors(list(shorter.messages)) == []
            assert shorter.messages[1]["role"] != "tool", "a result without its call"

    def test_it_reports_nothing_dropped_when_there_is_nothing_to_drop(self) -> None:
        """One message cannot be made shorter by removing messages, and the
        caller has to be able to tell that from a successful shrink -- it is
        what stops the retry reissuing the same request."""
        prefix = self._prefix([{"role": "system", "content": "s" * 40_000}])
        assert shrink_prefix(prefix, budget_tokens=10) == (prefix, 0)

    def test_it_keeps_the_newest_exchange_even_when_that_is_over_budget(self) -> None:
        """A budget no single message can meet is still better served by the
        newest one than by an empty replay: the caller reaches here having
        already failed at full size."""
        prefix = self._prefix([{"role": "system", "content": "s"}, *_session(6)])
        shorter, dropped = shrink_prefix(prefix, budget_tokens=1)
        assert dropped > 0
        assert len(shorter.messages) >= 2
        assert shorter.messages[-1] == prefix.messages[-1]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestTheStore:
    def test_a_checkpoint_survives_the_round_trip(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        original = _a_checkpoint(generation=2, supersedes="cp-001-test")
        path = store.write(original)
        assert path is not None
        assert CompactionCheckpoint.from_json(json.loads(path.read_text())) == original

    async def test_an_invented_pointer_never_reaches_the_stored_checkpoint(self, tmp_path: Path) -> None:
        """The whole path, since the stored file is what a later reader gets:
        a checkpoint outlives the turn that wrote it, so a pointer that
        resolves to nothing would keep costing a tool call long after the model
        that composed it is gone.
        """
        session = _session(OVER)
        real = SourceRef("session", _revision(session).scope, 3, 3)
        reply = SUMMARY_REPLY.replace(
            "## Pointers\n[None]",
            f"## Pointers\n- [[ref auth:retry:1]]\n- the failing span {real.render()}",
        )
        builder = _builder(tmp_path, _RecordingProvider(reply=reply))
        await builder.build(_ctx(_session(UNDER)))
        segment = await builder.build(_ctx(session))

        assert segment.meta["checkpoint_kind"] == "summary"
        assert "auth:retry:1" not in _checkpoint_message(segment.history)["content"]
        stored = _stored(tmp_path)
        assert "auth:retry:1" not in json.dumps(stored[-1].to_json())
        pointers = stored[-1].sections["Pointers"]
        assert [entry.text for entry in pointers] == [f"the failing span {real.render()}"]
        assert real in pointers[0].refs, "a real ref in the same reply survives"

    def test_an_unwritable_store_does_not_fail_the_turn(self, tmp_path: Path) -> None:
        """A summary that cannot be filed is still standing in the window; a
        read-only workspace is not a reason to end a conversation."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        assert CheckpointStore(blocked).write(_a_checkpoint()) is None

    def test_a_session_key_with_a_path_separator_stays_inside_the_store(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        checkpoint = _a_checkpoint()
        path = store.path_for(CompactionCheckpoint(**{**checkpoint.__dict__, "session_key": "../../etc/passwd"}))
        assert tmp_path in path.parents

    async def test_a_superseded_file_another_reader_holds_does_not_cost_the_turn_its_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows refuses to delete a file another process has open. The new
        checkpoint is written by then, so the stale one is left for the next
        write instead of failing the history segment and sending the turn out
        with nothing but its own message."""
        builder = _builder(tmp_path, _RecordingProvider())
        await builder.build(_ctx(_session(UNDER)))
        await builder.build(_ctx(_session(OVER)))
        [old] = CheckpointStore(tmp_path / CHECKPOINT_DIR)._checkpoint_files("s1")

        def held_open(self, missing_ok=False):
            raise PermissionError(32, "The process cannot access the file because it is being used", str(self))

        monkeypatch.setattr(Path, "unlink", held_open)
        other_log = "fedcba9876543210" * 2
        session = _session(OVER)
        await builder.build(_ctx(_session(UNDER), revision=_revision(session, log_id=other_log)))
        segment = await builder.build(_ctx(session, revision=_revision(session, log_id=other_log)))

        assert segment.meta["compaction_action"] == "compact"
        assert len(segment.history) > 2, "the history segment failed and the turn kept only its own message"
        assert old.exists()
        assert len(CheckpointStore(tmp_path / CHECKPOINT_DIR)._checkpoint_files("s1")) == 2


# ---------------------------------------------------------------------------
# Which failures earn the shorter retry
# ---------------------------------------------------------------------------


_CUT = SUMMARY_REPLY.split("## Findings")[0]

FAILURES = [
    (SummaryFailure.TIMEOUT, lambda: _RecordingProvider(hang=True)),
    (SummaryFailure.REFUSED, lambda: _RecordingProvider(finish_reason="error")),
    (SummaryFailure.TRANSPORT_ERROR, lambda: _RecordingProvider(error_classification="upstream_error")),
    (SummaryFailure.NO_TOKENS, lambda: _RecordingProvider(usage={"prompt_tokens": 10, "completion_tokens": 0})),
    (SummaryFailure.NO_SECTIONS, lambda: _RecordingProvider(reply="Sure, here is a recap!")),
    (SummaryFailure.MISSING_SECTIONS, lambda: _RecordingProvider(reply=_CUT)),
    (SummaryFailure.EMPTY_SECTIONS, lambda: _RecordingProvider(reply=HEADINGS_ONLY)),
    (SummaryFailure.CALL_FAILED, lambda: _RecordingProvider(raises=True)),
    (SummaryFailure.TOOL_CALL, lambda: _RecordingProvider(tool_call=True)),
    (SummaryFailure.CUT_OFF, lambda: _RecordingProvider(reply=_CUT, finish_reason="length")),
]


class TestFailureKinds:
    def test_seven_kinds_are_retried_and_three_are_not(self) -> None:
        """Size-shaped failures get a shorter attempt; a credential, a tool
        call and an output ceiling meet a shorter request the same way."""
        retryable = {kind for kind in SummaryFailure if kind.retryable}
        assert retryable == {
            SummaryFailure.TIMEOUT,
            SummaryFailure.REFUSED,
            SummaryFailure.TRANSPORT_ERROR,
            SummaryFailure.NO_TOKENS,
            SummaryFailure.NO_SECTIONS,
            SummaryFailure.MISSING_SECTIONS,
            SummaryFailure.EMPTY_SECTIONS,
        }
        assert set(SummaryFailure) - retryable == {
            SummaryFailure.CALL_FAILED,
            SummaryFailure.TOOL_CALL,
            SummaryFailure.CUT_OFF,
            SummaryFailure.NO_PROVIDER,
        }

    @pytest.mark.parametrize(("kind", "make"), FAILURES, ids=[kind.value for kind, _ in FAILURES])
    async def test_each_kind_is_classified_and_folds(self, tmp_path: Path, kind: SummaryFailure, make) -> None:
        provider = make()
        writer = CheckpointWriter(ContextSummaryConfig(timeout_seconds=0.05))
        messages = [{"role": "system", "content": "system prompt"}, *_session(20)]
        keys = [SYSTEM_KEY, *(session_message_key(index) for index in range(len(messages) - 1))]

        sections, _prefix, _cost, failure = await writer._ask(
            provider, "stub", ReusedPrefix(messages=tuple(messages), reused=len(messages)), []
        )
        checkpoint = await writer.compact(
            provider=make(),
            model="stub",
            session_key="s1",
            generation=1,
            covers=SourceRef(store="session", scope=f"{LOG_ID}@0", lo=0, hi=39),
            previous=None,
            keys=keys,
            fresh=messages,
            record=None,
            tool_defs=[],
            folded_messages=40,
            folded_tokens=4_000,
        )

        assert sections is None
        assert failure is kind
        assert len(provider.sent) == (2 if kind.retryable else 1)
        assert checkpoint.kind == "fold"

    async def test_no_provider_folds_without_asking(self) -> None:
        writer = CheckpointWriter(ContextSummaryConfig())
        prefix = ReusedPrefix(messages=({"role": "system", "content": "s"},), reused=1)
        assert (await writer._ask(None, "stub", prefix, []))[3] is SummaryFailure.NO_PROVIDER


# ---------------------------------------------------------------------------
# The eight sections in the window, and resuming from disk
# ---------------------------------------------------------------------------


class TestTheSummaryInTheWindow:
    async def test_the_window_holds_all_eight_sections_in_order(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, _RecordingProvider())
        await _turn(builder, _session(UNDER))
        segment, _ = await _turn(builder, _session(OVER))

        assert segment.meta["checkpoint_kind"] == "summary"
        rendered = _checkpoint_message(segment.history)["content"]
        assert [line for line in rendered.splitlines() if line.startswith("## ")] == [f"## {n}" for n in SECTIONS]
        assert "don't touch middleware" in rendered

    async def test_a_new_process_resumes_the_summary_without_asking_again(self, tmp_path: Path) -> None:
        provider = _RecordingProvider()
        builder = _builder(tmp_path, provider)
        await _turn(builder, _session(UNDER))
        before, _ = await _turn(builder, _session(OVER))
        assert len(provider.sent) == 1

        fresh_provider = _RecordingProvider()
        after = await _builder(tmp_path, fresh_provider).build(_ctx(_session(OVER)))

        assert fresh_provider.sent == []
        assert after.meta["compaction_action"] == "pass"
        assert after.meta["checkpoint_id"] == before.meta["checkpoint_id"]
        assert json.dumps(after.history) == json.dumps(before.history)

    async def test_a_resumed_chain_is_superseded_by_the_next_compaction(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path, _RecordingProvider())
        await _turn(builder, _session(UNDER))
        first, _ = await _turn(builder, _session(OVER))

        second = await _builder(tmp_path, _RecordingProvider()).build(_ctx(_session(FAR_OVER)))

        assert second.meta["compaction_generation"] == 2
        assert second.meta["checkpoint_supersedes"] == first.meta["checkpoint_id"]


class TestTheStoredState:
    def _state(self, tmp_path: Path) -> Path:
        return CheckpointStore(tmp_path / CHECKPOINT_DIR).folder("s1") / "state.json"

    async def _compacted(self, tmp_path: Path):
        session = _session(OVER)
        segment = await _builder(tmp_path, _RecordingProvider(), config=_summary_config(mode="off")).build(
            _ctx(session)
        )
        return session, segment

    async def test_a_state_from_another_revision_is_ignored(self, tmp_path: Path) -> None:
        session, _ = await self._compacted(tmp_path)
        store = CheckpointStore(tmp_path / CHECKPOINT_DIR)

        loaded = store.load("s1", _revision(session, log_id="e" * 32), message_count=len(session))

        assert (loaded.generation, loaded.pruned, loaded.checkpoint) == (0, frozenset(), None)

    async def test_a_missing_standing_checkpoint_falls_back_to_the_newest_valid_one(self, tmp_path: Path) -> None:
        session, segment = await self._compacted(tmp_path)
        state = json.loads(self._state(tmp_path).read_text())
        state["checkpoint_id"] = "cp-009-gone"
        self._state(tmp_path).write_text(json.dumps(state))

        loaded = CheckpointStore(tmp_path / CHECKPOINT_DIR).load("s1", _revision(session), message_count=len(session))

        assert loaded.checkpoint is not None
        assert loaded.checkpoint.checkpoint_id == segment.meta["checkpoint_id"]
        assert loaded.stale is True

    async def test_an_unreadable_state_reads_as_a_fresh_one(self, tmp_path: Path) -> None:
        session, _ = await self._compacted(tmp_path)
        self._state(tmp_path).write_text("{not json")

        loaded = CheckpointStore(tmp_path / CHECKPOINT_DIR).load("s1", _revision(session), message_count=len(session))

        assert loaded.checkpoint is None and loaded.generation == 0

    async def test_the_retired_ordinals_survive_the_round_trip(self, tmp_path: Path) -> None:
        session, segment = await self._compacted(tmp_path)
        loaded = CheckpointStore(tmp_path / CHECKPOINT_DIR).load("s1", _revision(session), message_count=len(session))

        assert loaded.retired
        assert len(loaded.retired) == segment.meta["history_total"] - segment.meta["history_kept"]
        assert max(loaded.retired) < len(session)
