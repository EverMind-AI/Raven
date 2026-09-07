"""``session/set_mode`` and the ``modes`` object on session responses (raven/acp/methods.py).

The protocol half of session modes. A build that declares none keeps answering
method-not-found and its session responses carry no ``modes`` key -- the
pre-modes wire, byte for byte. A build that declares some advertises them on
every route into a session, switches a session on request, and hands the chosen
profile to the engine as that session's policy -- at the switch, and again
before every turn, because the host re-sends the mode on every route in and the
engine must never run a turn on a stale profile.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.acp import protocol
from raven.acp.methods import AcpMethodError, AcpMethods
from raven.acp.modes import build_session_modes
from raven.acp.updates import UpdateTranslator
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import Config
from raven.session.manager import SessionManager
from raven.tui_rpc.dispatcher import Dispatcher

CATALOGUE = {
    "low": {"name": "Low", "description": "quick", "reasoningEffort": "low"},
    "high": {"name": "High", "description": "today's behaviour"},
    "max": {"name": "Max", "description": "deepest", "reasoningEffort": "max"},
}


class _Engine:
    """What the methods layer reads off the engine, plus the policy seam it writes."""

    def __init__(self, workspace) -> None:
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.policies: list[tuple[str, str | None]] = []
        self.cleared: list[str] = []
        self.pins: list[str] = []
        self.unpins: list[str] = []

    def set_session_policy(self, session_key: str, *, reasoning_effort: str | None = None) -> None:
        self.policies.append((session_key, reasoning_effort))

    def clear_session_policy(self, session_key: str) -> None:
        self.cleared.append(session_key)

    def pin_session_policy(self, session_key: str) -> None:
        self.pins.append(session_key)

    def unpin_session_policy(self, session_key: str) -> None:
        self.unpins.append(session_key)


class _Stack:
    """The RPC handlers the ACP layer calls, on a real dispatcher.

    ``turn.send`` settles the turn it accepted by feeding the translator a
    terminal error event, the same way the failure-settles tests do, so a
    ``session/prompt`` returns instead of waiting on the engine's stream.
    """

    def __init__(self, engine: _Engine) -> None:
        self.engine = engine
        self.dispatcher = Dispatcher()
        self.translator: UpdateTranslator | None = None
        self.sent: list[dict[str, Any]] = []
        self._next_sub = 0
        self.dispatcher.register("turn.subscribe", self._subscribe)
        self.dispatcher.register("turn.unsubscribe", self._unsubscribe)
        self.dispatcher.register("turn.cancel", self._cancel)
        self.dispatcher.register("session.resume", self._resume)
        self.dispatcher.register("turn.send", self._send)

    async def _subscribe(self, params: dict) -> dict:
        self._next_sub += 1
        return {"subscription_id": f"sub-{self._next_sub}"}

    async def _unsubscribe(self, params: dict) -> dict:
        return {"unsubscribed": True}

    async def _cancel(self, params: dict) -> dict:
        return {"cancelled": False}

    async def _resume(self, params: dict) -> dict:
        return {"session_id": params.get("session_id"), "info": {}, "messages": []}

    async def _send(self, params: dict) -> dict:
        self.sent.append(
            {
                "params": params,
                "policies_so_far": list(self.engine.policies),
                "pins_so_far": list(self.engine.pins),
            }
        )
        assert self.translator is not None
        event = {
            "subscription_id": f"sub-{self._next_sub}",
            "event": {
                "type": "error",
                "payload": {"code": -32099, "message": "turn_failed", "reason": "test", "turn_id": "t1"},
            },
        }
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(
                self.translator.send_frame({"jsonrpc": "2.0", "method": "event", "params": event})
            )
        )
        return {"turn_id": "t1"}


class _Rig:
    def __init__(self, methods: AcpMethods, stack: _Stack, engine: _Engine, modes, tmp_path) -> None:
        self.methods = methods
        self.stack = stack
        self.engine = engine
        self.modes = modes
        self.tmp_path = tmp_path

    async def call(self, method: str, params: dict | None = None, *, request_id: int = 1):
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self) -> None:
        await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def new_session(self) -> dict:
        response = await self.call("session/new", {"cwd": str(self.tmp_path / "project"), "mcpServers": []})
        assert "error" not in response, response
        return response["result"]

    async def set_mode(self, session_id: str | None, mode_id: str | None) -> dict:
        params: dict[str, Any] = {}
        if session_id is not None:
            params["sessionId"] = session_id
        if mode_id is not None:
            params["modeId"] = mode_id
        return await self.call("session/set_mode", params)


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    from raven.config import load_config

    config = load_config()
    monkeypatch.setattr(type(config), "workspace_path", property(lambda self: tmp_path / "ws"))
    monkeypatch.setattr("raven.config.load_config", lambda: config)
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)

    def _make(catalogue: dict | None, default: str | None = "high") -> _Rig:
        block: dict = {}
        if catalogue is not None:
            block = {"modes": catalogue, "defaultMode": default}
        modes = build_session_modes(Config.model_validate({"acp": block}))
        engine = _Engine(tmp_path / "ws")
        stack = _Stack(engine)
        translator = UpdateTranslator(emit=lambda frame: None)
        stack.translator = translator
        methods = AcpMethods(
            dispatcher=stack.dispatcher,
            translator=translator,
            emit=lambda frame: None,
            agent_loop=engine,
            modes=modes,
        )
        return _Rig(methods, stack, engine, modes, tmp_path)

    return _make


def _error(response: dict) -> dict:
    assert "error" in response, response
    return response["error"]


class TestWithoutDeclaredModes:
    async def test_set_mode_stays_not_implemented(self, make_rig) -> None:
        rig = make_rig(None)
        await rig.handshake()
        session = await rig.new_session()

        error = _error(await rig.set_mode(session["sessionId"], "low"))

        assert error["code"] == protocol.METHOD_NOT_FOUND

    async def test_session_responses_carry_no_modes_key(self, make_rig) -> None:
        rig = make_rig(None)
        await rig.handshake()
        session = await rig.new_session()
        assert "modes" not in session

        loaded = await rig.call(
            "session/load", {"sessionId": session["sessionId"], "cwd": str(rig.tmp_path / "project"), "mcpServers": []}
        )
        assert loaded["result"] == {}

    async def test_a_prompt_records_no_policy(self, make_rig) -> None:
        rig = make_rig(None)
        await rig.handshake()
        session = await rig.new_session()

        response = await rig.call(
            "session/prompt", {"sessionId": session["sessionId"], "prompt": [{"type": "text", "text": "hi"}]}
        )

        assert response["result"]["stopReason"] == "end_turn"
        assert rig.engine.policies == []


class TestWithDeclaredModes:
    async def test_session_new_carries_the_mode_state(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()

        session = await rig.new_session()

        assert session["sessionId"]
        assert session["modes"] == {
            "currentModeId": "high",
            "availableModes": [
                {"id": "low", "name": "Low", "description": "quick"},
                {"id": "high", "name": "High", "description": "today's behaviour"},
                {"id": "max", "name": "Max", "description": "deepest"},
            ],
        }

    async def test_set_mode_switches_the_session_and_records_its_policy(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        session = await rig.new_session()
        sid = session["sessionId"]

        response = await rig.set_mode(sid, "low")

        assert response["result"] == {}
        assert rig.engine.policies == [(sid, "low")]
        assert rig.modes.current(sid) == "low"

    async def test_the_switch_is_scoped_to_its_session(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        first = (await rig.new_session())["sessionId"]
        await rig.set_mode(first, "max")

        second = await rig.new_session()

        assert second["modes"]["currentModeId"] == "high"
        assert rig.engine.policies == [(first, "max")]

    async def test_load_and_resume_report_the_current_mode(self, make_rig) -> None:
        """A client reconnecting to a session it did not open has no other way to
        learn which mode it is in."""
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "low")
        params = {"sessionId": sid, "cwd": str(rig.tmp_path / "project"), "mcpServers": []}

        loaded = await rig.call("session/load", params)
        resumed = await rig.call("session/resume", params)

        assert loaded["result"]["modes"]["currentModeId"] == "low"
        assert resumed["result"]["modes"]["currentModeId"] == "low"
        assert len(loaded["result"]["modes"]["availableModes"]) == 3

    async def test_the_default_mode_moves_no_knob(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "low")

        await rig.set_mode(sid, "high")

        assert rig.engine.policies[-1] == (sid, None)

    async def test_an_unknown_mode_is_invalid_params_naming_the_catalogue(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]

        error = _error(await rig.set_mode(sid, "turbo"))

        assert error["code"] == protocol.INVALID_PARAMS
        assert error["data"]["field"] == "modeId"
        assert error["data"]["availableModes"] == ["low", "high", "max"]
        assert rig.modes.current(sid) == "high"
        assert rig.engine.policies == []

    async def test_a_missing_mode_id_is_invalid_params(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]

        error = _error(await rig.set_mode(sid, None))

        assert error["code"] == protocol.INVALID_PARAMS
        assert error["data"]["field"] == "modeId"

    async def test_an_unknown_session_is_resource_not_found(self, make_rig) -> None:
        """Looked up before the mode: a mode error about a session that does not
        exist would send the client debugging the wrong thing."""
        rig = make_rig(CATALOGUE)
        await rig.handshake()

        error = _error(await rig.set_mode("acp:nope", "turbo"))

        assert error["code"] == protocol.RESOURCE_NOT_FOUND

    async def test_a_prompt_reasserts_the_policy_before_the_turn_starts(self, make_rig) -> None:
        """The engine must never start a turn on a stale profile, so the session's
        current profile is handed over again right before ``turn.send``."""
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "low")

        response = await rig.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})

        assert response["result"]["stopReason"] == "end_turn"
        assert len(rig.stack.sent) == 1
        assert rig.stack.sent[0]["policies_so_far"][-1] == (sid, "low")
        assert rig.engine.policies == [(sid, "low"), (sid, "low")]

    async def test_a_prompt_pins_the_submitted_turns_policy(self, make_rig) -> None:
        """The pin lands after the policy reassert and before turn.send, so a
        set_mode racing the submission window cannot re-effort this turn."""
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "low")

        response = await rig.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})

        assert response["result"]["stopReason"] == "end_turn"
        assert rig.engine.pins == [sid]
        assert rig.stack.sent[0]["pins_so_far"] == [sid]
        assert rig.engine.unpins == []

    async def test_a_refused_turn_send_unpins(self, make_rig, monkeypatch) -> None:
        """A refused turn never starts, so its pinned snapshot must not leak
        onto the session's next turn."""
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "low")

        original = rig.methods._call

        async def refuse(method: str, params: dict) -> dict:
            if method == "turn.send":
                raise AcpMethodError(-32003, "a turn is already running")
            return await original(method, params)

        monkeypatch.setattr(rig.methods, "_call", refuse)
        response = await rig.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})

        assert response["result"]["stopReason"] == "end_turn"
        assert rig.engine.pins == [sid]
        assert rig.engine.unpins == [sid]

    async def test_a_prompt_on_the_default_mode_declares_the_empty_policy(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]

        await rig.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})

        assert rig.engine.policies == [(sid, None)]

    async def test_closing_a_session_forgets_its_mode_and_policy(self, make_rig) -> None:
        rig = make_rig(CATALOGUE)
        await rig.handshake()
        sid = (await rig.new_session())["sessionId"]
        await rig.set_mode(sid, "max")

        response = await rig.call("session/close", {"sessionId": sid})

        assert response["result"] == {}
        assert rig.engine.cleared == [sid]
        assert rig.modes.current(sid) == "high"
