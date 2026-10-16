"""The Mirasim adapter's ACP methods, against a stand-in ``mirasim`` run as a real process."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from raven.acp.mirasim import server as server_mod
from raven.acp.mirasim.server import MirasimAgent
from raven.acp.mirasim.store import BindingStore
from raven.acp.mirasim.uicli import UiCli
from raven.acp.protocol import INTERNAL_ERROR, INVALID_PARAMS, RESOURCE_NOT_FOUND
from tests.acp_schema import validate_def
from tests.mirasim_stub_cli import install

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in Mirasim is a /bin/sh wrapper")


class Harness:
    """One adapter over a stand-in Mirasim, and what it has said so far."""

    def __init__(self, tmp_path: Path, *, agent: str = "claude", mode: str = "ok") -> None:
        self.tmp_path = tmp_path
        self.store = BindingStore(tmp_path / "store.json")
        self.updates: list[dict[str, Any]] = []
        self.seen: set[Any] = set()
        self.ids = 0
        self.use(mode, agent=agent)

    def use(self, mode: str, *, agent: str = "claude") -> None:
        """A fresh adapter process's worth of state, sharing the store and Mirasim's conversations."""
        exe = install(self.tmp_path, mode)
        cli = UiCli(executable=str(exe), workdir=str(self.tmp_path / "workdir"))
        self.agent = MirasimAgent(agent=agent, cli=cli, store=self.store, emit=self.updates.append)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.ids += 1
        response = await self.agent.handle({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params})
        assert response is not None
        return response

    async def ok(self, method: str, **params: Any) -> dict[str, Any]:
        response = await self.call(method, **params)
        assert "result" in response, response
        return response["result"]

    async def refused(self, method: str, **params: Any) -> dict[str, Any]:
        response = await self.call(method, **params)
        assert "error" in response, response
        return response["error"]

    def replies(self) -> list[str]:
        return [
            update["params"]["update"]["content"]["text"]
            for update in self.updates
            if update.get("method") == "session/update"
        ]

    def prompts(self) -> list[list[str]]:
        return [command[1:] for command in self.commands() if command[:1] == ["prompt"]]

    def routes(self) -> list[list[str]]:
        """The arguments of each ``session route`` the adapter ran, in order."""
        return [command[2:] for command in self.commands() if command[:2] == ["session", "route"]]

    def commands(self) -> list[list[str]]:
        """Each one-shot ``ui-cli`` command, verb first, past the flag naming its server's directory."""
        out = []
        for argv in self.calls():
            rest = argv[1:] if argv[:1] == ["ui-cli"] else []
            if rest[:1] and rest[0].startswith("--workdir="):
                out.append(rest[1:])
        return out

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.tmp_path / "stub" / "calls.jsonl").read_text().splitlines()]

    async def asked(self, method: str, *, limit: float = 30) -> dict[str, Any]:
        """The next request of ``method`` the adapter put to its client, waited for."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            for frame in self.updates:
                if frame.get("method") == method and "id" in frame and frame["id"] not in self.seen:
                    self.seen.add(frame["id"])
                    return frame
            await asyncio.sleep(0.05)
        pytest.fail(f"the adapter never sent {method}")

    async def respond(self, request: dict[str, Any], **outcome: Any) -> None:
        """Answer a request the adapter sent, as its client: ``result=`` or ``error=``."""
        assert await self.agent.handle({"jsonrpc": "2.0", "id": request["id"], **outcome}) is None


def text(words: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": words}]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_initialize_answers_at_once_and_reads_the_menus_behind_it(harness: Harness) -> None:
    result = await harness.ok("initialize", protocolVersion=1)
    validate_def("InitializeResponse", result)
    assert result["agentCapabilities"]["loadSession"] is True
    assert result["agentCapabilities"]["sessionCapabilities"] == {"delete": {}}
    assert result["agentInfo"] == {"name": "mirasim-claude", "title": "claude (Mirasim)", "version": "0.0.465"}
    await harness.agent._prefetch
    # The pool's liveness probe is a repeat initialize with five seconds: it must
    # answer from what is in hand, not start another read.
    again = await harness.ok("initialize", protocolVersion=1)
    assert again["agentInfo"]["title"] == "Claude Code (Mirasim)"
    calls = (harness.tmp_path / "stub" / "calls.jsonl").read_text()
    assert calls.count('"agents"') == 1 and calls.count('"version"') == 1


async def test_a_new_session_offers_the_menus_and_starts_no_conversation(harness: Harness) -> None:
    result = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    validate_def("NewSessionResponse", result)
    assert result["configOptions"][0]["currentValue"] == "sonnet"
    assert result["modes"]["currentModeId"] == "high"
    assert harness.prompts() == []


async def test_each_turn_names_its_model_and_effort_and_the_next_continues_the_conversation(
    harness: Harness,
) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    picked = await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="opus")
    validate_def("SetSessionConfigOptionResponse", picked)
    assert picked["configOptions"][0]["currentValue"] == "opus"
    validate_def("SetSessionModeResponse", await harness.ok("session/set_mode", sessionId=sid, modeId="max"))

    first = await harness.ok("session/prompt", sessionId=sid, prompt=text("-n is not a flag"))
    validate_def("PromptResponse", first)
    assert first == {"stopReason": "end_turn"}
    validate_def("SessionNotification", harness.updates[0]["params"])
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))

    assert harness.replies() == [
        "turn 1 of sess-1 on opus effort max files []: -n is not a flag",
        "turn 2 of sess-1 on opus effort max files []: again",
    ]
    first_args, second_args = harness.prompts()
    assert "--agent=claude" in first_args and f"--cwd={harness.tmp_path}" in first_args
    assert "--session=sess-1" in second_args and not any(arg.startswith("--agent=") for arg in second_args)
    # An id with no route prefix is the model on Mirasim's own route, which no flag names.
    assert not any(arg.startswith("--route") for args in (first_args, second_args) for arg in args)
    assert harness.routes() == []


async def test_a_model_without_rungs_is_sent_no_effort(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="glm-5.3-flash")
    await harness.ok("session/set_mode", sessionId=sid, modeId="max")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    assert harness.replies() == ["turn 1 of sess-1 on glm-5.3-flash effort - files []: hi"]


async def test_another_process_loads_the_session_back_on_mirasims_default(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("remember PELICAN-7"))

    harness.use("ok")
    loaded = await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    validate_def("LoadSessionResponse", loaded)
    # Raven keeps no pin across its own restart, so the session comes back on
    # the default rather than on the model it last ran on.
    assert loaded["configOptions"][0]["currentValue"] == "sonnet"
    await harness.ok("session/prompt", sessionId=sid, prompt=text("what was it?"))
    assert harness.replies()[-1] == "turn 2 of sess-1 on sonnet effort high files []: what was it?"


async def test_an_unknown_session_is_not_found_so_raven_opens_a_new_one(harness: Harness) -> None:
    error = await harness.refused("session/load", sessionId="never-made", cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == RESOURCE_NOT_FOUND
    error = await harness.refused("session/prompt", sessionId="never-made", prompt=text("hi"))
    assert error["code"] == RESOURCE_NOT_FOUND


async def test_a_conversation_is_not_carried_into_another_directory(harness: Harness, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sid = (await harness.ok("session/new", cwd=str(tmp_path), mcpServers=[]))["sessionId"]
    # Before its first turn there is no conversation to strand, so the session moves.
    await harness.ok("session/load", sessionId=sid, cwd=str(elsewhere), mcpServers=[])
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    assert f"--cwd={elsewhere}" in harness.prompts()[0]
    error = await harness.refused("session/load", sessionId=sid, cwd=str(tmp_path), mcpServers=[])
    assert error["code"] == RESOURCE_NOT_FOUND and str(elsewhere) in error["message"]


async def test_a_failed_turn_is_an_error_even_though_mirasim_exits_0(harness: Harness) -> None:
    harness.use("fail")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert error["code"] == INTERNAL_ERROR
    assert "failed to initialize sqlite state runtime" in error["message"]
    # The conversation exists in Mirasim whatever the turn did, so it is kept.
    assert harness.store.get(sid).mirasim_key == "sess-1"


async def test_a_conversation_mirasim_forgot_starts_again_once_and_says_so(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    harness.use("forget")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/prompt", sessionId=sid, prompt=text("second"))
    reply = harness.replies()[-1]
    assert reply.startswith("turn 1 of sess-2 ")
    assert "[Mirasim] The Mirasim conversation this session continued no longer exists" in reply
    assert harness.store.get(sid).mirasim_key == "sess-2"


async def test_an_unoffered_model_or_effort_is_refused_with_what_is_offered(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    error = await harness.refused("session/set_config_option", sessionId=sid, configId="model", value="gpt-9")
    assert error["code"] == INVALID_PARAMS and "'glm-5.3-flash'" in error["message"]
    error = await harness.refused("session/set_mode", sessionId=sid, modeId="ultra")
    assert error["code"] == INVALID_PARAMS
    error = await harness.refused("session/set_config_option", sessionId=sid, configId="effort", value="max")
    assert error["code"] == INVALID_PARAMS


async def test_a_turn_that_comes_back_still_asking_names_the_question_in_its_reply(harness: Harness) -> None:
    harness.use("ask")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")) == {"stopReason": "end_turn"}
    reply = harness.replies()[-1]
    assert "- Claude asks: Which colour? (Red / Blue)" in reply
    # Only the interaction that can be answered is named.
    assert "no prompt id" not in reply and "not an interaction" not in reply


FORMS = {"elicitation": {"form": {}}}


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Look at a running turn's server without the pauses a real one is spared."""
    monkeypatch.setattr(server_mod, "WATCH_PAUSES_S", (0.05,))


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_a_question_the_turn_stops_on_is_put_to_the_client_and_answered(harness: Harness) -> None:
    harness.use("asks")
    await harness.ok("initialize", protocolVersion=1, clientCapabilities=FORMS)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")))

    request = await harness.asked("elicitation/create")
    validate_def("CreateElicitationRequest", request["params"])
    assert request["params"]["sessionId"] == sid
    assert request["params"]["requestedSchema"]["properties"]["answer"]["enum"] == ["Red", "Blue"]
    await harness.respond(request, result={"action": "accept", "content": {"answer": "Blue"}})

    assert await asyncio.wait_for(turn, 30) == {"stopReason": "end_turn"}
    assert harness.replies()[-1] == "turn 1 of sess-1 got select=Blue"
    # A new conversation has no key yet, so its server's rows named it; the
    # next turn continues by key and is looked at directly.
    asked_for = [argv[3] for argv in harness.calls() if argv[2:3] == ["raw"]]
    assert any('"listSessions"' in frame for frame in asked_for)
    assert harness.store.get(sid).mirasim_key == "sess-1"


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_a_tool_approval_goes_out_as_a_permission_request(harness: Harness) -> None:
    from tests.test_acp_mirasim_interactions import approval

    harness.use("asks")
    (harness.tmp_path / "stub" / "questions.json").write_text(json.dumps([approval()]))
    # No forms declared: an approval does not need them.
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("list files")))
    request = await harness.asked("session/request_permission")
    validate_def("RequestPermissionRequest", request["params"])
    allow = next(option for option in request["params"]["options"] if option["kind"] == "allow_once")
    await harness.respond(request, result={"outcome": {"outcome": "selected", "optionId": allow["optionId"]}})
    assert await asyncio.wait_for(turn, 30) == {"stopReason": "end_turn"}
    assert harness.replies()[-1] == "turn 1 of sess-1 got select=Allow"


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_a_client_that_takes_no_forms_has_the_question_skipped_and_named(harness: Harness) -> None:
    harness.use("asks")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("paint it"))
    reply = harness.replies()[-1]
    assert reply.startswith("turn 1 of sess-1 got skip=None")
    assert "[Mirasim] The agent asked: Claude asks: Which colour? (Red / Blue)" in reply
    assert "takes no forms" in reply
    assert not [frame for frame in harness.updates if frame.get("method") == "elicitation/create"]


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_a_question_the_client_refuses_is_dismissed_and_the_turn_goes_on(harness: Harness) -> None:
    harness.use("asks")
    await harness.ok("initialize", protocolVersion=1, clientCapabilities=FORMS)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")))
    request = await harness.asked("elicitation/create")
    await harness.respond(request, error={"code": -32601, "message": "no forms after all"})
    assert await asyncio.wait_for(turn, 30) == {"stopReason": "end_turn"}
    reply = harness.replies()[-1]
    assert reply.startswith("turn 1 of sess-1 got cancel=None")
    assert "could not be put to you" in reply and "no forms after all" in reply


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_an_answer_that_did_not_arrive_is_sent_again_without_asking_twice(harness: Harness) -> None:
    harness.use("flaky")
    await harness.ok("initialize", protocolVersion=1, clientCapabilities=FORMS)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")))
    request = await harness.asked("elicitation/create")
    await harness.respond(request, result={"action": "accept", "content": {"answer": "Red"}})
    assert await asyncio.wait_for(turn, 30) == {"stopReason": "end_turn"}
    assert harness.replies()[-1] == "turn 1 of sess-1 got select=Red"
    assert [frame["method"] for frame in harness.updates if "id" in frame] == ["elicitation/create"]
    assert sum(1 for argv in harness.calls() if argv[2:3] == ["answer"]) == 2


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_each_of_several_questions_is_asked_in_turn(harness: Harness) -> None:
    from tests.mirasim_stub_cli import QUESTION

    second = {**QUESTION, "promptId": "claude-control-1:1", "questions": [{"id": "q1", "text": "Why?", "type": "text"}]}
    harness.use("asks")
    (harness.tmp_path / "stub" / "questions.json").write_text(json.dumps([QUESTION, second]))
    await harness.ok("initialize", protocolVersion=1, clientCapabilities=FORMS)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")))
    first = await harness.asked("elicitation/create")
    await harness.respond(first, result={"action": "accept", "content": {"answer": "Red"}})
    then = await harness.asked("elicitation/create")
    assert then["params"]["requestedSchema"]["properties"]["answer"]["title"] == "Why?"
    await harness.respond(then, result={"action": "decline"})
    assert await asyncio.wait_for(turn, 30) == {"stopReason": "end_turn"}
    assert harness.replies()[-1] == "turn 1 of sess-1 got select=Red | skip=None"


@pytest.mark.slow
@pytest.mark.usefixtures("quick")
async def test_a_cancel_while_a_question_is_out_takes_the_question_back(harness: Harness) -> None:
    harness.use("asks")
    await harness.ok("initialize", protocolVersion=1, clientCapabilities=FORMS)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("paint it")))
    request = await harness.asked("elicitation/create")
    await harness.agent.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    assert await asyncio.wait_for(turn, 15) == {"stopReason": "cancelled"}
    retracted = [frame for frame in harness.updates if frame.get("method") == "$/cancel_request"]
    assert retracted == [{"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": request["id"]}}]


async def test_a_prompt_too_long_for_one_argument_travels_as_a_file(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "INLINE_TEXT_LIMIT", 10)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("x" * 50))
    reply = harness.replies()[-1]
    assert "files ['prompt.md:50']" in reply and "This message continues in the attached file prompt.md" in reply
    staged = next(arg for arg in harness.prompts()[0] if arg.startswith("--file="))
    assert not Path(staged.removeprefix("--file=")).exists()


async def test_a_local_link_is_attached_and_a_remote_one_stays_in_the_text(harness: Harness) -> None:
    notes = harness.tmp_path / "notes.md"
    notes.write_text("twelve bytes")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    prompt = [
        {"type": "text", "text": "read these"},
        {"type": "resource_link", "uri": notes.as_uri(), "name": "notes.md"},
        {"type": "resource_link", "uri": "https://example.com/spec", "name": "spec"},
    ]
    await harness.ok("session/prompt", sessionId=sid, prompt=prompt)
    reply = harness.replies()[-1]
    assert "files ['notes.md:12']" in reply and "[spec](https://example.com/spec)" in reply
    error = await harness.refused(
        "session/prompt", sessionId=sid, prompt=[{"type": "image", "data": "", "mimeType": "x"}]
    )
    assert error["code"] == INVALID_PARAMS


async def test_mcp_servers_handed_over_are_noted_once_not_dropped_silently(harness: Harness) -> None:
    servers = [{"name": "fs", "command": "fs-server", "args": [], "env": []}]
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=servers))["sessionId"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    await harness.ok("session/prompt", sessionId=sid, prompt=text("two"))
    first, second = harness.replies()
    assert "MCP servers fs were not handed to the agent" in first
    assert "MCP" not in second


async def test_a_deleted_session_is_forgotten(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    validate_def("DeleteSessionResponse", await harness.ok("session/delete", sessionId=sid))
    assert harness.store.get(sid) is None
    harness.use("ok")
    error = await harness.refused("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == RESOURCE_NOT_FOUND


async def test_an_agent_mirasim_lacks_or_has_not_installed_is_refused(tmp_path: Path) -> None:
    missing = Harness(tmp_path, agent="ghost")
    error = await missing.refused("session/new", cwd=str(tmp_path), mcpServers=[])
    assert error["code"] == INVALID_PARAMS and "has no agent 'ghost'" in error["message"]
    absent = Harness(tmp_path, agent="zcode")
    error = await absent.refused("session/new", cwd=str(tmp_path), mcpServers=[])
    assert "mirasim ui-cli cli install --agent zcode" in error["message"]


@pytest.mark.slow
async def test_a_cancel_ends_the_turn_and_everything_it_started(harness: Harness) -> None:
    harness.use("hang")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("wait")))
    marker = harness.tmp_path / "stub" / "grandchild.pid"
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())

    started = time.monotonic()
    assert (
        await harness.agent.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}) is None
    )
    assert await asyncio.wait_for(turn, 10) == {"stopReason": "cancelled"}
    # Inside the five seconds Raven's client waits for a cancelled turn to settle.
    assert time.monotonic() - started < 5
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the turn's grandchild outlived the cancel")


async def test_a_model_mirasim_added_since_the_menus_were_read_is_accepted(harness: Harness) -> None:
    """The adapter reads Mirasim's menus again before refusing a model it does not know."""
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    from tests.mirasim_stub_cli import CATALOG

    grown = {**CATALOG, "models": [*CATALOG["models"], {"id": "new-model", "label": "New"}]}
    (harness.tmp_path / "stub" / "catalog.json").write_text(json.dumps(grown))
    picked = await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="new-model")
    assert picked["configOptions"][0]["currentValue"] == "new-model"


async def test_a_failed_re_read_keeps_the_menus_in_hand(harness: Harness) -> None:
    first = await harness.agent.catalog()
    install(harness.tmp_path, "broken")
    assert await harness.agent.catalog(refresh=True) is first


async def test_a_failed_catalog_refresh_preserves_the_selected_model_and_route(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("first"))
    first = await harness.agent.catalog()
    install(harness.tmp_path, "nomenu")
    assert await harness.agent.catalog(refresh=True) is first
    await harness.ok("session/prompt", sessionId=sid, prompt=text("second"))
    assert harness.routes() == []
    assert "--model=opus" in harness.prompts()[-1]
    assert "--effort=high" in harness.prompts()[-1]


async def test_with_no_menus_in_hand_a_mirasim_that_cannot_list_its_agents_is_refused(tmp_path: Path) -> None:
    broken = Harness(tmp_path, mode="broken")
    assert (await broken.ok("initialize", protocolVersion=1))["agentInfo"]["version"] == "0.0.465"
    error = await broken.refused("session/new", cwd=str(tmp_path), mcpServers=[])
    assert error["code"] == INTERNAL_ERROR and "could not list its agents" in error["message"]


async def test_the_frame_loop_answers_each_request_and_a_line_it_cannot_read(tmp_path: Path) -> None:
    import io

    from raven.acp.mirasim.server import serve

    exe = install(tmp_path)
    reader = asyncio.StreamReader()
    out = io.BytesIO()
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}},
        {"jsonrpc": "2.0", "id": 3, "method": "no/such", "params": {}},
    ]
    reader.feed_data(b"not json\n" + b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
    reader.feed_eof()
    await serve(
        reader, out, agent="claude", cli=UiCli(executable=str(exe)), store=BindingStore(tmp_path / "store.json")
    )
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    by_id = {answer.get("id"): answer for answer in answers}
    assert by_id[None]["error"]["code"] == -32700
    assert by_id[1]["result"]["agentInfo"]["name"] == "mirasim-claude"
    assert "sessionId" in by_id[2]["result"]
    assert by_id[3]["error"]["code"] == -32601


def test_the_entry_refuses_an_agent_id_that_is_not_one(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        server_mod.main(["--agent", "Not An Id"])
    assert stopped.value.code == 2
    assert "is not a Mirasim agent id" in capsys.readouterr().err


def test_python_dash_m_runs_the_server_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(server_mod, "main", lambda: 0)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("raven.acp.mirasim", run_name="__main__")
    assert stopped.value.code == 0


async def test_a_load_in_the_same_process_drops_the_model_the_session_was_put_on(harness: Harness) -> None:
    """Raven re-asserts its choice on every route in, so a load answers on Mirasim's default."""
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="opus")
    await harness.ok("session/set_mode", sessionId=sid, modeId="max")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("one"))
    loaded = await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    assert loaded["configOptions"][0]["currentValue"] == "sonnet"
    assert loaded["modes"]["currentModeId"] == "high"
    await harness.ok("session/prompt", sessionId=sid, prompt=text("two"))
    assert harness.replies()[-1] == "turn 2 of sess-1 on sonnet effort high files []: two"


async def test_idle_sessions_past_the_cap_are_let_go_and_a_recorded_one_comes_back(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_mod, "MAX_IDLE_SESSIONS", 2)
    talked = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.ok("session/prompt", sessionId=talked, prompt=text("hi"))
    silent = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    for _ in range(2):
        await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert talked not in harness.agent.sessions and silent not in harness.agent.sessions
    # The one that had a turn is on disk; the one that never spoke has nothing to load.
    await harness.ok("session/load", sessionId=talked, cwd=str(harness.tmp_path), mcpServers=[])
    error = await harness.refused("session/load", sessionId=silent, cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == RESOURCE_NOT_FOUND


async def test_frames_that_are_not_requests_are_answered_with_nothing(harness: Harness) -> None:
    assert await harness.agent.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None
    assert await harness.agent.handle({"jsonrpc": "2.0", "method": "no/such", "params": {}}) is None
    assert (
        await harness.agent.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "x"}}) is None
    )


async def test_an_unexpected_failure_preserves_the_original_error_text(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("server at http://127.0.0.1:1/?token=s3cr3t went away")

    monkeypatch.setattr(harness.agent, "new_session", boom)
    error = await harness.refused("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert error["code"] == INTERNAL_ERROR
    assert error["message"] == "server at http://127.0.0.1:1/?token=s3cr3t went away"
    assert await harness.agent.handle({"jsonrpc": "2.0", "method": "session/new", "params": {}}) is None


async def test_a_mirasim_that_cannot_say_its_version_still_initializes(tmp_path: Path) -> None:
    mute = Harness(tmp_path, mode="mute")
    assert (await mute.ok("initialize", protocolVersion=1))["agentInfo"]["version"] == "unknown"


async def test_a_mirasim_that_cannot_list_models_still_opens_a_session_with_no_menu(tmp_path: Path) -> None:
    nomenu = Harness(tmp_path, mode="nomenu")
    result = await nomenu.ok("session/new", cwd=str(tmp_path), mcpServers=[])
    assert "configOptions" not in result and "modes" not in result
    await nomenu.ok("session/prompt", sessionId=result["sessionId"], prompt=text("hi"))
    assert nomenu.replies() == ["turn 1 of sess-1 on - effort - files []: hi"]


async def test_a_menu_that_could_not_be_read_goes_out_marked_as_unread(tmp_path: Path) -> None:
    """The two empties are one shape on the wire, and only one is a fact.

    A reader that cannot tell them apart records a failed read as a measurement
    of "no models", and nothing invalidates a measurement -- so the row offers
    no model picker until somebody presses Test by hand. The mark is what makes
    the difference visible off this process.
    """
    nomenu = Harness(tmp_path, mode="nomenu")
    result = await nomenu.ok("session/new", cwd=str(tmp_path), mcpServers=[])
    assert result["_meta"]["raven"]["modelMenuUnread"] is True
    validate_def("NewSessionResponse", result)


async def test_a_menu_that_was_read_carries_no_unread_mark(harness: Harness) -> None:
    """Absent means measured, so an agent that answered says nothing extra."""
    result = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    assert "_meta" not in result or "raven" not in result.get("_meta", {})
    validate_def("NewSessionResponse", result)


async def test_menus_read_once_a_failed_read_is_not_cached_as_a_measurement(tmp_path: Path) -> None:
    """The unread flag clears itself the moment a read lands, without a restart."""
    flaky = Harness(tmp_path, mode="nomenu")
    first = await flaky.ok("session/new", cwd=str(tmp_path), mcpServers=[])
    assert first["_meta"]["raven"]["modelMenuUnread"] is True
    flaky.use("ok")
    second = await flaky.ok("session/new", cwd=str(tmp_path), mcpServers=[])
    assert "configOptions" in second and "_meta" not in second
    validate_def("NewSessionResponse", second)


async def test_malformed_requests_are_invalid_params(harness: Harness) -> None:
    assert (await harness.refused("session/new", mcpServers=[]))["code"] == INVALID_PARAMS
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path)))["sessionId"]
    bad = [
        ("session/set_config_option", {"sessionId": sid, "configId": "model", "value": ""}),
        ("session/prompt", {"sessionId": sid, "prompt": []}),
        ("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "   "}]}),
    ]
    for method, params in bad:
        assert (await harness.refused(method, **params))["code"] == INVALID_PARAMS, method


async def test_embedded_text_and_attachment_only_prompts_reach_the_agent(harness: Harness) -> None:
    notes = harness.tmp_path / "notes.md"
    notes.write_text("abc")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    embedded = [{"type": "resource", "resource": {"uri": "mem://brief", "text": "the brief"}}]
    await harness.ok("session/prompt", sessionId=sid, prompt=embedded)
    assert harness.replies()[-1].endswith("files []: [mem://brief]\nthe brief")
    await harness.ok("session/prompt", sessionId=sid, prompt=[{"type": "resource_link", "uri": notes.as_uri()}])
    assert harness.replies()[-1].endswith("files ['notes.md:3']: See the attached files.")


async def test_a_turn_that_ends_neither_settled_nor_asking_is_a_failure_that_says_so(tmp_path: Path) -> None:
    unsettled = Harness(tmp_path, mode="unsettled")
    sid = (await unsettled.ok("session/new", cwd=str(tmp_path), mcpServers=[]))["sessionId"]
    error = await unsettled.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert "Mirasim reported the turn unsettled (phase 'running')" in error["message"]
    install(tmp_path, "garbage")
    error = await unsettled.refused("session/prompt", sessionId=sid, prompt=text("hi"))
    assert "`mirasim ui-cli prompt` exited 3 with no result" in error["message"]


async def test_a_turn_that_ran_is_answered_even_when_its_record_cannot_be_saved(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(binding: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.store, "put", refuse)
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    assert await harness.ok("session/prompt", sessionId=sid, prompt=text("hi")) == {"stopReason": "end_turn"}


async def test_a_cancel_with_nothing_running_changes_nothing(harness: Harness) -> None:
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    await harness.agent.cancel({"sessionId": sid})
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    assert harness.replies() == ["turn 1 of sess-1 on sonnet effort high files []: hi"]


@pytest.mark.slow
async def test_deleting_a_session_mid_turn_ends_the_turn(harness: Harness) -> None:
    harness.use("hang")
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    turn = asyncio.create_task(harness.ok("session/prompt", sessionId=sid, prompt=text("wait")))
    marker = harness.tmp_path / "stub" / "grandchild.pid"
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await harness.ok("session/delete", sessionId=sid)
    assert await asyncio.wait_for(turn, 10) == {"stopReason": "cancelled"}


@pytest.mark.slow
async def test_a_client_that_hangs_up_mid_turn_takes_the_turn_down(tmp_path: Path) -> None:
    import io

    from raven.acp.mirasim.server import serve

    exe = install(tmp_path, "hang")
    reader = asyncio.StreamReader()
    out = io.BytesIO()
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}},
    ]
    reader.feed_data(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
    serving = asyncio.create_task(
        serve(reader, out, agent="claude", cli=UiCli(executable=str(exe)), store=BindingStore(tmp_path / "s.json"))
    )
    deadline = time.monotonic() + 15
    while b'"sessionId"' not in out.getvalue() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    sid = json.loads(out.getvalue().splitlines()[0])["result"]["sessionId"]
    prompt = {"jsonrpc": "2.0", "id": 2, "method": "session/prompt", "params": {"sessionId": sid, "prompt": text("x")}}
    reader.feed_data(json.dumps(prompt).encode() + b"\n")
    marker = tmp_path / "stub" / "grandchild.pid"
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())
    reader.feed_eof()
    await asyncio.wait_for(serving, 15)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}} in answers
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the turn's grandchild outlived the client")


async def test_a_frame_the_client_is_gone_for_is_dropped_not_raised(tmp_path: Path) -> None:
    from raven.acp.mirasim.server import serve

    class Gone:
        def write(self, data: bytes) -> int:
            raise BrokenPipeError("client went away")

        def flush(self) -> None:
            pass

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n')
    reader.feed_eof()
    await serve(
        reader,
        Gone(),
        agent="claude",
        cli=UiCli(executable=str(install(tmp_path))),
        store=BindingStore(tmp_path / "s.json"),
    )


async def test_the_entry_serves_stdin_until_it_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    import contextlib
    import io

    exe = install(tmp_path)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}\n')
    reader.feed_eof()

    @contextlib.asynccontextmanager
    async def fake_stdin():
        yield reader

    monkeypatch.setattr(server_mod, "open_stdin", fake_stdin)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    out = io.BytesIO()
    await server_mod._main(argparse.Namespace(agent="claude", executable=str(exe), verbose=False), out)
    assert json.loads(out.getvalue().splitlines()[0])["result"]["agentInfo"]["name"] == "mirasim-claude"


def test_main_wires_the_logger_the_stdout_claim_and_the_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib
    import io

    calls: list[str] = []

    class Logger:
        def remove(self) -> None:
            calls.append("remove")

        def add(self, sink: Any, level: str) -> None:
            calls.append(f"add {level}")

    @contextlib.contextmanager
    def claim():
        calls.append("claim")
        yield io.BytesIO()

    def run(coro: Any) -> None:
        calls.append("run")
        coro.close()

    monkeypatch.setattr(server_mod, "logger", Logger())
    monkeypatch.setattr(server_mod, "claim_stdout", claim)
    monkeypatch.setattr(server_mod.asyncio_runner, "run", run)
    assert server_mod.main(["--agent", "claude", "--verbose"]) == 0
    assert calls == ["remove", "add INFO", "claim", "run"]


class ScriptedCli:
    """Commands to a turn's server answered from a script, so the watcher is tested on its own."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked: list[list[str]] = []

    async def query(self, args: list[str], *, port: int | None = None, timeout_s: float = 0.0) -> Any:
        self.asked.append([f"--port={port}", *args])
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Announced:
    """A running turn whose server announced ``port``."""

    def __init__(self, port: int | None) -> None:
        self.port = port

    async def announced(self) -> int | None:
        return self.port


def watcher(tmp_path: Path, cli: ScriptedCli) -> tuple[MirasimAgent, server_mod.Session]:
    agent = MirasimAgent(agent="claude", cli=cli, store=BindingStore(tmp_path / "s.json"), emit=lambda frame: None)  # type: ignore[arg-type]
    return agent, server_mod.Session(id="acp-1", cwd=str(tmp_path))


async def test_a_turn_whose_server_never_announced_itself_is_not_watched(tmp_path: Path) -> None:
    cli = ScriptedCli()
    agent, session = watcher(tmp_path, cli)
    await asyncio.wait_for(agent._watch(session, Announced(None), []), 5)  # type: ignore[arg-type]
    assert cli.asked == []


@pytest.mark.usefixtures("quick")
async def test_a_new_conversation_is_found_once_its_server_lists_it_running(tmp_path: Path) -> None:
    from tests.test_acp_mirasim_interactions import ask

    not_yet = {"responses": [{"type": "sessions", "sessions": [{"sessionKey": "claude:other"}]}]}
    listed = {"responses": [{"type": "sessions", "sessions": [{"sessionKey": "claude:k1", "transport": {}}]}]}
    live = {"responses": [{"type": "snapshot", "sessionKey": "claude:k1", "snapshot": {"interactions": [ask()]}}]}
    cli = ScriptedCli(not_yet, listed, live, {"answered": "claude-control-1"})
    agent, session = watcher(tmp_path, cli)
    notes: list[str] = []
    watching = asyncio.create_task(agent._watch(session, Announced(4970), notes))  # type: ignore[arg-type]
    deadline = time.monotonic() + 10
    while len(cli.asked) < 4 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    watching.cancel()
    await asyncio.gather(watching, return_exceptions=True)
    sent = [argv[1:3] for argv in cli.asked[:4]]
    assert sent == [
        ["raw", '{"type": "listSessions"}'],
        ["raw", '{"type": "listSessions"}'],
        ["raw", '{"type": "subscribe", "sessionKey": "claude:k1"}'],
        ["answer", "--prompt-id=claude-control-1"],
    ]
    assert cli.asked[3][-1] == "--action=skip" and all(argv[0] == "--port=4970" for argv in cli.asked)
    assert "takes no forms" in notes[0]


@pytest.mark.usefixtures("quick")
async def test_a_watcher_that_breaks_leaves_the_turn_running(tmp_path: Path) -> None:
    cli = ScriptedCli(RuntimeError("the frame was not JSON"))
    agent, session = watcher(tmp_path, cli)
    session.mirasim_key = "claude:k1"
    await asyncio.wait_for(agent._watch(session, Announced(4970), []), 5)  # type: ignore[arg-type]
    assert cli.asked[0][1:3] == ["raw", '{"type": "subscribe", "sessionKey": "claude:k1"}']


class LateAnnounced:
    """A running turn whose server says its port only after ``after`` seconds."""

    def __init__(self, port: int, after: float) -> None:
        self.port = port
        self.after = after

    async def announced(self) -> int | None:
        await asyncio.sleep(self.after)
        return self.port


@pytest.mark.usefixtures("quick")
async def test_a_server_slow_to_say_its_port_is_logged_and_then_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loguru import logger

    monkeypatch.setattr(server_mod, "ANNOUNCE_WARN_S", 0.05)
    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    cli = ScriptedCli()
    agent, session = watcher(tmp_path, cli)
    session.mirasim_key = "claude:k1"
    try:
        watching = asyncio.create_task(agent._watch(session, LateAnnounced(4970, 0.3), []))  # type: ignore[arg-type]
        deadline = time.monotonic() + 10
        while not cli.asked and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)
    finally:
        logger.remove(sink_id)
    assert any("has not said its port after" in record for record in records)
    assert cli.asked and cli.asked[0][0] == "--port=4970"


async def test_what_the_agent_asks_reaches_the_client_unchanged(tmp_path: Path) -> None:
    from raven.acp.capabilities import ClientCapabilities
    from raven.acp.mirasim.interactions import Reply
    from tests.test_acp_mirasim_interactions import approval, ask

    secret = "sk-or-v1-" + "a" * 32
    sent: list[dict[str, Any]] = []
    agent = MirasimAgent(agent="claude", cli=ScriptedCli(), store=BindingStore(tmp_path / "s.json"), emit=sent.append)  # type: ignore[arg-type]
    agent.client = ClientCapabilities.from_params({"clientCapabilities": FORMS})
    session = server_mod.Session(id="acp-1", cwd=str(tmp_path))

    question = ask(text=f"Use the key {secret}?")
    question["hint"] = "the server at http://127.0.0.1:1/?token=s3cr3t asked"
    tool = approval()
    tool["questions"][0]["text"] = (
        f'Claude wants to use Bash: {{"command": "curl -H \'Authorization: Bearer {secret}\'"}}'
    )
    for item, answer in ((question, {"action": "decline"}), (tool, {"outcome": {"outcome": "cancelled"}})):
        relay = asyncio.create_task(agent._relay(session, item, []))
        while not [frame for frame in sent if "id" in frame]:
            await asyncio.sleep(0.01)
        request = sent.pop()
        params = request["params"]
        if item["kind"] == "permission":
            assert params["toolCall"]["title"] == item["questions"][0]["text"]
        else:
            assert params["requestedSchema"]["properties"]["answer"]["title"] == item["questions"][0]["text"]
            assert question["hint"] in params["message"]
        agent.outbound.resolve({"jsonrpc": "2.0", "id": request["id"], "result": answer})
        assert await asyncio.wait_for(relay, 5) in (Reply("skip"), Reply("cancel"))


async def opened(harness: Harness, model: str | None = None) -> str:
    """A new session, put on ``model`` when one is named."""
    sid = (await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[]))["sessionId"]
    if model is not None:
        await harness.ok("session/set_config_option", sessionId=sid, configId="model", value=model)
    return sid


async def test_an_own_model_chosen_on_the_platform_starts_its_conversation_there(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies() == [
        "turn 1 of sess-1 on opus via platform effort high files []: hi",
        "turn 2 of sess-1 on opus via platform effort high files []: again",
    ]
    first, second = harness.prompts()
    assert "--model=opus" in first and "--route=platform" in first
    # The conversation keeps its route, so the next turn neither names nor switches it.
    assert not any(arg.startswith("--route") for arg in second)
    assert harness.routes() == []


async def test_choosing_the_other_route_switches_the_conversation_before_its_next_turn(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies()[-1] == "turn 2 of sess-1 on opus via own effort high files []: again"
    assert harness.routes() == [["--session=sess-1", "--to=own"]]
    verbs = [command[0] for command in harness.commands() if command[0] in ("prompt", "session")]
    assert verbs == ["prompt", "session", "prompt"]


async def test_undoing_a_choice_hands_the_route_back_to_mirasim(harness: Harness) -> None:
    opened_session = await harness.ok("session/new", cwd=str(harness.tmp_path), mcpServers=[])
    sid = opened_session["sessionId"]
    # Raven undoes a choice by sending back the value the session first reported.
    baseline = opened_session["configOptions"][0]["currentValue"]
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value=baseline)
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies()[-1] == "turn 2 of sess-1 on sonnet effort high files []: again"
    assert harness.routes() == [["--session=sess-1", "--to=default"]]


async def test_a_model_only_the_platform_runs_leaves_the_route_to_mirasim(harness: Harness) -> None:
    sid = await opened(harness, "own:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="glm-5.3-flash")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies() == [
        "turn 1 of sess-1 on opus via own effort high files []: hi",
        "turn 2 of sess-1 on glm-5.3-flash effort - files []: again",
    ]
    assert harness.routes() == [["--session=sess-1", "--to=default"]]


@pytest.mark.slow
async def test_another_process_continues_on_the_route_the_conversation_is_on(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    harness.use("ok")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies()[-1] == "turn 2 of sess-1 on opus via platform effort high files []: again"
    assert harness.routes() == []


@pytest.mark.slow
async def test_a_conversation_with_no_recorded_route_is_put_on_one(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    # A record written before routes were kept says nothing about the one the conversation is on.
    doc = json.loads(harness.store.path.read_text())
    del doc["sessions"][sid]["route"]
    harness.store.path.write_text(json.dumps(doc))
    harness.use("ok")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.routes() == [["--session=sess-1", "--to=platform"]]


@pytest.mark.slow
async def test_an_agent_the_platform_does_not_run_is_never_given_a_route(tmp_path: Path) -> None:
    harness = Harness(tmp_path, agent="qwen")
    sid = await opened(harness, "opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="glm-5.3-flash")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    harness.use("ok", agent="qwen")
    await harness.ok("session/load", sessionId=sid, cwd=str(tmp_path), mcpServers=[])
    await harness.ok("session/prompt", sessionId=sid, prompt=text("once more"))
    assert not any(arg.startswith("--route") for args in harness.prompts() for arg in args)
    assert harness.routes() == []
    assert (await harness.refused("session/set_config_option", sessionId=sid, configId="model", value="platform:opus"))[
        "code"
    ] == INVALID_PARAMS


@pytest.mark.slow
async def test_a_route_mirasim_did_not_switch_refuses_the_turn_and_is_switched_next_time(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    harness.use("noroute")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("again"))
    assert error["code"] == INTERNAL_ERROR and "own" in error["message"]
    assert len(harness.prompts()) == 1
    # Not known to be on either route any more, so the next turn switches it whatever it asks for.
    stored = harness.store.get(sid)
    assert stored is not None and stored.route is None
    harness.use("ok")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.routes()[-1] == ["--session=sess-1", "--to=platform"]
    assert harness.replies()[-1] == "turn 2 of sess-1 on opus via platform effort high files []: again"


@pytest.mark.slow
async def test_a_forgotten_conversation_is_begun_again_on_the_route_chosen(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    harness.use("forget")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    assert harness.replies()[-1].startswith("turn 1 of sess-2 on opus via platform effort high files []: again")
    assert "--route=platform" in harness.prompts()[-1]
    stored = harness.store.get(sid)
    assert stored is not None and (stored.mirasim_key, stored.route) == ("sess-2", "platform")


@pytest.mark.slow
async def test_a_route_another_process_switched_is_read_before_the_turn(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    held = harness.agent
    # A second adapter process continues the same conversation on the other route.
    harness.use("ok")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("again"))
    # The first, still holding the session from before, is asked for the platform again.
    harness.agent = held
    await harness.ok("session/prompt", sessionId=sid, prompt=text("and again"))
    assert harness.routes() == [["--session=sess-1", "--to=own"], ["--session=sess-1", "--to=platform"]]
    assert harness.replies()[-1] == "turn 3 of sess-1 on opus via platform effort high files []: and again"


@pytest.mark.slow
async def test_a_cancel_while_the_route_is_switched_ends_the_switch_and_runs_no_turn(harness: Harness) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    harness.use("routehang")
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    turn = asyncio.create_task(harness.call("session/prompt", sessionId=sid, prompt=text("again")))
    deadline = time.monotonic() + 20
    while not harness.routes() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert harness.routes(), "the switch never started"
    started = time.monotonic()
    await harness.agent.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    response = await asyncio.wait_for(turn, 10)
    assert response["result"] == {"stopReason": "cancelled"}
    assert time.monotonic() - started < 5
    assert len(harness.prompts()) == 1
    stored = harness.store.get(sid)
    assert stored is not None and stored.route is None


@pytest.mark.slow
async def test_a_switch_that_never_answers_refuses_the_turn(harness: Harness, monkeypatch) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    harness.use("routehang")
    monkeypatch.setattr(server_mod, "QUERY_TIMEOUT_S", 1.0)
    await harness.ok("session/load", sessionId=sid, cwd=str(harness.tmp_path), mcpServers=[])
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("again"))
    assert error["code"] == INTERNAL_ERROR and "did not answer within 1s" in error["message"]
    assert len(harness.prompts()) == 1


async def test_a_switch_that_cannot_start_refuses_the_turn(harness: Harness, tmp_path: Path) -> None:
    sid = await opened(harness, "platform:opus")
    await harness.ok("session/prompt", sessionId=sid, prompt=text("hi"))
    await harness.ok("session/set_config_option", sessionId=sid, configId="model", value="own:opus")
    harness.agent.cli = UiCli(executable=str(tmp_path / "no-such-mirasim"))
    error = await harness.refused("session/prompt", sessionId=sid, prompt=text("again"))
    assert error["code"] == INTERNAL_ERROR and "the own route" in error["message"]
    assert len(harness.prompts()) == 1
