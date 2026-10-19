"""The Mirasim adapter's ACP methods, against a stand-in Mirasim host over a real WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from raven.acp.mirasim import server as server_mod
from raven.acp.mirasim.server import MirasimAgent
from raven.acp.mirasim.store import Binding, BindingStore
from raven.acp.protocol import INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, RESOURCE_NOT_FOUND
from tests.acp_schema import validate_def
from tests.mirasim_fake_host import FakeHost, FakeSession, echo

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Harness:
    """One adapter over a stand-in host, and what it has said so far."""

    def __init__(self, tmp_path: Path, host: FakeHost) -> None:
        self.tmp_path = tmp_path
        self.host = host
        self.store = BindingStore(tmp_path / "store.json")
        self.updates: list[dict[str, Any]] = []
        self.seen: set[Any] = set()
        self.ids = 0
        self.agents: list[MirasimAgent] = []
        self.use()

    def use(self, agent: str = "claude") -> MirasimAgent:
        """A fresh adapter process's worth of state, sharing the store and the host's conversations."""
        self.agent = MirasimAgent(
            agent=agent, store=self.store, emit=self.updates.append, locate=lambda: self.host.address
        )
        self.agents.append(self.agent)
        return self.agent

    async def close(self) -> None:
        for agent in self.agents:
            await agent.shutdown()
            await agent.close()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.ids += 1
        response = await self.agent.handle({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params})
        assert response is not None
        return response

    async def ok(self, method: str, **params: Any) -> Any:
        response = await self.call(method, **params)
        assert "result" in response, response
        return response["result"]

    async def refused(self, method: str, **params: Any) -> dict[str, Any]:
        response = await self.call(method, **params)
        assert "error" in response, response
        return response["error"]

    async def new(self, **params: Any) -> str:
        result = await self.ok("session/new", **{"cwd": str(self.tmp_path), "mcpServers": [], **params})
        return result["sessionId"]

    def session_updates(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            frame["params"]["update"]
            for frame in self.updates
            if frame.get("method") == "session/update"
            and (session_id is None or frame["params"]["sessionId"] == session_id)
        ]

    def said(self, session_id: str | None = None, kind: str = "agent_message_chunk") -> str:
        return "".join(
            update["content"].get("text", "")
            for update in self.session_updates(session_id)
            if update["sessionUpdate"] == kind
        )

    def kinds(self, session_id: str | None = None) -> list[str]:
        return [update["sessionUpdate"] for update in self.session_updates(session_id)]

    def prompts(self) -> list[dict[str, Any]]:
        return self.host.frames("prompt")

    async def asked(self, method: str, *, limit: float = 10) -> dict[str, Any]:
        """The next request of ``method`` the adapter put to its client, waited for."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            for frame in self.updates:
                if frame.get("method") == method and "id" in frame and frame["id"] not in self.seen:
                    self.seen.add(frame["id"])
                    return frame
            await asyncio.sleep(0.01)
        pytest.fail(f"the adapter never sent {method}")

    async def respond(self, request: dict[str, Any], **outcome: Any) -> None:
        """Answer a request the adapter sent, as its client: ``result=`` or ``error=``."""
        assert await self.agent.handle({"jsonrpc": "2.0", "id": request["id"], **outcome}) is None


def text(words: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": words}]


@pytest.fixture(autouse=True)
def _short_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host's agent list is taken as final after this much quiet; Mirasim's own pace is not the point here."""
    monkeypatch.setattr(server_mod, "CLIS_QUIET_S", 0.2)


@pytest.fixture
async def host() -> Any:
    fake = FakeHost()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def harness(tmp_path: Path, host: FakeHost) -> Any:
    h = Harness(tmp_path, host)
    yield h
    await h.close()


def script(*steps: Any) -> Any:
    return lambda prompt, session: list(steps)


# -- the handshake --------------------------------------------------------------


async def test_initialize_answers_at_once_and_reads_the_menus_behind_it(harness: Harness) -> None:
    result = await harness.ok("initialize", protocolVersion=1)
    validate_def("InitializeResponse", result)
    caps = result["agentCapabilities"]
    assert caps["loadSession"] is True
    assert caps["sessionCapabilities"] == {"list": {}, "resume": {}, "close": {}, "delete": {}}
    assert caps["promptCapabilities"] == {"image": True, "audio": False, "embeddedContext": True}
    assert caps["_meta"] == {"raven.steer": True}
    assert result["agentInfo"] == {"name": "mirasim-claude", "title": "claude (Mirasim)", "version": "0.0.537"}
    await harness.agent._prefetch
    # The pool's liveness probe is a repeat initialize with five seconds: it
    # answers from what is in hand rather than reading again.
    again = await harness.ok("initialize", protocolVersion=1)
    assert again["agentInfo"]["title"] == "Claude Code (Mirasim)"
    assert len(harness.host.frames("listClis")) == 1 and len(harness.host.frames("getCatalog")) == 1


async def test_an_agent_that_takes_no_steer_and_no_image_declares_neither(harness: Harness) -> None:
    harness.use("qwen")
    caps = (await harness.ok("initialize", protocolVersion=1))["agentCapabilities"]
    assert "_meta" not in caps and caps["promptCapabilities"]["image"] is False


async def test_with_no_host_running_initialize_still_answers_and_a_session_says_why_not(tmp_path: Path) -> None:
    agent = MirasimAgent(
        agent="claude", store=BindingStore(tmp_path / "s.json"), emit=lambda frame: None, locate=lambda: None
    )
    try:
        response = await agent.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
        )
        assert response["result"]["agentInfo"]["version"] == "unknown"
        refused = await agent.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}}
        )
        assert refused["error"]["code"] == INTERNAL_ERROR
        assert "No Mirasim host is running" in refused["error"]["message"]
    finally:
        await agent.shutdown()
        await agent.close()


async def test_a_new_session_offers_the_menus_and_the_agents_commands_and_starts_no_conversation(
    harness: Harness,
) -> None:
    result = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    validate_def("NewSessionResponse", result)
    assert result["configOptions"][0]["currentValue"] == "sonnet"
    assert result["modes"]["currentModeId"] == "high"
    assert harness.prompts() == []
    await harness.host.wait_for(lambda: "available_commands_update" in harness.kinds())
    commands = harness.session_updates()[-1]
    validate_def("SessionUpdate", commands)
    # The host's own verbs are not the agent's commands.
    assert [command["name"] for command in commands["availableCommands"]] == ["compact", "review"]


async def test_an_agent_mirasim_has_not_installed_is_refused_after_reading_again(harness: Harness) -> None:
    harness.use("zcode")
    error = await harness.refused("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == INTERNAL_ERROR and "not installed" in error["message"]
    assert len(harness.host.frames("listClis")) == 2
    harness.use("ghost")
    error = await harness.refused("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == INVALID_PARAMS and "no agent 'ghost'" in error["message"]


async def test_an_agent_the_host_lists_installed_only_in_a_later_frame_is_found(harness: Harness) -> None:
    harness.host.progressive = {"claude": 3}
    assert await harness.new()


async def test_a_host_that_never_lists_its_agents_refuses_the_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CLIS_TIMEOUT_S", 0.3)
    harness.host.silent.add("listClis")
    error = await harness.refused("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert "could not list its agents" in error["message"]


# -- turns ----------------------------------------------------------------------


async def test_a_turn_streams_and_the_next_continues_the_conversation(harness: Harness) -> None:
    sid = await harness.new()
    result = await harness.ok("session/prompt", sessionId=sid, prompt=text("remember PELICAN-7"))
    assert result == {"stopReason": "end_turn"}
    first = harness.prompts()[0]
    assert {key: first[key] for key in ("agent", "workdir", "model", "effort", "route", "prompt")} == {
        "agent": "claude",
        "workdir": str(harness.tmp_path),
        "model": "sonnet",
        "effort": "high",
        "route": None,
        "prompt": "remember PELICAN-7",
    }
    key = harness.host.session().key
    assert harness.said(sid) == f"turn 1 of {key} on sonnet effort high: remember PELICAN-7"
    assert harness.said(sid, "agent_thought_chunk") == "thinking it over"
    for update in harness.session_updates(sid):
        validate_def("SessionUpdate", update)
    # The turn ends with the context it left, measured against the model's window.
    assert harness.session_updates(sid)[-1] == {"sessionUpdate": "usage_update", "used": 1234, "size": 200000}
    assert harness.store.get(sid).mirasim_key == key

    await harness.ok("session/prompt", sessionId=sid, prompt=text("what was it?"))
    second = harness.prompts()[1]
    assert second["sessionKey"] == key and "agent" not in second and "workdir" not in second
    assert harness.said(sid).endswith(f"turn 2 of {key} on sonnet effort high: what was it?")


async def test_a_tool_call_streams_from_its_start_to_its_result_between_the_words_around_it(
    harness: Harness,
) -> None:
    call = {"id": "call_1", "name": "Bash", "summary": "Run shell: ls", "input": "ls", "status": "running"}
    harness.host.respond = script(
        ("text", "Let me look."),
        ("tool", call),
        ("tool", {"id": "call_1", "status": "done", "result": "a b"}),
        ("text", " Done."),
    )
    sid = await harness.new()
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("go")) == {"stopReason": "end_turn"}
    turn = [u for u in harness.session_updates(sid) if u["sessionUpdate"] != "available_commands_update"]
    for update in turn:
        validate_def("SessionUpdate", update)
    assert [u["sessionUpdate"] for u in turn] == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
        "usage_update",
    ]
    assert turn[1] == {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_1",
        "title": "Run shell: ls",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": "ls",
    }
    assert (turn[2]["status"], turn[2]["content"][0]["content"]["text"]) == ("completed", "a b")
    assert (turn[0]["content"]["text"], turn[3]["content"]["text"]) == ("Let me look.", " Done.")


@pytest.mark.parametrize("segments", [False, True])
async def test_coalesced_tool_completion_preserves_the_collectors_whole_reply(harness: Harness, segments: bool) -> None:
    from raven.acp_client.acp_agent import _TurnCollector

    call = {"id": "c1", "name": "Bash", "status": "running"}
    changed: dict[str, Any] = {"toolCalls": [{**call, "status": "done", "result": "ok"}]}
    if segments:
        changed["segments"] = [
            {"kind": "text", "from": 0, "to": 7},
            {"kind": "tools", "ids": ["c1"]},
            {"kind": "text", "from": 7, "to": 11},
        ]
    original = harness.host._step

    async def coalesced(session: FakeSession, step: Any) -> str | None:
        if step[0] == "coalesced":
            await harness.host._patch(session, {"appendText": "All ", "set": changed})
            return None
        return await original(session, step)

    harness.host._step = coalesced
    harness.host.respond = script(("text", "Check. "), ("tool", call), ("coalesced",), ("text", "checks passed."))
    sid = await harness.new()
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("check")) == {"stopReason": "end_turn"}
    collector = _TurnCollector(track_files=False)
    for update in harness.session_updates(sid):
        await collector("session/update", {"sessionId": sid, "update": update})
    assert collector.reply == "All checks passed."


async def test_the_usage_a_turn_ends_on_is_measured_against_whatever_names_the_window(harness: Harness) -> None:
    await harness.agent.catalog()
    usage = harness.agent._usage
    assert usage({"usage": {"contextUsedTokens": 10, "contextWindowTokens": 100}})["size"] == 100
    assert usage({"model": "opus", "usage": {"contextUsedTokens": 10}})["size"] == 1000000
    # Claude Code's snapshots carry the percentage and not the window.
    assert usage({"model": "unknown", "usage": {"contextUsedTokens": 55729, "contextPercent": 5.6}})["size"] == 995161
    assert usage({"usage": {"contextUsedTokens": 10}}) is None
    assert usage({"usage": {"contextPercent": 5}}) is None and usage({"usage": None}) is None


async def test_text_arrives_before_the_turn_ends(harness: Harness) -> None:
    gate = asyncio.Event()

    async def wait() -> None:
        await gate.wait()

    harness.host.respond = script(("text", "first half "), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "first half ")
    assert not turn.done()
    await harness.ok("session/cancel", sessionId=sid)
    assert (await turn) == {"stopReason": "cancelled"}


async def test_a_model_without_rungs_is_sent_no_effort_and_a_rung_it_lacks_is_dropped(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="glm-5.3-flash")
    await harness.ok("session/set_mode", sessionId=sid, modeId="max")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    sent = harness.prompts()[0]
    assert sent["model"] == "glm-5.3-flash" and "effort" not in sent


async def test_each_route_choice_puts_the_conversation_on_its_route(harness: Harness) -> None:
    sid = await harness.new()
    for value, route, model in (
        ("platform:sonnet", "cloud", "sonnet"),
        ("own:opus", "local", "opus"),
        ("profile:abc", None, "profile:abc"),
    ):
        await harness.ok("session/set_config_option", sessionId=sid, configId="model", value=value)
        await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
        sent = harness.prompts()[-1]
        assert (sent["route"], sent["model"]) == (route, model)
        assert harness.host.session().route == route


async def test_an_agent_the_platform_does_not_run_is_never_given_a_route(harness: Harness) -> None:
    harness.use("qwen")
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    assert "route" not in harness.prompts()[0]


async def test_an_unoffered_model_or_effort_is_refused_with_what_is_offered(harness: Harness) -> None:
    sid = await harness.new()
    error = await harness.refused("session/set_config_option", sessionId=sid, configId="model", value="gpt-9")
    assert error["code"] == INVALID_PARAMS and "'own:sonnet'" in error["message"]
    error = await harness.refused("session/set_mode", sessionId=sid, modeId="ultra")
    assert error["code"] == INVALID_PARAMS and "'low'" in error["message"]
    error = await harness.refused("session/set_config_option", sessionId=sid, configId="theme", value="dark")
    assert "only 'model'" in error["message"]
    error = await harness.refused("session/set_config_option", sessionId=sid, configId="model", value="")
    assert error["code"] == INVALID_PARAMS


async def test_a_model_mirasim_added_since_the_menus_were_read_is_accepted(harness: Harness) -> None:
    sid = await harness.new()
    harness.host.catalogs["claude"]["models"].append({"id": "sonnet-next", "label": "Next"})
    harness.host.catalogs["claude"]["effort"].append({"id": "ultra", "label": "Ultra"})
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="sonnet-next")
    await harness.ok("session/set_mode", sessionId=sid, modeId="ultra")
    assert any(frame.get("refresh") for frame in harness.host.frames("getCatalog"))


async def test_a_failed_turn_with_nothing_said_is_an_error(harness: Harness) -> None:
    harness.host.respond = script(("error", "API Error: 422 the model is served only by the platform"))
    sid = await harness.new()
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert error["code"] == INTERNAL_ERROR
    assert error["message"] == "Mirasim claude turn failed: API Error: 422 the model is served only by the platform"


async def test_a_failed_turn_that_said_something_ends_and_says_how(harness: Harness) -> None:
    harness.host.respond = script(("text", "partial answer"), ("error", "rate limited"))
    sid = await harness.new()
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("hi")) == {"stopReason": "end_turn"}
    assert harness.said(sid) == "partial answer\n\n[Mirasim] The turn ended with an error: rate limited"


async def test_a_turn_mirasim_reports_incomplete_says_so(harness: Harness) -> None:
    harness.host.respond = script(("text", "half"), ("set", {"incomplete": True}))
    original = harness.host._patch

    async def keep_incomplete(session: FakeSession, patch: dict[str, Any]) -> None:
        if (patch.get("set") or {}).get("phase") == "done":
            patch["set"]["incomplete"] = True
        await original(session, patch)

    harness.host._patch = keep_incomplete  # type: ignore[method-assign]
    sid = await harness.new()
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("hi")) == {"stopReason": "end_turn"}
    assert harness.said(sid).endswith("[Mirasim] Mirasim reported the turn incomplete.")


async def test_a_turn_the_host_refuses_is_an_error_that_says_why(harness: Harness) -> None:
    harness.host.refuse["prompt"] = "claude is signed out; sign in first"
    sid = await harness.new()
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert error["message"] == "Mirasim did not run the claude turn: claude is signed out; sign in first"


async def test_a_host_that_never_takes_the_prompt_is_an_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "ACCEPT_TIMEOUT_S", 0.3)
    sid = await harness.new()
    harness.host.silent.add("prompt")
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert "did not take the claude turn" in error["message"]


@pytest.mark.parametrize("after_timeout", ["open", "close", "delete"])
async def test_a_timed_out_first_prompt_is_stopped_and_its_conversation_settled(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, after_timeout: str
) -> None:
    monkeypatch.setattr(server_mod, "ACCEPT_TIMEOUT_S", 0.05)
    harness.host.accepting = asyncio.Event()
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    await harness.refused("session/prompt", sessionId=sid, prompt=text("late"))
    if after_timeout != "open":
        await harness.ok(f"session/{after_timeout}", sessionId=sid)
    harness.host.accepting.set()
    await harness.host.wait_for(lambda: harness.host.frames("stop"), limit=1)
    assert not harness.agent._turns
    if after_timeout == "delete":
        await harness.host.wait_for(lambda: not harness.host.sessions, limit=1)
        assert harness.store.get(sid) is None
    else:
        await harness.host.wait_for(lambda: harness.host.session().running is None, limit=1)
        binding = await harness.host.wait_for(lambda: harness.store.get(sid), limit=1)
        assert binding.mirasim_key == harness.host.session().key


async def test_a_prompt_accepted_while_timeout_unwinds_is_stopped_and_recorded(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "ACCEPT_TIMEOUT_S", 0.05)
    harness.host.accepting = asyncio.Event()
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    request = harness.agent.host.request

    async def accept_before_unwind(frame: dict[str, Any], reply: Any, *, timeout: float) -> dict[str, Any]:
        try:
            return await request(frame, reply, timeout=timeout)
        except TimeoutError:
            harness.host.accepting.set()
            await harness.host.wait_for(lambda: harness.agent.sessions[sid].turn.task_id, limit=1)
            raise

    monkeypatch.setattr(harness.agent.host, "request", accept_before_unwind)
    await harness.refused("session/prompt", sessionId=sid, prompt=text("late"))
    await harness.host.wait_for(lambda: harness.host.session().running is None, limit=1)
    assert harness.host.frames("stop")[0]["taskId"] == "task-1"
    assert harness.store.get(sid).mirasim_key == harness.host.session().key
    assert not harness.agent._turns


async def test_a_timed_out_continuation_is_recalled_without_stopping_the_apps_turn(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("hold",))
    session.running = asyncio.ensure_future(harness.host._run(session, {"prompt": "the app's"}, "task-app"))
    await harness.host.wait_for(lambda: session.snapshot.get("taskId") == "task-app")
    monkeypatch.setattr(server_mod, "ACCEPT_TIMEOUT_S", 0.05)
    harness.host.accepting = asyncio.Event()
    await harness.refused("session/prompt", sessionId=sid, prompt=text("late"))
    late = harness.prompts()[-1]
    # The recall sent on giving up overtakes the held prompt and finds nothing to take back,
    # as on the real host: only the one sent when the late `accepted` arrives can.
    await harness.host.wait_for(lambda: [f for f in harness.host.handled if f.get("type") == "recall"])
    harness.host.accepting.set()
    await harness.host.wait_for(lambda: late in session.prompts)
    await harness.host.wait_for(lambda: not session.queue, limit=1)
    assert [f["ref"] for f in harness.host.frames("recall")] == [late["clientRef"], late["clientRef"]]
    assert session.running is not None and not session.running.done()
    assert not harness.host.frames("stop")


async def test_a_deleted_conversation_starts_again_once_and_says_so(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    old = harness.host.session().key
    del harness.host.sessions[old]
    harness.host.deleted.add(old)
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("two")) == {"stopReason": "end_turn"}
    new = harness.store.get(sid).mirasim_key
    assert new != old and new in harness.host.sessions
    assert harness.said(sid).endswith(
        "[Mirasim] The Mirasim conversation this session continued was deleted, so this turn ran in a new one without "
        "the earlier context."
    )


async def test_a_conversation_the_host_does_not_know_starts_again_once_and_says_so(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    old = harness.host.session().key
    del harness.host.sessions[old]
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("two")) == {"stopReason": "end_turn"}
    new = harness.store.get(sid).mirasim_key
    assert new != old and new in harness.host.sessions
    assert harness.said(sid).endswith(
        "[Mirasim] The Mirasim conversation this session continued is not one this Mirasim host can continue, so "
        "this turn ran in a new one without the earlier context."
    )
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("three")) == {"stopReason": "end_turn"}
    assert harness.store.get(sid).mirasim_key == new


async def test_mcp_servers_handed_over_are_noted_once(harness: Harness) -> None:
    sid = await harness.new(mcpServers=[{"name": "docs", "command": "x", "args": [], "env": []}, "junk"])
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    await harness.ok("session/prompt", sessionId=sid, prompt=text("two"))
    note = "MCP servers docs were not handed to the agent: Mirasim takes no servers per session."
    assert harness.said(sid).count(note) == 1


async def test_a_prompt_that_would_run_the_hosts_own_command_is_refused(harness: Harness) -> None:
    sid = await harness.new()
    for words in ("/branch", "  /branch  try another way"):
        error = await harness.refused("session/prompt", sessionId=sid, prompt=text(words))
        assert error["code"] == INVALID_PARAMS and "would run Mirasim's own command" in error["message"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("/branches are fine"))
    assert len(harness.prompts()) == 1


async def test_malformed_requests_are_invalid_params(harness: Harness) -> None:
    for params in ({"cwd": ""}, {}):
        assert (await harness.refused("session/new", **params))["code"] == INVALID_PARAMS
    sid = await harness.new()
    for prompt in ([], None, [{"type": "text", "text": "  "}], [{"type": "audio", "data": "x"}]):
        assert (await harness.refused("session/prompt", sessionId=sid, prompt=prompt))["code"] == INVALID_PARAMS
    assert (await harness.refused("session/prompt", sessionId="nope", prompt=text("x")))["code"] == RESOURCE_NOT_FOUND
    assert (await harness.refused("session/fork", sessionId=sid))["code"] == METHOD_NOT_FOUND


# -- what a prompt carries ------------------------------------------------------


async def test_a_local_link_goes_as_a_path_and_a_remote_one_stays_in_the_text(harness: Harness) -> None:
    doc = harness.tmp_path / "notes.md"
    doc.write_text("hello")
    sid = await harness.new()
    prompt = [
        {"type": "text", "text": "read these"},
        {"type": "resource_link", "uri": doc.as_uri(), "name": "notes.md"},
        {"type": "resource_link", "uri": "https://example.com/x", "name": "site"},
        {"type": "resource_link", "uri": (harness.tmp_path / "gone.md").as_uri(), "name": "gone"},
    ]
    await harness.ok("session/prompt", sessionId=sid, prompt=prompt)
    sent = harness.prompts()[0]
    assert sent["paths"] == [{"path": str(doc)}]
    assert "[site](https://example.com/x)" in sent["prompt"] and "[gone](" in sent["prompt"]
    assert "attachments" not in sent


async def test_an_image_reaches_an_agent_that_sees_images_inline(harness: Harness) -> None:
    picture = harness.tmp_path / "red.png"
    picture.write_bytes(PNG)
    sid = await harness.new()
    await harness.ok(
        "session/prompt",
        sessionId=sid,
        prompt=[
            {"type": "resource_link", "uri": picture.as_uri(), "name": "red.png", "mimeType": "image/png"},
            {"type": "image", "data": "R0lGODlh", "mimeType": "image/gif"},
        ],
    )
    sent = harness.prompts()[0]
    assert sent["attachments"] == [
        f"data:image/png;base64,{base64.b64encode(PNG).decode()}",
        "data:image/gif;base64,R0lGODlh",
    ]
    assert sent["prompt"] == "See the attached files." and "paths" not in sent


async def test_inline_image_content_is_refused_where_images_are_not_declared_or_not_an_image(
    harness: Harness,
) -> None:
    sid = await harness.new()
    for block in (
        {"type": "image", "data": "R0lGODlh", "mimeType": "image/tiff"},
        {"type": "image", "mimeType": "image/png"},
    ):
        error = await harness.refused("session/prompt", sessionId=sid, prompt=[block])
        assert error["code"] == INVALID_PARAMS and "image/png" in error["message"]
    harness.use("qwen")
    sid = await harness.new()
    error = await harness.refused(
        "session/prompt", sessionId=sid, prompt=[{"type": "image", "data": "R0lGODlh", "mimeType": "image/gif"}]
    )
    assert error["code"] == INVALID_PARAMS and "resource_link" in error["message"]
    assert harness.prompts() == []


async def test_an_image_too_large_or_for_an_agent_not_seen_seeing_images_goes_as_its_path(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    picture = harness.tmp_path / "shot.webp"
    picture.write_bytes(PNG)
    monkeypatch.setattr(server_mod, "IMAGE_LIMIT_BYTES", 10)
    sid = await harness.new()
    await harness.ok(
        "session/prompt", sessionId=sid, prompt=[{"type": "resource_link", "uri": picture.as_uri(), "name": "s"}]
    )
    assert harness.prompts()[0]["paths"] == [{"path": str(picture)}]
    monkeypatch.setattr(server_mod, "IMAGE_LIMIT_BYTES", 10 * 1024 * 1024)
    harness.use("qwen")
    qwen = await harness.new()
    await harness.ok(
        "session/prompt", sessionId=qwen, prompt=[{"type": "resource_link", "uri": picture.as_uri(), "name": "s"}]
    )
    assert harness.prompts()[1]["paths"] == [{"path": str(picture)}]


async def test_embedded_text_is_inlined(harness: Harness) -> None:
    sid = await harness.new()
    resource = {"type": "resource", "resource": {"uri": "file:///spec.md", "text": "THE SPEC"}}
    await harness.ok("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "follow"}, resource])
    assert harness.prompts()[0]["prompt"] == "follow\n[file:///spec.md]\nTHE SPEC"


# -- questions ------------------------------------------------------------------


def question(prompt_id: str = "claude-control-1") -> dict[str, Any]:
    return {
        "promptId": prompt_id,
        "kind": "user-input",
        "title": "Claude asks",
        "questions": [
            {"id": "q0", "text": "Which colour?", "type": "select", "choices": [{"label": "Red"}, {"label": "Blue"}]}
        ],
        "currentIndex": 0,
    }


def approval() -> dict[str, Any]:
    return {
        "promptId": "acp-perm-1",
        "kind": "permission",
        "title": "Permission required",
        "questions": [
            {
                "id": "choice",
                "text": "Allow the agent to use Bash?",
                "type": "select",
                "choices": [
                    {"label": "Allow", "value": "opt-allow", "intent": "allow_once"},
                    {"label": "Reject", "value": "opt-reject", "intent": "reject_once"},
                ],
            }
        ],
    }


async def forms(harness: Harness) -> None:
    await harness.ok("initialize", protocolVersion=1, clientCapabilities={"elicitation": {"form": {}}})


async def test_a_question_reaches_the_client_as_it_is_asked_and_the_answer_goes_back(harness: Harness) -> None:
    await forms(harness)
    harness.host.respond = script(("ask", question()), ("text", "You chose."))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("pick")))
    request = await harness.asked("elicitation/create")
    assert request["params"]["requestedSchema"]["properties"]["answer"]["enum"] == ["Red", "Blue"]
    await harness.respond(request, result={"action": "accept", "content": {"answer": "Red"}})
    assert await turn == {"stopReason": "end_turn"}
    assert harness.host.frames("interact") == [
        {"type": "interact", "promptId": "claude-control-1", "action": "select", "value": "Red"}
    ]


async def test_a_tool_approval_goes_out_as_a_permission_request_by_its_intents(harness: Harness) -> None:
    harness.host.respond = script(("ask", approval()), ("text", "ran it"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    request = await harness.asked("session/request_permission")
    assert [(option["name"], option["kind"]) for option in request["params"]["options"]] == [
        ("Allow", "allow_once"),
        ("Reject", "reject_once"),
    ]
    chosen = request["params"]["options"][1]["optionId"]
    await harness.respond(request, result={"outcome": {"outcome": "selected", "optionId": chosen}})
    await turn
    assert harness.host.frames("interact")[0]["value"] == "opt-reject"


async def test_a_client_that_takes_no_forms_has_the_question_skipped_and_named(harness: Harness) -> None:
    harness.host.respond = script(("ask", question()), ("text", "went on"))
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("pick"))
    assert harness.host.frames("interact") == [{"type": "interact", "promptId": "claude-control-1", "action": "skip"}]
    assert "This client takes no forms" in harness.said(sid)


async def test_a_question_the_client_refuses_is_dismissed_and_named(harness: Harness) -> None:
    await forms(harness)
    harness.host.respond = script(("ask", question()), ("text", "went on"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("pick")))
    request = await harness.asked("elicitation/create")
    await harness.respond(request, error={"code": -32603, "message": "no person here"})
    await turn
    assert harness.host.frames("interact")[0]["action"] == "cancel"
    assert "It could not be put to you" in harness.said(sid)


async def test_each_of_several_questions_is_asked_in_turn(harness: Harness) -> None:
    await forms(harness)
    harness.host.respond = script(("ask", question("p1")), ("ask", question("p2")), ("text", "both"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("pick")))
    for answer in ("Red", "Blue"):
        request = await harness.asked("elicitation/create")
        await harness.respond(request, result={"action": "accept", "content": {"answer": answer}})
    await turn
    assert [(frame["promptId"], frame["value"]) for frame in harness.host.frames("interact")] == [
        ("p1", "Red"),
        ("p2", "Blue"),
    ]


async def test_a_cancel_while_a_question_is_out_takes_the_question_back(harness: Harness) -> None:
    await forms(harness)
    harness.host.respond = script(("ask", question()), ("text", "never"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("pick")))
    await harness.asked("elicitation/create")
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    assert any(frame.get("method") == "$/cancel_request" for frame in harness.updates)
    assert harness.host.frames("interact") == []


async def test_an_answer_the_connection_lost_is_given_again_without_asking_twice(harness: Harness) -> None:
    await forms(harness)
    harness.host.respond = script(("ask", question()), ("text", "answered"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("pick")))
    request = await harness.asked("elicitation/create")
    harness.host.silent.add("interact")
    await harness.respond(request, result={"action": "accept", "content": {"answer": "Red"}})
    await harness.host.wait_for(lambda: harness.host.frames("interact"))
    harness.host.silent.discard("interact")
    await harness.host.drop()
    assert await turn == {"stopReason": "end_turn"}
    assert [frame["value"] for frame in harness.host.frames("interact")] == ["Red", "Red"]
    assert sum(frame.get("method") == "elicitation/create" for frame in harness.updates) == 1


# -- cancel, queue, steer -------------------------------------------------------


async def test_a_cancel_stops_the_running_turn_by_its_task(harness: Harness) -> None:
    harness.host.respond = script(("text", "working"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "working")
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    stop = harness.host.frames("stop")[0]
    assert stop == {"type": "stop", "sessionKey": harness.host.session().key, "taskId": "task-1"}


async def test_a_turn_stopped_from_the_app_ends_cancelled(harness: Harness) -> None:
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    session = await harness.host.wait_for(lambda: harness.host.sessions and harness.host.session())
    await harness.host.wait_for(lambda: session.running is not None)
    session.stopped = True
    session.wake.set()
    assert await turn == {"stopReason": "cancelled"}


async def test_a_cancel_the_host_never_confirms_is_answered_within_its_budget(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CANCEL_SETTLE_S", 0.2)
    harness.host.respond = script(("text", "working"), ("hold",))
    harness.host.silent.add("stop")
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "working")
    started = time.monotonic()
    await harness.ok("session/cancel", sessionId=sid)
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    assert time.monotonic() - started < 2
    assert len(harness.host.frames("stop")) == 1


async def test_a_cancel_before_the_host_takes_the_first_prompt_stops_the_turn_it_then_starts(
    harness: Harness,
) -> None:
    harness.host.respond = script(("text", "working"), ("hold",))
    harness.host.accepting = asyncio.Event()
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(harness.prompts)
    # No conversation exists yet, so there is nothing to stop or recall until the host answers.
    await harness.ok("session/cancel", sessionId=sid)
    harness.host.accepting.set()
    assert await turn == {"stopReason": "cancelled"}
    stop = await harness.host.wait_for(lambda: harness.host.frames("stop"))
    assert stop[0]["taskId"] == "task-1"
    await harness.host.wait_for(lambda: harness.host.session().running is None)


async def test_a_cancel_with_nothing_running_changes_nothing(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/cancel", sessionId=sid)
    await harness.ok("session/cancel", sessionId="unknown")
    assert harness.host.frames("stop") == [] and harness.host.frames("recall") == []


async def test_a_prompt_behind_a_turn_from_the_app_waits_its_turn_and_shows_only_its_own(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("text", "the app's turn"), ("hold",))
    app_task = asyncio.ensure_future(harness.host._run(session, {"prompt": "typed in the app"}, "task-app"))
    session.running = app_task
    await harness.host.wait_for(lambda: "the app's turn" in harness.said(sid))
    harness.host.respond = echo
    before = harness.said(sid)
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("mine")))
    await harness.host.wait_for(lambda: session.queue)
    session.stopped = True
    session.wake.set()
    assert await turn == {"stopReason": "end_turn"}
    # The app's turn ran on with nobody's prompt open, then this prompt's own turn.
    assert harness.said(sid)[len(before) :] == f"turn 3 of {session.key} on sonnet effort high: mine"


async def test_a_cancel_takes_a_queued_prompt_back_before_it_runs(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("hold",))
    session.running = asyncio.ensure_future(harness.host._run(session, {"prompt": "the app's"}, "task-app"))
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("mine")))
    await harness.host.wait_for(lambda: session.queue)
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    recall = await harness.host.wait_for(lambda: harness.host.frames("recall"))
    assert recall[0]["ref"] == harness.prompts()[-1]["clientRef"]
    await harness.host.wait_for(lambda: not session.queue)
    assert harness.host.frames("stop") == []
    session.stopped = True
    session.wake.set()


async def test_a_turn_from_the_app_on_a_held_session_is_forwarded_and_ended_with_usage(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("text", "a background report"))
    before = len(harness.session_updates(sid))
    await harness.host._run(session, {"prompt": "wake", "model": "sonnet"}, "task-wake")
    await harness.host.wait_for(lambda: harness.kinds(sid)[before:].count("usage_update") == 1)
    assert harness.said(sid).endswith("a background report")


async def test_steering_queues_the_text_and_takes_it_into_the_running_turn(harness: Harness) -> None:
    harness.host.respond = script(("text", "counting "), ("hold",), ("text", " and done"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("count")))
    await harness.host.wait_for(lambda: harness.said(sid) == "counting ")
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="stop counting") == {"status": "injected"}
    assert await turn == {"stopReason": "end_turn"}
    assert harness.said(sid, "user_message_chunk") == "stop counting"
    assert harness.said(sid) == "counting steered: stop counting and done"
    queued = harness.prompts()[1]
    assert queued["sessionKey"] == harness.host.session().key and queued["prompt"] == "stop counting"
    assert harness.host.frames("steer") == [
        {"type": "steer", "sessionKey": queued["sessionKey"], "ref": queued["clientRef"]}
    ]


async def test_a_steer_with_no_turn_running_is_no_turn_and_sends_nothing(harness: Harness) -> None:
    sid = await harness.new()
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="hello") == {"status": "no_turn"}
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late") == {"status": "no_turn"}
    assert len(harness.prompts()) == 1


async def test_an_agent_that_takes_no_steer_refuses_the_method(harness: Harness) -> None:
    harness.use("qwen")
    sid = await harness.new()
    error = await harness.refused("_raven/session/steer", sessionId=sid, text="x")
    assert error["code"] == METHOD_NOT_FOUND


async def test_a_steer_the_turn_ends_before_taking_is_recalled(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "STEER_TIMEOUT_S", 0.5)
    harness.host.respond = script(("text", "busy"), ("hold",))
    harness.host.silent.add("steer")
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late text") == {"status": "no_turn"}
    recall = (await harness.host.wait_for(lambda: harness.host.frames("recall")))[0]
    assert recall["ref"] == harness.prompts()[1]["clientRef"]
    await harness.ok("session/cancel", sessionId=sid)
    await turn


async def test_a_steer_that_started_a_turn_of_its_own_is_stopped(harness: Harness) -> None:
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    session = harness.host.session()
    # The host's turn ended before the adapter heard, so the text starts one of its own.
    first = session.running
    session.running = None
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="x") == {"status": "no_turn"}
    started = (await harness.host.wait_for(lambda: harness.host.frames("stop")))[-1]
    assert started["taskId"] == "task-2" and started["sessionKey"] == session.key
    first.cancel()
    turn.cancel()
    await asyncio.gather(first, turn, return_exceptions=True)


# -- sessions -------------------------------------------------------------------


async def test_another_process_loads_the_session_back_with_its_history_on_mirasims_default(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("remember PELICAN-7"))
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    harness.use()
    marker = len(harness.updates)
    result = await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    validate_def("LoadSessionResponse", result)
    assert result["configOptions"][0]["currentValue"] == "sonnet"
    replayed = [
        frame["params"]["update"] for frame in harness.updates[marker:] if frame.get("method") == "session/update"
    ]
    users = [update["content"]["text"] for update in replayed if update["sessionUpdate"] == "user_message_chunk"]
    assert users == ["remember PELICAN-7", "again"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("what was it?"))
    sent = harness.prompts()[-1]
    assert sent["sessionKey"] == harness.store.get(sid).mirasim_key and sent["model"] == "sonnet"


async def test_a_replay_reads_every_page_of_history_oldest_first(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "HISTORY_PAGE", 2)
    turns = [{"taskId": f"t{i}", "prompt": f"p{i}", "text": f"a{i}"} for i in range(5)]
    session = harness.host.add_session("claude", str(harness.tmp_path), turns)
    harness.store.put(Binding(session_id="acp-1", cwd=str(harness.tmp_path), mirasim_key=session.key))
    await harness.ok("session/load", sessionId="acp-1", cwd=str(harness.tmp_path), mcpServers=[])
    assert harness.said("acp-1", "user_message_chunk") == "p0p1p2p3p4"
    assert len(harness.host.frames("getHistory")) == 3


async def test_resume_reopens_without_replaying(harness: Harness) -> None:
    session = harness.host.add_session(
        "claude", str(harness.tmp_path), [{"taskId": "t0", "prompt": "old", "text": "x"}]
    )
    harness.store.put(Binding(session_id="acp-1", cwd=str(harness.tmp_path), mirasim_key=session.key))
    result = await harness.ok("session/resume", sessionId="acp-1", cwd=str(harness.tmp_path), mcpServers=[])
    validate_def("ResumeSessionResponse", result)
    assert harness.said("acp-1", "user_message_chunk") == ""
    # Nor is the turn the conversation ended on: its snapshot is its client's, not new words.
    assert harness.said("acp-1") == ""
    assert [u for u in harness.session_updates("acp-1") if u["sessionUpdate"] != "available_commands_update"] == []
    assert harness.host.frames("subscribe")[-1]["sessionKey"] == session.key


async def test_an_unknown_session_is_not_found_so_raven_opens_a_new_one(harness: Harness) -> None:
    for method in ("session/load", "session/resume"):
        error = await harness.refused(method, sessionId="nope", cwd=str(harness.tmp_path), mcpServers=[])
        assert error["code"] == RESOURCE_NOT_FOUND


async def test_a_conversation_is_not_carried_into_another_directory(harness: Harness, tmp_path: Path) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    error = await harness.refused("session/load", sessionId=sid, cwd=str(other), mcpServers=[])
    assert error["code"] == RESOURCE_NOT_FOUND and "open a new session there" in error["message"]
    # A session with no conversation yet follows the caller.
    fresh = await harness.new()
    await harness.ok("session/resume", sessionId=fresh, cwd=str(other), mcpServers=[])
    assert harness.agent.sessions[fresh].cwd == str(other)


async def test_the_list_is_this_agents_conversations_in_the_directory(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("mine"))
    from_app = harness.host.add_session(
        "claude", str(harness.tmp_path), [{"taskId": "t", "prompt": "typed in the app"}], title="App chat"
    )
    harness.host.add_session("codex", str(harness.tmp_path), [{"taskId": "t", "prompt": "codex"}])
    harness.host.add_session("claude", "/somewhere/else", [{"taskId": "t", "prompt": "elsewhere"}])
    listed = await harness.ok("session/list", cwd=str(harness.tmp_path))
    validate_def("ListSessionsResponse", listed)
    by_id = {row["sessionId"]: row for row in listed["sessions"]}
    assert set(by_id) == {sid, from_app.key}
    assert by_id[from_app.key]["title"] == "App chat"
    assert by_id[sid]["title"] == "mine" and by_id[sid]["updatedAt"].endswith("Z")
    everywhere = await harness.ok("session/list")
    assert len(everywhere["sessions"]) == 3


async def test_a_conversation_begun_in_the_app_is_loaded_by_its_key(harness: Harness) -> None:
    from_app = harness.host.add_session(
        "claude", str(harness.tmp_path), [{"taskId": "t0", "prompt": "from the app", "text": "hi"}]
    )
    await harness.ok("session/load", sessionId=from_app.key, cwd=str(harness.tmp_path), mcpServers=[])
    assert harness.said(from_app.key, "user_message_chunk") == "from the app"
    await harness.ok("session/prompt", sessionId=from_app.key, prompt=text("continue"))
    assert harness.prompts()[0]["sessionKey"] == from_app.key
    assert harness.store.get(from_app.key).mirasim_key == from_app.key
    # Another agent's key, or one in no listing, is nobody's to load here.
    codex = harness.host.add_session("codex", str(harness.tmp_path), [])
    for key in (codex.key, "claude:00000000-0000-0000-0000-000000000000"):
        error = await harness.refused("session/load", sessionId=key, cwd=str(harness.tmp_path), mcpServers=[])
        assert error["code"] == RESOURCE_NOT_FOUND


async def test_deleting_a_session_deletes_its_mirasim_conversation_without_asking(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    key = harness.host.session().key
    assert await harness.ok("session/delete", sessionId=sid) == {}
    assert harness.store.get(sid) is None and key not in harness.host.sessions
    assert (await harness.refused("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[]))[
        "code"
    ] == RESOURCE_NOT_FOUND
    assert await harness.ok("session/delete", sessionId="never-seen") == {}
    assert len(harness.host.frames("deleteSession")) == 1


async def test_deleting_a_listed_conversation_by_its_key_deletes_it(harness: Harness) -> None:
    from_app = harness.host.add_session("claude", str(harness.tmp_path), [{"taskId": "t", "prompt": "x"}])
    await harness.ok("session/delete", sessionId=from_app.key)
    assert from_app.key not in harness.host.sessions


async def test_a_conversation_mirasim_would_not_delete_is_said_so_after_the_record_goes(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    harness.host.silent.add("deleteSession")
    original = server_mod.DELETE_TIMEOUT_S
    server_mod.DELETE_TIMEOUT_S = 0.2
    try:
        error = await harness.refused("session/delete", sessionId=sid)
    finally:
        server_mod.DELETE_TIMEOUT_S = original
    assert "the session is forgotten, but Mirasim did not delete conversation" in error["message"]
    assert harness.store.get(sid) is None


async def test_deleting_a_session_mid_turn_stops_the_turn(harness: Harness) -> None:
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.host.sessions and harness.host.session().running)
    await harness.ok("session/delete", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    assert harness.host.frames("stop")


async def test_closing_a_session_ends_its_turn_and_keeps_the_conversation(harness: Harness) -> None:
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: (running := harness.agent.sessions[sid].turn) and running.task_id)
    assert await harness.ok("session/close", sessionId=sid) == {}
    assert await turn == {"stopReason": "cancelled"}
    assert harness.store.get(sid) is not None and harness.host.sessions
    assert (await harness.refused("session/prompt", sessionId=sid, prompt=text("x")))["code"] == RESOURCE_NOT_FOUND
    assert await harness.ok("session/close", sessionId=sid) == {}


async def test_a_prompt_waiting_when_its_session_closes_is_answered_cancelled(harness: Harness) -> None:
    """The captured-session half of close: a prompt already waiting on the lock must not reach the host.

    It read ``sessions`` before the close, so removing the session from the table
    alone leaves it a live object: it would acquire the lock and start a turn whose
    snapshots nothing routes and which ``session/cancel`` cannot reach.
    """
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    first = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("first")))
    await harness.host.wait_for(lambda: (running := harness.agent.sessions[sid].turn) and running.task_id)
    second = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("second")))
    session = harness.agent.sessions[sid]
    # Parked on the lock: it has read its session and is waiting for the first turn.
    await harness.host.wait_for(lambda: session.pending == 2 and session.lock.locked())
    assert await harness.ok("session/close", sessionId=sid) == {}
    assert await first == await asyncio.wait_for(second, 5) == {"stopReason": "cancelled"}
    assert [frame["prompt"] for frame in harness.prompts()] == ["first"]
    assert harness.agent._turns == {}


async def first_prompt_held_at_the_host(harness: Harness, *steps: Any) -> tuple[str, asyncio.Future[Any]]:
    """A new session whose first prompt the host has received and not yet answered."""
    harness.host.respond = script(*steps)
    harness.host.accepting = asyncio.Event()
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(harness.prompts)
    return sid, turn


async def test_a_session_closed_before_the_host_takes_its_first_prompt_still_records_the_conversation(
    harness: Harness,
) -> None:
    sid, turn = await first_prompt_held_at_the_host(harness, ("text", "late words"), ("hold",))
    assert await harness.ok("session/close", sessionId=sid) == {}
    assert await turn == {"stopReason": "cancelled"}
    assert harness.host.accepting is not None
    harness.host.accepting.set()
    stop = await harness.host.wait_for(lambda: harness.host.frames("stop"))
    key = harness.host.session().key
    assert stop[0] == {"type": "stop", "sessionKey": key, "taskId": "task-1"}
    binding = await harness.host.wait_for(lambda: harness.store.get(sid))
    assert binding.mirasim_key == key
    await harness.host.wait_for(lambda: harness.host.session().running is None)
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    assert harness.said(sid, "user_message_chunk") == "go"


async def test_a_session_deleted_before_the_host_takes_its_first_prompt_takes_the_conversation_with_it(
    harness: Harness,
) -> None:
    sid, turn = await first_prompt_held_at_the_host(harness, ("hold",))
    assert await harness.ok("session/delete", sessionId=sid) == {}
    assert await turn == {"stopReason": "cancelled"}
    assert harness.host.frames("deleteSession") == []
    assert harness.host.accepting is not None
    harness.host.accepting.set()
    deleted = await harness.host.wait_for(lambda: harness.host.frames("deleteSession"))
    assert harness.host.deleted == {deleted[0]["sessionKey"]} and not harness.host.sessions
    assert harness.store.get(sid) is None


async def test_a_first_prompt_the_host_takes_after_its_cancel_settled_becomes_the_sessions_conversation(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CANCEL_SETTLE_S", 0.1)
    sid, turn = await first_prompt_held_at_the_host(harness, ("text", "late words"), ("hold",))
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    assert harness.host.accepting is not None
    harness.host.accepting.set()
    await harness.host.wait_for(lambda: harness.host.frames("stop"))
    session = harness.host.session()
    await harness.host.wait_for(lambda: session.running is None)
    harness.host.respond = echo
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.prompts()[-1]["sessionKey"] == session.key
    # The given-up turn's words were never forwarded as a turn nobody prompted.
    assert harness.said(sid) == f"turn 2 of {session.key} on sonnet effort high: again"


async def test_idle_sessions_past_the_cap_are_let_go_and_a_recorded_one_comes_back(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "MAX_IDLE_SESSIONS", 2)
    first = await harness.new()
    await harness.ok("session/prompt", sessionId=first, prompt=text("one"))
    for _ in range(3):
        await harness.new()
    assert first not in harness.agent.sessions
    await harness.ok("session/load", sessionId=first, cwd=str(harness.tmp_path), mcpServers=[])
    assert first in harness.agent.sessions


# -- the connection -------------------------------------------------------------


async def test_a_turn_survives_the_connection_dropping_and_finishes_after_it_returns(harness: Harness) -> None:
    harness.host.respond = script(("text", "before "), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "before ")
    await harness.host.drop()
    await harness.host.wait_for(
        lambda: len(harness.host.frames("setStreamCadence")) >= 2 and harness.host.frames("subscribe")
    )
    session = harness.host.session()
    session.steered.append("x")
    session.wake.set()
    assert await turn == {"stopReason": "end_turn"}
    assert harness.said(sid) == "before steered: x"


async def test_a_turn_the_host_no_longer_runs_after_a_restart_is_lost(harness: Harness) -> None:
    harness.host.respond = script(("text", "before"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.call("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "before")
    session = harness.host.session()
    session.running.cancel()
    session.snapshot = {}
    await harness.host.drop()
    response = await turn
    assert response["error"]["code"] == INTERNAL_ERROR
    assert "no longer running it" in response["error"]["message"]


async def test_a_turn_unheard_of_long_after_the_connection_dropped_is_given_up(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "LOST_TURN_S", 0.3)
    harness.host.respond = script(("text", "before"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.call("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "before")
    harness.host.health = False
    harness.host.silent.add("subscribe")
    real = harness.host.address
    harness.agent.host._locate = lambda: None
    await harness.host.drop()
    response = await turn
    assert "lost the connection to the Mirasim host" in response["error"]["message"]
    harness.agent.host._locate = lambda: real


async def test_a_gap_in_the_patches_asks_for_a_fresh_snapshot(harness: Harness) -> None:
    harness.host.respond = script(("text", "a"), ("hold",), ("text", "c"))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "a")
    session = harness.host.session()
    subscribes = len(harness.host.frames("subscribe"))
    session.seq += 5
    session.snapshot["text"] = "ab"
    session.steered.append("y")
    session.wake.set()
    assert await turn == {"stopReason": "end_turn"}
    assert len(harness.host.frames("subscribe")) > subscribes
    assert harness.said(sid) == "absteered: yc"


async def test_a_client_that_hangs_up_mid_turn_stops_the_turn(tmp_path: Path, host: FakeHost) -> None:
    import io

    from raven.acp.mirasim.server import serve

    host.respond = script(("hold",))
    reader = asyncio.StreamReader()
    out = io.BytesIO()
    new = {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}}
    reader.feed_data(json.dumps(new).encode() + b"\n")
    serving = asyncio.ensure_future(
        serve(reader, out, agent="claude", store=BindingStore(tmp_path / "s.json"), locate=lambda: host.address)
    )
    await host.wait_for(lambda: b'"sessionId"' in out.getvalue())
    sid = json.loads(out.getvalue().splitlines()[0])["result"]["sessionId"]
    prompt = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": text("x")}}
    reader.feed_data(json.dumps(prompt).encode() + b"\n")
    await host.wait_for(lambda: host.sessions and host.session().running)
    reader.feed_eof()
    await asyncio.wait_for(serving, 10)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}} in answers
    assert host.frames("stop")


async def test_shutdown_refuses_a_prompt_waiting_on_the_session_lock(harness: Harness) -> None:
    harness.host.respond = script(("text", "working"), ("hold",))
    sid = await harness.new()
    first = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("first")))
    await harness.host.wait_for(lambda: harness.host.sessions and harness.host.session().running)
    queued = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("queued")))
    try:
        await harness.host.wait_for(lambda: harness.agent.sessions[sid].pending == 2)
        await harness.agent.shutdown()
        answers = await asyncio.wait_for(asyncio.gather(first, queued), 5)
        assert answers == [{"stopReason": "cancelled"}, {"stopReason": "cancelled"}]
        assert len(harness.prompts()) == 1
        assert not harness.agent._turns
    finally:
        first.cancel()
        queued.cancel()
        await asyncio.gather(first, queued, return_exceptions=True)


@pytest.mark.parametrize("steer", [False, True], ids=["prompt", "steer"])
async def test_shutdown_refuses_work_that_is_still_reading_the_catalogue(harness: Harness, monkeypatch, steer):
    harness.host.respond = script(("text", "working"), ("hold",))
    sid = await harness.new()
    other = await harness.new()
    first = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("first")))
    await harness.host.wait_for(lambda: harness.host.sessions and harness.host.session().running)
    entered = asyncio.Event()
    released = asyncio.Event()
    original = harness.agent.catalog

    async def delayed_catalog(**kwargs):
        entered.set()
        await released.wait()
        return await original(**kwargs)

    monkeypatch.setattr(harness.agent, "catalog", delayed_catalog)
    work = (
        harness.ok("_raven/session/steer", sessionId=sid, text="later")
        if steer
        else harness.ok("session/prompt", sessionId=other, prompt=text("later"))
    )
    waiting = asyncio.create_task(work)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await harness.agent.shutdown()
        released.set()
        assert await asyncio.wait_for(waiting, 5) == ({"status": "no_turn"} if steer else {"stopReason": "cancelled"})
        assert await asyncio.wait_for(first, 5) == {"stopReason": "cancelled"}
        assert len(harness.prompts()) == 1
        assert not harness.agent._turns
    finally:
        released.set()
        first.cancel()
        waiting.cancel()
        await asyncio.gather(first, waiting, return_exceptions=True)


async def test_the_frame_loop_answers_each_request_and_a_line_it_cannot_read(tmp_path: Path, host: FakeHost) -> None:
    import io

    from raven.acp.mirasim.server import serve

    reader = asyncio.StreamReader()
    out = io.BytesIO()
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}},
        {"jsonrpc": "2.0", "id": 3, "method": "no/such", "params": {}},
        {"jsonrpc": "2.0", "method": "no/such/notification", "params": {}},
    ]
    reader.feed_data(b"not json\n" + b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
    reader.feed_eof()
    await serve(reader, out, agent="claude", store=BindingStore(tmp_path / "store.json"), locate=lambda: host.address)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    by_id = {answer.get("id"): answer for answer in answers if "method" not in answer}
    assert by_id[None]["error"]["code"] == -32700
    assert by_id[1]["result"]["agentInfo"]["name"] == "mirasim-claude"
    assert "sessionId" in by_id[2]["result"]
    assert by_id[3]["error"]["code"] == -32601


async def test_a_frame_the_client_is_gone_for_is_dropped_not_raised(tmp_path: Path, host: FakeHost) -> None:
    from raven.acp.mirasim.server import serve

    class Gone:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError("client went away")

        def flush(self) -> None:
            pass

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n')
    reader.feed_eof()
    await serve(reader, Gone(), agent="claude", store=BindingStore(tmp_path / "s.json"), locate=lambda: host.address)


async def test_an_unexpected_failure_answers_with_its_own_words(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the store's disk is full")

    monkeypatch.setattr(harness.agent, "list_sessions", broken)
    error = await harness.refused("session/list")
    assert (error["code"], error["message"]) == (INTERNAL_ERROR, "the store's disk is full")
    assert await harness.agent.handle({"jsonrpc": "2.0", "method": "session/list", "params": {}}) is None
    assert await harness.agent.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None
    assert await harness.agent.handle({"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"id": 1}}) is None


# -- the entry ------------------------------------------------------------------


def test_the_entry_refuses_an_agent_id_that_is_not_one(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        server_mod.main(["--agent", "Not An Id"])
    assert stopped.value.code == 2
    assert "is not a Mirasim agent id" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        server_mod.main([])


def test_python_dash_m_runs_the_server_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(server_mod, "main", lambda: 0)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("raven.acp.mirasim", run_name="__main__")
    assert stopped.value.code == 0


async def test_the_entry_serves_stdin_until_it_closes_on_the_host_it_finds(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import contextlib
    import io

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}\n')
    reader.feed_eof()

    @contextlib.asynccontextmanager
    async def fake_stdin():
        yield reader

    assert host.address is not None
    monkeypatch.setattr(server_mod, "open_stdin", fake_stdin)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RAVEN_MIRASIM_HOST", str(host.address.port))
    monkeypatch.setenv("MIRASIM_HOME", str(tmp_path / "mirasim"))
    out = io.BytesIO()
    await server_mod._main(argparse.Namespace(agent="claude", verbose=False), out)
    answer = json.loads(out.getvalue().splitlines()[0])["result"]
    assert answer["agentInfo"] == {"name": "mirasim-claude", "title": "claude (Mirasim)", "version": "0.0.537"}


def test_main_wires_the_logger_the_stdout_claim_and_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib
    import io

    calls: list[str] = []

    class Logger:
        def remove(self) -> None:
            calls.append("remove")

        def add(self, sink: Any, level: str, **options: Any) -> None:
            calls.append(f"add {level}")

    @contextlib.contextmanager
    def claim():
        calls.append("claim")
        yield io.BytesIO()

    def run(coro: Any) -> int:
        calls.append("run")
        coro.close()
        return 0

    monkeypatch.setattr(server_mod, "logger", Logger())
    monkeypatch.setattr(server_mod, "claim_stdout", claim)
    monkeypatch.setattr(server_mod.asyncio_runner, "run", run)
    assert server_mod.main(["--agent", "claude", "--verbose"]) == 0
    assert calls == ["remove", "add INFO", "claim", "run"]
    calls.clear()
    assert server_mod.main(["--installed"]) == 0
    assert calls == ["run"]


async def test_the_entry_lists_the_agents_mirasim_has_installed(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await server_mod._print_installed(lambda: host.address) == 0
    assert json.loads(capsys.readouterr().out) == ["claude", "codex", "qwen"]
    assert await server_mod._print_installed(lambda: None) == 1
    assert "No Mirasim host is running" in capsys.readouterr().err


# -- the menus when the host cannot say -----------------------------------------


async def test_a_menu_that_could_not_be_read_goes_out_marked_as_unread_and_is_read_again(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "QUERY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(server_mod, "CATALOG_RETRY_S", 0.0)
    harness.host.silent.add("getCatalog")
    first = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert first["_meta"] == {"raven": {"modelMenuUnread": True}}
    assert "configOptions" not in first and "modes" not in first
    harness.host.silent.discard("getCatalog")
    second = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert "_meta" not in second and second["configOptions"][0]["currentValue"] == "sonnet"


async def test_a_failed_re_read_keeps_the_menus_in_hand_and_is_not_tried_on_every_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "QUERY_TIMEOUT_S", 0.2)
    await harness.agent.catalog()
    harness.host.silent.add("getCatalog")
    kept = await harness.agent.catalog(refresh=True)
    assert kept.default_model == "sonnet" and kept.models
    asked = len(harness.host.frames("getCatalog"))
    # Inside the pause after a failure the copy in hand answers, without asking again.
    assert (await harness.agent.catalog(refresh=True)).models
    assert len(harness.host.frames("getCatalog")) == asked


async def test_an_agent_installed_since_the_menus_were_read_opens_while_its_menu_read_fails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "QUERY_TIMEOUT_S", 0.2)
    harness.use("zcode")
    harness.host.catalogs["zcode"] = {
        "defaultModel": "glm-5.3-flash",
        "models": [{"id": "glm-5.3-flash"}],
        "effort": [],
    }
    with pytest.raises(server_mod.RefusalError):
        await harness.agent.new_session({"cwd": str(harness.tmp_path), "mcpServers": []})
    next(row for row in harness.host.rows if row["id"] == "zcode")["installed"] = True
    harness.host.silent.add("getCatalog")
    harness.agent._catalog_failed_at = None
    assert await harness.new()


async def test_with_no_menus_in_hand_a_host_that_cannot_list_its_agents_refuses(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CLIS_TIMEOUT_S", 0.3)
    await harness.agent.catalog()
    harness.host.silent.add("listClis")
    assert (await harness.agent.catalog(refresh=True)).models
    harness.use()
    with pytest.raises(server_mod.RefusalError, match="could not list its agents"):
        await harness.agent.catalog()


async def test_reads_the_host_cannot_answer_are_said_so(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "QUERY_TIMEOUT_S", 0.2)
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    harness.host.silent |= {"listSessions", "getHistory", "getCommands"}
    assert (
        "could not list its sessions" in (await harness.refused("session/list", cwd=str(harness.tmp_path)))["message"]
    )
    harness.use()
    error = await harness.refused("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    assert "could not read the session's history" in error["message"]
    assert await harness.agent._commands_for(str(harness.tmp_path)) == []


async def test_a_conversation_the_host_refuses_to_delete_is_said_so(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    harness.host.delete_error = "busy"
    error = await harness.refused("session/delete", sessionId=sid)
    assert error["message"].endswith("did not delete conversation " + harness.host.session().key + ": busy")


# -- edges of a turn ------------------------------------------------------------


async def test_each_turn_names_the_effort_the_session_was_put_on(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/set_mode", sessionId=sid, modeId="max")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    assert harness.prompts()[0]["effort"] == "max"


async def test_a_turn_that_ran_is_answered_even_when_its_record_cannot_be_saved(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(binding: Binding) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(harness.store, "put", broken)
    sid = await harness.new()
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("hi")) == {"stopReason": "end_turn"}


async def test_a_prompt_cancelled_before_the_host_took_it_is_stopped_when_it_does(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CANCEL_SETTLE_S", 0.1)
    harness.host.respond = script(("hold",))
    sid = await harness.new()
    harness.host.silent.add("prompt")
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.host.frames("prompt"))
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    # The host takes the prompt after all: the turn it starts is stopped at once.
    harness.host.silent.discard("prompt")
    client = harness.host.clients[0]
    await harness.host._prompt(client, harness.host.frames("prompt")[0])
    stop = await harness.host.wait_for(lambda: harness.host.frames("stop"))
    assert stop[0]["taskId"] == "task-1"


async def test_a_queued_prompt_given_up_before_the_host_took_it_is_recalled_when_it_does(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "CANCEL_SETTLE_S", 0.1)
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("hold",))
    session.running = asyncio.ensure_future(harness.host._run(session, {"prompt": "the app's"}, "task-app"))
    harness.host.silent.add("prompt")
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("mine")))
    await harness.host.wait_for(lambda: len(harness.host.frames("prompt")) == 2)
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    recalled_early = len(harness.host.frames("recall"))
    harness.host.silent.discard("prompt")
    await harness.host._prompt(harness.host.clients[0], harness.host.frames("prompt")[1])
    late = await harness.host.wait_for(lambda: harness.host.frames("recall")[recalled_early:])
    assert late[0]["ref"] == harness.host.frames("prompt")[1]["clientRef"]
    await harness.host.wait_for(lambda: not session.queue)
    session.stopped = True
    session.wake.set()


async def test_a_queued_turn_cancelled_as_it_starts_is_stopped(harness: Harness) -> None:
    sid = await harness.new()
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    session = harness.host.session()
    harness.host.respond = script(("hold",))
    session.running = asyncio.ensure_future(harness.host._run(session, {"prompt": "the app's"}, "task-app"))
    harness.host.silent.add("recall")
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("mine")))
    await harness.host.wait_for(lambda: session.queue)
    await harness.ok("session/cancel", sessionId=sid)
    assert await turn == {"stopReason": "cancelled"}
    # The recall never landed, so the host ran the prompt after the app's turn: it is stopped.
    session.stopped = True
    session.wake.set()
    stop = await harness.host.wait_for(
        lambda: [f for f in harness.host.frames("stop") if f.get("taskId") != "task-app"]
    )
    assert stop[0]["sessionKey"] == session.key
    session.stopped = True
    session.wake.set()


async def test_a_steer_the_host_cannot_queue_is_no_turn(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "STEER_TIMEOUT_S", 0.3)
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    harness.host.refuse["prompt"] = "the outbox is full"
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="x") == {"status": "no_turn"}
    del harness.host.refuse["prompt"]
    harness.host.silent.add("prompt")
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="y") == {"status": "no_turn"}
    harness.host.silent.discard("prompt")
    await harness.ok("session/cancel", sessionId=sid)
    await turn


@pytest.mark.parametrize("original_ended", [False, True])
async def test_a_timed_out_steer_is_recalled_or_stopped_when_accepted_later(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, original_ended: bool
) -> None:
    monkeypatch.setattr(server_mod, "STEER_TIMEOUT_S", 0.05)
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    harness.host.silent.add("prompt")
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late") == {"status": "no_turn"}
    late = harness.prompts()[-1]
    await harness.host.wait_for(lambda: [f for f in harness.host.handled if f.get("type") == "recall"])
    if original_ended:
        await harness.ok("session/cancel", sessionId=sid)
        await turn
    harness.host.silent.discard("prompt")
    await harness.host._prompt(harness.host.clients[0], late)
    session = harness.host.session()
    if original_ended:
        await harness.host.wait_for(lambda: session.running is None, limit=1)
        assert any(f.get("taskId") == "task-2" for f in harness.host.frames("stop"))
    else:
        await harness.host.wait_for(lambda: not session.queue, limit=1)
        assert [f["ref"] for f in harness.host.frames("recall")] == [late["clientRef"], late["clientRef"]]
        assert not harness.host.frames("stop")
        await harness.ok("session/cancel", sessionId=sid)
        await turn


async def test_a_steer_accepted_while_timeout_unwinds_stops_only_its_own_task(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "STEER_TIMEOUT_S", 0.05)
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    harness.host.silent.add("prompt")
    request = harness.agent.host.request

    async def accept_before_unwind(frame: dict[str, Any], reply: Any, *, timeout: float) -> dict[str, Any]:
        try:
            return await request(frame, reply, timeout=timeout)
        except TimeoutError:
            await harness.ok("session/cancel", sessionId=sid)
            await turn
            await harness.host._prompt(harness.host.clients[0], frame)
            await harness.host.wait_for(
                lambda: harness.agent.sessions[sid].view.snapshot.get("taskId") == "task-2", limit=1
            )
            raise

    monkeypatch.setattr(harness.agent.host, "request", accept_before_unwind)
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late") == {"status": "no_turn"}
    await harness.host.wait_for(lambda: harness.host.session().running is None, limit=1)
    assert [f["taskId"] for f in harness.host.frames("stop")] == ["task-1", "task-2"]


async def test_a_steer_send_failure_recalls_the_prompt_already_queued(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    send = harness.agent.host.send

    async def fail_steer(frame: dict[str, Any]) -> None:
        if frame["type"] == "steer":
            raise server_mod.HostUnavailableError("steer send failed")
        await send(frame)

    monkeypatch.setattr(harness.agent.host, "send", fail_steer)
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late") == {"status": "no_turn"}
    await harness.host.wait_for(lambda: not harness.host.session().queue, limit=1)
    assert harness.host.frames("recall")
    assert not harness.host.frames("stop")
    await harness.ok("session/cancel", sessionId=sid)
    await turn


async def test_a_steer_that_ran_as_a_turn_of_its_own_after_the_turn_ended_is_stopped(harness: Harness) -> None:
    harness.host.respond = script(("text", "busy"), ("hold",))
    sid = await harness.new()
    turn = asyncio.ensure_future(harness.ok("session/prompt", sessionId=sid, prompt=text("go")))
    await harness.host.wait_for(lambda: harness.said(sid) == "busy")
    session = harness.host.session()
    original = harness.host._steer

    async def end_then_steer(frame: dict[str, Any]) -> None:
        # The turn ends just as the steer arrives: the queued text runs as the next turn.
        harness.host.respond = script(("hold",))
        session.stopped = True
        session.wake.set()

    harness.host._steer = end_then_steer  # type: ignore[method-assign]
    assert await harness.ok("_raven/session/steer", sessionId=sid, text="late") == {"status": "no_turn"}
    harness.host._steer = original  # type: ignore[method-assign]
    await turn
    stops = await harness.host.wait_for(lambda: [f for f in harness.host.frames("stop") if f.get("taskId") == "task-2"])
    assert stops[0]["sessionKey"] == session.key


def test_an_image_that_cannot_be_read_goes_as_its_path_and_odd_mcp_params_name_none(tmp_path: Path) -> None:
    from raven.acp.mirasim.server import _inline_image, _mcp_names

    assert _inline_image(str(tmp_path / "gone.png"), "image/png") is None
    assert _inline_image(str(tmp_path / "x.txt"), "") is None
    assert _mcp_names({"mcpServers": "nope"}) == ()
    assert server_mod._iso(None) is None and server_mod._iso(0) is None
