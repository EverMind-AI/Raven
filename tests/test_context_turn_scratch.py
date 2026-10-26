"""Turn scratch: the query-conditioned segments ride in the user tail, never the log.

Covers the block helpers and the mark the assembler leaves on the envelope,
where the assembler puts ``stable = False`` segments with the switch on and off,
the normalised segment order, the session write that must never record the
block, the replay comparison that must not read it as a divergence, the
hook-facing question that must not include it, the cache-breakpoint budget with
the block in place, and the loop paths that copy or re-file the envelope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from raven.agent.context import ContextBuilder
from raven.context_engine import ContextAssembler, TurnContext
from raven.context_engine.scratch import (
    TURN_SCRATCH_KEY,
    TURN_SCRATCH_TAG,
    append_turn_scratch,
    cut_turn_scratch,
    mark_turn_scratch,
    render_turn_scratch,
    strip_turn_scratch,
    strip_turn_scratch_by_tag,
)
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
    SkillsSegmentBuilder,
)
from raven.contracts.assembled import TokenBudget
from raven.contracts.context import AssemblyContext, Segment
from raven.contracts.memory import Memory
from raven.memory_engine.skill_forge import RouterHit, SkillForgeRouter
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.providers.prompt_cache import STABLE_PREFIX_KEY

_SEP = "\n\n---\n\n"
_MEMORY_HEADING = re.compile(r"(?m)^# Memory$")
_SKILLS_HEADING = re.compile(r"(?m)^# Skills$")
_RUNTIME = f"{ContextBuilder._RUNTIME_CONTEXT_TAG}\nCurrent Time: now"
_SCRATCH = render_turn_scratch(["# Memory\n\nrecalled: espresso"])
_PASTED = f"the log says\n\n{TURN_SCRATCH_TAG}\n\nwhat does that mean?"


def _budget() -> TokenBudget:
    return TokenBudget(100_000, 4_000, 2_000, 1_000, 93_000)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [str(b.get("text", "")) for b in content or [] if isinstance(b, dict)]


def _whole_prompt(messages: list[dict[str, Any]]) -> str:
    return "\n".join(t for m in messages for t in _texts(m.get("content")))


def _envelope(content: Any, scratch: str = _SCRATCH) -> dict[str, Any]:
    return mark_turn_scratch({"role": "user", "content": content}, scratch)


class _EchoBackend:
    """Recall that moves with the query, and a record of what was stored."""

    def __init__(self) -> None:
        self.stored: list[list[dict]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def store(self, session_id, messages, *, metadata=None):
        self.stored.append(json.loads(json.dumps(messages, default=str)))

    async def feedback(self, signals):
        pass

    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        if user_id is None:
            return []
        return [Memory(text=f"RECALL<{query}>", score=1.0)]


class _EchoSource:
    name = "local"
    weight = 1.0

    async def search(self, query, history, k):
        return [RouterHit(qualified_id="local/e", name="e", content=f"skill for {query}", score=0.9)]


class _HistoryCurator:
    name = "curator"
    order = 6
    needs_prefix = True
    stable = False

    def __init__(self, text: str = "") -> None:
        self._text = text

    async def build(self, ctx: AssemblyContext) -> Segment:
        assert ctx.prefix is not None
        return Segment(text=self._text, history=list(ctx.session_messages))


@pytest.fixture
def builder(tmp_path: Path) -> ContextBuilder:
    (tmp_path / "TOOLS.md").write_text("bootstrap rules", encoding="utf-8")
    always = tmp_path / "skills" / "always-on"
    always.mkdir(parents=True)
    (always / "SKILL.md").write_text(
        '---\nname: always-on\ndescription: d\nmetadata: {"raven": {"always": true}}\n---\nalways body\n',
        encoding="utf-8",
    )
    return ContextBuilder(workspace=tmp_path)


def _builders(builder: ContextBuilder, history_text: str = "") -> list[Any]:
    return [
        IdentitySegmentBuilder(builder.workspace),
        BootstrapSegmentBuilder(builder.workspace),
        MemorySegmentBuilder(builder.memory, _EchoBackend()),
        ActiveSkillsSegmentBuilder(builder.skills),
        SkillsSegmentBuilder(SkillForgeRouter([_EchoSource()])),
        _HistoryCurator(history_text),
    ]


def _engine(builder: ContextBuilder, history_text: str = "", **kw) -> ContextAssembler:
    return ContextAssembler(_builders(builder, history_text), lambda: [], **kw)


# ---------------------------------------------------------------------------
# The block helpers and the mark
# ---------------------------------------------------------------------------


class TestBlockHelpers:
    def test_nothing_to_render_is_the_empty_string(self) -> None:
        assert render_turn_scratch([]) == ""
        assert render_turn_scratch(["", ""]) == ""

    def test_parts_join_under_one_tag(self) -> None:
        block = render_turn_scratch(["# Memory\n\na", "", "# Skills\n\nb"])
        assert block == f"{TURN_SCRATCH_TAG}\n\n# Memory\n\na{_SEP}# Skills\n\nb"

    def test_a_string_stays_a_string(self) -> None:
        assert append_turn_scratch("hi", "S") == "hi\n\nS"

    def test_a_block_list_gains_exactly_one_text_block(self) -> None:
        content = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]
        out = append_turn_scratch(content, "S")
        assert out[:-1] == content
        assert out[-1] == {"type": "text", "text": "S"}

    def test_the_mark_records_exactly_what_was_appended(self) -> None:
        message = _envelope("hi")
        assert message["content"] == f"hi\n\n{_SCRATCH}"
        assert message[TURN_SCRATCH_KEY] == _SCRATCH

    def test_nothing_appended_leaves_no_mark(self) -> None:
        message = mark_turn_scratch({"role": "user", "content": "hi"}, "")
        assert message == {"role": "user", "content": "hi"}

    def test_strip_undoes_the_mark_for_both_shapes(self) -> None:
        assert strip_turn_scratch(_envelope("hi")) == "hi"
        content = [{"type": "text", "text": "hi"}]
        assert strip_turn_scratch(_envelope(content)) == content

    def test_an_unmarked_message_is_returned_as_it_is(self) -> None:
        content = [{"type": "text", "text": _PASTED}]
        assert strip_turn_scratch({"role": "user", "content": content}) is content
        assert strip_turn_scratch({"role": "user", "content": _PASTED}) == _PASTED

    def test_a_pasted_tag_survives_the_cut(self) -> None:
        """Only the recorded text goes: a user quoting the tag keeps every word."""
        assert strip_turn_scratch(_envelope(_PASTED)) == _PASTED
        assert strip_turn_scratch(_envelope([{"type": "text", "text": _PASTED}])) == [{"type": "text", "text": _PASTED}]

    def test_a_note_appended_after_the_scratch_stays(self) -> None:
        """A hook note lands on the last message, which at turn start is the envelope."""
        message = _envelope("hi")
        message["content"] += "\n\n[hook note]"
        assert strip_turn_scratch(message) == "hi\n\n[hook note]"

    def test_a_cache_marked_string_is_cut_inside_its_one_block(self) -> None:
        content = [{"type": "text", "text": f"hi\n\n{_SCRATCH}", "cache_control": {"type": "ephemeral"}}]
        assert cut_turn_scratch(content, _SCRATCH) == [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        ]

    def test_scratch_alone_is_none(self) -> None:
        assert cut_turn_scratch(_SCRATCH, _SCRATCH) is None
        assert cut_turn_scratch([{"type": "text", "text": _SCRATCH}], _SCRATCH) is None

    def test_the_tag_fallback_is_only_for_recordings(self) -> None:
        assert strip_turn_scratch_by_tag(f"hi\n\n{_SCRATCH}") == "hi"
        assert strip_turn_scratch_by_tag([{"type": "text", "text": "hi"}, {"type": "text", "text": _SCRATCH}]) == [
            {"type": "text", "text": "hi"}
        ]
        assert strip_turn_scratch_by_tag("hi") == "hi"


# ---------------------------------------------------------------------------
# Where the assembler puts the segments
# ---------------------------------------------------------------------------


class TestPlacement:
    async def test_memory_is_in_the_user_tail_exactly_once(self, builder: ContextBuilder) -> None:
        ac = await _engine(builder).assemble("s", [], _budget(), turn=TurnContext(current_message="espresso"))

        system = ac.messages[0]["content"]
        user = ac.messages[-1]["content"]
        assert not _MEMORY_HEADING.search(system)
        assert not _SKILLS_HEADING.search(system)
        assert "espresso" not in system
        assert isinstance(user, str)
        tail = user[user.index(TURN_SCRATCH_TAG) :]
        assert len(_MEMORY_HEADING.findall(_whole_prompt(ac.messages))) == 1
        assert len(_MEMORY_HEADING.findall(tail)) == 1
        assert "RECALL<espresso>" in tail
        assert "skill for espresso" in tail
        assert tail.index("# Memory") < tail.index("# Skills")
        assert user.count(TURN_SCRATCH_TAG) == 1

    async def test_the_stable_segments_stay_in_system(self, builder: ContextBuilder) -> None:
        ac = await _engine(builder).assemble("s", [], _budget(), turn=TurnContext(current_message="espresso"))

        system = ac.messages[0]["content"]
        assert "bootstrap rules" in system
        assert "# Active Skills" in system
        assert "always body" in system

    async def test_a_block_list_message_gains_one_scratch_block(self, builder: ContextBuilder, tmp_path: Path) -> None:
        from PIL import Image

        picture = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "red").save(picture)
        turn = TurnContext(current_message="espresso", media=[str(picture)], can_see_images=True)
        plain = await _engine(builder, turn_scratch=False).assemble("s", [], _budget(), turn=turn)
        ac = await _engine(builder).assemble("s", [], _budget(), turn=turn)

        content = ac.messages[-1]["content"]
        assert isinstance(content, list)
        assert len(content) == len(plain.messages[-1]["content"]) + 1
        assert content[-1]["type"] == "text"
        assert content[-1]["text"].startswith(TURN_SCRATCH_TAG)
        assert sum(TURN_SCRATCH_TAG in t for t in _texts(content)) == 1
        assert len(_MEMORY_HEADING.findall(_whole_prompt(ac.messages))) == 1

    async def test_system_is_byte_identical_across_two_turns(self, builder: ContextBuilder) -> None:
        eng = _engine(builder)
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "sure"}]

        first = await eng.assemble("s", history, _budget(), turn=TurnContext(current_message="one"))
        second = await eng.assemble("s", history, _budget(), turn=TurnContext(current_message="two"))

        assert first.messages[0] == second.messages[0]
        assert first.messages[:-1] == second.messages[:-1]
        assert "RECALL<one>" in _whole_prompt(first.messages[-1:])
        assert "RECALL<two>" in _whole_prompt(second.messages[-1:])

    async def test_an_undeclared_builder_stays_in_system(self, builder: ContextBuilder) -> None:
        class _Plugin:
            name, order, needs_prefix = "plugin", 7, False

            async def build(self, ctx: AssemblyContext) -> Segment:
                return Segment(text="# Plugin\n\nplugin text")

        eng = ContextAssembler([*_builders(builder), _Plugin()], lambda: [])
        ac = await eng.assemble("s", [], _budget(), turn=TurnContext(current_message="hi"))

        assert "plugin text" in ac.messages[0]["content"]
        assert "plugin text" not in _whole_prompt(ac.messages[-1:])


class TestTurnScratchOff:
    async def test_the_system_prompt_is_the_segments_in_the_new_order(self, builder: ContextBuilder) -> None:
        """Off means the pre-scratch layout, but in the normalised order:
        identity, bootstrap, active skills, memory, skills."""
        ac = await _engine(builder, turn_scratch=False).assemble(
            "s", [], _budget(), turn=TurnContext(current_message="espresso")
        )

        ctx = AssemblyContext(
            session_key="s",
            current_message="espresso",
            media=None,
            channel=None,
            chat_id=None,
            session_messages=[],
            budget=_budget(),
        )
        expected = []
        for b in sorted(_builders(builder)[:5], key=lambda b: b.order):
            seg = await b.build(ctx)
            expected.append(seg.text if seg is not None else "")
        assert [b.name for b in sorted(_builders(builder)[:5], key=lambda b: b.order)] == [
            "identity",
            "bootstrap",
            "active_skills",
            "memory",
            "skills",
        ]
        # The recall fence carries a fresh nonce per render.
        nonce = re.compile(r"#[0-9a-f]{8}\b")
        assert nonce.sub("#N", ac.messages[0]["content"]) == nonce.sub("#N", _SEP.join(t for t in expected if t))
        assert TURN_SCRATCH_TAG not in _whole_prompt(ac.messages)
        assert ac.metadata["turn_scratch"] is False

    def test_the_factory_reads_the_switch(self, tmp_path: Path) -> None:
        from raven.config.raven import ContextCacheConfig, ContextConfig
        from raven.context_engine import build_context_engine

        class _Provider:
            def get_default_model(self) -> str:
                return "stub"

        def _build(turn_scratch: bool) -> ContextAssembler:
            return build_context_engine(
                workspace=tmp_path,
                builder=ContextBuilder(workspace=tmp_path),
                config=ContextConfig(cache=ContextCacheConfig(turn_scratch=turn_scratch)),
                provider=_Provider(),
                model="stub",
                context_window_tokens=100_000,
                get_tool_definitions=lambda: [],
            )

        assert _build(True)._turn_scratch is True
        assert _build(False)._turn_scratch is False


class TestStableHead:
    async def test_the_declared_head_is_the_three_stable_segments(self, builder: ContextBuilder) -> None:
        """With the scratch on and the history builder appending text, the head a
        provider may cache across turns is identity, bootstrap and active skills,
        separator included, and nothing that moves with the query."""
        ac = await _engine(builder, history_text="# Working State\n\nws").assemble(
            "s", [], _budget(), turn=TurnContext(current_message="espresso")
        )

        ctx = AssemblyContext(
            session_key="s",
            current_message="espresso",
            media=None,
            channel=None,
            chat_id=None,
            session_messages=[],
            budget=_budget(),
        )
        identity, bootstrap, _memory, active, _skills, _history = _builders(builder)
        head = _SEP.join([(await b.build(ctx)).text for b in (identity, bootstrap, active)])
        system = ac.messages[0]
        assert system[STABLE_PREFIX_KEY] == len(head) + len(_SEP)
        assert system["content"][: system[STABLE_PREFIX_KEY]] == head + _SEP
        assert system["content"][system[STABLE_PREFIX_KEY] :] == "# Working State\n\nws"


# ---------------------------------------------------------------------------
# The session write
# ---------------------------------------------------------------------------


def _loop_stub():
    from datetime import datetime

    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: datetime(2026, 10, 20, 12, 0)
    return loop


class TestSessionNeverRecordsTheScratch:
    def _save(self, tmp_path: Path, messages: list[dict], **kw) -> tuple[list[dict], str]:
        from raven.agent.loop.main import AgentLoop
        from raven.session.manager import SessionManager

        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")
        AgentLoop._save_turn(_loop_stub(), session, messages, 0, **kw)
        manager.save(session)
        return session.messages, manager.session_path("cli:x").read_text(encoding="utf-8")

    def test_both_shapes_reach_disk_without_the_block_or_the_mark(self, tmp_path: Path) -> None:
        plain = _envelope(f"{_RUNTIME}\n\nwhat did I say?")
        blocks = _envelope([{"type": "text", "text": _RUNTIME}, {"type": "text", "text": "and this?"}])

        stored, on_disk = self._save(tmp_path, [plain, blocks, {"role": "assistant", "content": "answer"}])

        assert TURN_SCRATCH_TAG not in on_disk
        assert TURN_SCRATCH_KEY not in on_disk
        assert "recalled: espresso" not in on_disk
        assert [m["content"] for m in stored] == ["what did I say?", [{"type": "text", "text": "and this?"}], "answer"]

    def test_the_inbound_original_path_records_the_words_sent(self, tmp_path: Path) -> None:
        stored, on_disk = self._save(
            tmp_path,
            [_envelope(f"{_RUNTIME}\n\nhello")],
            inbound_original="hello",
            received_at="2026-10-20T11:59:00",
        )

        assert TURN_SCRATCH_TAG not in on_disk
        assert TURN_SCRATCH_KEY not in on_disk
        assert stored[0]["content"] == "hello"

    def test_a_block_list_inbound_drops_only_the_scratch_block(self, tmp_path: Path) -> None:
        content = [
            {"type": "text", "text": _RUNTIME},
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

        stored, on_disk = self._save(tmp_path, [_envelope(content)], inbound_original="look")

        assert TURN_SCRATCH_TAG not in on_disk
        assert "recalled: espresso" not in on_disk
        texts = [b.get("text") for b in stored[0]["content"] if b.get("type") == "text"]
        assert "look" in texts

    def test_a_mid_turn_message_quoting_the_tag_is_filed_whole(self, tmp_path: Path) -> None:
        from raven.agent.loop._shared import _MID_TURN_USER_KEY

        stored, _ = self._save(
            tmp_path,
            [
                _envelope(f"{_RUNTIME}\n\nstart"),
                {"role": "assistant", "content": "working"},
                {"role": "user", "content": _PASTED, _MID_TURN_USER_KEY: True},
            ],
        )

        assert stored[0]["content"] == "start"
        assert stored[-1]["content"] == _PASTED

    def test_the_users_own_question_quoting_the_tag_is_filed_whole(self, tmp_path: Path) -> None:
        stored, _ = self._save(tmp_path, [_envelope(f"{_RUNTIME}\n\n{_PASTED}")])

        assert stored[0]["content"] == _PASTED


# ---------------------------------------------------------------------------
# Replay and the hook-facing question
# ---------------------------------------------------------------------------


class TestReplayIgnoresTheScratch:
    def _recorded(self, user_content: Any) -> dict[str, Any]:
        return {
            "model": "m",
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": user_content}],
            "tools": [],
        }

    def _live(self, message: dict) -> list[dict]:
        return [{"role": "system", "content": "sys"}, message]

    def test_a_string_message_matches_a_recording_without_the_scratch(self) -> None:
        from raven.trajectory.replay import compare_llm_request

        assert compare_llm_request(self._recorded("hi"), self._live(_envelope("hi")), [], "m") is None

    def test_a_block_list_message_matches_a_recording_without_the_scratch(self) -> None:
        from raven.trajectory.replay import compare_llm_request

        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert compare_llm_request(self._recorded(content), self._live(_envelope(content)), [], "m") is None

    def test_a_recording_that_carried_other_recall_still_matches(self) -> None:
        """A recorded request went through the trace without the mark; its scratch
        is cut by the tag, and the live one by the mark."""
        from raven.trajectory.replay import compare_llm_request

        recorded = append_turn_scratch("hi", render_turn_scratch(["# Memory\n\nwhat the recording recalled"]))
        assert compare_llm_request(self._recorded(recorded), self._live(_envelope("hi")), [], "m") is None

    def test_a_pasted_tag_inside_the_envelope_still_counts(self) -> None:
        """The recorded twin is cut at its last tag, which is its own scratch, so
        what the user wrote around a pasted one is still compared."""
        from raven.trajectory.replay import compare_llm_request

        recorded = append_turn_scratch(_PASTED, render_turn_scratch(["# Memory\n\nold recall"]))
        assert compare_llm_request(self._recorded(recorded), self._live(_envelope(_PASTED)), [], "m") is None
        edited = _envelope(_PASTED.replace("what does that mean?", "why?"))
        assert compare_llm_request(self._recorded(recorded), self._live(edited), [], "m") is not None

    def test_a_different_question_still_diverges(self) -> None:
        from raven.trajectory.replay import compare_llm_request

        mismatch = compare_llm_request(self._recorded("hi"), self._live(_envelope("bye")), [], "m")
        assert mismatch is not None and mismatch.field == "messages[1]"

    def test_a_pasted_tag_is_compared_like_any_other_text(self) -> None:
        from raven.trajectory.replay import compare_llm_request

        live = self._live({"role": "user", "content": f"hi\n\n{TURN_SCRATCH_TAG} one"})
        mismatch = compare_llm_request(self._recorded(f"hi\n\n{TURN_SCRATCH_TAG} two"), live, [], "m")
        assert mismatch is not None


def test_the_turn_question_leaves_the_scratch_out_and_keeps_a_pasted_tag() -> None:
    from raven.agent.loop._shared import turn_question

    assert turn_question([_envelope(f"{_RUNTIME}\n\nwhy?")]) == "why?"
    assert turn_question([_envelope([{"type": "text", "text": "why?"}])]) == "why?"
    assert turn_question([_envelope(f"{_RUNTIME}\n\n{_PASTED}")]) == _PASTED


# ---------------------------------------------------------------------------
# Cache breakpoints with the scratch in place
# ---------------------------------------------------------------------------


def _marks(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    def count(value: Any) -> int:
        if isinstance(value, dict):
            return ("cache_control" in value) + sum(count(v) for v in value.values())
        if isinstance(value, list):
            return sum(count(v) for v in value)
        return 0

    return count(messages) + count(tools or [])


_TOOLS = [{"type": "function", "function": {"name": "exec", "parameters": {}}}]


class TestCacheOptimizerBudget:
    async def test_at_most_four_breakpoints_on_the_first_and_second_iteration(self, builder: ContextBuilder) -> None:
        from raven.token_wise.cache_optimizer import CacheOptimizer

        optimizer = CacheOptimizer(supports_caching=lambda _model: True)
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "sure"}]
        ac = await _engine(builder).assemble("s", history, _budget(), turn=TurnContext(current_message="espresso"))

        first, first_tools, _ = await optimizer.before_llm_call(list(ac.messages), _TOOLS, "m")
        assert _marks(first, first_tools) <= 4
        assert "cache_control" in str(first[0])

        second_in = [
            *ac.messages,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "ok"},
        ]
        second, second_tools, _ = await optimizer.before_llm_call(second_in, _TOOLS, "m")
        assert _marks(second, second_tools) <= 4
        assert second[0] == first[0]

    async def test_across_turns_only_the_tools_and_system_marks_carry_over(self, builder: ContextBuilder) -> None:
        """Turn N+1 opens on turn N's filed history. The tools and system marks
        read the same; the envelope turn N sent carried its scratch and the filed
        one does not, so no later mark keys on a prefix turn N wrote."""
        from raven.agent.loop.main import AgentLoop
        from raven.session.manager import Session
        from raven.token_wise.cache_optimizer import CacheOptimizer

        optimizer = CacheOptimizer(supports_caching=lambda _model: True)
        eng = _engine(builder)
        history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "sure"}]
        one = await eng.assemble("s", history, _budget(), turn=TurnContext(current_message="one"))
        session = Session(key="cli:x")
        AgentLoop._save_turn(_loop_stub(), session, [one.messages[-1], {"role": "assistant", "content": "ok"}], 0)
        two = await eng.assemble("s", [*history, *session.messages], _budget(), turn=TurnContext(current_message="two"))

        sent_one, tools_one, _ = await optimizer.before_llm_call(list(one.messages), _TOOLS, "m")
        sent_two, tools_two, _ = await optimizer.before_llm_call(list(two.messages), _TOOLS, "m")

        assert tools_one == tools_two
        assert sent_one[0] == sent_two[0]
        assert _whole_prompt([sent_one[-1]]) != _whole_prompt([sent_two[-3]])
        assert TURN_SCRATCH_TAG not in _whole_prompt(sent_two[:-1])


# ---------------------------------------------------------------------------
# The loop paths that copy or re-file the envelope
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):
    """Answers from a script and keeps each request's messages."""

    def __init__(self, script: list[Any] | None = None) -> None:
        super().__init__(api_key="test")
        self.requests: list[list[dict]] = []
        self._script = list(script or [])

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **_kw):
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        step = self._script.pop(0) if self._script else LLMResponse(content="ok", finish_reason="stop")
        if isinstance(step, BaseException):
            raise step
        return step

    def get_default_model(self) -> str:
        return "stub"


def _tool_call() -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCallRequest(id="t1", name="faketool", arguments={})], finish_reason="tool_calls"
    )


def _agent(workspace: Path, *, turn_scratch: bool = True, script: list[Any] | None = None, hooks=(), backend=None):
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
    from raven.config.raven import CheckpointConfig, ContextCacheConfig, ContextConfig, RuntimeConfig
    from raven.contracts.tool import Tool

    class _FakeTool(Tool):
        @property
        def name(self) -> str:
            return "faketool"

        @property
        def description(self) -> str:
            return "fake"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs) -> str:
            return "tool-ran"

    agent = AgentLoop(
        provider=_ScriptedProvider(script),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=6),
        host=HostWiring(hooks=list(hooks)),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            backend=backend or _EchoBackend(),
            context_config=ContextConfig(cache=ContextCacheConfig(turn_scratch=turn_scratch)),
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
        ),
    )

    async def _noop(**_kw) -> None:
        return None

    agent._start_executor = _noop
    agent._connect_mcp = _noop
    agent.tools.register(_FakeTool())
    return agent


def _request(text: str):
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="mock", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


def _on_disk(agent) -> str:
    return agent.sessions.session_path("mock:c1").read_text(encoding="utf-8")


def _assert_clean(text: str) -> None:
    assert TURN_SCRATCH_TAG not in text
    assert TURN_SCRATCH_KEY not in text
    assert "RECALL<" not in text


async def _collect(_event) -> None:
    return None


class TestTheLoopNeverFilesTheScratch:
    async def test_two_turns_share_a_system_prompt_and_the_store_sees_no_scratch(self, tmp_path: Path) -> None:
        backend = _EchoBackend()
        agent = _agent(tmp_path, backend=backend)

        await agent._process_message(_request("first question about espresso"))
        await agent._process_message(_request("second question about tea"))

        first, second = agent.provider.requests[0], agent.provider.requests[-1]
        assert len(agent.provider.requests) == 2
        assert first[0]["role"] == "system"
        assert first[0] == second[0]
        for request, query in ((first, "espresso"), (second, "tea")):
            assert not _MEMORY_HEADING.search(_whole_prompt(request[:1]))
            assert len(_MEMORY_HEADING.findall(_whole_prompt(request))) == 1
            tail = _whole_prompt(request[-1:])
            assert len(_MEMORY_HEADING.findall(tail)) == 1
            assert "RECALL<" not in _whole_prompt(request[:-1])
            assert query in tail[tail.index(TURN_SCRATCH_TAG) :]
        assert TURN_SCRATCH_TAG not in _whole_prompt(second[:-1])

        await agent.drain_backend_stores(timeout=5.0)
        _assert_clean(_on_disk(agent))
        assert "first question about espresso" in _on_disk(agent)
        assert backend.stored, "the backend was handed the turns"
        _assert_clean(json.dumps(backend.stored, ensure_ascii=False))

    async def test_a_mid_turn_inject_is_filed_whole_beside_a_clean_envelope(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, script=[_tool_call(), LLMResponse(content="done", finish_reason="stop")])
        drains = [[], [_request(_PASTED)]]

        def _drain() -> list:
            return drains.pop(0) if drains else []

        await agent.run_turn(_request("start the job"), _collect, _drain, stream=False)

        second = agent.provider.requests[1]
        assert any(_PASTED in t for m in second if m.get("role") == "user" for t in _texts(m.get("content")))
        on_disk = _on_disk(agent)
        assert "RECALL<" not in on_disk
        assert TURN_SCRATCH_KEY not in on_disk
        users = [
            json.loads(line) for line in on_disk.splitlines() if '"role": "user"' in line or '"role":"user"' in line
        ]
        contents = [u.get("content") for u in users]
        assert "start the job" in contents
        assert any(isinstance(c, str) and c.endswith(_PASTED) for c in contents), contents

    async def test_a_broken_turn_files_its_work_without_the_scratch(self, tmp_path: Path) -> None:
        import asyncio

        agent = _agent(tmp_path, script=[_tool_call(), asyncio.CancelledError()])

        with pytest.raises(asyncio.CancelledError):
            await agent._process_message(_request("do the thing"))

        on_disk = _on_disk(agent)
        _assert_clean(on_disk)
        assert "do the thing" in on_disk
        assert "tool-ran" in on_disk

    async def test_a_rerun_opens_on_the_marked_envelope_and_files_it_clean(self, tmp_path: Path) -> None:
        from raven.agent.loop import TURN_BUDGETS_KEY
        from raven.contracts.loop_hooks import AgentHook, HookDecision

        class _Budgeted(AgentHook):
            @property
            def name(self) -> str:
                return "Budgeted"

            async def before_user_inbound(self, ctx) -> HookDecision:
                ctx.metadata[TURN_BUDGETS_KEY] = {"dead_end_retries": 1}
                return HookDecision()

        dead = [LLMResponse(content="", finish_reason="stop")] * 3
        agent = _agent(
            tmp_path, script=[*dead, LLMResponse(content="Alice won.", finish_reason="stop")], hooks=[_Budgeted()]
        )

        out = await agent._process_message(_request("who won?"))

        assert out[0] == "Alice won."
        opening, rerun = agent.provider.requests[0], agent.provider.requests[3]
        assert rerun[-1]["content"] == opening[-1]["content"]
        assert _whole_prompt(rerun[-1:]).count(TURN_SCRATCH_TAG) == 1
        _assert_clean(_on_disk(agent))

    async def test_turn_scratch_off_keeps_memory_in_system(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, turn_scratch=False)

        await agent._process_message(_request("question about espresso"))

        request = agent.provider.requests[0]
        assert _MEMORY_HEADING.search(request[0]["content"])
        assert TURN_SCRATCH_TAG not in _whole_prompt(request)
