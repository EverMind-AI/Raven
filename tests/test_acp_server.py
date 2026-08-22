"""The connection: its frame loop, its concurrency, and its teardown.

The engine is stubbed. What is under test is everything around it, and the one
claim worth the file is that a suspended ``session/prompt`` does not block the
``session/cancel`` that has to reach it -- handling frames inline would make
cancellation, the one operation the protocol requires to always work,
unreachable.
"""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from loguru import logger

from raven.acp import server
from raven.acp.updates import UpdateTranslator


class _Broker:
    """The one method the ACP question routing calls on the runtime's broker."""

    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    def reply(self, key: str, answer: str) -> bool:
        self.answers.append((key, answer))
        return True


class _Stack:
    """The subset of ``RpcStack`` the server touches, with a teardown log."""

    def __init__(self, *, teardown_error: Exception | None = None) -> None:
        from raven.rpc.dispatcher import Dispatcher

        self.dispatcher = Dispatcher()
        self.agent_loop = None
        self.torn_down = 0
        # The runtime's ask-user broker. Present on ``RpcStack``, and the ACP
        # question routing answers it -- a stub without it would let a missing
        # wire-up pass.
        self.question_broker = _Broker()
        self._teardown_error = teardown_error

    async def teardown(self) -> None:
        self.torn_down += 1
        if self._teardown_error is not None:
            raise self._teardown_error


def _reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


def _frames(out: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line]


@pytest.fixture
def stub(monkeypatch):
    stack = _Stack()

    async def _build(translator, *, channel=server.ACP_CHANNEL, approval_responder=None):
        stack.channel = channel
        stack.approval_responder = approval_responder
        return stack

    monkeypatch.setattr(server, "build_stack", _build)
    return stack


class TestFrameLoop:
    async def test_it_answers_each_request_and_stays_silent_on_notifications(self, stub):
        out = io.BytesIO()
        payload = (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            b'{"jsonrpc":"2.0","method":"$/cancel_request","params":{"id":1}}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"session/load","params":{}}\n'
        )

        await server.serve(_reader(payload), out)

        assert [f["id"] for f in _frames(out)] == [1, 2]

    async def test_an_unreadable_frame_is_reported_on_the_wire(self, stub):
        """The client is what has to learn its frame was unreadable; a log line
        reaches nobody who can act on it."""
        out = io.BytesIO()

        await server.serve(_reader(b'garbage\n{"jsonrpc":"2.0","id":3,"method":"initialize","params":{}}\n'), out)

        frames = _frames(out)
        assert frames[0]["error"]["code"] == -32700
        assert frames[0]["id"] is None
        assert frames[1]["id"] == 3

    async def test_a_client_that_says_nothing_is_answered_with_nothing(self, stub):
        out = io.BytesIO()

        await server.serve(_reader(b""), out)

        assert out.getvalue() == b""

    async def test_the_engine_is_built_on_the_acp_channel(self, stub):
        """Sharing ``"tui"`` would put an editor's sessions in the terminal's
        picker, because the session listing filters on the key prefix."""
        await server.serve(_reader(b""), io.BytesIO())

        assert stub.channel == "acp"
        assert server.ACP_CHANNEL == "acp"


class TestConcurrency:
    async def test_a_cancel_reaches_a_session_whose_prompt_is_still_running(self, stub, monkeypatch):
        """The claim the whole design rests on. Handled inline, the cancel would
        sit behind the prompt it is meant to interrupt -- forever, since the
        prompt is waiting for it."""
        out = io.BytesIO()
        started = asyncio.Event()
        order: list[str] = []

        async def _handle(frame):
            method = frame.get("method")
            if method == "slow":
                order.append("slow-start")
                started.set()
                await asyncio.sleep(0.05)
                order.append("slow-end")
                return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}
            order.append(method)
            return None

        class _Methods:
            handle = staticmethod(_handle)

        monkeypatch.setattr(server, "AcpMethods", lambda **kw: _Methods())

        await server.serve(
            _reader(b'{"jsonrpc":"2.0","id":1,"method":"slow"}\n{"jsonrpc":"2.0","method":"fast"}\n'), out
        )

        assert order == ["slow-start", "fast", "slow-end"], (
            "the second frame must be handled while the first is still suspended"
        )
        assert started.is_set()

    async def test_a_finite_input_still_gets_its_answers(self, stub):
        """EOF arrives before the handlers have run at all when the input is a
        fixed payload. Cancelling on EOF would answer a batch of three requests
        with nothing -- which is what ``echo '...' | raven acp`` and every smoke
        test look like."""
        out = io.BytesIO()
        payload = b"".join(b'{"jsonrpc":"2.0","id":%d,"method":"initialize","params":{}}\n' % n for n in (1, 2, 3))

        await server.serve(_reader(payload), out)

        assert [f["id"] for f in _frames(out)] == [1, 2, 3]

    async def test_a_stuck_handler_is_cancelled_after_the_grace_period(self, stub, monkeypatch):
        """The backstop. A handler waiting on something the event stream cannot
        resolve would otherwise keep the process alive after the window closed."""
        out = io.BytesIO()
        events: list[str] = []
        monkeypatch.setattr(server, "SHUTDOWN_GRACE_S", 0.01)

        async def _handle(frame):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                events.append("handler-unwound")
                raise
            return None

        class _Methods:
            handle = staticmethod(_handle)

        monkeypatch.setattr(server, "AcpMethods", lambda **kw: _Methods())
        original = stub.teardown

        async def _teardown():
            events.append("teardown")
            await original()

        stub.teardown = _teardown

        await server.serve(_reader(b'{"jsonrpc":"2.0","id":1,"method":"slow"}\n'), out)

        assert events == ["handler-unwound", "teardown"], (
            "the handler must unwind before the engine it is using is torn down"
        )

    async def test_a_suspended_prompt_is_settled_rather_than_cancelled(self, stub, monkeypatch):
        """The difference that matters: settled, the handler returns through its
        own code and writes the ``cancelled`` stop reason. Cancelled, the client
        gets nothing for a request it is still holding."""
        from raven.acp.updates import AcpSession

        out = io.BytesIO()
        captured = {}

        class _Methods:
            def __init__(self, **kw):
                captured["translator"] = kw["translator"]

            async def handle(self, frame):
                translator = captured["translator"]
                translator.add(AcpSession(session_id="acp:s", session_key="acp:s", cwd="/w", subscription_id="sub"))
                stop = await translator.begin_turn("acp:s")
                return {"jsonrpc": "2.0", "id": frame["id"], "result": {"stopReason": stop}}

        monkeypatch.setattr(server, "AcpMethods", _Methods)

        await server.serve(_reader(b'{"jsonrpc":"2.0","id":1,"method":"session/prompt"}\n'), out)

        assert _frames(out) == [{"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "cancelled"}}]

    async def test_a_handler_that_finished_is_not_cancelled_again(self, stub):
        out = io.BytesIO()

        await server.serve(_reader(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'), out)

        assert [f["id"] for f in _frames(out)] == [1]


class TestHandlerFailures:
    async def test_a_handler_failure_is_logged_rather_than_swallowed(self, stub, monkeypatch):
        """A task's exception is retrieved by the shutdown gather and then
        discarded, so a write that failed mid-session would leave no trace
        anywhere."""
        records = []
        sink_id = logger.add(lambda message: records.append(message.record["message"]), level="ERROR")

        class _Methods:
            @staticmethod
            async def handle(frame):
                raise OSError("broken pipe")

        monkeypatch.setattr(server, "AcpMethods", lambda **kw: _Methods())
        try:
            await server.serve(_reader(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'), io.BytesIO())
        finally:
            logger.remove(sink_id)

        assert any("answering initialize failed" in message for message in records)

    async def test_a_failing_handler_does_not_stop_the_loop(self, stub, monkeypatch):
        seen = []

        class _Methods:
            @staticmethod
            async def handle(frame):
                seen.append(frame["id"])
                raise OSError("nope")

        monkeypatch.setattr(server, "AcpMethods", lambda **kw: _Methods())

        await server.serve(
            _reader(b'{"jsonrpc":"2.0","id":1,"method":"a"}\n{"jsonrpc":"2.0","id":2,"method":"b"}\n'),
            io.BytesIO(),
        )

        assert seen == [1, 2]

    async def test_subscriptions_are_closed_before_the_engine_is_torn_down(self, stub, monkeypatch):
        order = []

        class _Methods:
            def __init__(self, **kw):
                pass

            async def handle(self, frame):
                return None

            async def unsubscribe_all(self):
                order.append("unsubscribe")

        monkeypatch.setattr(server, "AcpMethods", _Methods)
        original = stub.teardown

        async def _teardown():
            order.append("teardown")
            await original()

        stub.teardown = _teardown

        await server.serve(_reader(b""), io.BytesIO())

        assert order == ["unsubscribe", "teardown"]

    async def test_a_failing_unsubscribe_does_not_block_the_teardown(self, stub, monkeypatch):
        class _Methods:
            def __init__(self, **kw):
                pass

            async def handle(self, frame):
                return None

            async def unsubscribe_all(self):
                raise RuntimeError("emitter gone")

        monkeypatch.setattr(server, "AcpMethods", _Methods)

        await server.serve(_reader(b""), io.BytesIO())

        assert stub.torn_down == 1


class TestTeardown:
    async def test_the_engine_is_torn_down_on_a_clean_exit(self, stub):
        await server.serve(_reader(b""), io.BytesIO())

        assert stub.torn_down == 1

    async def test_the_engine_is_torn_down_when_the_reader_fails(self, stub):
        """A pipe that dies mid-frame must not leave the browser profile locked
        and MCP subprocesses orphaned."""
        reader = asyncio.StreamReader()
        reader.set_exception(ConnectionResetError("pipe died"))

        with pytest.raises(ConnectionResetError):
            await server.serve(reader, io.BytesIO())

        assert stub.torn_down == 1

    async def test_a_failing_teardown_does_not_replace_a_clean_exit(self, monkeypatch):
        """Every step inside the real teardown is already guarded; this catches a
        failure in the callable itself, whose traceback the client cannot see
        anyway."""
        stack = _Stack(teardown_error=RuntimeError("cron would not stop"))

        async def _build(translator, *, channel=server.ACP_CHANNEL, approval_responder=None):
            return stack

        monkeypatch.setattr(server, "build_stack", _build)

        await server.serve(_reader(b""), io.BytesIO())

        assert stack.torn_down == 1


class TestBuildStack:
    async def test_it_hands_the_translator_sink_to_the_rpc_stack(self, monkeypatch):
        """The seam that makes one sink intercept the entire outbound surface: the
        emitter, all three brokers and the MCP bridge are given the same
        callable."""
        seen = {}

        async def _build_rpc_stack(send_frame, *, channel="tui", approval_responder=None):
            seen["send_frame"] = send_frame
            seen["channel"] = channel
            seen["approval_responder"] = approval_responder
            return _Stack()

        monkeypatch.setattr("raven.rpc.bootstrap.build_rpc_stack", _build_rpc_stack)
        translator = UpdateTranslator(emit=lambda f: None)

        await server.build_stack(translator, channel="acp", approval_responder="broker")

        assert seen["send_frame"] == translator.send_frame
        assert seen["channel"] == "acp"
        assert seen["approval_responder"] == "broker", (
            "the shell approval transport has to be replaced, or an ACP client is asked "
            "for permission over a method it does not implement"
        )


class TestApprovalWiring:
    """The engine's exec tool has to be told what to ask about.

    Without this, an editor's agent runs ``git push``, ``npm install`` and
    ``curl -o`` with nothing on screen -- the built-in policy asks about deletion
    and nothing else. It is the single most visible difference between an agent
    somebody trusts and one they do not.
    """

    def test_the_external_effect_families_are_registered_on_the_exec_tool(self):
        from raven.agent.tools.registry import ToolRegistry
        from raven.agent.tools.shell import ExecTool
        from raven.agent.tools.shell_policy import CommandDecision

        tool = ExecTool(working_dir="/tmp")
        registry = ToolRegistry()
        registry.register(tool)

        server._ask_before_external_effects(SimpleNamespace(tools=registry))

        assert tool._policy.evaluate("git push origin main") is CommandDecision.REQUIRE_APPROVAL
        assert tool._policy.evaluate("npm install lodash") is CommandDecision.REQUIRE_APPROVAL
        assert tool._policy.evaluate("pytest -q") is CommandDecision.ALLOW

    def test_a_sub_agents_own_tool_is_not_reached(self):
        """Recorded rather than left to be discovered. A sub-agent builds its own
        ``ExecTool`` with its own policy and never gets a responder, so its
        ``git push`` still runs unannounced. Registering the families there would
        make it refused instead of asked, which is a different product decision.
        """
        from raven.agent.tools.registry import ToolRegistry
        from raven.agent.tools.shell import ExecTool
        from raven.agent.tools.shell_policy import CommandDecision

        main_tool = ExecTool(working_dir="/tmp")
        sub_tool = ExecTool(working_dir="/tmp")
        registry = ToolRegistry()
        registry.register(main_tool)

        server._ask_before_external_effects(SimpleNamespace(tools=registry))

        assert main_tool._policy.evaluate("git push origin main") is CommandDecision.REQUIRE_APPROVAL
        assert sub_tool._policy.evaluate("git push origin main") is CommandDecision.ALLOW, (
            "if this ever starts asking, the compatibility matrix entry is stale"
        )

    def test_no_engine_and_no_tool_are_both_survivable(self):
        from raven.agent.tools.registry import ToolRegistry

        server._ask_before_external_effects(None)
        server._ask_before_external_effects(SimpleNamespace())
        server._ask_before_external_effects(SimpleNamespace(tools=ToolRegistry()))

    async def test_the_permission_broker_is_the_stacks_approval_transport(self, stub):
        from raven.acp.permissions import AcpPermissionBroker

        await server.serve(_reader(b""), io.BytesIO())

        assert isinstance(stub.approval_responder, AcpPermissionBroker), (
            "the RPC broker emits approval.request, which an ACP client does not implement"
        )


class TestOutboundLifecycle:
    async def test_outstanding_requests_are_failed_before_the_handlers_drain(self, stub, monkeypatch):
        """A handler suspended on a permission prompt is waiting on an outbound
        future. Draining first would wait out the grace period for a promise
        nothing can keep."""
        from raven.acp.outbound import ConnectionClosedError

        outcome = {}

        class _Methods:
            def __init__(self, **kw):
                self.outbound = kw["outbound"]

            async def handle(self, frame):
                try:
                    await self.outbound.call("session/request_permission", {}, timeout=30)
                except ConnectionClosedError:
                    outcome["failed"] = True
                return None

            async def unsubscribe_all(self):
                return None

        monkeypatch.setattr(server, "AcpMethods", _Methods)

        await server.serve(_reader(b'{"jsonrpc":"2.0","id":1,"method":"session/prompt"}\n'), io.BytesIO())

        assert outcome.get("failed") is True

    async def test_a_response_frame_resolves_an_outstanding_request(self, stub):
        """The half of the loop that makes a prompt answerable: a frame with an id
        and no method is not garbage, it is the answer."""
        out = io.BytesIO()
        answered = {}

        payload = (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n'
            b'{"jsonrpc":"2.0","id":1,"result":{"outcome":{"outcome":"cancelled"}}}\n'
        )

        real_methods = server.AcpMethods

        def _spy(**kw):
            answered["outbound"] = kw["outbound"]
            return real_methods(**kw)

        import unittest.mock

        with unittest.mock.patch.object(server, "AcpMethods", _spy):
            await server.serve(_reader(payload), out)

        assert answered["outbound"] is not None


class TestQuestionWiring:
    async def test_a_clarify_request_reaches_the_question_routing(self, stub, monkeypatch):
        """The frame the runtime emits when ``ask_user`` fires. Dropped, it stalls
        a tool call for the whole of the broker's timeout, and a client shows a
        spinner and then a reply that ignores what it asked."""
        taken = []

        class _Questions:
            def __init__(self, **kw):
                pass

            def set_broker(self, broker):
                return None

            def set_client(self, client):
                return None

            def handle(self, method, params):
                taken.append((method, params))
                return True

            async def drain(self):
                return None

        monkeypatch.setattr(server, "AcpQuestions", _Questions)
        captured = {}
        real = server.UpdateTranslator

        def _spy(emit, **kwargs):
            captured["translator"] = real(emit, **kwargs)
            return captured["translator"]

        monkeypatch.setattr(server, "UpdateTranslator", _spy)

        async def _serve_and_emit():
            await server.serve(_reader(b""), io.BytesIO())

        await _serve_and_emit()
        await captured["translator"].send_frame(
            {"jsonrpc": "2.0", "method": "clarify.request", "params": {"question": "which?"}}
        )

        assert taken == [("clarify.request", {"question": "which?"})]

    async def test_questions_are_drained_before_the_outbound_futures_are_failed(self, stub, monkeypatch):
        """A round trip that has just been answered gets to deliver that answer to
        the broker instead of being cancelled one step short."""
        order = []

        class _Questions:
            def __init__(self, **kw):
                pass

            def set_broker(self, broker):
                return None

            def set_client(self, client):
                return None

            def handle(self, method, params):
                return False

            async def drain(self):
                order.append("drain")

        monkeypatch.setattr(server, "AcpQuestions", _Questions)
        original = stub.teardown

        async def _teardown():
            order.append("teardown")
            await original()

        stub.teardown = _teardown

        await server.serve(_reader(b""), io.BytesIO())

        assert order == ["drain", "teardown"]

    async def test_a_failing_drain_does_not_block_the_shutdown(self, stub, monkeypatch):
        class _Questions:
            def __init__(self, **kw):
                pass

            def set_broker(self, broker):
                return None

            def set_client(self, client):
                return None

            def handle(self, method, params):
                return False

            async def drain(self):
                raise RuntimeError("a question round trip is wedged")

        monkeypatch.setattr(server, "AcpQuestions", _Questions)

        await server.serve(_reader(b""), io.BytesIO())

        assert stub.torn_down == 1


class TestCrashHandlers:
    def test_the_previous_excepthook_still_runs(self):
        """stderr is deliberately kept as a destination: an ACP client surfaces
        it, and a crash that is only in a log file is a crash nobody is told
        about."""
        import sys

        called = []
        original = sys.excepthook
        sys.excepthook = lambda *args: called.append(args)
        try:
            server.install_crash_handlers()
            sys.excepthook(ValueError, ValueError("boom"), None)
        finally:
            sys.excepthook = original

        assert called and called[0][0] is ValueError

    async def test_an_asyncio_error_is_logged_with_its_exception(self):
        # ``sys.excepthook`` is restored too, not only the loop handler: it is
        # process-global, so leaving it installed would chain another copy onto
        # every later test in the session.
        import sys

        records = []
        sink_id = logger.add(lambda message: records.append(message.record), level="ERROR")
        original_hook = sys.excepthook
        try:
            server.install_crash_handlers()
            handler = asyncio.get_running_loop().get_exception_handler()
            assert handler is not None
            handler(asyncio.get_running_loop(), {"message": "task blew up", "exception": ValueError("why")})
            handler(asyncio.get_running_loop(), {"message": "no exception here", "future": "x"})
        finally:
            logger.remove(sink_id)
            asyncio.get_running_loop().set_exception_handler(None)
            sys.excepthook = original_hook

        assert any("task blew up" in r["message"] for r in records)
        assert any("no exception here" in r["message"] for r in records)

    def test_it_installs_the_hook_even_with_no_loop_running(self):
        """The excepthook half is what covers a failure during startup, before
        the loop exists."""
        import sys

        original = sys.excepthook
        try:
            server.install_crash_handlers()

            assert sys.excepthook is not original
        finally:
            sys.excepthook = original
