"""The turn's record reaches disk as the turn produces it.

A process killed at the task budget used to leave only the question behind: the
whole turn was written at the end. These pin the pieces that make an interrupted
turn readable -- the incremental append, and the repair of a call that never got
its result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.spine import ChatType, Origin, Source, TurnRequest


class _ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]

    def get_default_model(self) -> str:
        return "fake/model"


def _text(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _tool_call(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)])


def _req() -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )


def _loop(tmp_path: Path, provider, hooks=None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=6),
        host=HostWiring(hooks=hooks or []),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


def _filed(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            if data.get("_type") != "metadata":
                rows.append(data)
    return rows


@pytest.mark.asyncio
async def test_a_tool_round_is_on_disk_before_the_next_call(tmp_path):
    """The call and its result are filed while the turn is still running."""
    seen: list[tuple[int, int]] = []
    path_holder: dict[str, Path] = {}

    class Watch(AgentHook):
        async def before_iteration(self, ctx):
            path = path_holder.get("path")
            if path is not None and path.exists():
                rows = _filed(path)
                seen.append(
                    (
                        sum(1 for r in rows if r.get("role") == "assistant" and r.get("tool_calls")),
                        sum(1 for r in rows if r.get("role") == "tool"),
                    )
                )
            return HookDecision()

    loop = _loop(tmp_path, _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("done")]), [Watch()])
    path_holder["path"] = loop.sessions.session_path("cli:c")

    await loop._process_message(_req())

    rows = _filed(path_holder["path"])
    assert any(r.get("role") == "assistant" and r.get("tool_calls") for r in rows)
    assert any(r.get("role") == "tool" for r in rows)
    assert any(count >= 1 for count, _ in seen), "the second iteration saw the first round already filed"


def test_a_call_that_never_returned_is_repaired_on_read(tmp_path):
    """A transcript can end mid-call; a strict provider rejects it unrepaired."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}})
        + "\n"
        + json.dumps(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
            }
        )
        + "\n"
        + json.dumps({"role": "assistant", "content": "unfinished"}).replace("unfinished", "later")
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    assert [m.get("role") for m in session.messages] == ["assistant", "tool", "assistant"]
    repaired = [m for m in session.messages if m.get("role") == "tool"]
    assert repaired and repaired[0]["tool_call_id"] == "c1"
    assert repaired[0]["name"] == "exec"
    assert repaired[0]["content"].startswith("[interrupted]")


def test_a_repaired_call_keeps_its_place_across_a_restart(tmp_path):
    """The synthetic result stays at the call boundary when the turn resumes and saves."""
    from raven.session.manager import SessionManager, read_session_file

    manager = SessionManager(tmp_path)
    path = manager.session_path("cli:c")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "user", "content": "go"}),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    first = read_session_file(path, "cli:c")
    assert first is not None
    assert [m.get("role") for m in first.messages] == ["user", "assistant", "tool"]

    first.record({"role": "user", "content": "continue"})
    first.record({"role": "assistant", "content": "resumed"})
    manager.save(first)

    second = read_session_file(path, "cli:c")
    assert second is not None
    assert [m.get("role") for m in second.messages] == ["user", "assistant", "tool", "user", "assistant"]


def test_only_the_missing_result_of_a_multi_call_is_inserted(tmp_path):
    """A partial round keeps its answered call first and repairs the one still open."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                        ],
                    }
                ),
                json.dumps({"role": "tool", "tool_call_id": "c1", "name": "exec", "content": "ok"}),
                json.dumps({"role": "assistant", "content": "later"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    assert [m.get("role") for m in session.messages] == ["assistant", "tool", "tool", "assistant"]
    assert [m.get("tool_call_id") for m in session.messages if m.get("role") == "tool"] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_a_turns_closing_row_is_filed_and_never_twice(tmp_path):
    """The last answer is a row of its own, and the id keeps it from repeating."""
    loop = _loop(tmp_path, _ScriptedProvider([_tool_call("list_dir", {"path": "."}), _text("all done")]))
    path = loop.sessions.session_path("cli:c")

    await loop._process_message(_req())

    rows = _filed(path)
    answers = [r for r in rows if r.get("role") == "assistant" and r.get("content") == "all done"]
    assert len(answers) == 1, "the closing answer is filed exactly once"
    ids = [r["_row_id"] for r in rows if r.get("_row_id")]
    assert len(ids) == len(set(ids)), "no row is written under two identifiers"


def test_a_turn_meta_restates_a_row_by_its_identifier(tmp_path):
    """A stamp filed after the row was written is merged on the way in."""
    from raven.session.manager import read_session_file

    path = tmp_path / "s.jsonl"
    row = {"role": "assistant", "content": "done", "_row_id": "abc123"}
    path.write_text(
        "\n".join(
            [
                json.dumps({"_type": "metadata", "key": "cli:c", "created_at": "2026-01-01T00:00:00", "metadata": {}}),
                json.dumps({"role": "user", "content": "hi"}),
                json.dumps(row),
                json.dumps(
                    {"_type": "turn_meta", "patches": [{"row_id": "abc123", "keys": {"replied_via_tool": True}}]}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    session = read_session_file(path, "cli:c")

    assert session is not None
    filed = [m for m in session.messages if m.get("content") == "done"]
    assert filed and filed[0]["replied_via_tool"] is True
    assert "_row_id" not in filed[0], "the filing key is not handed to readers"


def test_an_incremental_write_stops_before_a_picture():
    """A picture row waits for the window, which is what names it in the file."""
    from raven.agent.loop.turn_path import _incremental_upto

    text = {"role": "assistant", "content": "looking"}
    picture = {"role": "tool", "content": [{"type": "image_url", "image_url": {"url": "data:,"}}]}
    after = {"role": "assistant", "content": "done"}

    assert _incremental_upto([text, picture, after], 0) == 1
    assert _incremental_upto([picture, after], 0) is None, "a leading picture leaves nothing to write"
    assert _incremental_upto([text, after], 0) == 2
    assert _incremental_upto([text, after], 2) is None, "nothing new is nothing to write"


def test_a_grown_list_is_adopted_and_a_shrunken_one_is_not():
    """The log follows the record grown at the end, never a window's view of it."""
    from raven.agent.window.turn_log import TurnLog

    opening = {"role": "user", "content": "hi"}
    live = [opening]
    log = TurnLog(live, 1)

    appended = [opening, {"role": "assistant", "content": "one"}]
    log.adopt_appended(appended)
    assert [m.get("content") for m in log.messages()] == ["hi", "one"]

    shorter = [opening]
    log.adopt_appended(shorter)
    assert [m.get("content") for m in log.messages()] == ["hi", "one"], "a shrunken view changes nothing"

    stranger = [{"role": "user", "content": "different"}, {"role": "assistant", "content": "x"}]
    log.adopt_appended(stranger)
    assert [m.get("content") for m in log.messages()] == ["hi", "one"], "a list that differs early is a view"
