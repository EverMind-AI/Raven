"""Streaming terminal parser and title-only agent state detection."""

import pytest

from raven.terminal.host import OutputParser, detect_title_status


@pytest.mark.parametrize(
    "title,status",
    [
        ("\u2733 Ready", "idle"),
        ("\u280b Task", "working"),
        ("\u25d0 Task", "working"),
        ("Codex working", "working"),
        ("Codex permission", "permission"),
        ("Codex ready", "idle"),
        ("claude agents", None),
        ("Claude Code", None),
        ("editor running", None),
        ("~/codex/ready", None),
    ],
)
def test_title_rules(title, status):
    assert detect_title_status(title) == status


def test_parser_handles_split_osc_utf8_and_show_cursor():
    parser = OutputParser()
    data = "\x1b]0;\u2733 Ready\x1b\\body\x1b[31mred\x1b[0m\x1b[?25h".encode()
    titles = []
    text = ""
    markers = 0
    for byte in data:
        plain, observed, cursors = parser.feed(bytes([byte]))
        text += plain
        titles.extend(observed)
        markers += cursors
    assert titles == ["\u2733 Ready"]
    assert text == "bodyred"
    assert markers == 1


def test_bell_terminated_titles_and_body_text_cannot_drive_status():
    parser = OutputParser()
    plain, titles, _ = parser.feed(b"permission working idle\x1b]2;Codex ready\x07")
    assert plain == "permission working idle"
    assert titles == ["Codex ready"]


async def test_hook_is_bound_to_pane_and_launch_token():
    from raven.contracts.terminal import TerminalError, TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    state = TerminalState(record, 123, -1, "correct-token", "worker-a")
    host._terminals[record.handle] = state
    with pytest.raises(TerminalError):
        await host.hook(record.pane_key, "wrong-token", "permission")
    assert record.status == "unknown"
    await host.hook(record.pane_key, "correct-token", "permission", blocked_reason="codex-startup")
    assert state.permission_sequence == 1
    assert state.blocked_reason == "codex-startup"
    await host.hook(record.pane_key, "correct-token", "working")
    await host.hook(record.pane_key, "correct-token", "working")
    assert state.working_sequence == 1
    await host.hook(record.pane_key, "correct-token", "idle")
    await host.hook(record.pane_key, "correct-token", "working")
    assert state.working_sequence == 2


def test_liveness_uncertainty_is_not_exit(monkeypatch):
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    host._terminals[record.handle] = TerminalState(record, 123, -1, "token", "worker-a")

    def unknown(*args):
        raise ChildProcessError()

    monkeypatch.setattr("os.waitpid", unknown)
    assert host.show(record.handle).liveness == "unverifiable"


async def test_rename_accepts_only_the_existing_canonical_name():
    from raven.contracts.terminal import TerminalError, TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", title="worker-a")
    host._terminals[record.handle] = TerminalState(record, 123, -1, "token", "worker-a")
    assert await host.rename(record.handle, "worker-a") == record
    with pytest.raises(TerminalError):
        await host.rename(record.handle, "worker-b")


async def test_subscription_replays_raw_startup_before_live_output():
    import asyncio

    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    state = TerminalState(record, 123, -1, "token", "worker-a")
    host._terminals[record.handle] = state
    await host.observe_output(state, b"\x1b[31mstartup")
    seen = []
    async def callback(data):
        await asyncio.sleep(0)
        seen.append(data)
    unsubscribe = host.subscribe(record.handle, callback)
    for subscriber in tuple(state.subscribers):
        await subscriber(b"live")
    assert seen == [b"\x1b[31mstartup", b"live"]
    unsubscribe()
    assert not state.subscribers


async def test_known_codex_native_idle_titles_clear_startup_and_working():
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    state = TerminalState(record, 123, -1, "token", "worker-a", startup_pending=True)
    state.provider = "codex"
    host._terminals[record.handle] = state
    await host.observe_output(state, b"\x1b]0;work\x07")
    assert record.status == "idle"
    assert not state.startup_pending
    await host.observe_output(state, "\x1b]0;\u280b Reply\x07".encode())
    assert record.status == "working"
    await host.observe_output(state, b"\x1b]0;Reply\x07")
    assert record.status == "idle"


@pytest.mark.parametrize("status", ["working", "idle"])
async def test_human_input_does_not_restore_dirty_after_status_observation(status):
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", status="idle")
    state = TerminalState(record, 123, -1, "token", "worker-a", composer_dirty=True)
    host._terminals[record.handle] = state

    async def write(handle, data):
        await host.set_status(handle, status)

    host.write = write
    await host.input(record.handle, b"human input")
    assert not state.composer_dirty


async def test_explicit_clear_clears_dirty_but_idle_alone_does_not():
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", status="idle")
    state = TerminalState(record, 123, -1, "token", "worker-a", composer_dirty=True)
    host._terminals[record.handle] = state

    async def write(handle, data):
        pass

    host.write = write
    await host.set_status(record.handle, "idle")
    assert state.composer_dirty
    await host.input(record.handle, b"\x15")
    await host.set_status(record.handle, "idle")
    assert not state.composer_dirty


@pytest.mark.parametrize(
    "chunks",
    [
        [b"\x1b[?1;2c"],
        [b"\x1b[24;80R"],
        [b"\x1b[I\x1b[O\x1b[?2004;1$y"],
        [b"\x1b]10;rgb:ffff/ffff/ffff\x07"],
        [b"\x1b]11;rgb:0000/0000/0000\x1b\\"],
        [b"\x1bOP"],
        [b"\x1b", b"O", b"P"],
        [b"\x1b", b"[?1;", b"2c\x1b[24;", b"80R"],
        [b"\x1b]11;rgb:0000", b"/0000/0000\x1b", b"\\"],
        [b"\x1b]52;c;\x15\x03payload\x07"],
        [b"\x1bP1$r0m\x1b\\\x1b(B"],
    ],
)
async def test_terminal_reports_preserve_composer_and_human_input_state(chunks):
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    state = TerminalState(record, 123, -1, "token", "worker-a")
    host._terminals[record.handle] = state
    written = []

    async def write(handle, data):
        written.append(data)

    host.write = write
    for dirty in (False, True):
        state.composer_dirty = dirty
        for chunk in chunks:
            await host.input(record.handle, chunk)
        assert state.composer_dirty is dirty
        assert not state.human_input_pending
    assert written == chunks * 2


@pytest.mark.parametrize("text", [b"hello", b"\x08", b"\t", b"\x7f", b"\xc3\xa9"])
async def test_printable_and_editing_input_between_reports_is_human_input(text):
    from raven.contracts.terminal import TerminalRecord
    from raven.terminal.host import TerminalHost, TerminalState

    host = TerminalHost()
    record = TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work")
    state = TerminalState(record, 123, -1, "token", "worker-a")
    host._terminals[record.handle] = state

    async def write(handle, data):
        pass

    host.write = write
    await host.input(record.handle, b"\x1b[?1;2c\x1b[200~" + text + b"\x1b[201~\x1b[24;80R")
    assert state.composer_dirty
    assert state.human_input_pending
