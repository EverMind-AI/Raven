"""The runtime-context block is filed with its user message and replayed as it was sent.

Covers the whole round trip through the default history strategy, the record
the assembler leaves on the envelope, the session write that files the lines
beside the user's own words, the history projection that puts them back, and
the token estimate that has to count them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from raven.agent.context import ContextBuilder
from raven.config.raven import ContextConfig
from raven.context_engine import ContextAssembler, TurnContext, headroom
from raven.context_engine.history.compacting import CompactingHistoryBuilder
from raven.context_engine.history_trimmer import HistoryTrimmer
from raven.context_engine.runtime_context import RUNTIME_CONTEXT_FIELD, RUNTIME_CONTEXT_KEY, with_runtime_context
from raven.context_engine.segments import IdentitySegmentBuilder, render
from raven.contracts.assembled import TokenBudget
from raven.providers.litellm_provider import LiteLLMProvider
from raven.session.manager import SessionManager
from raven.utils.tokens import estimate_message_tokens

WINDOW = 32_768
_TAG = ContextBuilder._RUNTIME_CONTEXT_TAG
_LINES = f"{_TAG}\nCurrent Time: 2026-11-12 09:00 (Thursday) (UTC)\nChannel: cli\nChat ID: x"


class _StubProvider:
    api_key = "test"

    def get_default_model(self) -> str:
        return "stub"

    def wire_model_id(self, model: str) -> str:
        return model


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 9, 0)

    def __call__(self) -> datetime:
        return self.now


def _budget() -> TokenBudget:
    reserved = headroom.reply_headroom("stub", window=WINDOW)
    return TokenBudget(
        context_length=WINDOW,
        reserved_output=reserved,
        reserved_tools=0,
        reserved_system=200,
        available_history=WINDOW - reserved - 200,
    )


def _loop_stub():
    from raven.agent.loop.main import AgentLoop

    loop = object.__new__(AgentLoop)
    loop._now_fn = lambda: datetime(2026, 11, 12, 9, 0)
    return loop


def _save(session: Any, messages: list[dict[str, Any]]) -> None:
    from raven.agent.loop.main import AgentLoop

    AgentLoop._save_turn(_loop_stub(), session, messages, 0)


def _default_engine(workspace: Path, clock: _Clock) -> ContextAssembler:
    history = CompactingHistoryBuilder(
        workspace,
        ContextConfig(),
        _StubProvider(),  # type: ignore[arg-type]
        "stub",
        WINDOW,
        lambda: [],
    )
    return ContextAssembler([IdentitySegmentBuilder(workspace), history], lambda: [], now_fn=clock)


async def _turn(engine: ContextAssembler, session: Any, text: str) -> list[dict[str, Any]]:
    turn = TurnContext(current_message=text, channel="cli", chat_id="x", session_revision=session.revision)
    assembled = await engine.assemble(session.key, list(session.messages), _budget(), turn=turn)
    return assembled.messages


def _user_contents(messages: list[dict[str, Any]]) -> list[Any]:
    return [m.get("content") for m in messages if m.get("role") == "user"]


class TestTheNextTurnReplaysWhatWasSent:
    async def test_the_first_turns_message_comes_back_byte_identical(self, tmp_path: Path) -> None:
        clock = _Clock()
        engine = _default_engine(tmp_path, clock)
        session = SessionManager(tmp_path / "sessions").get_or_create("cli:x")

        first = await _turn(engine, session, "first question")
        sent = first[-1]["content"]
        assert isinstance(sent, str) and sent.startswith(_TAG)
        _save(session, [first[-1], {"role": "assistant", "content": "first answer"}])

        clock.now = datetime(2026, 11, 12, 9, 7)
        second = await _turn(engine, session, "second question")

        replayed = _user_contents(second)[:-1]
        assert replayed == [sent]
        assert second[-1]["content"] != sent
        assert session.messages[0]["content"] == "first question"


class _Scent:
    async def build(self, message: str, session_messages: list[dict[str, Any]]) -> Any:
        return SimpleNamespace(text="Possibly relevant skills:\n- one: d", skill_ids=["one"])


def _plain_engine(workspace: Path, clock: _Clock, **kw: Any) -> ContextAssembler:
    return ContextAssembler([IdentitySegmentBuilder(workspace)], lambda: [], now_fn=clock, **kw)


async def _envelope(engine: ContextAssembler, text: str = "hello", **turn: Any) -> dict[str, Any]:
    assembled = await engine.assemble(
        "cli:x", [], _budget(), turn=TurnContext(current_message=text, channel="cli", chat_id="x", **turn)
    )
    return assembled.messages[-1]


class TestTheJoin:
    def test_a_string_gets_the_lines_and_a_blank_line_in_front(self) -> None:
        assert with_runtime_context(_LINES, "hi") == f"{_LINES}\n\nhi"

    def test_a_block_list_gets_one_leading_text_block(self) -> None:
        blocks = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:,"}}]

        assert with_runtime_context(_LINES, blocks) == [{"type": "text", "text": _LINES}, *blocks]


class TestTheEnvelopeRecordsItsLines:
    async def test_the_record_is_what_build_runtime_context_returned(self, tmp_path: Path) -> None:
        clock = _Clock()
        message = await _envelope(_plain_engine(tmp_path, clock))

        assert message[RUNTIME_CONTEXT_KEY] == render.build_runtime_context(clock, "cli", "x")
        assert message["content"] == with_runtime_context(message[RUNTIME_CONTEXT_KEY], "hello")

    async def test_the_scent_menu_is_sent_but_not_recorded(self, tmp_path: Path) -> None:
        clock = _Clock()
        message = await _envelope(_plain_engine(tmp_path, clock, scent=_Scent()))

        assert "Possibly relevant skills" in message["content"]
        assert message[RUNTIME_CONTEXT_KEY] == render.build_runtime_context(clock, "cli", "x")
        assert "Possibly relevant skills" not in message[RUNTIME_CONTEXT_KEY]

    async def test_a_block_list_envelope_records_the_same_lines(self, tmp_path: Path) -> None:
        from PIL import Image

        picture = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "red").save(picture)
        clock = _Clock()
        message = await _envelope(_plain_engine(tmp_path, clock), media=[str(picture)], can_see_images=True)

        assert isinstance(message["content"], list)
        assert message["content"][0] == {"type": "text", "text": message[RUNTIME_CONTEXT_KEY]}

    async def test_the_record_never_reaches_the_chat_payload(self, tmp_path: Path) -> None:
        message = await _envelope(_plain_engine(tmp_path, _Clock()))

        [sent] = LiteLLMProvider._sanitize_messages([message])

        assert RUNTIME_CONTEXT_KEY not in sent
        assert RUNTIME_CONTEXT_FIELD not in sent


class TestTheSessionFilesTheLines:
    def _save(self, tmp_path: Path, messages: list[dict[str, Any]], **kw: Any) -> tuple[list[dict], str]:
        from raven.agent.loop.main import AgentLoop

        manager = SessionManager(tmp_path)
        session = manager.get_or_create("cli:x")
        AgentLoop._save_turn(_loop_stub(), session, messages, 0, **kw)
        manager.save(session)
        return session.messages, manager.session_path("cli:x").read_text(encoding="utf-8")

    @staticmethod
    def _sent(content: Any) -> dict[str, Any]:
        return {"role": "user", "content": with_runtime_context(_LINES, content), RUNTIME_CONTEXT_KEY: _LINES}

    def test_a_string_keeps_its_words_and_files_the_lines_beside_them(self, tmp_path: Path) -> None:
        stored, on_disk = self._save(tmp_path, [self._sent("what did I say?")])

        assert stored[0]["content"] == "what did I say?"
        assert stored[0][RUNTIME_CONTEXT_FIELD] == _LINES
        assert RUNTIME_CONTEXT_KEY not in on_disk

    def test_a_block_list_keeps_its_blocks_and_files_the_lines(self, tmp_path: Path) -> None:
        stored, _ = self._save(tmp_path, [self._sent([{"type": "text", "text": "and this?"}])])

        assert stored[0]["content"] == [{"type": "text", "text": "and this?"}]
        assert stored[0][RUNTIME_CONTEXT_FIELD] == _LINES

    def test_the_inbound_original_path_files_the_lines_too(self, tmp_path: Path) -> None:
        stored, _ = self._save(tmp_path, [self._sent("hello")], inbound_original="hello")

        assert stored[0]["content"] == "hello"
        assert stored[0][RUNTIME_CONTEXT_FIELD] == _LINES

    def test_the_users_own_text_quoting_the_tag_is_filed_whole(self, tmp_path: Path) -> None:
        pasted = f"{_TAG}\nCurrent Time: yesterday\n\nwhy does my log say this?"

        stored, _ = self._save(tmp_path, [self._sent(pasted)])

        assert stored[0]["content"] == pasted
        assert stored[0][RUNTIME_CONTEXT_FIELD] == _LINES

    def test_lines_still_in_the_content_are_not_filed_a_second_time(self, tmp_path: Path) -> None:
        sent = self._sent("hi")
        sent["content"] = f"[Recovery -- the previous turn was interrupted]\n\n{sent['content']}"

        stored, _ = self._save(tmp_path, [sent])

        assert _LINES in stored[0]["content"]
        assert RUNTIME_CONTEXT_FIELD not in stored[0]

    def test_a_block_still_holding_the_lines_is_not_filed_a_second_time(self, tmp_path: Path) -> None:
        sent = self._sent([{"type": "text", "text": "q"}])
        sent["content"] = [{"type": "text", "text": f"[Recovery]\n\n{_LINES}"}, {"type": "text", "text": "q"}]

        stored, _ = self._save(tmp_path, [sent])

        assert RUNTIME_CONTEXT_FIELD not in stored[0]

    def test_only_a_user_message_files_the_lines(self, tmp_path: Path) -> None:
        stray = {"role": "assistant", "content": "answer", RUNTIME_CONTEXT_KEY: _LINES}

        stored, on_disk = self._save(tmp_path, [self._sent("q"), stray])

        assert RUNTIME_CONTEXT_FIELD not in stored[1]
        assert RUNTIME_CONTEXT_KEY not in on_disk


class TestTheHistoryPutsTheLinesBack:
    def test_a_filed_string_is_replayed_as_it_was_sent(self) -> None:
        stored = [{"role": "user", "content": "hi", RUNTIME_CONTEXT_FIELD: _LINES, "timestamp": "t"}]

        [replayed] = HistoryTrimmer.history_from_ids(stored, [0])

        assert replayed == {"role": "user", "content": with_runtime_context(_LINES, "hi")}

    def test_a_filed_block_list_is_replayed_as_it_was_sent(self) -> None:
        blocks = [{"type": "text", "text": "look"}]
        stored = [{"role": "user", "content": blocks, RUNTIME_CONTEXT_FIELD: _LINES}]

        [replayed] = HistoryTrimmer.history_from_ids(stored, [0])

        assert replayed["content"] == with_runtime_context(_LINES, blocks)

    def test_only_a_user_message_has_lines_joined_back(self) -> None:
        stored = [{"role": "assistant", "content": "answer", RUNTIME_CONTEXT_FIELD: _LINES}]

        assert HistoryTrimmer.history_from_ids(stored, [0]) == [{"role": "assistant", "content": "answer"}]

    def test_an_entry_filed_before_the_field_existed_is_replayed_unchanged(self) -> None:
        stored = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

        assert HistoryTrimmer.history_from_ids(stored, [0, 1]) == stored


class TestMemoryReadsTheWordsOnly:
    def test_the_annotators_transcript_leaves_the_lines_out(self) -> None:
        from raven.memory_engine.consolidate.consolidator import MemoryStore

        filed = {"role": "user", "content": "hi", RUNTIME_CONTEXT_FIELD: _LINES, "timestamp": "2026-11-12T09:00"}

        transcript = MemoryStore._format_messages([filed])

        assert "hi" in transcript
        assert "Current Time" not in transcript


class TestTheEstimateCountsTheLines:
    def test_a_filed_message_costs_what_its_replay_costs(self) -> None:
        bare = {"role": "user", "content": "hi"}
        filed = {**bare, RUNTIME_CONTEXT_FIELD: _LINES}
        replayed = {"role": "user", "content": with_runtime_context(_LINES, "hi")}

        assert estimate_message_tokens(filed) > estimate_message_tokens(bare)
        assert abs(estimate_message_tokens(filed) - estimate_message_tokens(replayed)) <= 2
