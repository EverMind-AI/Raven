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
    repaired = [m for m in session.messages if m.get("role") == "tool"]
    assert repaired and repaired[0]["tool_call_id"] == "c1"
    assert repaired[0]["name"] == "exec"
    assert repaired[0]["content"].startswith("[interrupted]")


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
    filed = [m for m in session.messages if m.get("_row_id") == "abc123"]
    assert filed and filed[0]["replied_via_tool"] is True
