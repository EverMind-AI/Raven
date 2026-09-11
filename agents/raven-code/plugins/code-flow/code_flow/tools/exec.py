"""exec -- the host's shell tool with the fork's three fixes, served by name.

The trunk ``ExecTool`` is kept whole (its guards, its background lane, its
machine lane); this subclass changes three things a coding run kept tripping
over, and does it on the executor seam ``ExecTool.__init__`` already exposes:

*A long timeout is clamped, not refused.* Trunk's schema says ``maximum: 600``,
so a model asking for 900 seconds for a slow test suite is answered with a
validation error and learns nothing. Here the schema carries no maximum, the
request is clamped to the configured ceiling, and the result says so.

*A timed-out command keeps the output it produced.* Trunk's executor waits on
``communicate()``; when that raises on timeout the buffered output is gone and
the model reads ``Timed out after 600s`` with nothing else -- for a test suite
that printed forty passes and one hang, the forty passes vanish. This executor
drains the pipes as it goes and hands back what arrived, flagged as partial.

*A large output is saved whole, not lost in the middle.* Trunk keeps the head
and the tail of 10,000 characters; the middle is nowhere. Here the budget is
30,000 and the complete output is written under Agent home (never into the
repository), with the path in the result, so the model can grep it instead of
re-running blind.

Sandbox discipline: this executor runs on the host, exactly as the trunk's
``DirectExecutor`` does when ``tools.sandbox.backend`` is ``none``. The factory
serves this tool only in that case and declines otherwise, so a configured
sandbox is never bypassed by a same-name replacement.

Environment discipline: the host executor hands a child only an allowlisted
baseline of the process environment (never credentials), and that allowlist is
a private detail of the trunk module -- a product plugin may not import it
(the cargo redlines). So the baseline is learned from the host executor itself:
one probe command run through the public ``exec`` reports which variables a
child receives, and this executor passes exactly those, read fresh from the
process environment at every spawn. No copy of the list, so no drift.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.tools.shell import ExecTool
from raven.contracts.tool import ToolOutput, ToolResult
from raven.sandbox.direct_executor import DirectExecutor
from raven.sandbox.interfaces import ExecResult

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TIMEOUT = 1200
MAX_OUTPUT = 30_000
#: Where an oversized output is saved whole, under Agent home.
SPILL_SUBDIR = "exec-output"

TIMED_OUT_NOTE = (
    "[the command hit its time limit and was killed; the output above is what it had "
    "produced by then and is partial. Retry with a larger `timeout` if it just needs "
    "longer, or run it with run_in_background: true and read its log]"
)

_SPILL_COUNTER = itertools.count(1)

#: Variables the probe's own shell adds on top of the baseline; not part of it.
_SHELL_ADDED = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})
_PROBE_TIMEOUT = 15


def spill_output(text: str, spill_dir: Path | None) -> str | None:
    """Save ``text`` whole under ``spill_dir``; the path, or None when it cannot be saved."""
    if spill_dir is None:
        return None
    path = spill_dir / f"exec-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{next(_SPILL_COUNTER)}.log"
    try:
        spill_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("code-flow exec: could not save the full output to {}: {}", path, exc)
        return None
    return str(path)


@dataclass
class CodeExecResult(ExecResult):
    """Trunk's result plus what a coding run needs to read it right."""

    timed_out: bool = False
    spill_dir: Path | None = None

    def as_text(self, max_chars: int = MAX_OUTPUT) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(f"STDERR:\n{self.stderr}")
        parts.append(f"\nExit code: {self.exit_code}")
        if self.timed_out:
            parts.append(TIMED_OUT_NOTE)
        result = "\n".join(parts)
        if len(result) <= max_chars:
            return result
        half = max_chars // 2
        total_lines = result.count("\n") + 1
        spilled = spill_output(result, self.spill_dir)
        if spilled:
            recovery = (
                f"... [full output saved to {spilled}; grep it, or page through it with read_file offset/limit] ...\n\n"
            )
        else:
            recovery = (
                "... [re-run redirecting to a file (`cmd > out.log 2>&1`) and page through it "
                "with read_file to see all of it] ...\n\n"
            )
        marker = (
            f"\n\n... ({len(result) - max_chars:,} chars truncated; full output "
            f"{len(result):,} chars / {total_lines:,} lines) ...\n" + recovery
        )
        return result[:half] + marker + result[-half:]


async def _drain(stream: asyncio.StreamReader | None, into: bytearray) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        into.extend(chunk)


class CodeExecutor(DirectExecutor):
    """The host executor, draining the pipes as the command runs.

    Same spawn (``sh -c``, the baseline environment allowlist, its own
    session so the whole group can be killed) and the same kill on timeout;
    the difference is that output already read is returned instead of
    dropped, and the ceiling is the product's, not the module constant.
    """

    def __init__(self, *, max_timeout: int = DEFAULT_MAX_TIMEOUT, spill_dir: Path | None = None) -> None:
        super().__init__()
        self._max_timeout = max_timeout
        self._spill_dir = spill_dir
        self._allowed_keys: frozenset[str] | None = None

    async def allowed_keys(self) -> frozenset[str]:
        """The environment variables the host executor lets a child see.

        Learned once, through the public ``exec`` of the parent class: the
        probe is this interpreter printing the child's variable names, so what
        comes back is exactly the trunk allowlist as applied on this host. A
        probe that cannot run leaves exec unusable on purpose -- guessing a
        list here is how credentials would start reaching commands.
        """
        if self._allowed_keys is None:
            probe = (
                shlex.quote(sys.executable)
                + " -c 'import json,os,sys;sys.stdout.write(json.dumps(sorted(os.environ)))'"
            )
            result = await super().exec(probe, timeout=_PROBE_TIMEOUT)
            try:
                names = json.loads(result.stdout)
            except ValueError as exc:
                raise RuntimeError(
                    f"could not learn the host executor's environment baseline (exit {result.exit_code}): "
                    f"{result.stderr.strip() or result.stdout[:200]!r}"
                ) from exc
            self._allowed_keys = frozenset(str(n) for n in names) - _SHELL_ADDED
        return self._allowed_keys

    async def _spawn_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        allowed = await self.allowed_keys()
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(extra or {})
        return env

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> CodeExecResult:
        effective = min(DEFAULT_TIMEOUT if timeout is None else timeout, self._max_timeout)
        spawn_env = await self._spawn_env(env)
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=spawn_env,
            start_new_session=True,
        )
        pgid = process.pid
        out, err = bytearray(), bytearray()
        drains = [
            asyncio.create_task(_drain(process.stdout, out)),
            asyncio.create_task(_drain(process.stderr, err)),
        ]
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=effective)
        except asyncio.TimeoutError:
            timed_out = True
            self._kill_process_group(process, pgid)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            self._kill_process_group(process, pgid)
            for task in drains:
                task.cancel()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            raise
        # The pipes close when the group is gone; a grandchild that kept one open
        # after the shell exited is not waited on for ever.
        try:
            await asyncio.wait_for(asyncio.gather(*drains, return_exceptions=True), timeout=5.0)
        except asyncio.TimeoutError:
            for task in drains:
                task.cancel()
        stderr = err.decode("utf-8", errors="replace")
        if timed_out:
            stderr = (stderr.rstrip("\n") + "\n" if stderr.strip() else "") + f"Timed out after {effective}s"
        return CodeExecResult(
            stdout=out.decode("utf-8", errors="replace"),
            stderr=stderr,
            exit_code=-1 if timed_out else int(process.returncode if process.returncode is not None else -1),
            timed_out=timed_out,
            spill_dir=self._spill_dir,
        )


def _prefixed(note: str, out: str | ToolResult) -> str | ToolResult:
    """``note`` in front of a tool's answer, whatever shape the answer took."""
    if isinstance(out, ToolOutput):
        return ToolOutput(
            note + str(out),
            out.display_text,
            retryable=out.retryable,
            blocks_call=out.blocks_call,
            continuation=out.continuation,
            ok=out.ok,
            blocks=out.blocks,
            diff=out.diff,
            file_change=out.file_change,
        )
    if isinstance(out, ToolResult):
        return ToolResult(
            model_text=note + out.model_text,
            display_text=out.display_text,
            retryable=out.retryable,
            blocks_call=out.blocks_call,
            continuation=out.continuation,
            ok=out.ok,
            blocks=out.blocks,
            diff=out.diff,
            file_change=out.file_change,
        )
    return note + str(out)


class CodeExecTool(ExecTool):
    """Trunk's exec with a clamped ceiling and a 30,000-character budget."""

    _MAX_OUTPUT = MAX_OUTPUT

    def __init__(self, *args: Any, max_timeout: int = DEFAULT_MAX_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The base ``execute`` clamps against ``self._MAX_TIMEOUT``; the
        # instance value replaces the class constant of 600.
        self._MAX_TIMEOUT = max_timeout
        # The loop's backstop above the executor's own timeout, as trunk sets it.
        self.timeout_seconds = float(max_timeout + 60)

    @property
    def parameters(self) -> dict[str, Any]:
        schema = super().parameters
        timeout = schema["properties"]["timeout"]
        timeout.pop("maximum", None)
        timeout["description"] = (
            "Timeout in seconds for a command on THIS computer. Increase for long-running work "
            f"such as a build or a test suite (default {self.timeout}, ceiling {self._MAX_TIMEOUT}; a "
            "larger value is clamped to the ceiling, not rejected). A command that hits its limit is "
            "killed and the output it produced so far comes back marked partial. Not read with "
            "'machine' or 'run_in_background'."
        )
        return schema

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        machine: str = "",
        run_in_background: bool = False,
        **kwargs: Any,
    ) -> str | ToolResult:
        note = ""
        if timeout is not None and not machine and not run_in_background and timeout > self._MAX_TIMEOUT:
            note = (
                f"[note: timeout {timeout}s clamped to the {self._MAX_TIMEOUT}s ceiling; for longer "
                "work use run_in_background: true]\n"
            )
            timeout = self._MAX_TIMEOUT
        out = await super().execute(
            command,
            working_dir=working_dir,
            timeout=timeout,
            machine=machine,
            run_in_background=run_in_background,
            **kwargs,
        )
        return _prefixed(note, out) if note else out


__all__ = [
    "DEFAULT_MAX_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT",
    "SPILL_SUBDIR",
    "TIMED_OUT_NOTE",
    "CodeExecResult",
    "CodeExecTool",
    "CodeExecutor",
    "spill_output",
]
