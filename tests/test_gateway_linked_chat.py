"""Owner-bound Telegram turns use the existing page conversation lane."""

import ast
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from raven.gateway.linked_chat import Binding, LinkedChat, load_binding
from raven.rpc.question_broker import QuestionBroker, RoutingQuestionBroker
from raven.rpc.spine import build_rpc_spine
from raven.session.manager import SessionManager
from raven.spine import ChatType, Notice, NoticeKind, Origin, Source, StreamDelta, Text, TurnRequest, Usage
from raven.spine.runner import TurnOutcome

ROOT = Path(__file__).resolve().parents[1]
KEY = "tui:test_existing_project"
OWNER = "123456789"


def dm(text="hello", **changes):
    source = Source(
        "telegram",
        OWNER,
        OWNER + "|test_owner",
        ChatType.DM,
        {"user_id": int(OWNER), "is_group": False, "is_forum": False, "message_thread_id": None},
    )
    return TurnRequest(Origin.USER, replace(source, **changes), text)


class Emitter:
    def __init__(self):
        self.events = []

    async def emit(self, cid, event):
        self.events.append((cid, event))

    def has_subscribers(self, cid):
        return True


class Hub:
    def __init__(self):
        self.events = []

    async def dispatch(self, event):
        self.events.append(event)


class AskTool:
    def set_broker(self, broker):
        self.broker = broker

    async def ask_direct(self, prompt, choices, conversation_id, **kw):
        return await self.broker.await_question(conversation_id, prompt=prompt, choices=choices, **kw)


class Loop:
    def __init__(self, sessions):
        self.sessions = sessions
        self.tools = {"ask_user": AskTool()}
        self.subagent_questions_config = SimpleNamespace()
        self.entered = asyncio.Queue()
        self.release = asyncio.Event()
        self.calls = []
        self.running = 0
        self.maximum = 0
        self.notified = 0
        self.action = None

    def notify_turn_complete(self):
        self.notified += 1

    async def run_turn(self, req, emit, drain, **kwargs):
        self.running += 1
        self.maximum = max(self.maximum, self.running)
        self.calls.append((req, kwargs))
        self.entered.put_nowait(req)
        try:
            session = self.sessions.get_or_create(req.conversation)
            session.add_message("user", req.text)
            self.sessions.save(session)
            if self.action:
                await self.action(req)
            await self.release.wait()
            for injected in drain():
                session.add_message("user", injected.text)
            if req.text == "fail":
                raise RuntimeError("private failure detail")
            await emit(Notice(NoticeKind.PROGRESS, detail="working"))
            if kwargs["stream"]:
                await emit(StreamDelta("token"))
            await emit(Text("reply:" + req.text))
            session.add_message("assistant", "reply:" + req.text)
            self.sessions.save(session)
            return TurnOutcome(Usage(1, 1, 2), explicit_reply=True)
        finally:
            self.running -= 1


class PlatformTests(unittest.TestCase):
    def test_absent_binding_on_a_platform_without_posix_file_checks(self):
        with tempfile.TemporaryDirectory() as tmp, patch("raven.gateway.linked_chat.os", SimpleNamespace()):
            self.assertIsNone(load_binding(Path(tmp) / "missing.json"))

    def test_present_binding_on_an_unsupported_platform_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binding.json"
            path.write_text("{}")
            with patch("raven.gateway.linked_chat.os", SimpleNamespace()):
                with self.assertRaisesRegex(ValueError, "requires POSIX"):
                    load_binding(path)


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "getuid"), "POSIX file ownership required")
class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "telegram-project-link.json"

    def write(self, data):
        self.path.write_text(json.dumps(data))
        self.path.chmod(0o600)

    def test_missing_disabled_and_exact_schema(self):
        self.assertIsNone(load_binding(self.path))
        self.write({"chat_id": OWNER, "sender_id": OWNER, "session_key": KEY})
        self.assertEqual(load_binding(self.path).session_key, KEY)

    def test_bad_ids_keys_modes_and_symlink_fail_closed(self):
        good = {"chat_id": OWNER, "sender_id": OWNER, "session_key": KEY}
        for field, value in [
            ("chat_id", int(OWNER)),
            ("sender_id", True),
            ("chat_id", "01"),
            ("sender_id", OWNER + "|name"),
            ("session_key", "tui:../evil"),
            ("session_key", ""),
            ("sender_id", "222"),
        ]:
            with self.subTest(field=field, value=value):
                self.write({**good, field: value})
                with self.assertRaises(ValueError):
                    load_binding(self.path)
        self.write({**good, "extra": 1})
        with self.assertRaises(ValueError):
            load_binding(self.path)
        self.write(good)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_binding(self.path)
        self.path.unlink()
        self.path.symlink_to("missing")
        with self.assertRaises((OSError, ValueError)):
            load_binding(self.path)


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sessions = SessionManager(self.base)
        project = self.sessions.get_or_create(KEY)
        project.metadata.update({"title": "Existing project", "workdir": str(self.base / "project")})
        project.add_message("user", "old project history")
        self.sessions.save(project)
        prior = self.sessions.get_or_create("telegram:" + OWNER)
        prior.add_message("user", "old DM history")
        self.sessions.save(prior)
        self.prior_bytes = self.sessions.session_path(prior.key).read_bytes()
        self.metadata = dict(project.metadata)
        self.hub = Hub()
        self.loop = Loop(self.sessions)
        self.link = LinkedChat(Binding(OWNER, OWNER, KEY), self.loop, self.hub, {})
        self.emitter = Emitter()
        self.closed = []
        self.scheduler, self.rpc_hub, self.turn_ids, self.teardown = build_rpc_spine(
            self.loop, self.emitter, linked_chat=self.link, on_turn_end=self.closed.append
        )
        self.addAsyncCleanup(self.teardown)

        async def question(frame):
            if frame["method"] == "clarify.request":
                self.hub.events.append(frame)

        self.broker = QuestionBroker(question, timeout_s=0.1)
        self.page_broker = QuestionBroker(question, timeout_s=0.1)
        self.routed = RoutingQuestionBroker(self.page_broker, self.broker, channel_route=self.link.question_route)
        self.loop.tools["ask_user"].set_broker(self.routed)
        self.subagents = SimpleNamespace(cancel_by_session=self.cancel_children)
        self.child_stops = []

    async def cancel_children(self, cid, **kwargs):
        self.child_stops.append(cid)
        return 0

    async def dispatch(self, req):
        return await self.link.dispatch(req, self.scheduler, self.broker, self.subagents)

    def ui(self, text="ui", **changes):
        return TurnRequest(
            Origin.USER, Source("tui", "test_existing_project", "user", ChatType.DM), text, conversation=KEY, **changes
        )

    async def idle(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        for _ in range(100):
            if not self.scheduler.has_running():
                return
            await asyncio.sleep(0.001)
        self.fail("scheduler did not become idle")

    async def test_native_intake_and_shared_history_one_lane(self):
        from raven.channels.intake import Intake

        first = self.scheduler.submit(self.ui())
        await self.loop.entered.get()
        intake = Intake("telegram", SimpleNamespace(allow_from=[OWNER]), allow_check=lambda s: s.split("|")[0] == OWNER)
        intake.set_submit(self.dispatch)
        await intake.publish(OWNER + "|test_owner", OWNER, "dm", metadata=dm().source.extras)
        await asyncio.sleep(0)
        self.assertEqual(len(self.loop.calls), 1)
        self.loop.release.set()
        await first.result()
        await self.idle()
        self.assertEqual(self.loop.maximum, 1)
        self.assertEqual([r.conversation for r, _ in self.loop.calls], [KEY, KEY])
        self.assertEqual([kw["stream"] for _, kw in self.loop.calls], [True, False])
        self.assertEqual([e.content for e in self.hub.events if isinstance(e, Text)], ["reply:dm"])
        self.assertEqual(self.sessions.peek(KEY).metadata, self.metadata)
        self.assertEqual(self.sessions.session_path("telegram:" + OWNER).read_bytes(), self.prior_bytes)
        self.assertEqual(
            [m["content"] for m in self.sessions.peek(KEY).messages if m["role"] == "user"],
            ["old project history", "ui", "dm"],
        )

    async def test_isolation_and_new_refusal(self):
        for req in [
            dm(sender_id="222"),
            dm(chat_id="-123"),
            dm(sender_id="01|x"),
            dm(sender_id=OWNER + "||x"),
            dm(chat_type=ChatType.GROUP),
            dm(extras={}),
            dm(extras={**dm().source.extras, "is_group": True}),
            dm(extras={**dm().source.extras, "message_thread_id": 1}),
            dm(extras={**dm().source.extras, "is_forum": True}),
            replace(dm(), origin=Origin.SUBAGENT),
            replace(dm(), conversation=KEY),
            replace(dm(), direct_target=("agent", "handle")),
        ]:
            await self.dispatch(req)
        for command in ["/new", "/NEW@RavenBot", "/new extra", "/restart"]:
            await self.dispatch(dm(command))
        self.assertEqual(self.loop.calls, [])
        self.assertEqual(len(self.sessions.peek(KEY).messages), 1)

    async def test_missing_page_or_target_never_creates(self):
        await self.link.dispatch(dm(), None, self.broker, self.subagents)
        self.assertEqual(self.loop.calls, [])
        self.sessions.delete(KEY)
        await self.dispatch(dm())
        self.assertFalse(self.sessions.exists(KEY))
        self.assertEqual(self.loop.calls, [])

    async def test_ui_inject_during_dm_falls_back_not_merged(self):
        from raven.spine.turn import BusyPolicy

        await self.dispatch(dm("dm"))
        await self.loop.entered.get()
        inject = self.scheduler.submit(self.ui("ui-inject", busy=BusyPolicy.INJECT))
        self.loop.release.set()
        await inject.result()
        await self.idle()
        self.assertEqual([r.text for r, _ in self.loop.calls], ["dm", "ui-inject"])
        self.assertEqual([e.content for e in self.hub.events if isinstance(e, Text)], ["reply:dm"])

    async def test_failure_and_stop_clear_queued_ui_slots(self):
        await self.dispatch(dm("hold"))
        await self.loop.entered.get()
        self.turn_ids[KEY] = "queued-ui"
        queued = self.scheduler.submit(self.ui("queued", turn_id="queued-ui"))
        await self.dispatch(dm("/stop"))
        await queued.result()
        await self.idle()
        self.assertNotIn(KEY, self.turn_ids)
        self.assertIn(KEY, self.closed)
        self.assertEqual(self.child_stops, [KEY])
        self.loop.release.set()
        await self.dispatch(dm("fail"))
        await self.idle()
        replies = [e.content for e in self.hub.events if isinstance(e, Text)]
        self.assertIn("Sorry, I encountered an error.", replies)
        self.assertFalse(any("private failure detail" in text for text in replies))

    async def test_question_and_approval_roundtrip_no_credentials(self):
        from raven.contracts.permissions import ApprovalChoice
        from raven.permissions.turn import current_turn

        answers = []

        async def action(req):
            turn = current_turn()
            self.assertIsNone(turn.credentials)
            answers.append(await turn.responder.await_approval(description="Test action", command="safe-test"))

        self.loop.action = action
        self.loop.release.set()
        await self.dispatch(dm("ask"))
        for _ in range(100):
            if self.broker.pending_req(KEY):
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(self.broker.pending_req(KEY))
        self.assertIsNone(self.page_broker.pending_req(KEY))
        await self.dispatch(dm("1", sender_id="222"))
        self.assertIsNotNone(self.broker.pending_req(KEY))
        await self.dispatch(dm("Deny"))
        await self.idle()
        self.assertEqual(answers[0].choice, ApprovalChoice.DENY)
        self.assertEqual(len(self.loop.calls), 1)

    async def test_background_relay_is_page_only(self):
        self.loop.release.set()
        req = replace(dm("background"), origin=Origin.SUBAGENT, conversation=KEY)
        await self.scheduler.submit(req).result()
        self.assertEqual(self.hub.events, [])
        self.assertEqual(self.loop.calls[0][0].source.channel, "tui")
        self.assertTrue(any(e["type"] == "token.delta" for _, e in self.emitter.events))

    async def test_actual_gateway_dispatch_selects_page_not_gateway(self):
        tree = ast.parse((ROOT / "raven/cli/gateway_commands.py").read_text())
        dispatch = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_inbound_dispatch"
        )
        namespace = {
            "link_unavailable": False,
            "linked_chat": self.link,
            "page_mount": SimpleNamespace(scheduler=self.scheduler),
            "question_broker": self.broker,
            "agent": SimpleNamespace(subagents=self.subagents),
        }
        exec(compile(ast.Module(body=[dispatch], type_ignores=[]), "native_gateway_dispatch", "exec"), namespace)
        self.loop.release.set()
        await namespace["_inbound_dispatch"](dm("actual ingress"))
        await self.idle()
        self.assertEqual(self.loop.calls[0][0].conversation, KEY)
        self.assertEqual(self.loop.calls[0][0].source, dm().source)
        self.assertEqual(len(self.loop.calls), 1)

    async def test_stopped_ui_gets_terminal_event(self):
        handle = self.scheduler.submit(self.ui("holding", turn_id="ui-running"))
        await self.loop.entered.get()
        await self.dispatch(dm("/stop"))
        await handle.result()
        terminal = [e for _, e in self.emitter.events if e["type"] == "error"]
        self.assertEqual(terminal[-1]["payload"]["turn_id"], "ui-running")
        self.assertEqual(terminal[-1]["payload"]["reason"], "cancelled_by_client")

    async def test_bound_workdir_stays_project(self):
        from raven.agent.workdir import WorkdirPolicy, WorkdirResolver

        resolver = WorkdirResolver(
            agent_home=self.base, policy=WorkdirPolicy.PER_CHANNEL, sessions=self.sessions, launch_dir=self.base
        )
        self.assertEqual(resolver.resolve(KEY, create=False).resolve(), Path(self.metadata["workdir"]).resolve())

    async def test_deleted_while_queued_fails_closed(self):
        ui = self.scheduler.submit(self.ui("holding"))
        await self.loop.entered.get()
        await self.dispatch(dm("queued"))
        self.sessions.delete(KEY)
        await ui.cancel()
        await ui.result()
        await self.idle()
        self.assertEqual([r.text for r, _ in self.loop.calls], ["holding"])
        self.assertFalse(self.sessions.exists(KEY))
        self.assertEqual(self.link.active, {})

    async def test_no_binding_keeps_native_page_spine(self):
        emitter = Emitter()
        scheduler, hub, slots, close = build_rpc_spine(self.loop, emitter)
        try:
            self.loop.release.set()
            await scheduler.submit(self.ui()).result()
            self.assertTrue(self.loop.calls[0][1]["stream"])
            self.assertEqual(self.hub.events, [])
        finally:
            await close()

    async def _stop_pending_prompt(self, approval):
        from raven.agent.tools.ask_user import AskUserTool
        from raven.permissions.turn import current_turn

        self.loop.tools["ask_user"] = AskUserTool(broker=self.routed)
        self.broker.default_timeout_s = 60
        continued = []

        async def action(req):
            if approval:
                await current_turn().responder.await_approval(description="Stop regression")
            else:
                await self.routed.await_question(KEY, prompt="Stop regression")
            continued.append(True)

        self.loop.action = action
        await self.dispatch(dm("waiting"))
        await self.loop.entered.get()
        for _ in range(100):
            if self.broker.pending_req(KEY):
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(self.broker.pending_req(KEY))
        stopper = asyncio.create_task(self.dispatch(dm("/stop")))
        try:
            done, _ = await asyncio.wait([stopper], timeout=0.1)
            observed = (stopper in done, self.scheduler.has_running(), list(continued), list(self.child_stops))
        finally:
            self.loop.release.set()
            await asyncio.wait_for(stopper, 1)
        self.assertEqual(observed, (True, False, [], [KEY]))
        self.assertIsNone(self.broker.pending_req(KEY))

    async def test_stop_pending_approval_cancels_host_and_children(self):
        await self._stop_pending_prompt(True)

    async def test_stop_pending_question_cancels_host_and_children(self):
        await self._stop_pending_prompt(False)

    async def test_question_close_returns_default_without_cancelling_caller(self):
        waiter = asyncio.create_task(self.broker.await_question(KEY, prompt="Close", default="Deny"))
        for _ in range(100):
            if self.broker.pending_req(KEY):
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(self.broker.pending_req(KEY))
        self.broker.cancel_all()
        self.assertEqual(await waiter, "Deny")
        self.assertFalse(waiter.cancelled())

    async def test_approval_timeout_never_allows(self):
        from raven.contracts.permissions import ApprovalChoice
        from raven.permissions.turn import current_turn

        answers = []

        async def action(req):
            answers.append(await current_turn().responder.await_approval(description="Wait"))

        self.loop.action = action
        self.loop.release.set()
        self.broker.default_timeout_s = 0.005
        await self.dispatch(dm("ask"))
        await self.idle()
        self.assertEqual(answers[0].choice, ApprovalChoice.DENY)
        self.assertFalse(answers[0].answered)


if __name__ == "__main__":
    unittest.main()
