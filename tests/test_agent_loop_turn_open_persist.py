"""The turn's question reaches disk when the turn opens, not when it ends.

A session used to be created lazily and written once, at the end of the turn:
until the answer landed there was no file, so ``list_sessions`` could not see
the conversation and a reader who navigated away had no row to click back to --
while the turn went on running on the server with nobody watching it.

These tests drive the real ``_process_message`` with a stub provider and assert
from *inside* the provider call, which is the only place that can say what disk
looked like while the turn was still running.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class WatchingProvider(LLMProvider):
    """Records what the session looked like on disk mid-turn, then answers."""

    def __init__(self, key: str, content: str = "stub response"):
        super().__init__(api_key="test")
        self.manager: Any = None
        self._key = key
        self._content = content
        self.seen: list[dict[str, Any]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        listed = self.manager.list_sessions()
        stored = self.manager.peek(self._key)
        self.seen.append(
            {
                "exists": self.manager.exists(self._key),
                "listed": [row["key"] for row in listed],
                "counts": {row["key"]: row["message_count"] for row in listed},
                "users": [m for m in (stored.messages if stored is not None else []) if m.get("role") == "user"],
            }
        )
        return LLMResponse(content=self._content, finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
        text=content,
    )


def _persisted(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "the session was never written"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


def _agent(workspace: Path, provider: WatchingProvider, *, backend: Any = None) -> AgentLoop:
    loop = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(backend=backend),
    )
    # The provider reads the session manager the loop built, so it can only be
    # handed over once the loop exists.
    provider.manager = loop.sessions
    return loop


@pytest.mark.asyncio
async def test_the_session_is_on_disk_and_listed_while_the_turn_is_still_running(workspace):
    """The whole point: a reader who leaves mid-turn has a row to come back to."""
    provider = WatchingProvider("tui:chat1")
    agent = _agent(workspace, provider)

    out = await agent._process_message(_make_msg("read the repo and summarise it"))
    assert out is not None

    assert len(provider.seen) == 1
    mid = provider.seen[0]
    assert mid["exists"] is True, "the turn was running with nothing on disk to find it by"
    assert mid["listed"] == ["tui:chat1"]
    assert mid["counts"]["tui:chat1"] == 1
    assert [m["content"] for m in mid["users"]] == ["read the repo and summarise it"]


@pytest.mark.asyncio
async def test_the_question_is_filed_once_and_the_answer_follows_it(workspace):
    """The end-of-turn write starts after the question, or it files it twice."""
    provider = WatchingProvider("tui:chat1", content="a summary")
    agent = _agent(workspace, provider)

    await agent._process_message(_make_msg("read the repo and summarise it"))

    msgs = _persisted(workspace)
    users = [m for m in msgs if m.get("role") == "user"]
    assert len(users) == 1, f"the question was filed twice: {users}"
    assert users[0]["content"] == "read the repo and summarise it"
    assert [m.get("role") for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "a summary"


@pytest.mark.asyncio
async def test_the_slice_handed_to_the_memory_backend_still_opens_on_the_question(workspace):
    """``prev_len`` is read before the opening write, or the backend is handed
    the answer with no question in front of it -- and a memory built from that
    remembers replies to nothing."""

    class _Recording:
        def __init__(self) -> None:
            self.slices: list[list[dict[str, Any]]] = []

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def feedback(self, signals):
            pass

        async def recall(self, query, *, user_id=None, agent_id=None, top_k):
            return []

        async def store(self, session_id, messages, *, metadata=None):
            self.slices.append([dict(m) for m in messages])

    backend = _Recording()
    provider = WatchingProvider("tui:chat1", content="a summary")
    agent = _agent(workspace, provider, backend=backend)

    await agent._process_message(_make_msg("read the repo and summarise it"))
    await agent.drain_backend_stores(timeout=5.0)

    assert backend.slices, "the backend was never handed this turn"
    roles = [m.get("role") for m in backend.slices[0]]
    assert roles[0] == "user", f"the turn's slice does not open on the question: {roles}"
    assert backend.slices[0][0]["content"] == "read the repo and summarise it"
