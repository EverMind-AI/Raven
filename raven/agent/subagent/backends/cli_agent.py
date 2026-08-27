"""Third-party CLI agent backend: shell out to an external agent (claude code,
codex, ...) as a spawned sub-agent (req5).

Runs on the host rather than through the sandbox executor - these CLIs need the
host's auth, config, and PATH, which is the login shell's rather than raven's
own. Prompt delivery: a ``{prompt}`` token in the
command is substituted as a single argv token (injection-safe), ``{prompt_file}``
as a path to a file holding the prompt; with neither, the prompt goes on the
child's stdin.

With ``resume_command`` set the agent is stateful: an instance handle is bound
to the CLI's own session id in :mod:`raven.agent.subagent.instances`, so a later
spawn with the same handle resumes that session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.backends.base import bounded_delta, clamp_output
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.backends.observability import (
    external_agent_span,
    record_outcome,
    record_transcript,
)
from raven.agent.subagent.backends.transcript import (
    delta_reader,
    parse_claude_stream_json,
    parse_codex_jsonl,
    parse_openclaw_json,
    parse_opencode_json,
)
from raven.agent.subagent.instances import InstanceRegistry, get_registry, hold_handle

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


_READ_CHUNK = 65536
"""Stdout/stderr read size for the streaming pump. Not a line cap: lines are
reassembled from these chunks, so a transcript line of any length is fine."""

_FAILURE_CHARS = 240
"""Cap on what a failed dispatch says out loud. The whole stdout and stderr are
kept on the span (see ``_record``), so this text exists for a person reading a
conversation, not for diagnosis -- and uncapped it put an entire JSON transcript
into one."""


def _one_line(said: str) -> str:
    """One line, capped.

    A structured field holds whatever the agent put in it, newlines included, so
    collapsing is part of the promise rather than tidying: a "one line" that
    reaches a conversation as three is not one.
    """
    return " ".join(said.split())[:_FAILURE_CHARS]


def _parsed(line: str) -> Any:
    """This line's JSON, or ``None`` if it is not a frame.

    The opener test is not a shortcut for the parse; it is what keeps the parse
    off every line of a plain-text transcript.
    """
    if not (line.startswith("{") or line.startswith("[")):
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _framed(line: str) -> bool:
    """Whether this line is structured output rather than a sentence.

    Asymmetric, because the two openers say different amounts. Nothing prints
    prose beginning with ``{``, so an object opener is a frame whether or not it
    parses -- which is what still keeps a transcript truncated mid-line from
    being read out as the failure.

    A leading ``[`` says very little: ``[ERROR] token expired`` and
    ``[2026-08-22T10:11:12] connecting`` are what a great many CLIs print, and
    calling those frames discarded exactly the sentence this module exists to
    find. So a bracket counts only when the line really is JSON.
    """
    if line.startswith("{"):
        return True
    return line.startswith("[") and _parsed(line) is not None


def _human_failure(stdout: str, stderr: str) -> str:
    """The one line worth showing for a CLI that exited non-zero.

    An agent with a JSON transcript format reports its own errors as a frame per
    line, carrying the sentence a person actually needs (an expired login, a
    disabled account, a bad flag) in ``result`` or ``error``. Read from the last
    line back, because the terminal frame is the one holding the verdict.

    Then the last plain line either stream ends with -- stderr first, then
    stdout. Not stderr alone: ``transcript_format`` defaults to ``text``, and
    such a command says ``token expired`` on stdout with nothing on stderr at
    all; so does a JSON-format one that dies before emitting any JSON, on its
    usage message. Framed lines are skipped here, because an unparsed frame is
    the dump this exists to keep out.

    Nothing at all is the last resort, and a fine one: the caller then says
    which agent failed and with what code, which beats showing a transcript.
    """
    for line in reversed(stdout.splitlines()):
        frame = _parsed(line.strip())
        if not isinstance(frame, dict):
            continue
        for key in ("result", "error", "message"):
            said = frame.get(key)
            if isinstance(said, str) and said.strip():
                return _one_line(said)
    for stream in (stderr, stdout):
        for line in reversed(stream.splitlines()):
            line = line.strip()
            if line and not _framed(line):
                return _one_line(line)
    return ""


def _host_home_env() -> dict[str, str]:
    """This raven's own ``RAVEN_HOME``, or nothing when it is running on the default.

    Read from ``os.environ`` at spawn time rather than captured once: a test (and
    ``raven serve``) may point a process at a different home between spawns, and
    a value frozen at import would outlive the change.
    """
    home = os.environ.get("RAVEN_HOME", "").strip()
    return {"RAVEN_HOME": home} if home else {}


class CliAgentTimeoutError(RuntimeError):
    """Raised when a CLI invocation exceeds its configured ``timeout``.

    Kept distinct from a hard non-zero exit so the resume path can tell a slow
    (but possibly still valid) session from a genuinely pruned one.
    """


class CliAgentReportedError(RuntimeError):
    """Raised when a transcript reports its own error (e.g. Claude's ``is_error``)
    despite a zero exit code.

    Kept distinct from a hard non-zero exit for the same reason as
    :class:`CliAgentTimeoutError`.
    """


class CliAgentBackend:
    kind = "cli"
    streams = False
    """Whether *this configured command* will emit reply deltas; see ``_can_stream``.

    False on the class and decided per instance, because for this transport the
    capability is a property of the command rather than of the kind: the same
    backend streams or does not depending on the flags the operator wrote.
    """

    @staticmethod
    def _can_stream(command: str, resume_command: str | None, transcript_format: str | None) -> bool:
        """Whether the configured templates ask the CLI for partial output.

        Read from the command, never declared, for the same reason ``stateful``
        is read from ``resume_command``: the flag is the mechanism that would
        have to deliver it, and a config field claiming otherwise would be a
        wish. Measured per CLI rather than assumed:

        - ``claude`` emits ``content_block_delta`` frames only under
          ``--include-partial-messages``, and only alongside
          ``--output-format stream-json`` -- which is what ``claude_stream_json``
          means. Without the flag the same run prints its answer once, at the end.
        - ``codex exec --json`` has no partial event to ask for: a whole reply
          arrives as one ``item.completed``. No flag turns that into a stream.

        Both templates must carry it. A create that streams and a resume that
        does not would make the same instance stream on its first turn and stop
        on every later one -- and a direct chat is almost entirely resumes.
        """
        if transcript_format != "claude_stream_json":
            return False
        templates = [command, *([resume_command] if resume_command else [])]
        return all("--include-partial-messages" in template for template in templates)

    def __init__(
        self,
        *,
        name: str,
        command: str,
        resume_command: str | None = None,
        id_source: str = "provisioned",
        session_id_pattern: str | None = None,
        output_pattern: str | None = None,
        transcript_format: str = "text",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        max_output_chars: int = 30000,
        registry: InstanceRegistry | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.resume_command = resume_command
        self.id_source = id_source
        self.transcript_format = transcript_format
        self.cwd = cwd
        self.env = env or {}
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._session_id_re = re.compile(session_id_pattern) if session_id_pattern else None
        self._output_re = re.compile(output_pattern) if output_pattern else None
        self._registry = registry or get_registry()
        self.streams = self._can_stream(command, resume_command, transcript_format)

    @property
    def is_stateful(self) -> bool:
        return bool(self.resume_command)

    def _build_argv(self, template: str, prompt: str, prompt_file: str, agent_id: str | None) -> tuple[list[str], bool]:
        argv: list[str] = []
        used_placeholder = False
        for tok in shlex.split(template):
            if agent_id is not None:
                tok = tok.replace("{agent_id}", agent_id)
            if "{prompt}" in tok:
                argv.append(tok.replace("{prompt}", prompt))
                used_placeholder = True
            elif "{prompt_file}" in tok:
                argv.append(tok.replace("{prompt_file}", prompt_file))
                used_placeholder = True
            else:
                argv.append(tok)
        return argv, used_placeholder

    @staticmethod
    async def _kill_process_group(proc: asyncio.subprocess.Process, pgid: int) -> None:
        """Kill the whole process group, not just the launcher.

        A `codex`-style child reparents its real worker; killing only the
        launcher (`proc.kill()`) leaves that worker running, so a manual stop
        would not actually stop anything. This is called only from the
        timeout/cancel paths, i.e. only while `communicate()` has not
        returned -- which means *something* in the group still holds the
        pipes open. That something is not necessarily the launcher: a
        reparented worker can keep the pipes open well after the launcher
        itself has already exited and been reaped, so `proc.returncode` being
        set is not evidence there is nothing left to kill. `killpg` is
        therefore unconditional here, keyed off `pgid` captured at spawn time
        (not re-derived via `os.getpgid(proc.pid)` now, which would risk
        hitting an unrelated process if the launcher's pid was already
        recycled by the OS).
        """
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.returncode is None:
            await proc.wait()

    async def _communicate_streaming(
        self,
        proc: asyncio.subprocess.Process,
        stdin_bytes: bytes | None,
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> tuple[bytes, bytes]:
        """``communicate()``, plus the run's output observed as it lands.

        Returns the same ``(stdout, stderr)`` bytes the buffered call does, so
        everything downstream -- the transcript parse, the session-id search,
        the recorded attempt -- reads exactly what it read before. Streaming is
        an observation of the run, not a second way of getting its result.

        Two observers, both optional and both live-only:

        * ``on_delta`` gets the reply text of each stdout line, where the
          transcript format carries partial text (``delta_reader``);
        * the activity console (:func:`activity.note_console`) gets whatever a
          human watching the terminal would have seen -- extracted reply text
          where a delta reader exists, raw stdout lines for the plain ``text``
          format, and stderr always, since that is where a CLI logs its
          progress. Machine formats with no delta reader (codex_jsonl and kin)
          put nothing readable on stdout mid-run, so only their stderr shows.

        Fixed-size reads with a line buffer of its own rather than
        ``StreamReader.readline``, whose 64 KiB limit raises on a longer line:
        one transcript line carries a whole tool result, and a run that read
        even a modest file would fail on a cap the buffered path never had.
        Bytes are only decoded once a newline has closed the line, so a
        multi-byte character split across two reads cannot be mangled.

        Stdout is drained concurrently with the stdin write for the reason
        ``communicate`` does it: a child that fills the stdout pipe while raven
        is still writing its prompt deadlocks both ends.
        """
        read_delta = delta_reader(self.transcript_format)
        raw_console = read_delta is None and (self.transcript_format or "text") == "text"
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        async def feed() -> None:
            if proc.stdin is None:
                return
            try:
                if stdin_bytes:
                    proc.stdin.write(stdin_bytes)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The child exited without reading its prompt; its own output
                # and exit code are the report, not this.
                pass
            finally:
                proc.stdin.close()

        async def publish_out_line(line: str) -> None:
            if read_delta is not None:
                if text := read_delta(line):
                    activity.note_console(text)
                    if on_delta is not None:
                        await on_delta(text)
            elif raw_console:
                activity.note_console(line + "\n")

        async def pump_out() -> None:
            if proc.stdout is None:
                return
            pending = b""
            while chunk := await proc.stdout.read(_READ_CHUNK):
                out_chunks.append(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    await publish_out_line(line.decode("utf-8", "replace"))
            # A transcript's last line need not end in a newline.
            if pending:
                await publish_out_line(pending.decode("utf-8", "replace"))

        async def pump_err() -> None:
            if proc.stderr is None:
                return
            pending = b""
            while chunk := await proc.stderr.read(_READ_CHUNK):
                err_chunks.append(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    activity.note_console(line.decode("utf-8", "replace") + "\n")
            if pending:
                activity.note_console(pending.decode("utf-8", "replace"))

        pumps = [asyncio.create_task(coro) for coro in (feed(), pump_out(), pump_err())]
        try:
            await asyncio.gather(*pumps)
        finally:
            # gather leaves its siblings running when one of them raises -- a
            # delta sink whose client went away would otherwise leave two tasks
            # reading a subprocess nobody is waiting for any more.
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        await proc.wait()
        return b"".join(out_chunks), b"".join(err_chunks)

    async def _exec(
        self,
        template: str,
        task: str,
        task_id: str,
        cwd: str,
        agent_id: str | None,
        attempts: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        fd, prompt_path = tempfile.mkstemp(prefix=f"raven_subagent_{task_id}_", suffix=".prompt.txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(task)
            argv, used_placeholder = self._build_argv(template, task, prompt_path, agent_id)
            # The capture shells out and can block for real seconds on a slow
            # profile (nvm/conda init); to_thread keeps that off the event loop.
            env_base = await asyncio.to_thread(login_shell_env)
            # Which raven spawned this child is not something a login shell can
            # answer. The capture is there so the child finds the same tools the
            # user's own shell would (nvm, conda, a homebrew PATH); RAVEN_HOME is
            # a different kind of fact -- it names the install this process IS,
            # and it is normally set on the command line rather than in a profile,
            # so the capture comes back without it and the child resolves the
            # default home instead.
            #
            # The failure that costs is silent, because both homes exist: started
            # as RAVEN_HOME=~/.raven-main, the host polls that home's cron store
            # while a sub-agent writing back to "the host's store" writes into
            # ~/.raven. The hand-off lands in a file nobody reads and the wake
            # simply never arrives.
            env = {**env_base, **_host_home_env(), **(runtime_env or {}), **self.env}
            logger.info("Subagent [{}] CLI agent {!r}: {}", task_id, self.name, argv[:1])
            proc = await asyncio.create_subprocess_exec(
                *argv,
                # Inheriting the parent's stdin (None) would leave it open on a
                # non-TTY pipe (e.g. under a supervisor), and codex exec reads
                # and blocks on stdin until EOF whenever it isn't a TTY.
                stdin=asyncio.subprocess.DEVNULL if used_placeholder else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                # New session/process-group leader, so a manual stop can kill
                # the whole tree with killpg instead of only this launcher.
                start_new_session=True,
            )
            # Captured now, not re-derived later: `start_new_session=True`
            # makes the launcher's pid double as the group's pgid, and this
            # is the only reliable point to read it, before the launcher can
            # exit and have its pid recycled by the OS.
            pgid = proc.pid
            stdin_bytes = None if used_placeholder else task.encode("utf-8")
            # Always the line pump, never buffered `communicate()`: the live
            # activity console watches every run now, not only the ones whose
            # caller asked to stream the reply. The pump returns the same bytes
            # the buffered call did.
            capture = self._communicate_streaming(proc, stdin_bytes, on_delta)
            try:
                if self.timeout is not None:
                    out, err = await asyncio.wait_for(capture, timeout=self.timeout)
                else:
                    out, err = await capture
            except asyncio.TimeoutError:
                await self._kill_process_group(proc, pgid)
                raise CliAgentTimeoutError(f"CLI agent {self.name!r} timed out after {self.timeout}s") from None
            except asyncio.CancelledError:
                await self._kill_process_group(proc, pgid)
                raise
            except Exception:
                # Reachable only through the line pump (a delta sink that
                # raised): the capture is abandoned here, and abandoning it
                # without the kill leaves the reparented worker running for the
                # rest of the process's life. See ``_kill_process_group``.
                await self._kill_process_group(proc, pgid)
                raise
            stdout = out.decode("utf-8", "replace")
            stderr = err.decode("utf-8", "replace")
            # Recorded before the exit-code check, so a *failed* invocation is
            # kept too -- that is the one whose output is worth having, and it is
            # exactly what used to be reduced to a 2000-char tail in an exception
            # message and then discarded.
            if attempts is not None:
                attempts.append(
                    {
                        "argv0": argv[0] if argv else "",
                        "agentId": agent_id,
                        "exitCode": proc.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                )
            if proc.returncode != 0:
                # One line, not the tail of the transcript. The transcript itself
                # is on the span above, which is where a diagnosis reads it from;
                # what reaches an exception message ends up quoted verbatim in
                # the instance's conversation and in the DAG node's error, so a
                # 2000-char JSON dump was shown to whoever was watching.
                said = _human_failure(stdout, stderr)
                raise RuntimeError(f"CLI agent {self.name!r} exited {proc.returncode}" + (f": {said}" if said else ""))
            return stdout, stderr
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        runtime_env: dict[str, str] = {}
        if model:
            runtime_env["RAVEN_PARENT_MODEL"] = model
        reasoning_effort = getattr(getattr(provider, "generation", None), "reasoning_effort", None)
        if reasoning_effort:
            runtime_env["RAVEN_PARENT_REASONING_EFFORT"] = str(reasoning_effort)
        handle = instance or task_id
        # Re-checked rather than trusted: `streams` is what the manager reads to
        # decide whether to offer the hook, and a caller driving this backend
        # directly never consulted it.
        # Two callbacks travel down, and the distinction matters: `sink` is
        # budgeted so what streams stays a prefix of what returns, while the raw
        # `on_delta` carries raven's own truncation notice. A reply that
        # saturates the budget would otherwise swallow the one line explaining
        # that it was cut -- which is the whole point of the notice.
        sink = bounded_delta(on_delta, self.max_output_chars) if self.streams else None
        started = time.monotonic()
        # One span per dispatch, so a resume that fails and retries as a fresh
        # create reads as two invocations of one task rather than two tasks. The
        # span is opened here rather than by the caller for the reason
        # `observability` gives: it nests on a contextvar, so neither `spawn` nor
        # the DAG runner has to pass anything down.
        attempts: list[dict[str, Any]] = []
        output = ""
        with external_agent_span(agent=self.name, transport="cli", task_id=task_id, instance=handle) as span:
            try:
                output = await self._dispatch(
                    task,
                    task_id,
                    workspace,
                    session_key=session_key,
                    handle=handle,
                    resumable=instance is not None,
                    attempts=attempts,
                    on_delta=sink,
                    notice_sink=on_delta,
                    runtime_env=runtime_env,
                )
                return output
            except Exception as exc:
                span.error(exc)
                raise
            finally:
                self._record(span, attempts, output=output, started=started)

    def _record(self, span: Any, attempts: list[dict[str, Any]], *, output: str, started: float) -> None:
        """Put this dispatch's raw material on the span.

        No per-step timeline: this transport has none. The steps are read out of
        the transcript after the process exits, which is why its roster rows are
        tagged ``no-progress`` and why the outcome deliberately omits
        ``update_counts`` rather than reporting zero. A command configured for
        reply streaming does not change that: what it forwards live is the
        answer's text, not the run's steps, and it is forwarded to a human
        rather than recorded here.

        What is new is that the transcript survives at all. It used to be read for
        a session id and a reply and then dropped, with a 2000-char tail reaching
        an exception message on failure and nothing at all on success.
        """
        last = attempts[-1] if attempts else {}
        record_outcome(
            span,
            answer_chars=len(output),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            exit_code=last.get("exitCode"),
        )
        span.set(**{"subagent.external.invocations": len(attempts)})
        if attempts:
            record_transcript(
                span,
                {
                    "agent": self.name,
                    "transcriptFormat": self.transcript_format,
                    "invocations": attempts,
                },
            )

    async def _dispatch(
        self,
        task: str,
        task_id: str,
        workspace: Path,
        *,
        session_key: str | None,
        handle: str,
        resumable: bool,
        attempts: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        notice_sink: Callable[[str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> str:
        skey = session_key or "default"
        cwd = self.cwd or str(workspace)

        if self.is_stateful:
            # Held across lookup, run and commit: the whole sequence is what
            # binds a handle to one CLI session, and a concurrent spawn or DAG
            # node on the same handle would otherwise resume that session
            # side by side. See ``hold_handle``.
            #
            # Only a named handle has such a session. Without one nothing is
            # looked up (``resumable``, decided by the caller), so each run mints
            # its own
            # agent_id and there is nothing for a second run to interleave with
            # -- while the key it would serialize on is a DAG node's
            # author-chosen id, which repeats across graphs by design. Taking
            # the lock there stalls unrelated runs on the shared dispatch gate,
            # since a node holds its slot while it waits.
            guard = hold_handle(skey, self.name, handle) if resumable else nullcontext()
            async with guard:
                return await self._run_stateful(
                    task,
                    task_id,
                    cwd,
                    skey,
                    handle,
                    resumable=resumable,
                    attempts=attempts,
                    on_delta=on_delta,
                    notice_sink=notice_sink,
                    runtime_env=runtime_env,
                )

        return await self._attempt(
            task,
            task_id,
            cwd,
            agent_id=None,
            template=self.command,
            created=False,
            skey=skey,
            handle=handle,
            attempts=attempts,
            on_delta=on_delta,
            notice_sink=notice_sink,
            runtime_env=runtime_env,
        )

    async def _run_stateful(
        self,
        task: str,
        task_id: str,
        cwd: str,
        skey: str,
        handle: str,
        *,
        resumable: bool,
        attempts: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        notice_sink: Callable[[str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> str:
        """Resume this handle's session, or mint one.

        Only an explicitly named ``instance`` resumes -- that is ``resumable``
        -- and only that case runs under ``hold_handle``; an unnamed one shares
        no session, so the caller does not serialize it. Without a name the
        handle falls back to ``task_id``, which for a DAG node is its
        author-chosen id -- so a later graph with a node called ``research``
        would otherwise pick up an earlier graph's session, having asked for
        nothing of the sort. (A spawn's ``task_id`` is a fresh uuid, so nothing
        there could ever match anyway.)

        The binding is still committed, and the rule holds in one direction
        only: an unnamed run never reads one, but what it writes is the bare
        handle, so a later run naming ``instance="research"`` does resume the
        session that node left behind. Closing that means namespacing the
        *write*, not tightening this lookup -- and the web monitor reads the
        binding by ``<agent>/<node id>`` (deriveInstances.ts), so both sides
        move together. Predates the gate on ``resumable``, which narrowed the
        case rather than removing it.
        """
        existing = await self._registry.lookup(skey, self.name, handle) if resumable else None
        if existing is not None:
            try:
                return await self._attempt(
                    task,
                    task_id,
                    cwd,
                    agent_id=existing,
                    template=self.resume_command or self.command,
                    created=False,
                    skey=skey,
                    handle=handle,
                    attempts=attempts,
                    on_delta=on_delta,
                    notice_sink=notice_sink,
                    runtime_env=runtime_env,
                )
            except (CliAgentTimeoutError, CliAgentReportedError):
                # Neither is evidence the CLI's session store pruned this id: a
                # timeout may just mean the run was slow, and a transcript-level
                # error can happen inside a perfectly valid session. Forgetting
                # the handle here would discard a valid binding for nothing.
                raise
            except Exception:  # noqa: BLE001 - a hard non-zero exit: the CLI's own session store may have pruned this id
                # A caller watching this turn keeps whatever the failed attempt
                # streamed, and the retry appends to it. Measured on claude: a
                # resume of an unknown id exits before emitting any text delta,
                # so the case that would read as two answers does not arise.
                logger.warning(
                    "Subagent [{}] resume of {}/{!r} failed; dropping the stale binding and "
                    "retrying once as a fresh create",
                    task_id,
                    self.name,
                    handle,
                )
                await self._registry.unbind(skey, self.name, handle)
        # Minted independently of `handle`: a CLI constrains its session
        # id (claude rejects a non-UUID) while a handle is free-form.
        agent_id = None if self.id_source == "derived" else str(uuid.uuid4())
        return await self._attempt(
            task,
            task_id,
            cwd,
            agent_id=agent_id,
            template=self.command,
            created=True,
            skey=skey,
            handle=handle,
            attempts=attempts,
            on_delta=on_delta,
            notice_sink=notice_sink,
            runtime_env=runtime_env,
        )

    async def _attempt(
        self,
        task: str,
        task_id: str,
        cwd: str,
        *,
        agent_id: str | None,
        template: str,
        created: bool,
        skey: str,
        handle: str,
        attempts: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        notice_sink: Callable[[str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> str:
        """Run one CLI invocation (create or resume) and return its output, raising on failure."""
        stdout, stderr = await self._exec(
            template,
            task,
            task_id,
            cwd,
            agent_id,
            attempts,
            on_delta=on_delta,
            runtime_env=runtime_env,
        )

        jsonl_id: str | None = None
        jsonl_reply: str | None = None
        if self.transcript_format == "codex_jsonl":
            jsonl_id, jsonl_reply = parse_codex_jsonl(stdout)
        elif self.transcript_format == "claude_stream_json":
            jsonl_id, jsonl_reply, is_error = parse_claude_stream_json(stdout)
            # Claude does not reliably exit non-zero under -p, so the exit code
            # check above is not enough on its own.
            if is_error:
                raise CliAgentReportedError(
                    f"CLI agent {self.name!r} reported an error: {(jsonl_reply or stdout).strip()[-2000:]}"
                )
        elif self.transcript_format == "openclaw_json":
            jsonl_id, jsonl_reply = parse_openclaw_json(stdout)
        elif self.transcript_format == "opencode_json":
            jsonl_id, jsonl_reply = parse_opencode_json(stdout)

        if created and self.id_source == "derived":
            if jsonl_id is not None:
                agent_id = jsonl_id
            elif self._session_id_re is not None:
                # stdout first, then stderr: hermes prints the id only on stderr,
                # so id recovery has to read both streams even though the reply
                # no longer does.
                for stream in (stdout, stderr):
                    if (m := self._session_id_re.search(stream)) is not None:
                        agent_id = m.group(1)
                        break

        # Deferred commit: binding a handle after a failed create would resume a
        # session that never existed.
        if created and agent_id is not None:
            await self._registry.commit(skey, self.name, handle, agent_id)

        if jsonl_reply is not None:
            output = jsonl_reply.strip()
        elif self._output_re is not None and (m := self._output_re.search(stdout)) is not None:
            output = m.group(1).strip()
        else:
            # stderr is a log lane, not part of the answer: it streams to the
            # live console and rides in the attempts record, and folding it
            # into a successful reply is what forced quiet launchers to bury
            # their progress in files instead of logging it. It still stands
            # in when stdout said nothing at all -- an answerless run's stderr
            # is the only account it left.
            output = stdout.strip() or stderr.strip()

        warning = None
        if created and self.id_source == "derived" and agent_id is None:
            warning = (
                "\n\n[raven] Warning: no session id could be extracted from the create "
                "output, so this instance is not resumable. Use a new handle to recreate."
            )

        # The cap is applied here, at the process boundary, but never silently:
        # `clamp_output` hands the whole answer to the run's activity, from which
        # the record writes out.md, so what the caller is given being the head of
        # the answer no longer means the head is all that survives.
        return await clamp_output(
            output,
            self.max_output_chars,
            agent=self.name,
            reserved=warning or "",
            # The unbounded one: `on_delta` here is the budgeted wrapper, and a
            # reply that filled its budget has nothing left to say the reply was
            # cut with.
            sink=notice_sink,
        )
