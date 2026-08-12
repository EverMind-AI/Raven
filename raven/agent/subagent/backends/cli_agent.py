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
import os
import re
import shlex
import signal
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.backends.transcript import (
    parse_claude_stream_json,
    parse_codex_jsonl,
    parse_openclaw_json,
    parse_opencode_json,
)
from raven.agent.subagent.instances import InstanceRegistry, get_registry

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider


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

    async def _exec(self, template: str, task: str, task_id: str, cwd: str, agent_id: str | None) -> tuple[str, str]:
        fd, prompt_path = tempfile.mkstemp(prefix=f"raven_subagent_{task_id}_", suffix=".prompt.txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(task)
            argv, used_placeholder = self._build_argv(template, task, prompt_path, agent_id)
            # The capture shells out and can block for real seconds on a slow
            # profile (nvm/conda init); to_thread keeps that off the event loop.
            env_base = await asyncio.to_thread(login_shell_env)
            env = {**env_base, **self.env}
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
            try:
                if self.timeout is not None:
                    out, err = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=self.timeout)
                else:
                    out, err = await proc.communicate(input=stdin_bytes)
            except asyncio.TimeoutError:
                await self._kill_process_group(proc, pgid)
                raise CliAgentTimeoutError(f"CLI agent {self.name!r} timed out after {self.timeout}s") from None
            except asyncio.CancelledError:
                await self._kill_process_group(proc, pgid)
                raise
            stdout = out.decode("utf-8", "replace")
            stderr = err.decode("utf-8", "replace")
            if proc.returncode != 0:
                tail = (stdout + "\n" + stderr).strip()[-2000:]
                raise RuntimeError(f"CLI agent {self.name!r} exited {proc.returncode}: {tail}")
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
    ) -> str:
        # The parent's provider/model are accepted and ignored: this backend
        # shells out to an agent that authenticates and picks a model itself.
        skey = session_key or "default"
        handle = instance or task_id
        cwd = self.cwd or str(workspace)

        if self.is_stateful:
            existing = await self._registry.lookup(skey, self.name, handle)
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
                    )
                except (CliAgentTimeoutError, CliAgentReportedError):
                    # Neither is evidence the CLI's session store pruned this id: a
                    # timeout may just mean the run was slow, and a transcript-level
                    # error can happen inside a perfectly valid session. Forgetting
                    # the handle here would discard a valid binding for nothing.
                    raise
                except Exception:  # noqa: BLE001 - a hard non-zero exit: the CLI's own session store may have pruned this id
                    logger.warning(
                        "Subagent [{}] resume of {}/{!r} failed; forgetting the stale handle and "
                        "retrying once as a fresh create",
                        task_id,
                        self.name,
                        handle,
                    )
                    await self._registry.forget(skey, self.name, handle)
            # Minted independently of `handle`: a CLI constrains its session
            # id (claude rejects a non-UUID) while a handle is free-form.
            agent_id = None if self.id_source == "derived" else str(uuid.uuid4())
            return await self._attempt(
                task, task_id, cwd, agent_id=agent_id, template=self.command, created=True, skey=skey, handle=handle
            )

        return await self._attempt(
            task, task_id, cwd, agent_id=None, template=self.command, created=False, skey=skey, handle=handle
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
    ) -> str:
        """Run one CLI invocation (create or resume) and return its output, raising on failure."""
        stdout, stderr = await self._exec(template, task, task_id, cwd, agent_id)

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
                # which `combined` below already counts as part of the transcript.
                for stream in (stdout, stderr):
                    if (m := self._session_id_re.search(stream)) is not None:
                        agent_id = m.group(1)
                        break

        # Deferred commit: binding a handle after a failed create would resume a
        # session that never existed.
        if created and agent_id is not None:
            await self._registry.commit(skey, self.name, handle, agent_id)

        combined = f"{stdout}\n{stderr}" if stderr else stdout
        if jsonl_reply is not None:
            output = jsonl_reply.strip()
        elif self._output_re is not None and (m := self._output_re.search(stdout)) is not None:
            output = m.group(1).strip()
        else:
            output = combined.strip()

        warning = None
        if created and self.id_source == "derived" and agent_id is None:
            warning = (
                "\n\n[raven] Warning: no session id could be extracted from the create "
                "output, so this instance is not resumable. Use a new handle to recreate."
            )

        if warning is not None:
            # Reserve room for the warning so it survives truncation when possible,
            # but clamp to max_output_chars regardless: cap protects downstream context.
            base_output = output[: max(0, self.max_output_chars - len(warning))]
            return (base_output + warning)[: self.max_output_chars]
        else:
            return output[: self.max_output_chars]
