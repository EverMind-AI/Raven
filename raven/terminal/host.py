"""POSIX PTY processes with bounded output and title/hook-derived agent activity."""

from __future__ import annotations

import asyncio
import codecs
import errno
import hmac
import os
import re
import secrets
import shlex
import shutil
import signal
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven.contracts.terminal import TerminalError, TerminalRecord

StatusCallback = Callable[[str, dict], Awaitable[None]]
OutputCallback = Callable[[bytes], Awaitable[None]]
_AGENT = re.compile(r"(?<![\w./\\-])(?:codex|claude)(?![\w/\\-])", re.I)
_IDLE = re.compile(r"(?<![\w./\\-])(?:ready|idle|done)(?![\w-])", re.I)
_WORKING = re.compile(r"(?<![\w./\\-])(?:working|thinking|running)(?![\w-])", re.I)
_SPINNER = re.compile(r"[\u2800-\u28ff\u25d0-\u25d3]")


def detect_title_status(title: str) -> str | None:
    if re.fullmatch(r"\s*claude\s+agents\s*", title, re.I):
        return None
    if title == "\u2733" or title.startswith("\u2733 "):
        return "idle"
    if _SPINNER.search(title):
        return "working"
    if not _AGENT.search(title):
        return None
    if any(word in title.lower() for word in ("action required", "permission", "waiting")):
        return "permission"
    if _IDLE.search(title):
        return "idle"
    if _WORKING.search(title) or title.startswith(". "):
        return "working"
    return "idle"


class OutputParser:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"
        self.sequence = ""

    def feed(self, data: bytes) -> tuple[str, list[str], int]:
        plain: list[str] = []
        titles: list[str] = []
        cursors = 0
        for char in self.decoder.decode(data):
            if self.state == "text":
                if char == "\x1b":
                    self.state = "escape"
                elif char in "\n\t" or ord(char) >= 32:
                    plain.append(char)
            elif self.state == "escape":
                self.sequence = ""
                self.state = {"[": "csi", "]": "osc", "P": "discard", "_": "discard", "^": "discard"}.get(char, "text")
            elif self.state == "csi":
                self.sequence += char
                if "@" <= char <= "~":
                    cursors += self.sequence == "?25h"
                    self.state = "text"
                elif len(self.sequence) > 4096:
                    self.state = "text"
            elif self.state == "osc":
                if char == "\x07":
                    self._title(titles)
                elif char == "\x1b":
                    self.state = "osc_escape"
                elif len(self.sequence) < 4096:
                    self.sequence += char
                else:
                    self.state = "discard"
            elif self.state == "osc_escape":
                if char == "\\":
                    self._title(titles)
                else:
                    self.state = "discard"
            elif self.state == "discard":
                if char == "\x1b":
                    self.state = "discard_escape"
                elif char == "\x07":
                    self.state = "text"
            elif self.state == "discard_escape":
                self.state = "text" if char == "\\" else "discard"
        return "".join(plain), titles, cursors

    def _title(self, titles: list[str]) -> None:
        kind, separator, title = self.sequence.partition(";")
        if separator and kind in {"0", "1", "2"}:
            titles.append(title)
        self.state = "text"
        self.sequence = ""


@dataclass
class TerminalState:
    record: TerminalRecord
    pid: int
    fd: int
    launch_token: str
    canonical_name: str
    parser: OutputParser = field(default_factory=OutputParser)
    output_tail: str = ""
    raw_tail: bytes = b""
    replay_tasks: set[asyncio.Task] = field(default_factory=set)
    agent_title: str = ""
    working_sequence: int = 0
    permission_sequence: int = 0
    show_cursor_sequence: int = 0
    status_seen: bool = False
    startup_pending: bool = False
    provider: str | None = None
    last_output_monotonic: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    subscribers: set[OutputCallback] = field(default_factory=set)
    reader: asyncio.Task | None = None
    returncode: int | None = None
    closed: bool = False
    composer_dirty: bool = False
    blocked_reason: str | None = None


class TerminalHost:
    def __init__(self, *, emit: StatusCallback | None = None, hook_url: str = ""):
        self._terminals: dict[str, TerminalState] = {}
        self.emit = emit
        self.hook_url = hook_url
        self.topology_revisions: dict[str, int] = {}

    async def _emit(self, event: str, data: dict) -> None:
        if self.emit is not None:
            await self.emit(event, data)

    async def create(
        self, worktree_id: str, command: str | list[str], title: str = "", owner: str = "human"
    ) -> TerminalRecord:
        if os.name != "posix":
            raise TerminalError("not_supported", "The PTY host requires POSIX")
        import fcntl
        import termios

        repo, separator, path = worktree_id.partition("::")
        if not repo or not separator or not Path(path).is_absolute() or not Path(path).is_dir():
            raise TerminalError("invalid_worktree", "worktree_id must name an existing absolute directory")
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or not all(isinstance(value, str) and value and "\x00" not in value for value in argv):
            raise TerminalError("invalid_command", "command must contain an executable")
        executable = shutil.which(argv[0])
        if executable is None:
            raise TerminalError("command_not_found", f"Executable not found: {argv[0]}")
        record = TerminalRecord(
            worktree_id=worktree_id, worktree_path=path, title=title, owner=owner, visible=False
        )
        token = secrets.token_urlsafe(32)
        env = {key: value for key, value in os.environ.items() if not key.startswith("ORCA_")}
        env.update(
            TERM="xterm-256color",
            COLORTERM="truecolor",
            TERM_PROGRAM="raven",
            RAVEN_TERMINAL_HANDLE=record.handle,
            RAVEN_PANE_KEY=record.pane_key,
            RAVEN_TAB_ID=record.tab_id,
            RAVEN_WORKTREE_ID=worktree_id,
            RAVEN_LAUNCH_TOKEN=token,
        )
        if self.hook_url:
            env["RAVEN_HOOK_URL"] = self.hook_url
        pid, fd = os.forkpty()
        if pid == 0:
            try:
                os.chdir(path)
                os.execve(executable, argv, env)
            except BaseException:
                os._exit(127)
        os.set_blocking(fd, False)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        record.connected = True
        record.writable = True
        record.liveness = "live"
        state = TerminalState(record, pid, fd, token, title)
        if Path(argv[0]).name in {"codex", "codex.exe"}:
            state.provider = "codex"
            state.startup_pending = True
        self._terminals[record.handle] = state
        self.topology_revisions[worktree_id] = self.topology_revisions.get(worktree_id, 0) + 1
        state.reader = asyncio.create_task(self._read(state))
        await self._emit("terminal.created", record.model_dump(by_alias=True, mode="json"))
        return record

    def state(self, handle: str) -> TerminalState:
        try:
            return self._terminals[handle]
        except KeyError as exc:
            raise TerminalError("terminal_not_found", f"Unknown terminal: {handle}") from exc

    def show(self, handle: str) -> TerminalRecord:
        state = self.state(handle)
        if state.returncode is None:
            try:
                pid, status = os.waitpid(state.pid, os.WNOHANG)
                if pid:
                    state.returncode = os.waitstatus_to_exitcode(status)
                    state.record.liveness = "exited"
                    state.record.connected = False
                    state.record.writable = False
                    state.changed.set()
            except (ChildProcessError, PermissionError):
                state.record.liveness = "unverifiable"
        return state.record

    def list(self, worktree_id: str | None = None) -> list[TerminalRecord]:
        return [
            self.show(handle)
            for handle, state in self._terminals.items()
            if not state.closed and (worktree_id is None or state.record.worktree_id == worktree_id)
        ]

    def tail(self, handle: str) -> str:
        return self.state(handle).output_tail

    async def _ready(self, fd: int, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def ready():
            if not future.done():
                future.set_result(None)

        add = loop.add_writer if write else loop.add_reader
        remove = loop.remove_writer if write else loop.remove_reader
        add(fd, ready)
        try:
            await future
        finally:
            remove(fd)

    async def _read(self, state: TerminalState) -> None:
        try:
            while not state.closed:
                try:
                    data = os.read(state.fd, 65536)
                except BlockingIOError:
                    await self._ready(state.fd)
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                await self.observe_output(state, data)
                for subscriber in tuple(state.subscribers):
                    try:
                        await subscriber(data)
                    except Exception:
                        state.subscribers.discard(subscriber)
        except asyncio.CancelledError:
            raise
        except OSError:
            state.record.liveness = "unverifiable"
        finally:
            state.record.connected = False
            state.record.writable = False
            self.show(state.record.handle)
            state.changed.set()

    async def observe_output(self, state: TerminalState, data: bytes) -> None:
        state.raw_tail = (state.raw_tail + data)[-256 * 1024 :]
        plain, titles, cursors = state.parser.feed(data)
        state.last_output_monotonic = time.monotonic()
        state.record.last_output_at = int(time.time() * 1000)
        state.show_cursor_sequence += cursors
        combined = (state.output_tail + plain).encode("utf-8")[-256 * 1024 :].decode("utf-8", "ignore")
        state.output_tail = "".join(combined.splitlines(keepends=True)[-2000:])
        for title in titles:
            state.agent_title = title
            status = detect_title_status(title)
            if status is None and state.provider == "codex" and title:
                if not state.startup_pending or title == Path(state.record.worktree_path).resolve().name:
                    status = "idle"
            if status is not None:
                await self.set_status(state.record.handle, status)
        state.changed.set()

    async def set_status(self, handle: str, status: str, *, blocked_reason: str | None = None) -> None:
        if status not in {"idle", "working", "permission"}:
            raise TerminalError("invalid_status", "Unknown agent status")
        state = self.state(handle)
        if status == "working" and state.record.status != "working":
            state.working_sequence += 1
            state.composer_dirty = False
        if status == "permission":
            state.permission_sequence += 1
        if status == "idle":
            state.startup_pending = False
        state.record.status = status
        state.status_seen = True
        state.blocked_reason = blocked_reason if status == "permission" else None
        state.changed.set()
        await self._emit("terminal.status", {"handle": handle, "status": status, "liveness": state.record.liveness})

    async def write(self, handle: str, data: bytes) -> None:
        state = self.state(handle)
        if not self.show(handle).writable or state.closed:
            raise TerminalError("terminal_not_writable", "Terminal is not writable")
        view = memoryview(data)
        while view:
            try:
                count = os.write(state.fd, view)
            except BlockingIOError:
                await self._ready(state.fd, write=True)
                continue
            except OSError as exc:
                raise TerminalError("terminal_not_writable", "Terminal write failed") from exc
            view = view[count:]

    async def input(self, handle: str, data: bytes) -> None:
        state = self.state(handle)
        async with state.send_lock:
            await self.write(handle, data)
            if b"\x03" in data or b"\x15" in data:
                state.composer_dirty = False
            elif any(byte >= 32 or byte in (8, 9, 127) for byte in data):
                state.composer_dirty = True

    async def resize(self, handle: str, cols: int, rows: int) -> None:
        import fcntl
        import termios

        if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
            raise TerminalError("invalid_size", "Terminal size must be between 1 and 1000")
        state = self.state(handle)
        if state.closed or not state.record.writable:
            raise TerminalError("terminal_not_writable", "Terminal is not writable")
        fcntl.ioctl(state.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def raw_snapshot(self, handle: str) -> bytes:
        return self.state(handle).raw_tail

    def subscribe(
        self, handle: str, callback: OutputCallback, *, replay_callback: OutputCallback | None = None
    ) -> Callable[[], None]:
        state = self.state(handle)
        snapshot = state.raw_tail
        active = True

        async def replay() -> None:
            if snapshot and active:
                await (replay_callback or callback)(snapshot)

        task = asyncio.create_task(replay())
        state.replay_tasks.add(task)

        def settled(done: asyncio.Task) -> None:
            state.replay_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                state.subscribers.discard(live)

        async def live(data: bytes) -> None:
            await task
            if active:
                await callback(data)

        task.add_done_callback(settled)
        state.subscribers.add(live)

        def unsubscribe() -> None:
            nonlocal active
            active = False
            state.subscribers.discard(live)
            task.cancel()

        return unsubscribe

    async def rename(self, handle: str, title: str) -> TerminalRecord:
        state = self.state(handle)
        if title != state.canonical_name:
            raise TerminalError("canonical_name_required", "Only the canonical name is accepted")
        return state.record

    async def close(self, handle: str, owner: str) -> None:
        state = self.state(handle)
        if owner not in {"human", state.record.owner}:
            raise TerminalError("forbidden", "Only the creating identity or human may close this terminal")
        if state.closed:
            return
        liveness = self.show(handle).liveness
        if liveness == "unverifiable":
            raise TerminalError("unverifiable", "Cannot verify terminal process ownership")
        if liveness != "exited":
            try:
                if os.getpgid(state.pid) != state.pid:
                    raise TerminalError("unverifiable", "Cannot verify the terminal process group")
                os.killpg(state.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(50):
                if self.show(handle).liveness == "exited":
                    break
                await asyncio.sleep(0.02)
            if state.returncode is None:
                try:
                    os.killpg(state.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for _ in range(100):
                    if self.show(handle).liveness == "exited":
                        break
                    await asyncio.sleep(0.01)
                if state.returncode is None:
                    raise TerminalError("unverifiable", "Terminal exit could not be verified")
        state.closed = True
        state.record.connected = False
        state.record.writable = False
        state.record.visible = False
        state.record.liveness = "exited"
        state.changed.set()
        if state.reader is not None:
            state.reader.cancel()
            await asyncio.gather(state.reader, return_exceptions=True)
        for replay in tuple(state.replay_tasks):
            replay.cancel()
        await asyncio.gather(*state.replay_tasks, return_exceptions=True)
        state.subscribers.clear()
        os.close(state.fd)
        self.topology_revisions[state.record.worktree_id] += 1
        await self._emit("terminal.closed", {"handle": handle})

    async def shutdown(self) -> None:
        for handle in tuple(self._terminals):
            await self.close(handle, "human")

    async def hook(self, pane_key: str, launch_token: str, status: str, *, blocked_reason: str | None = None) -> None:
        state = next(
            (state for state in self._terminals.values() if state.record.pane_key == pane_key and not state.closed),
            None,
        )
        if state is None or not hmac.compare_digest(state.launch_token, launch_token):
            raise TerminalError("forbidden", "Invalid terminal hook binding")
        await self.set_status(state.record.handle, status, blocked_reason=blocked_reason)

    def install_hook_route(self, app: Any) -> None:
        from aiohttp import web

        async def receive(request):
            try:
                body = await request.json()
                await self.hook(
                    body["paneKey"], body["launchToken"], body["status"], blocked_reason=body.get("blockedReason")
                )
            except TerminalError as exc:
                return web.json_response({"error": {"code": exc.code}}, status=403 if exc.code == "forbidden" else 400)
            except (ValueError, KeyError, TypeError):
                return web.json_response({"error": {"code": "invalid_params"}}, status=400)
            return web.json_response({"ok": True})

        app.router.add_post("/terminal/hook", receive)
