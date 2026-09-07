"""Prompt delivery timing, submission evidence, and addressed content acknowledgements."""

import asyncio

import pytest

from raven.contracts.terminal import Envelope, TerminalError, TerminalRecord
from raven.terminal.deliver import DeliveryService
from raven.terminal.host import TerminalState


class Host:
    def __init__(self):
        self.record = TerminalRecord(
            worktree_id="repo::/tmp/work", worktree_path="/tmp/work", connected=True, writable=True, liveness="live"
        )
        self.activity = TerminalState(self.record, 0, -1, "token", "worker-b", last_output_monotonic=0)
        self.writes = []
        self.render = True
        self.accept = True
        self.permission_after_submit = False

    def state(self, handle):
        return self.activity

    def show(self, handle):
        return self.record

    async def write(self, handle, data):
        self.writes.append(data)
        if b"\x1b[201~" in data and self.render:
            self.activity.show_cursor_sequence += 1
        if data == b"\r":
            if self.accept:
                self.activity.working_sequence += 1
                self.record.status = "working"
            if self.permission_after_submit:
                self.activity.permission_sequence += 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def setup():
    host, clock = Host(), Clock()
    return host, clock, DeliveryService(host, clock=clock.time, sleep=clock.sleep)


async def test_paste_escapes_control_bytes_chunks_and_waits_before_cr():
    host, clock, delivery = setup()
    result = await delivery.send(host.record.handle, "x" * 40000 + "\x1b[201~")
    assert result.accepted and result.state == "accepted"
    assert all(len(chunk) <= 16 * 1024 for chunk in host.writes)
    assert b"".join(host.writes) == b"\x1b[200~" + b"x" * 40000 + b"<ESC>[201~\x1b[201~\r"
    assert clock.now >= 1.5
    assert host.writes.count(b"\r") == 1


async def test_permission_blocks_before_any_write():
    host, _, delivery = setup()
    host.record.status = "permission"
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "no")
    assert error.value.code == "agent_prompt_blocked"
    assert host.writes == []


async def test_transient_permission_beats_working_evidence():
    host, _, delivery = setup()
    host.permission_after_submit = True
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "task")
    assert error.value.code == "agent_prompt_blocked"


async def test_redraw_alone_stalls_without_enter_retry():
    host, clock, delivery = setup()
    host.accept = False
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "task")
    assert error.value.code == "agent_prompt_stalled"
    assert clock.now >= 6.5
    assert host.writes.count(b"\r") == 1


async def test_working_with_rendered_composer_is_queued():
    host, _, delivery = setup()
    host.record.status = "working"
    host.accept = False
    result = await delivery.send(host.record.handle, "next task")
    assert result.state == "queued"
    assert result.accepted


async def test_working_without_render_evidence_is_not_called_queued():
    host, clock, delivery = setup()
    host.record.status = "working"
    host.accept = False
    host.render = False
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "next task")
    assert error.value.code == "agent_prompt_stalled"
    assert clock.now >= 13


async def test_human_composer_is_never_appended_to():
    host, _, delivery = setup()
    host.activity.composer_dirty = True
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "task")
    assert error.value.code == "composer_not_empty"
    assert host.writes == []


async def test_wait_quiet_fallback_only_without_agent_status():
    host, clock, delivery = setup()
    result = await delivery.wait(host.record.handle, timeout_ms=5000)
    assert result.satisfied and clock.now >= 3
    host.activity.status_seen = True
    host.record.status = "working"
    result = await delivery.wait(host.record.handle, timeout_ms=5000)
    assert not result.satisfied and result.timed_out


async def test_wait_reports_startup_blocker():
    host, _, delivery = setup()
    host.record.status = "permission"
    host.activity.blocked_reason = "codex-startup"
    result = await delivery.wait(host.record.handle)
    assert not result.satisfied
    assert result.blocked_reason == "codex-startup"


async def test_ack_matches_reversed_participants_scope_and_nonce():
    host, _, delivery = setup()
    original = Envelope(
        sender="worker-a", recipient="worker-b", scope="local/dev", body="task", reply_terminal="term_sender"
    )
    await delivery.send(host.record.handle, original.to_text())
    wrong = Envelope(sender="worker-c", recipient="worker-a", scope="local/dev", body=f"ack_for={original.nonce}")
    assert not delivery.match_ack("term_sender", wrong.to_text())
    reply = Envelope(sender="worker-b", recipient="worker-a", scope="local/dev", body=f"ack_for={original.nonce}")
    assert not delivery.match_ack("term_wrong", reply.to_text())
    assert delivery.match_ack("term_sender", reply.to_text())
    assert not delivery.match_ack("term_sender", reply.to_text())


async def test_require_ack_times_out_with_content_ack_unverified():
    host, _, delivery = setup()
    original = Envelope(sender="worker-a", recipient="worker-b", scope="local/dev", body="task")
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, original.to_text(), require_ack=True, ack_timeout_ms=100)
    assert error.value.code == "content_ack_unverified"


async def test_native_host_ack_is_real_content_and_requires_no_pty_write():
    host, _, delivery = setup()
    events = []

    async def emit(name, payload):
        events.append((name, payload))

    delivery.emit = emit
    original = Envelope(sender="raven", recipient="worker-b", scope="local/dev", body="task")
    sending = asyncio.create_task(delivery.send(host.record.handle, original.to_text(), require_ack=True))
    for _ in range(200):
        if any(data == b"\r" for data in host.writes):
            break
        await asyncio.sleep(0)
    writes_before = list(host.writes)
    assert await delivery.receive_host(f"ack_for={original.nonce} task received")
    result = await sending
    assert result.content_ack is True
    assert host.writes == writes_before
    assert events[-1][0] == "a2a.ack.matched"
    assert events[-1][1]["handle"] == host.record.handle
    assert events[-1][1]["to"] == "raven"


@pytest.mark.parametrize("provider", ["codex", "claude", "opencode", "hermes", "openclaw", "raven"])
async def test_known_provider_startup_never_uses_the_quiet_composer_fallback(provider):
    host, _, delivery = setup()
    host.activity.startup_pending = True
    host.activity.provider = provider
    result = await delivery.wait(host.record.handle, timeout_ms=5000)
    assert not result.satisfied
    assert result.blocked_reason == f"{provider}-startup-unverified"
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "task")
    assert error.value.code == "agent_prompt_blocked"
    assert error.value.data["reason"] == "startup_pending"
    assert host.writes == []


async def test_permission_reason_survives_delivery_failure_result():
    host, _, delivery = setup()
    host.permission_after_submit = True
    with pytest.raises(TerminalError) as error:
        await delivery.send(host.record.handle, "task")
    assert error.value.data["reason"] == "permission"
    assert error.value.data["bytesWritten"] > 0
