"""One ``mirasim ui-cli`` command: its argv, its environment, the JSON it answers with.

Mirasim drives its agents through ``ui-cli``, one command per request, so every
ACP method this package serves ends in one of these. Each rule below was
measured against Mirasim 0.0.465:

- Without ``--port`` a command starts a private server of its own, bound to
  loopback and gone when the command exits. Nothing here keeps one running, so
  this adds no standing server to a machine that may already run Mirasim's.
- stdout carries that server's log lines ahead of the one JSON document the
  command answers with (``[server 12:31:37] ...``, ``[model-inject ...]``), and
  one of them is the server's own URL with its access token in the query. The
  document is therefore read from the end of stdout, and nothing else on stdout
  is passed on. The document and the command's error text retain Mirasim's content.
- A failed turn can exit 0. A Codex that could not open its state answered
  ``phase: "done"`` with ``error`` set and ``incomplete: true``, so the document
  decides whether a turn worked, not the exit code.
- ``--timeout`` bounds the turn: given 5 s, a turn still streaming came back
  with ``settled: false`` on exit 0, and its agent went down with the server.
  The help calls the flag a per-request timeout defaulting to 30 s, yet a 79 s
  turn settled without it, so the default is not something to lean on either
  way. A turn is given :data:`TURN_TIMEOUT_MS`, below the 2**31 - 1 ms past
  which a Node timer fires at once, and ends when the caller cancels it.
- The agent the server starts runs in a process session of its own. When
  ``ui-cli`` exits on its own, the agent goes with the server; SIGINT, SIGTERM
  or SIGKILL to ``ui-cli`` or its process group left the agent, its MCP servers
  and the tool it was running alive under PID 1. So a cancel ends the tree it
  recorded first (:func:`end_tree`), and every command runs under
  :mod:`raven.acp.mirasim.guard`, which does the same when the adapter itself
  is killed.
- A value is passed as ``--flag=value``, so a prompt that begins with a dash
  cannot be read as a flag. ``--text -`` is the literal text "-" rather than
  stdin, so a prompt too long for one argv entry travels as a ``--file``.
- A turn's private server announces itself on stdout as it boots
  (``mirasim server v0.0.465: http://localhost:<port>/?token=...``), and other
  commands reach it for as long as the turn runs with ``--port <port>``, which
  reads the token from Mirasim's own run directory. That is how a running
  turn is watched, answered and stopped from outside it (:func:`server_port`);
  nothing here reads or keeps the token.
- A server keeps a session's route -- and its other per-session settings -- in
  an index named after the directory it runs in, ``--workdir`` or else the
  command's own working directory, and a server started in another directory
  does not see them: measured, a route set from one directory was absent from
  the next server's index in another. So :class:`UiCli` can be given one
  directory for every server it starts, which then holds whatever directory
  Raven itself was started in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXECUTABLE = "mirasim"

TURN_TIMEOUT_MS = 2_000_000_000
"""What a turn's ``--timeout`` is set to: about 23 days, which is no limit in
practice, and inside the range a Node timer holds."""

QUERY_TIMEOUT_S = 90.0
"""How long a read-only command (``agents``, ``catalog``) may take. Measured at 7 s
alone; eighteen at once, which is nine adapters starting together, took 42 s.
It stays inside the presets' 120 s ``readyTimeoutMs``, which a first
``session/new`` spends waiting on these, so a Mirasim that hangs is reported in
its own words rather than as an agent that never became ready."""

INLINE_TEXT_LIMIT = 100_000
"""The most bytes of prompt text passed as ``--text``. Linux refuses a single argv
entry over 128 KiB (``E2BIG``), and a delegated prompt carrying its upstream
results can be that long; past this the text goes in a file instead."""

PROMPT_FILE_NOTE = "This message continues in the attached file {name}. Read all of it and act on it."

_WINDOWS_COMMAND_LINE_LIMIT = 32_767

TREE_GRACE_S = 2.0
"""How long a cancelled turn's processes get to exit on SIGTERM before SIGKILL.

Bounded well inside the five seconds Raven's client gives a cancelled turn to
settle (``_CANCEL_SETTLE_S`` in ``raven/acp_client/client.py``). Measured: the
MCP servers and the tool exited within a second of SIGTERM, and ``claude``
itself was still running three seconds after it, so the second signal is not a
formality."""

GUARD_SLACK_S = 1.0
"""How long past :data:`TREE_GRACE_S` a guard has to exit after a cancel before
it is ended from here, together with what it was guarding."""

_GUARD = ("-P", "-m", "raven.acp.mirasim.guard", "--")
_LOG_LINE = re.compile(r"^\[[^\]\s]+ \d{2}:\d{2}:\d{2}\] ")
_SESSION_GONE = "not one of this server's sessions"
_SERVER_URL = re.compile(r"\bmirasim server v\S+: http://(?:localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})/")
_PORT_SCAN_BYTES = 64 * 1024
"""How much of a command's stdout is searched for its server's announcement,
which comes among the first boot lines; the rest of a long reply is not."""
_CHUNK = 64 * 1024


class UiCliError(RuntimeError):
    """A ``ui-cli`` command could not be run, or did not answer with a document."""


def is_log_line(line: str) -> bool:
    """Whether ``line`` is one of the server's own timestamped log lines."""
    return bool(_LOG_LINE.match(line))


def last_document(stdout: str) -> Any:
    """The JSON document a command answered with, or ``None`` if it printed none.

    The document is the last value that opens at the start of a line and runs to
    the end of stdout, with nothing after it but log lines. Read from the end so
    a log line that happens to start with a brace cannot be taken for it, and
    raw-decoded rather than parsed line by line because the document is
    pretty-printed across many.
    """
    decoder = json.JSONDecoder()
    starts: list[int] = []
    offset = 0
    for line in stdout.splitlines(keepends=True):
        if line[:1] in ("{", "[") and not is_log_line(line):
            starts.append(offset)
        offset += len(line)
    for start in reversed(starts):
        try:
            value, end = decoder.raw_decode(stdout, start)
        except ValueError:
            continue
        if all(not rest.strip() or is_log_line(rest) for rest in stdout[end:].splitlines()):
            return value
    return None


def session_gone(failure: str) -> bool:
    """Whether a refused ``--session`` says Mirasim no longer has that conversation."""
    return _SESSION_GONE in failure


def server_port(text: str) -> int | None:
    """The port a private server announced in ``text``, or ``None``.

    Only a loopback address counts: a server announcing any other host is not
    one this adapter started, and nothing is sent to it.
    """
    match = _SERVER_URL.search(text)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 0 < port < 65536 else None


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment a ``ui-cli`` command runs under: ``env``, less a borrowed ``CODEX_HOME``.

    A ``CODEX_HOME`` inside Mirasim's own state directory is the home Mirasim
    built for a Codex it launched, inherited by whatever that Codex started --
    a Raven run from inside it, for instance. Handed back to Mirasim it fails
    every Codex turn, because Mirasim builds that home itself for each launch:
    measured, ``failed to initialize sqlite state runtime under
    ~/.mirasim/agent-homes/codex-relay``, on an exit status of 0. One the user
    pointed anywhere else is theirs and is kept, and so is everything else:
    Claude Code's own session markers (``CLAUDECODE``, ``CLAUDE_CODE_ENTRYPOINT``)
    were measured to change nothing about the Claude Code Mirasim starts.
    """
    out = dict(env)
    codex_home = out.get("CODEX_HOME", "").strip()
    if codex_home:
        state = Path(out.get("MIRASIM_HOME", "").strip() or Path.home() / ".mirasim")
        try:
            inside = Path(codex_home).expanduser().resolve().is_relative_to(state.expanduser().resolve())
        except OSError:
            inside = False
        if inside:
            del out["CODEX_HOME"]
    return out


def prompt_args(
    *,
    text: str,
    agent: str,
    cwd: str,
    session_key: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    route: str | None = None,
    files: Sequence[str] = (),
) -> list[str]:
    """The ``ui-cli`` arguments of one turn.

    A new conversation names the agent and the directory it works in; one that
    continues names only the session, which carries both. ``--model`` is sent on
    every turn the caller has one for, because a resumed session keeps the model
    it last ran on -- measured: resumed without the flag, a session last run on
    a platform model stayed on it rather than returning to the agent's default.

    ``route`` (``platform`` or ``own``) goes with a new conversation only: the
    flag is read on a new session's first turn, so a continued one is put on its
    route by :func:`route_args` before the turn instead.
    """
    args = ["prompt", f"--text={text}", f"--timeout={TURN_TIMEOUT_MS}"]
    if session_key:
        args.append(f"--session={session_key}")
    else:
        args += [f"--agent={agent}", f"--cwd={cwd}"]
        if route:
            args.append(f"--route={route}")
    if model:
        args.append(f"--model={model}")
    if effort:
        args.append(f"--effort={effort}")
    args += [f"--file={path}" for path in files]
    return args


def route_args(session_key: str, route: str) -> list[str]:
    """Put a session on ``route``: ``platform``, ``own``, or ``default`` for Mirasim's own setting.

    Measured: it answers ``{sessionKey, route}`` for any key, one Mirasim has no
    conversation under included, so a forgotten conversation shows only on the
    turn that follows.
    """
    return ["session", "route", f"--session={session_key}", f"--to={route}"]


def sessions_args(seconds: int) -> list[str]:
    """Ask a server for its session rows (``listSessions``), waiting at most ``seconds``."""
    return ["raw", json.dumps({"type": "listSessions"}), "--await=sessions", f"--seconds={seconds}"]


def snapshot_args(session_key: str, seconds: int) -> list[str]:
    """Ask a server for one session's live snapshot (``subscribe``), waiting at most ``seconds``."""
    return [
        "raw",
        json.dumps({"type": "subscribe", "sessionKey": session_key}),
        "--await=snapshot",
        f"--seconds={seconds}",
    ]


def answer_args(prompt_id: str, action: str, value: str | None = None) -> list[str]:
    """Resolve one interaction; ``value`` goes with ``select`` and ``text``."""
    args = ["answer", f"--prompt-id={prompt_id}", f"--action={action}"]
    if value is not None:
        args.append(f"--value={value}")
    return args


def live_session(doc: Any) -> str | None:
    """The one session a server is running, from its ``listSessions`` answer, or ``None``.

    The rows come from Mirasim's shared store, so they include every
    conversation in it, another server's running ones as well. ``transport``
    is the one field a server sets only on a session whose agent it started
    itself -- measured: a fresh server listed sixty rows and none had it -- so
    on a turn's own server it marks that turn's session. Two such rows would be
    a server this adapter did not start, and neither is guessed at.
    """
    found = {
        row["sessionKey"]
        for frame in _responses(doc)
        if frame.get("type") == "sessions" and isinstance(frame.get("sessions"), list)
        for row in frame["sessions"]
        if isinstance(row, dict) and isinstance(row.get("sessionKey"), str) and isinstance(row.get("transport"), dict)
    }
    return found.pop() if len(found) == 1 else None


def snapshot_of(doc: Any, session_key: str) -> dict[str, Any] | None:
    """The live snapshot a ``subscribe`` answer carries for ``session_key``, or ``None``.

    A server sends one only for a session it is running; for any other it
    answers with the queue and nothing else.
    """
    for frame in _responses(doc):
        snapshot = frame.get("snapshot")
        if frame.get("type") == "snapshot" and frame.get("sessionKey") == session_key and isinstance(snapshot, dict):
            return snapshot
    return None


def _responses(doc: Any) -> list[dict[str, Any]]:
    frames = doc.get("responses") if isinstance(doc, dict) else None
    return [frame for frame in frames if isinstance(frame, dict)] if isinstance(frames, list) else []


@dataclass(frozen=True)
class Outcome:
    """What one command left behind."""

    exit_code: int | None
    document: Any
    stderr: str
    cancelled: bool = False
    """True only when a cancel ended the command while it was still running. A
    cancel that lands after it exited leaves the real result standing."""

    @property
    def failure(self) -> str:
        """The command's own last word on stderr; empty if it said nothing."""
        for line in reversed(self.stderr.splitlines()):
            line = line.strip()
            if line and not is_log_line(line):
                return line
        return ""


def _guard_argv(argv: list[str]) -> list[str]:
    return [sys.executable, *_GUARD, *argv]


def _fits_windows_command_line(argv: list[str]) -> bool:
    # CreateProcess counts UTF-16 units, including quoting and the final NUL.
    command = subprocess.list2cmdline(_guard_argv(argv))
    return len(command.encode("utf-16-le")) // 2 + 1 <= _WINDOWS_COMMAND_LINE_LIMIT


@contextlib.contextmanager
def _staged_command(argv: list[str]) -> Iterator[list[str]]:
    """Keep the complete Windows guard command within the native launch limit."""
    if sys.platform != "win32" or _fits_windows_command_line(argv):
        yield argv
        return
    index = next((i for i, arg in enumerate(argv) if arg.startswith("--text=")), None)
    if index is None:
        raise UiCliError("command exceeds the Windows command-line limit")
    with tempfile.TemporaryDirectory(prefix="raven-mirasim-") as folder:
        path = Path(folder) / "prompt.md"
        path.write_text(argv[index].removeprefix("--text="), encoding="utf-8", newline="")
        staged = list(argv)
        staged[index] = "--text=" + PROMPT_FILE_NOTE.format(name=path.name)
        staged.append(f"--file={path}")
        if not _fits_windows_command_line(staged):
            raise UiCliError("command exceeds the Windows command-line limit even with its prompt in a file")
        yield staged


class Invocation:
    """One ``ui-cli`` command, which a cancel ends together with everything it started.

    The command runs under :mod:`raven.acp.mirasim.guard`, on this interpreter,
    in a session of the guard's own and with the guard's stdin held open here: a
    cancel closes that pipe, and so does this process dying, which is the one
    end a handler here could not see.
    """

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str | None) -> None:
        self.argv = argv
        self._env = env
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self._ended = False
        self.port: int | None = None
        """The port the command's own server announced, once it has (:func:`server_port`)."""
        self._announced = asyncio.Event()

    async def announced(self) -> int | None:
        """The port the command's server announced; ``None`` once the command ended without one."""
        await self._announced.wait()
        return self.port

    async def run(self) -> Outcome:
        """Run to completion and read what the command printed."""
        try:
            return await self._run()
        finally:
            self._announced.set()

    async def _run(self) -> Outcome:
        if self._cancel_requested:
            return Outcome(None, None, "", cancelled=True)
        if shutil.which(self.argv[0], path=self._env.get("PATH")) is None:
            raise UiCliError(f"cannot start {self.argv[0]!r}: no such executable")
        with _staged_command(self.argv) as argv:
            return await self._execute(argv)

    async def _execute(self, argv: list[str]) -> Outcome:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_guard_argv(argv),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
            )
        except OSError as exc:
            raise UiCliError(f"cannot start the command guard for {self.argv[0]!r}: {exc}") from exc
        proc = self._proc
        if self._cancel_requested:
            await self._end()
        try:
            # Not communicate(), which closes stdin at once -- and a closed
            # stdin is what tells the guard to end the command.
            out, err, _ = await asyncio.gather(self._read_out(proc.stdout), proc.stderr.read(), proc.wait())
        except asyncio.CancelledError:
            await asyncio.shield(self._end())
            raise
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
        return Outcome(
            proc.returncode,
            last_document(out.decode("utf-8", "replace")),
            err.decode("utf-8", "replace"),
            cancelled=self._ended,
        )

    async def _read_out(self, stream: asyncio.StreamReader) -> bytes:
        """All of stdout, noting the server's port the moment its announcement arrives."""
        out = bytearray()
        while chunk := await stream.read(_CHUNK):
            out += chunk
            # The whole head is searched again on each chunk that reaches into
            # it, so an announcement split across two chunks is still found.
            if self.port is None and len(out) - len(chunk) < _PORT_SCAN_BYTES:
                self.port = server_port(out[:_PORT_SCAN_BYTES].decode("utf-8", "replace"))
                if self.port is not None:
                    self._announced.set()
        return bytes(out)

    async def cancel(self) -> None:
        """End the command and its tree if it is still running; harmless otherwise."""
        self._cancel_requested = True
        if self._proc is not None:
            await self._end()

    async def _end(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None or self._ended:
            return
        self._ended = True
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), TREE_GRACE_S + GUARD_SLACK_S)
        except asyncio.TimeoutError:
            await end_tree(proc, grace_s=GUARD_SLACK_S)


class UiCli:
    """Runs ``mirasim`` commands under one environment, their servers in one directory when given one."""

    def __init__(
        self,
        *,
        executable: str = EXECUTABLE,
        env: Mapping[str, str] | None = None,
        workdir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.env = child_env(os.environ if env is None else env)
        self.executable = shutil.which(executable, path=self.env.get("PATH")) or executable
        self.workdir = os.fspath(workdir) if workdir else None

    def start(self, args: Sequence[str], *, cwd: str | None = None, port: int | None = None) -> Invocation:
        """An unstarted ``mirasim ui-cli <args>``, sent to the server on ``port`` when one is named.

        Without a port the command starts a server of its own, which is told
        :attr:`workdir`; one sent to a running server starts none.
        """
        if port is not None:
            head = [self.executable, "ui-cli", f"--port={port}"]
        else:
            head = [self.executable, "ui-cli", *([f"--workdir={self.workdir}"] if self.workdir else [])]
        return Invocation([*head, *args], env=self.env, cwd=cwd)

    async def query(self, args: Sequence[str], *, timeout_s: float = QUERY_TIMEOUT_S, port: int | None = None) -> Any:
        """The document a command answers with, or :class:`UiCliError`."""
        invocation = self.start(args, port=port)
        try:
            outcome = await asyncio.wait_for(invocation.run(), timeout_s)
        except asyncio.TimeoutError:
            raise UiCliError(f"`mirasim ui-cli {args[0]}` did not answer within {timeout_s:.0f}s") from None
        if outcome.exit_code != 0:
            raise UiCliError(outcome.failure or f"`mirasim ui-cli {args[0]}` exited {outcome.exit_code}")
        if outcome.document is None:
            raise UiCliError(f"`mirasim ui-cli {args[0]}` printed no JSON document")
        return outcome.document

    async def version(self, *, timeout_s: float = 30.0) -> str:
        """``mirasim version``, which starts no server."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable,
                "version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except OSError as exc:
            raise UiCliError(f"cannot start {self.executable!r}: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise UiCliError(f"`mirasim version` did not answer within {timeout_s:.0f}s") from None
        version = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not version:
            said = err.decode("utf-8", "replace").strip()
            raise UiCliError(f"`mirasim version` exited {proc.returncode}" + (f": {said}" if said else ""))
        return version.splitlines()[-1].strip()


async def exit_status(proc: asyncio.subprocess.Process) -> int:
    """``proc``'s exit status once it has exited, whoever still holds its pipes.

    Not ``proc.wait()``: asyncio settles that only when the process has exited
    and every one of its pipes has closed, and a process it started that
    inherited them keeps them open for as long as it lives.
    """
    while proc.returncode is None:
        await asyncio.sleep(0.05)
    return proc.returncode


async def end_tree(proc: asyncio.subprocess.Process, *, grace_s: float = TREE_GRACE_S) -> None:
    """Stop ``proc`` and every process it started, and wait for ``proc`` to exit.

    The tree is read before the first signal, because the moment ``ui-cli``
    dies its children are reparented and nothing links them to it any more.
    Each descendant's process group is signalled as a whole -- Mirasim gives an
    agent a session of its own, and the agent gives each tool call another -- so
    a grandchild the snapshot missed still goes with its group. ``proc`` itself
    shares this adapter's group and is signalled by pid, which is also why that
    one group is never signalled whole: it holds this process too.

    The tree is read again before the second signal. An agent that outlives the
    first -- Claude Code does -- can start a tool meanwhile in a session of its
    own, which is in no group signalled so far.
    """
    if os.name == "nt":
        await _taskkill(proc)
        return
    tree = await asyncio.to_thread(descendants, proc.pid)
    signal_tree(tree, signal.SIGTERM, pid=proc.pid)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_s
    while loop.time() < deadline and (proc.returncode is None or any(_alive(pid) for pid, _ in tree)):
        await asyncio.sleep(0.05)
    if proc.returncode is None or any(_alive(pid) for pid, _ in tree):
        tree += await asyncio.to_thread(descendants, proc.pid)
        signal_tree(tree, signal.SIGKILL, pid=proc.pid)
    await exit_status(proc)


def signal_tree(tree: list[tuple[int, int]], sig: signal.Signals, *, pid: int | None = None) -> None:
    """Send ``sig`` to each ``(pid, pgid)`` of ``tree``, and to ``pid`` when one is given.

    A process in a group of its own is signalled through the group, so whatever
    it started there goes too. One in this process's group is signalled alone,
    since signalling that group would reach this process as well.
    """
    own_group = os.getpgrp()
    for target in (*(() if pid is None else (pid,)), *(child for child, pgid in tree if pgid == own_group)):
        _quietly(os.kill, target, sig)
    for pgid in {pgid for _, pgid in tree if pgid not in (own_group, 0)}:
        _quietly(os.killpg, pgid, sig)


def descendants(root: int) -> list[tuple[int, int]]:
    """Every live descendant of ``root`` as ``(pid, pgid)``, children before theirs."""
    table = _process_table()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    out: list[tuple[int, int]] = []
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            out.append((child, table[child][1]))
            pending.append(child)
    return out


def _process_table(proc: Path = Path("/proc")) -> dict[int, tuple[int, int]]:
    """``{pid: (ppid, pgid)}`` for every live process: ``/proc`` where there is one, else ``ps``."""
    if (proc / "self" / "stat").exists():
        return _proc_table(proc)
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="],  # noqa: S607 -- ps location varies; PATH lookup intended
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return _ps_table(listing)


def _proc_table(proc: Path) -> dict[int, tuple[int, int]]:
    table: dict[int, tuple[int, int]] = {}
    for entry in os.scandir(proc):
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The command name is parenthesised and may itself hold spaces and
        # parentheses, so the fields are counted from the last ")".
        fields = stat.rpartition(")")[2].split()
        if len(fields) >= 3 and fields[0] != "Z":
            table[int(entry.name)] = (int(fields[1]), int(fields[2]))
    return table


def _ps_table(listing: str) -> dict[int, tuple[int, int]]:
    """Read ``ps -o pid=,ppid=,pgid=,stat=`` (macOS and the BSDs), zombies left out."""
    table: dict[int, tuple[int, int]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 3 or not all(part.isdigit() for part in parts[:3]):
            continue
        if len(parts) > 3 and parts[3].startswith("Z"):
            continue
        table[int(parts[0])] = (int(parts[1]), int(parts[2]))
    return table


def _alive(pid: int, proc: Path = Path("/proc")) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = proc / str(pid) / "stat"
    if stat.exists():
        try:
            return stat.read_text(encoding="utf-8", errors="replace").rpartition(")")[2].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    return True


def _quietly(send: Any, target: int, sig: signal.Signals) -> None:
    try:
        send(target, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _taskkill(proc: asyncio.subprocess.Process) -> None:
    """Windows: ``taskkill /T`` walks the child tree by pid, the platform's own way to do this."""
    taskkill = shutil.which("taskkill")
    if taskkill:
        killer = await asyncio.create_subprocess_exec(
            taskkill,
            "/T",
            "/F",
            "/PID",
            str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await exit_status(proc)


__all__ = [
    "EXECUTABLE",
    "GUARD_SLACK_S",
    "INLINE_TEXT_LIMIT",
    "QUERY_TIMEOUT_S",
    "TREE_GRACE_S",
    "TURN_TIMEOUT_MS",
    "Invocation",
    "Outcome",
    "UiCli",
    "UiCliError",
    "answer_args",
    "child_env",
    "descendants",
    "end_tree",
    "exit_status",
    "is_log_line",
    "last_document",
    "live_session",
    "prompt_args",
    "route_args",
    "server_port",
    "session_gone",
    "sessions_args",
    "signal_tree",
    "snapshot_args",
    "snapshot_of",
]
