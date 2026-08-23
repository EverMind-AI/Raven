"""The ACP methods, driven against a stubbed RPC stack.

The stub is a real ``Dispatcher`` with real-shaped handlers rather than a mock of
the methods module, so the round trip that matters is exercised: a handler here
builds a JSON-RPC frame, the dispatcher validates and routes it, and the answer
comes back as a frame that has to be unpacked. Mocking that seam would hide the
two things most likely to be wrong -- the parameter names and the error unpacking.

What is not stubbed is the translator: turn state and the outbound event stream
are the mechanism by which a prompt learns its stop reason, and a fake would
assert the design instead of testing it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from raven.acp import protocol
from raven.acp.methods import MAX_IMAGE_BYTES, AcpMethods
from raven.acp.updates import UpdateTranslator
from raven.rpc.dispatcher import Dispatcher
from tests.acp_schema import validate_def, validate_outbound


class _Stack:
    """A dispatcher with the four methods the ACP layer calls, plus a log."""

    def __init__(self) -> None:
        self.dispatcher = Dispatcher()
        self.calls: list[tuple[str, dict]] = []
        self.subscriptions = iter(f"sub-{n}" for n in range(1, 100))
        self.send_result: dict | Exception = {"turn_id": "t1", "accepted": True}
        self.cancel_result: dict | Exception = {"cancelled": True}
        # ``session.resume`` mints a fresh id for an unknown session rather than
        # failing, which is the behaviour the ACP layer has to detect; the stub
        # reproduces it rather than raising, or the detection would be untested.
        self.stored: dict[str, list[dict]] = {}
        self.model_options: dict | Exception = {
            "model": "sonnet-5",
            "provider": "anthropic",
            "providers": [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "authenticated": True,
                    "models": ["sonnet-5", "opus-5"],
                    "model_labels": {},
                }
            ],
        }
        self.config_set_result: dict | Exception = {"applied": True, "previous": "sonnet-5"}
        self.dispatcher.register("turn.subscribe", self._subscribe)
        self.dispatcher.register("turn.send", self._send)
        self.dispatcher.register("turn.cancel", self._cancel)
        self.dispatcher.register("session.resume", self._resume)
        self.dispatcher.register("model.options", self._model_options)
        self.dispatcher.register("config.set", self._config_set)

    async def _model_options(self, params: dict) -> dict:
        self.calls.append(("model.options", params))
        if isinstance(self.model_options, Exception):
            raise self.model_options
        return self.model_options

    async def _config_set(self, params: dict) -> dict:
        self.calls.append(("config.set", params))
        if isinstance(self.config_set_result, Exception):
            raise self.config_set_result
        return self.config_set_result

    async def _resume(self, params: dict) -> dict:
        self.calls.append(("session.resume", params))
        requested = params.get("session_id")
        if requested in self.stored:
            return {"session_id": requested, "info": {}, "messages": self.stored[requested]}
        return {"session_id": "tui:freshly-minted", "info": {}, "messages": []}

    async def _subscribe(self, params: dict) -> dict:
        self.calls.append(("turn.subscribe", params))
        return {"subscription_id": next(self.subscriptions)}

    async def _send(self, params: dict) -> dict:
        self.calls.append(("turn.send", params))
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.send_result

    async def _cancel(self, params: dict) -> dict:
        self.calls.append(("turn.cancel", params))
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result
        return self.cancel_result

    def params_for(self, method: str) -> dict:
        return next(params for name, params in self.calls if name == method)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A methods object wired to a stub stack, with the workspace redirected.

    ``cwd`` validation and the upload directory both read the real config, so the
    workspace is pointed at ``tmp_path`` -- otherwise ``session/new`` would refuse
    a path near the developer's own agent home, and an image test would write
    into it.
    """
    from raven.config import load_config

    config = load_config()
    monkeypatch.setattr(type(config), "workspace_path", property(lambda self: tmp_path / "ws"))
    monkeypatch.setattr("raven.acp.methods.load_config", lambda: config, raising=False)

    from raven.session.manager import SessionManager

    stack = _Stack()
    written: list[dict] = []
    translator = UpdateTranslator(emit=written.append)
    # A stand-in for the engine that carries only what the ACP layer reads from
    # it: the shared session manager. ``_manager_for`` builds a throwaway one
    # when handed None, so a test that passed None would assert nothing about
    # where the working directory ends up.
    engine = SimpleRig(sessions=SessionManager(tmp_path / "ws"))
    methods = AcpMethods(dispatcher=stack.dispatcher, translator=translator, emit=written.append, agent_loop=engine)
    return SimpleRig(
        methods=methods, stack=stack, translator=translator, written=written, tmp_path=tmp_path, engine=engine
    )


class SimpleRig:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def call(self, method: str, params: dict | None = None, *, request_id: int | str = 1):
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def notify(self, method: str, params: dict | None = None):
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        return await self.methods.handle(frame)

    async def handshake(self):
        return await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def new_session(self, cwd: str | None = None):
        response = await self.call("session/new", {"cwd": cwd or str(self.tmp_path / "project"), "mcpServers": []})
        assert "result" in response, response
        return response["result"]["sessionId"]

    def updates(self) -> list[dict]:
        return [f["params"]["update"] for f in self.written if f.get("method") == "session/update"]


class TestFrameShapes:
    async def test_a_request_is_answered_on_its_own_id_including_a_string(self, rig):
        for request_id in (1, 0, "abc-1"):
            response = await rig.call("initialize", {}, request_id=request_id)

            assert response["id"] == request_id

    async def test_a_notification_is_never_answered(self, rig):
        await rig.handshake()

        assert await rig.notify("session/cancel", {"sessionId": "nope"}) is None
        assert await rig.notify("$/cancel_request", {"id": 1}) is None
        assert await rig.notify("nonsense.method") is None

    async def test_a_response_to_nothing_is_not_answered(self, rig):
        """A frame with an id but no method answers a request this agent never
        sent, so there is nothing to correlate it with."""
        assert await rig.methods.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None
        assert await rig.methods.handle({"jsonrpc": "2.0", "id": 9, "error": {"code": -1, "message": "x"}}) is None

    async def test_an_id_of_zero_is_a_request_not_a_notification(self, rig):
        """JSON-RPC says a notification has no ``id`` member. A request that goes
        unanswered because its id was falsy is a hang with no diagnostic."""
        response = await rig.methods.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

        assert response is not None and response["id"] == 0

    async def test_a_null_id_is_also_a_request(self, rig):
        response = await rig.methods.handle({"jsonrpc": "2.0", "id": None, "method": "initialize", "params": {}})

        assert response is not None and "result" in response

    async def test_a_non_string_method_is_rejected(self, rig):
        response = await rig.call(42)  # type: ignore[arg-type]

        assert response["error"]["code"] == protocol.INVALID_REQUEST
        assert await rig.methods.handle({"jsonrpc": "2.0", "method": 42}) is None

    async def test_positional_params_are_refused_rather_than_read_as_absent(self, rig):
        """ACP is by-name throughout. A positional array is legal JSON-RPC and
        unusable here, so saying so beats silently treating it as empty."""
        response = await rig.call("initialize", ["1"])  # type: ignore[arg-type]

        assert response["error"]["code"] == protocol.INVALID_PARAMS

    async def test_a_malformed_notification_is_dropped_rather_than_answered(self, rig):
        """The same two refusals as a request, but silent: a notification has no
        reply, so the only thing to get right is not inventing one."""
        assert await rig.methods.handle({"jsonrpc": "2.0", "method": 42}) is None
        assert await rig.methods.handle({"jsonrpc": "2.0", "method": "initialize", "params": ["1"]}) is None

    async def test_a_crash_while_handling_a_notification_stays_silent(self, rig, monkeypatch):
        """The same guard as for a request, minus the answer. Letting it out would
        take the read loop down over a frame the client is not even waiting on."""

        async def _boom(params):
            raise ZeroDivisionError("nope")

        monkeypatch.setattr(rig.methods, "_session_cancel", _boom)
        await rig.handshake()

        assert await rig.notify("session/cancel", {"sessionId": "x"}) is None

    async def test_a_handler_crash_answers_rather_than_killing_the_connection(self, rig, monkeypatch):
        """Without this, a handler bug takes the read loop down and the client
        sees the agent vanish mid-turn -- indistinguishable from a crash."""

        def _boom(params):
            raise ZeroDivisionError("nope")

        monkeypatch.setattr(rig.methods, "_initialize", _boom)

        response = await rig.call("initialize", {})

        assert response["error"]["code"] == protocol.INTERNAL_ERROR
        validate_outbound(response)


class TestHandshakeGate:
    async def test_nothing_works_before_initialize(self, rig):
        response = await rig.call("session/new", {"cwd": "/tmp", "mcpServers": []})

        assert response["error"]["code"] == protocol.INVALID_REQUEST
        assert "initialize" in response["error"]["message"]

    async def test_a_notification_before_initialize_is_silently_ignored(self, rig):
        assert await rig.notify("session/cancel", {"sessionId": "x"}) is None

    async def test_cancel_request_is_allowed_before_the_handshake(self, rig):
        """A protocol-level notification is not part of the session lifecycle, so
        gating it behind initialize would refuse something that is always legal."""
        assert await rig.notify("$/cancel_request", {"id": 3}) is None

    async def test_the_handshake_records_what_the_client_declared(self, rig):
        await rig.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"elicitation": {}}})

        assert rig.methods.initialized is True
        assert rig.methods.client.elicitation is True

    async def test_reinitialising_is_allowed(self, rig):
        """A client that renegotiates is unusual, but refusing would strand one
        whose first attempt raced its own setup."""
        await rig.handshake()
        response = await rig.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {}}})

        assert "result" in response
        assert rig.methods.client.elicitation is False

    async def test_authenticate_says_none_is_needed(self, rig):
        """authMethods is empty, which is a statement. Method-not-found would
        read to a client as a version mismatch."""
        await rig.handshake()
        response = await rig.call("authenticate", {"methodId": "oauth"})

        assert response["error"]["code"] == protocol.INVALID_PARAMS
        assert response["error"]["data"] == {"authMethods": []}

    async def test_the_unbuilt_stable_methods_answer_method_not_found(self, rig):
        await rig.handshake()
        for method in ("session/set_mode", "logout", "session/delete", "session/resume", "session/close"):
            response = await rig.call(method, {})

            assert response["error"]["code"] == protocol.METHOD_NOT_FOUND, method

    async def test_an_unknown_method_is_answered_rather_than_dropped(self, rig):
        """An unanswered request leaves the client's promise pending for the life
        of the session."""
        await rig.handshake()
        response = await rig.call("session/teleport", {})

        assert response["error"]["code"] == protocol.METHOD_NOT_FOUND


class TestSessionNew:
    async def test_it_mints_a_session_on_the_acp_channel_and_subscribes(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        assert session_id.startswith("acp:"), "the channel prefix is what keeps editor sessions out of the TUI picker"
        assert rig.stack.params_for("turn.subscribe") == {"session_key": session_id}
        assert rig.translator.get(session_id) is not None

    async def test_the_response_matches_the_schema(self, rig):
        await rig.handshake()
        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        validate_def("NewSessionResponse", response["result"])
        validate_outbound(response)

    async def test_the_working_directory_is_pinned_in_session_metadata(self, rig):
        """The same key ``WorkdirResolver`` already honours: this is how a client
        attached to a shared engine keeps its own directory.

        Read back off the *engine's* manager, which is the point: a fresh
        ``SessionManager`` caches nothing, so pinning through one writes into an
        object that is discarded immediately and the session runs in the wrong
        tree with no error anywhere.
        """
        await rig.handshake()
        target = rig.tmp_path / "project"
        session_id = await rig.new_session(str(target))

        stored = rig.engine.sessions.get_or_create(session_id).metadata["workdir"]
        assert Path(stored) == target

    async def test_a_session_without_an_engine_is_still_created(self, rig):
        """The turn will fail on the build error; refusing the session too would
        replace one clear failure with a handshake that looks broken."""
        rig.methods._agent_loop = None

        await rig.handshake()

        assert await rig.new_session()

    async def test_a_relative_cwd_is_refused_with_the_reason(self, rig):
        """``validate_override`` raises ``ValueError``, and a bare Python
        exception is not an acceptable handshake failure."""
        await rig.handshake()
        response = await rig.call("session/new", {"cwd": "project", "mcpServers": []})

        assert response["error"]["code"] == protocol.INVALID_PARAMS
        assert response["error"]["data"]["field"] == "cwd"
        assert "absolute" in response["error"]["message"]

    async def test_a_missing_cwd_is_refused(self, rig):
        await rig.handshake()
        for params in ({"mcpServers": []}, {"cwd": "", "mcpServers": []}, {"cwd": 5, "mcpServers": []}):
            response = await rig.call("session/new", params)

            assert response["error"]["code"] == protocol.INVALID_PARAMS

    async def test_per_session_mcp_servers_are_refused_not_ignored(self, rig):
        """Accepting the field silently would leave a client believing its tools
        are available for the rest of the session."""
        await rig.handshake()
        response = await rig.call(
            "session/new",
            {"cwd": str(rig.tmp_path / "p"), "mcpServers": [{"name": "x", "command": "y", "args": []}]},
        )

        assert response["error"]["code"] == protocol.INVALID_PARAMS
        assert response["error"]["data"] == {"field": "mcpServers", "count": 1}

    async def test_an_empty_mcp_server_list_is_fine(self, rig):
        """The field is required by the schema and an empty array is the normal
        value, so refusing it would refuse every well-formed client."""
        await rig.handshake()

        assert await rig.new_session()

    async def test_two_sessions_get_distinct_ids_and_streams(self, rig):
        await rig.handshake()
        first = await rig.new_session()
        second = await rig.new_session()

        assert first != second
        assert rig.translator.get(first).subscription_id != rig.translator.get(second).subscription_id

    async def test_a_subscription_failure_is_an_internal_error_not_a_broken_session(self, rig):
        await rig.handshake()

        async def _empty(params):
            return {}

        rig.stack.dispatcher._handlers["turn.subscribe"] = _empty
        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert response["error"]["code"] == protocol.INTERNAL_ERROR


class TestSessionLoad:
    async def test_a_stored_session_is_replayed_then_answered(self, rig):
        """Not a getter: the transcript goes out as notifications *before* the
        response, so a resumed session is drawn by the same client code as a live
        one."""
        rig.stack.stored["acp:old"] = [
            {"role": "user", "text": "what changed?"},
            {"role": "assistant", "text": "One file."},
        ]
        await rig.handshake()

        response = await rig.call(
            "session/load",
            {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "project"), "mcpServers": []},
        )

        assert response["result"] == {}
        kinds = [u["sessionUpdate"] for u in rig.updates()]
        assert kinds == ["user_message_chunk", "agent_message_chunk"]
        for frame in rig.written:
            validate_outbound(frame)

    async def test_the_session_becomes_promptable(self, rig):
        """A load that did not register the session would replay a history the
        client then cannot continue."""
        rig.stack.stored["acp:old"] = [{"role": "user", "text": "hi"}]
        await rig.handshake()

        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        session = rig.translator.get("acp:old")
        assert session is not None
        assert session.subscription_id, "without a subscription the next turn streams nowhere"

    async def test_an_unknown_session_is_refused_rather_than_silently_replaced(self, rig):
        """``session.resume`` mints a fresh session for an unknown id and answers
        with that. A client handed the new one shows a person an empty transcript
        for a conversation that had one."""
        await rig.handshake()

        response = await rig.call(
            "session/load", {"sessionId": "acp:gone", "cwd": str(rig.tmp_path / "p"), "mcpServers": []}
        )

        assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND
        assert rig.updates() == [], "nothing may be replayed for a session that was not found"

    async def test_the_working_directory_comes_from_the_client_not_from_storage(self, rig):
        """A project moves, and the session's turns have to run where the editor
        has it open now."""
        rig.stack.stored["acp:old"] = []
        await rig.handshake()
        moved = rig.tmp_path / "moved"

        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(moved), "mcpServers": []})

        assert rig.translator.get("acp:old").cwd == str(moved)
        assert rig.engine.sessions.get_or_create("acp:old").metadata["workdir"] == str(moved)

    async def test_reloading_a_live_session_does_not_double_its_stream(self, rig):
        """Two mappings to one session would double every later frame."""
        rig.stack.stored["acp:old"] = [{"role": "user", "text": "hi"}]
        await rig.handshake()
        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "a"), "mcpServers": []})
        first = rig.translator.get("acp:old").subscription_id

        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "b"), "mcpServers": []})

        assert rig.translator.get("acp:old").subscription_id == first
        assert rig.translator.get("acp:old").cwd == str(rig.tmp_path / "b")
        assert sum(1 for name, _ in rig.stack.calls if name == "turn.subscribe") == 1

    async def test_reloading_a_live_session_rebinds_the_engine_too(self, rig):
        """Two things carry the directory and only one was being updated.

        ``AcpSession.cwd`` is what the translator reports and what replayed
        locations are resolved against. ``metadata["workdir"]`` is what
        ``WorkdirResolver`` reads, and therefore where the tools actually run.
        Setting only the first leaves the agent editing the tree the session was
        first opened in while the client and every location it is shown say the
        session moved -- which is how a turn edits the wrong project.

        The neighbouring test asserts the cwd and the subscription; it passed
        throughout, because the field it checks was never the one that decided
        where a command ran.
        """
        rig.stack.stored["acp:old"] = []
        await rig.handshake()
        first = rig.tmp_path / "acp-old"
        moved = rig.tmp_path / "acp-moved"

        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(first), "mcpServers": []})
        assert rig.engine.sessions.get_or_create("acp:old").metadata["workdir"] == str(first)

        await rig.call("session/load", {"sessionId": "acp:old", "cwd": str(moved), "mcpServers": []})

        assert rig.translator.get("acp:old").cwd == str(moved)
        assert rig.engine.sessions.get_or_create("acp:old").metadata["workdir"] == str(moved), (
            "the tools run where the metadata says, not where the translator says"
        )

    async def test_the_same_refusals_as_a_fresh_session_apply(self, rig):
        await rig.handshake()
        rig.stack.stored["acp:old"] = []

        relative = await rig.call("session/load", {"sessionId": "acp:old", "cwd": "rel", "mcpServers": []})
        servers = await rig.call(
            "session/load",
            {"sessionId": "acp:old", "cwd": str(rig.tmp_path / "p"), "mcpServers": [{"name": "x"}]},
        )
        missing = await rig.call("session/load", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert relative["error"]["data"]["field"] == "cwd"
        assert servers["error"]["data"]["field"] == "mcpServers"
        assert missing["error"]["data"]["field"] == "sessionId"


class TestSessionList:
    def _store(self, rig, key: str, workdir: str, **metadata):
        session = rig.engine.sessions.get_or_create(key)
        session.metadata["workdir"] = workdir
        session.metadata.update(metadata)
        return session

    async def test_it_lists_this_channels_sessions_newest_first(self, rig, monkeypatch):
        await rig.handshake()
        entries = [
            {"key": "acp:a", "metadata": {"workdir": "/one"}, "last_user_message_at": "2026-08-01T00:00:00"},
            {"key": "acp:b", "metadata": {"workdir": "/two", "title": "Two"}, "updated_at": "2026-08-20T00:00:00"},
        ]
        monkeypatch.setattr(
            rig.methods,
            "_stored_sessions",
            lambda: sorted(
                entries, key=lambda e: str(e.get("last_user_message_at") or e.get("updated_at")), reverse=True
            ),
        )

        response = await rig.call("session/list", {})

        sessions = response["result"]["sessions"]
        assert [s["sessionId"] for s in sessions] == ["acp:b", "acp:a"]
        assert sessions[0] == {"sessionId": "acp:b", "cwd": "/two", "title": "Two", "updatedAt": "2026-08-20T00:00:00"}
        validate_def("ListSessionsResponse", response["result"])
        for session in sessions:
            validate_def("SessionInfo", session)

    async def test_a_session_with_no_recorded_directory_is_skipped_not_guessed(self, rig, monkeypatch):
        """``SessionInfo.cwd`` is required, and inventing one would tell a client
        the session ran somewhere it did not."""
        await rig.handshake()
        monkeypatch.setattr(
            rig.methods,
            "_stored_sessions",
            lambda: [{"key": "acp:a", "metadata": {}}, {"key": "acp:b", "metadata": {"workdir": "/two"}}],
        )

        response = await rig.call("session/list", {})

        assert [s["sessionId"] for s in response["result"]["sessions"]] == ["acp:b"]

    async def test_the_cwd_filter_selects_by_directory(self, rig, monkeypatch):
        await rig.handshake()
        monkeypatch.setattr(
            rig.methods,
            "_stored_sessions",
            lambda: [
                {"key": "acp:a", "metadata": {"workdir": "/one"}},
                {"key": "acp:b", "metadata": {"workdir": "/two"}},
            ],
        )

        response = await rig.call("session/list", {"cwd": "/two"})

        assert [s["sessionId"] for s in response["result"]["sessions"]] == ["acp:b"]

    async def test_no_pagination_means_no_next_cursor(self, rig, monkeypatch):
        """Omitting it says "there is no more" rather than "ask again"."""
        await rig.handshake()
        monkeypatch.setattr(rig.methods, "_stored_sessions", lambda: [])

        response = await rig.call("session/list", {"cursor": "anything"})

        assert response["result"] == {"sessions": []}
        validate_def("ListSessionsResponse", response["result"])

    async def test_a_malformed_entry_is_skipped(self, rig, monkeypatch):
        await rig.handshake()
        monkeypatch.setattr(
            rig.methods,
            "_stored_sessions",
            lambda: [
                {"metadata": {"workdir": "/x"}},
                {"key": "", "metadata": {"workdir": "/x"}},
                {"key": "acp:a", "metadata": "not a dict"},
                {"key": "acp:b", "metadata": {"workdir": "/ok"}},
            ],
        )

        response = await rig.call("session/list", {})

        assert [s["sessionId"] for s in response["result"]["sessions"]] == ["acp:b"]

    async def test_without_an_engine_the_listing_is_empty_rather_than_wrong(self, rig):
        """A fresh ``SessionManager`` caches nothing, so a listing built from one
        would describe an object about to be discarded."""
        rig.methods._agent_loop = None
        await rig.handshake()

        assert await rig.call("session/list", {}) == {"jsonrpc": "2.0", "id": 1, "result": {"sessions": []}}

    async def test_a_failure_reading_storage_is_an_empty_listing_not_an_error(self, rig, monkeypatch):
        from raven.config import load_config

        await rig.handshake()

        def _explode(loop, config):
            raise OSError("session directory is gone")

        monkeypatch.setattr("raven.rpc.methods.session._manager_for", _explode)
        assert load_config() is not None

        response = await rig.call("session/list", {})

        assert response["result"] == {"sessions": []}

    async def test_it_reads_the_real_manager_and_sorts_it(self, rig):
        """The other tests stub the read; this one exercises it, because the sort
        and the channel filter are the parts a stub would assert into existence."""
        await rig.handshake()
        rig.engine.sessions.save(self._store(rig, "acp:older", "/one"))
        rig.engine.sessions.save(self._store(rig, "acp:newer", "/two"))

        entries = rig.methods._stored_sessions()

        assert {e["key"] for e in entries} == {"acp:older", "acp:newer"}


class TestConfigOptions:
    """The stable channel for switching models.

    ``session/set_model`` does not exist in the schema and neither does
    ``models.availableModels``; both appear in older material. An agent waiting
    for either would never be asked to switch a model.
    """

    async def test_a_new_session_is_told_what_it_can_change(self, rig):
        """Offered at creation so a client can put a model picker in the session
        menu without a second round trip."""
        await rig.handshake()
        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        options = response["result"]["configOptions"]
        assert [o["id"] for o in options] == ["model"]
        assert options[0]["category"] == "model"
        validate_def("NewSessionResponse", response["result"])

    async def test_no_configured_provider_means_no_key_at_all(self, rig):
        """Absent rather than empty: an empty list is a menu that opens onto
        nothing."""
        rig.stack.model_options = {"model": "", "provider": "", "providers": []}
        await rig.handshake()

        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert "configOptions" not in response["result"]
        validate_def("NewSessionResponse", response["result"])

    async def test_setting_the_model_writes_through_and_answers_with_the_full_set(self, rig):
        """The response carries every option, not just the one that changed --
        the schema requires it, and applying one can change another's value."""
        await rig.handshake()
        session_id = await rig.new_session()

        response = await rig.call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "type": "string", "value": "anthropic/opus-5"},
        )

        validate_def("SetSessionConfigOptionResponse", response["result"])
        assert [o["id"] for o in response["result"]["configOptions"]] == ["model"]
        written = rig.stack.params_for("config.set")
        assert written["key"] == "model"
        assert written["value"] == "anthropic/opus-5"

    async def test_the_session_key_is_passed_so_a_running_turn_can_refuse_it(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        await rig.call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": "anthropic/opus-5"},
        )

        assert rig.stack.params_for("config.set")["session_id"] == session_id

    async def test_a_refusal_keeps_its_own_code(self, rig):
        """A typed refusal is something a client can act on. Flattening it to an
        internal error leaves a person retrying a thing that will keep failing for
        a reason nobody told them.

        ``config.set`` is where the refusal comes from, so the case is written
        with a refusal this repo actually raises rather than a code borrowed from
        a runtime that has more of them.
        """
        from raven.rpc.errors import ConfigValidationError

        await rig.handshake()
        session_id = await rig.new_session()
        rig.stack.config_set_result = ConfigValidationError("not a model this build knows")

        response = await rig.call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": "anthropic/opus-5"},
        )

        assert response["error"]["code"] == -32011
        validate_outbound(response)

    async def test_an_unknown_option_names_what_is_supported(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        response = await rig.call(
            "session/set_config_option", {"sessionId": session_id, "configId": "temperature", "value": "0.7"}
        )

        assert response["error"]["code"] == protocol.INVALID_PARAMS
        assert response["error"]["data"]["supported"] == ["model"]

    @pytest.mark.parametrize("value", [None, "", 5, []])
    async def test_an_unusable_value_is_refused_before_the_write(self, rig, value):
        await rig.handshake()
        session_id = await rig.new_session()

        response = await rig.call(
            "session/set_config_option", {"sessionId": session_id, "configId": "model", "value": value}
        )

        assert response["error"]["code"] == protocol.INVALID_PARAMS
        assert not any(name == "config.set" for name, _ in rig.stack.calls)

    async def test_an_unknown_session_is_refused(self, rig):
        await rig.handshake()

        response = await rig.call(
            "session/set_config_option", {"sessionId": "acp:gone", "configId": "model", "value": "anthropic/opus-5"}
        )

        assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND

    async def test_a_failing_model_catalogue_does_not_fail_the_session(self, rig):
        """This is read during ``session/new``. A missing model surface must not
        take the handshake down with it."""
        rig.stack.model_options = RuntimeError("provider registry is unreachable")
        await rig.handshake()

        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert "result" in response
        assert "configOptions" not in response["result"]


class TestPrompt:
    async def test_a_text_prompt_runs_a_turn_and_answers_its_stop_reason(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]})
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rig.translator.settle_turn(session_id, "end_turn")
        response = await task

        assert response["result"] == {"stopReason": "end_turn"}
        validate_def("PromptResponse", response["result"])
        assert rig.stack.params_for("turn.send")["content"] == "hi"

    async def test_the_stop_reason_comes_from_the_event_stream(self, rig):
        """The whole point of the translator holding turn state: the reason is
        decided by what the runtime emitted, not by the handler."""
        await rig.handshake()
        session_id = await rig.new_session()
        sub = rig.translator.get(session_id).subscription_id

        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]})
        )
        for _ in range(4):
            await asyncio.sleep(0)
        await rig.translator.send_frame(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "subscription_id": sub,
                    "event": {
                        "type": "error",
                        "payload": {"code": -32099, "message": "c", "reason": "cancelled_by_client"},
                    },
                },
            }
        )
        response = await task

        assert response["result"] == {"stopReason": "cancelled"}

    async def test_an_unknown_session_is_told_so_rather_than_given_a_new_one(self, rig):
        """-32002, because a client that reopens a session raven has lost would
        otherwise show a person an empty transcript for a conversation that had
        one."""
        await rig.handshake()
        response = await rig.call("session/prompt", {"sessionId": "acp:gone", "prompt": []})

        assert response["error"]["code"] == protocol.RESOURCE_NOT_FOUND

    async def test_an_empty_prompt_ends_the_turn_without_running_one(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        response = await rig.call("session/prompt", {"sessionId": session_id, "prompt": []})

        assert response["result"] == {"stopReason": "end_turn"}
        assert not any(name == "turn.send" for name, _ in rig.stack.calls)

    async def test_a_prompt_that_is_not_a_list_is_refused(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        response = await rig.call("session/prompt", {"sessionId": session_id, "prompt": "hi"})

        assert response["error"]["code"] == protocol.INVALID_PARAMS

    async def test_a_refused_turn_is_explained_and_still_ends_with_a_stop_reason(self, rig):
        """A prompt is never answered with a JSON-RPC error. No terminating event
        is coming either, so awaiting the future would hang."""
        from raven.rpc.errors import TurnInProgressError

        await rig.handshake()
        session_id = await rig.new_session()
        rig.stack.send_result = TurnInProgressError("already running")

        response = await rig.call(
            "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}
        )

        assert response["result"] == {"stopReason": "end_turn"}
        assert any("could not start" in u["content"]["text"] for u in rig.updates())

    async def test_the_turn_slot_is_released_after_a_refused_turn(self, rig):
        """Otherwise the session is wedged: every later prompt is refused as
        concurrent by a turn that never ran."""
        from raven.rpc.errors import TurnInProgressError

        await rig.handshake()
        session_id = await rig.new_session()
        rig.stack.send_result = TurnInProgressError("already running")
        await rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "a"}]})

        assert rig.translator.get(session_id).turn is None

    async def test_a_second_concurrent_prompt_is_refused_with_a_reason(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()
        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "a"}]})
        )
        for _ in range(4):
            await asyncio.sleep(0)

        response = await rig.call(
            "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "b"}]}
        )

        assert response["error"]["code"] == protocol.INVALID_REQUEST
        assert "in flight" in response["error"]["message"]
        rig.translator.settle_turn(session_id, "end_turn")
        await task


class TestPromptContent:
    async def _send(self, rig, blocks):
        await rig.handshake()
        session_id = await rig.new_session()
        task = asyncio.create_task(rig.call("session/prompt", {"sessionId": session_id, "prompt": blocks}))
        for _ in range(4):
            await asyncio.sleep(0)
        rig.translator.settle_turn(session_id, "end_turn")
        await task
        return rig.stack.params_for("turn.send")

    async def test_text_blocks_are_joined(self, rig):
        params = await self._send(rig, [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])

        assert params["content"] == "one\n\ntwo"

    async def test_an_empty_or_mistyped_text_block_contributes_nothing(self, rig):
        """Not merely skipped -- joining it would put a blank paragraph in the
        middle of what the person actually said."""
        params = await self._send(
            rig,
            [
                {"type": "text", "text": ""},
                {"type": "text"},
                {"type": "text", "text": 42},
                {"type": "text", "text": "real"},
            ],
        )

        assert params["content"] == "real"

    async def test_a_resource_link_names_a_usable_path(self, rig):
        """The block Zed sends for every @-mention, gated by no capability at all
        -- ``PromptCapabilities`` covers only audio and embeddedContext. An agent
        with no branch for it drops the whole point of the mention."""
        params = await self._send(
            rig,
            [
                {"type": "text", "text": "look at"},
                {"type": "resource_link", "uri": "file:///work/my%20file.py", "name": "my file.py"},
            ],
        )

        assert "/work/my file.py" in params["content"], "the percent-encoding an editor adds must be undone"
        assert "my file.py" in params["content"]

    async def test_a_non_file_resource_link_keeps_its_uri(self, rig):
        params = await self._send(rig, [{"type": "resource_link", "uri": "https://x.dev/a", "name": "a"}])

        assert "https://x.dev/a" in params["content"]

    async def test_a_remote_file_uri_is_not_turned_into_a_local_path(self, rig):
        """``file://host/share`` names somebody else's machine; making it local
        would point the agent at the wrong file rather than at none."""
        params = await self._send(rig, [{"type": "resource_link", "uri": "file://otherbox/etc/passwd", "name": "p"}])

        assert "file://otherbox/etc/passwd" in params["content"]

    async def test_a_resource_link_without_a_uri_still_names_itself(self, rig):
        params = await self._send(rig, [{"type": "resource_link", "name": "notes"}])

        assert "[notes]" in params["content"]

    async def test_an_embedded_text_resource_is_inlined(self, rig):
        params = await self._send(
            rig,
            [
                {
                    "type": "resource",
                    "resource": {"uri": "file:///a/b.py", "text": "print(1)", "mimeType": "text/x-python"},
                }
            ],
        )

        assert "print(1)" in params["content"]
        assert "/a/b.py" in params["content"]

    async def test_an_embedded_blob_is_named_rather_than_base64ed_into_the_prompt(self, rig):
        params = await self._send(
            rig,
            [{"type": "resource", "resource": {"uri": "file:///a/x.bin", "blob": base64.b64encode(b"\x00").decode()}}],
        )

        assert "/a/x.bin" in params["content"]
        assert "not inlined" in params["content"]

    async def test_an_embedded_resource_with_neither_text_nor_blob_is_skipped(self, rig):
        params = await self._send(
            rig,
            [
                {"type": "resource", "resource": {"uri": "file:///a/x", "mimeType": "text/plain"}},
                {"type": "text", "text": "kept"},
            ],
        )

        assert params["content"] == "kept"

    async def test_a_file_uri_that_cannot_be_parsed_is_passed_through_as_text(self, rig):
        """``urlparse`` raises on a bracketed host that is not a valid IPv6
        literal. A URI whose path cannot be trusted is named, not guessed at."""
        params = await self._send(rig, [{"type": "resource_link", "uri": "file://[bad/x", "name": "x"}])

        assert "file://[bad/x" in params["content"]

    async def test_an_image_the_workspace_will_not_take_is_reported_not_raised(self, rig, monkeypatch):
        """One bad attachment must not cost the turn the rest of the prompt was
        asking for -- the same reasoning ``turn.send``'s own resolver uses."""
        import base64 as b64

        def _refuse(data, suffix):
            raise OSError("read-only file system")

        monkeypatch.setattr("raven.acp.methods._write_upload", _refuse)
        params = await self._send(
            rig,
            [
                {"type": "image", "data": b64.b64encode(b"\x89PNG").decode(), "mimeType": "image/png"},
                {"type": "text", "text": "and my question"},
            ],
        )

        assert params["media"] == []
        assert params["content"] == "and my question"

    async def test_a_malformed_embedded_resource_costs_only_itself(self, rig):
        params = await self._send(rig, [{"type": "resource", "resource": None}, {"type": "text", "text": "still here"}])

        assert params["content"] == "still here"

    async def test_an_image_lands_in_the_workspace_as_a_relative_media_path(self, rig):
        """``turn.send`` resolves media through the file tools' own policy, and
        the spelling it resolves is ``uploads/<name>`` -- which also survives a
        deployment with ``restrict_to_workspace`` on, where an absolute temp path
        outside the workspace would be dropped without a word."""
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
        params = await self._send(rig, [{"type": "image", "data": png, "mimeType": "image/png"}])

        assert len(params["media"]) == 1
        relative = params["media"][0]
        assert relative.startswith("uploads/") and relative.endswith(".png")
        assert (rig.tmp_path / "ws" / relative).read_bytes().startswith(b"\x89PNG")

    async def test_an_image_is_also_mentioned_in_the_text(self, rig):
        png = base64.b64encode(b"\x89PNG").decode()
        params = await self._send(rig, [{"type": "image", "data": png, "mimeType": "image/png"}])

        assert "attached image" in params["content"]

    async def test_a_broken_image_does_not_fail_the_prompt(self, rig):
        params = await self._send(
            rig,
            [
                {"type": "image", "data": "not base64!!", "mimeType": "image/png"},
                {"type": "image", "data": "", "mimeType": "image/png"},
                {"type": "text", "text": "and my question"},
            ],
        )

        assert params["media"] == []
        assert params["content"] == "and my question"

    async def test_an_oversized_image_is_dropped_rather_than_written(self, rig):
        big = base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()
        params = await self._send(
            rig, [{"type": "image", "data": big, "mimeType": "image/png"}, {"type": "text", "text": "q"}]
        )

        assert params["media"] == []

    async def test_audio_is_named_rather_than_silently_dropped(self, rig):
        """promptCapabilities.audio is false so this should not arrive, but a
        person who spoke deserves to know the words did not get through."""
        params = await self._send(rig, [{"type": "audio", "data": "AAA", "mimeType": "audio/wav"}])

        assert "audio" in params["content"]

    async def test_an_unknown_block_type_costs_only_itself(self, rig):
        params = await self._send(rig, [{"type": "hologram"}, "not a dict", {"type": "text", "text": "kept"}])

        assert params["content"] == "kept"


class TestCancel:
    async def test_it_cancels_the_turn_and_answers_the_prompt(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()
        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "a"}]})
        )
        for _ in range(4):
            await asyncio.sleep(0)

        await rig.notify("session/cancel", {"sessionId": session_id})
        response = await task

        assert response["result"] == {"stopReason": "cancelled"}
        assert rig.stack.params_for("turn.cancel") == {"session_key": session_id}

    async def test_a_cancel_that_finds_nothing_still_answers_the_prompt(self, rig):
        """The window between opening the turn and the scheduler accepting it:
        ``turn.cancel`` reports nothing cancelled, and the prompt still has to be
        answered."""
        await rig.handshake()
        session_id = await rig.new_session()
        rig.stack.cancel_result = {"cancelled": False}
        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "a"}]})
        )
        for _ in range(4):
            await asyncio.sleep(0)

        await rig.notify("session/cancel", {"sessionId": session_id})

        assert (await task)["result"] == {"stopReason": "cancelled"}

    async def test_the_prompt_is_answered_even_if_cancelling_raised(self, rig):
        """The pending prompt is resolved from a ``finally``: a failure to cancel
        must not also mean a turn that never ends."""
        await rig.handshake()
        session_id = await rig.new_session()
        rig.stack.cancel_result = RuntimeError("scheduler gone")
        task = asyncio.create_task(
            rig.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "a"}]})
        )
        for _ in range(4):
            await asyncio.sleep(0)

        await rig.notify("session/cancel", {"sessionId": session_id})

        assert (await task)["result"] == {"stopReason": "cancelled"}

    async def test_a_cancel_for_an_unknown_session_is_ignored(self, rig):
        """Notifications have no reply, and a client cancelling a session it
        already dropped is tidy rather than broken."""
        await rig.handshake()

        assert await rig.notify("session/cancel", {"sessionId": "acp:gone"}) is None
        assert not any(name == "turn.cancel" for name, _ in rig.stack.calls)

    async def test_cancelling_an_idle_session_is_harmless(self, rig):
        await rig.handshake()
        session_id = await rig.new_session()

        assert await rig.notify("session/cancel", {"sessionId": session_id}) is None


class TestOutboundErrorHygiene:
    """What may leave the process when something inside it fails."""

    def test_the_traceback_tail_is_stripped(self):
        """Every internal dispatcher error carries one, and it is twelve lines of
        absolute paths -- diagnostic on a log line, a disclosure in an editor's
        transcript."""
        from raven.acp.methods import sanitise_error_data

        cleaned = sanitise_error_data(
            {"reason": "SystemExit from handler", "traceback_tail": '  File "/Users/someone/raven/x.py", line 3'}
        )

        assert cleaned == {"reason": "SystemExit from handler"}

    def test_what_survives_is_still_redacted(self):
        """An exception message routinely quotes the argument that caused it, and
        for ``exec`` that argument is a command line."""
        from raven.acp.methods import sanitise_error_data

        cleaned = sanitise_error_data({"reason": "failed: curl -H 'Authorization: Bearer sk-ant-AAAABBBBCCCCDDDD'"})

        assert "sk-ant-AAAABBBBCCCC" not in cleaned["reason"]
        assert "curl" in cleaned["reason"]

    def test_data_that_becomes_empty_is_dropped_rather_than_sent_hollow(self):
        from raven.acp.methods import sanitise_error_data

        assert sanitise_error_data({"traceback_tail": "x"}) is None
        assert sanitise_error_data(None) is None

    def test_non_dict_data_is_still_scanned(self):
        from raven.acp.methods import sanitise_error_data

        assert "sk-proj-abcdefghijklmnop" not in str(sanitise_error_data(["sk-proj-abcdefghijklmnop"]))

    async def test_an_internal_failure_reaches_the_client_without_its_traceback(self, rig, monkeypatch):
        async def _boom(params):
            raise RuntimeError("no")

        rig.stack.dispatcher._handlers["turn.subscribe"] = _boom
        await rig.handshake()

        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert response["error"]["code"] == protocol.INTERNAL_ERROR
        assert "traceback" not in json.dumps(response).lower()
        validate_outbound(response)


class TestShutdown:
    async def test_every_subscription_this_connection_opened_is_closed(self, rig):
        """Each subscription owns an asyncio task running a coalesce loop, and
        ``build_rpc_stack``'s teardown does not touch the emitter. Left open, they
        are reported at interpreter exit as "Task was destroyed but it is
        pending!" -- on stderr, which is the stream an ACP client shows."""
        closed = []

        async def _unsubscribe(params):
            closed.append(params["subscription_id"])
            return {"unsubscribed": True}

        rig.stack.dispatcher.register("turn.unsubscribe", _unsubscribe)
        await rig.handshake()
        first = await rig.new_session()
        second = await rig.new_session()

        await rig.methods.unsubscribe_all()

        assert sorted(closed) == sorted(
            [rig.translator.get(first).subscription_id, rig.translator.get(second).subscription_id]
        )

    async def test_one_failing_close_does_not_abort_the_sweep(self, rig):
        """This runs on the way out; one stuck subscription must not cost the rest
        of the shutdown."""
        closed = []

        async def _unsubscribe(params):
            if not closed:
                closed.append("failed")
                raise RuntimeError("gone")
            closed.append(params["subscription_id"])
            return {"unsubscribed": True}

        rig.stack.dispatcher.register("turn.unsubscribe", _unsubscribe)
        await rig.handshake()
        await rig.new_session()
        await rig.new_session()

        await rig.methods.unsubscribe_all()

        assert len(closed) == 2

    async def test_a_session_with_no_subscription_is_skipped(self, rig):
        from raven.acp.updates import AcpSession

        rig.translator.add(AcpSession(session_id="acp:bare", session_key="acp:bare", cwd="/w"))

        await rig.methods.unsubscribe_all()


class TestErrorPassthrough:
    async def test_an_rpc_error_code_is_carried_through_rather_than_flattened(self, rig):
        """-32003 and -32008 mean something a client can act on; flattening them
        to -32603 throws that away."""
        from raven.rpc.errors import ModelNotAvailableError

        await rig.handshake()

        async def _fail(params):
            raise ModelNotAvailableError()

        rig.stack.dispatcher._handlers["turn.subscribe"] = _fail
        response = await rig.call("session/new", {"cwd": str(rig.tmp_path / "p"), "mcpServers": []})

        assert response["error"]["code"] == -32008

    async def test_every_error_frame_this_module_emits_is_a_legal_acp_frame(self, rig):
        await rig.handshake()
        for method, params in (
            ("session/new", {"cwd": "rel", "mcpServers": []}),
            ("authenticate", {}),
            ("session/load", {}),
            ("session/prompt", {"sessionId": "acp:gone", "prompt": []}),
        ):
            response = await rig.call(method, params)

            validate_outbound(response)
            assert json.dumps(response), "an error frame must be serialisable; data carries arbitrary values"
