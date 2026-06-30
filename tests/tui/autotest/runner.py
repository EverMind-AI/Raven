"""Harness runner — Tier 1 (tui-use) impl per Day 0 spike (2026-05-20).

Public API contract: ``docs/openspec/changes/tui-auto-test/specs/tui-autotest.md`` §S3.
Tier 1 wraps the ``tui-use`` npm CLI (>=0.1.20) via ``subprocess.run``. The
tui-use daemon owns PTY lifecycle; this class only marshals verb→CLI args and
parses snapshot/info output.

Path B (DSL dropped, 2026-05-20): no verb registry / no .tape parser.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import uuid
from typing import Optional, Union


class HarnessError(Exception):
    """Base — maps to harness CLI exit code 2 (harness's own error)."""


class SpawnError(HarnessError):
    """Subprocess failed to start (tui-use start exit != 0 / timeout)."""


class BackendError(HarnessError):
    """Tier-specific backend internal error (daemon unreachable / parse fail)."""


class ExtrasMissingError(HarnessError):
    """``tui-use`` not on PATH (or pexpect/pyte missing on a Tier 3 fallback)."""


# Default env injected at spawn — see specs/tui-autotest.md §S5.1.
_DEFAULT_ENV: dict[str, str] = {
    "TERM": "xterm-256color",
    "FORCE_COLOR": "1",
}

_INFO_EXIT_RE = re.compile(r"Exit Code:\s*(-?\d+)")
_INFO_STATUS_RE = re.compile(r"Status:\s*(\w+)")
_BANNER_MARKER = "───"  # tui-use snapshot frames rows in U+2500 box-drawing chars

_TUI_USE_BIN = "tui-use"
_SPAWN_TIMEOUT_S = 15.0
_VERB_TIMEOUT_S = 10.0


class Harness:
    """Black-box PTY driver for any TUI subprocess (Tier 1 = tui-use)."""

    def __init__(
        self,
        *,
        cols: int = 120,
        rows: int = 40,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.cols = cols
        self.rows = rows
        self._cwd = cwd
        self._env_overrides: dict[str, str] = dict(env or {})
        self._session_id: Optional[str] = None
        self._killed = False
        self._cached_exit_code: Optional[int] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def spawn(self, command: str) -> None:
        if self._session_id is not None:
            raise HarnessError("Harness.spawn() called twice; one subprocess per Harness instance")

        label = f"eve-autotest-{uuid.uuid4().hex[:8]}"
        cmd = [
            _TUI_USE_BIN,
            "start",
            "--label",
            label,
            "--cols",
            str(self.cols),
            "--rows",
            str(self.rows),
            "--",  # stop tui-use option parsing; everything after is user cmd+args
        ]
        cmd.extend(shlex.split(command))

        env = self._build_env()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                cwd=self._cwd,
                timeout=_SPAWN_TIMEOUT_S,
            )
        except FileNotFoundError as e:
            raise ExtrasMissingError(f"`{_TUI_USE_BIN}` not on PATH: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise SpawnError(f"tui-use start timed out after {_SPAWN_TIMEOUT_S}s") from e

        if result.returncode != 0:
            raise SpawnError(
                f"tui-use start failed (exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}"
            )

        # tui-use start prints the new session id to stdout on its own line.
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise SpawnError(f"tui-use start did not emit a session id; stdout={result.stdout!r}")
        self._session_id = lines[-1].strip()
        # Make this the daemon's "current" session so the verbless tui-use
        # commands (type/press/wait/snapshot/info/kill) target it.
        self._run_tui_use("use", self._session_id, check=True)

    def kill(self) -> int:
        if self._session_id is None:
            return -1
        if self._killed:
            return self._cached_exit_code if self._cached_exit_code is not None else -1

        self._run_tui_use("kill", check=False)
        exit_code = self._poll_exit_code(timeout=2.0)
        self._killed = True
        self._cached_exit_code = exit_code
        return exit_code if exit_code is not None else -1

    # ── Input ────────────────────────────────────────────────────────────

    def type(self, text: str) -> None:
        self._require_spawned("type")
        self._run_tui_use("type", text, check=True)

    def press(self, key: str) -> None:
        self._require_spawned("press")
        self._run_tui_use("press", key, check=True)

    def env_set(self, mapping: dict[str, str]) -> None:
        if self._session_id is not None:
            raise HarnessError("env_set() must be called before spawn()")
        self._env_overrides.update(mapping)

    # ── Wait / observation ───────────────────────────────────────────────

    def wait(
        self,
        pattern: Union[str, re.Pattern],
        timeout: float,
    ) -> bool:
        self._require_spawned("wait")
        if isinstance(pattern, re.Pattern):
            compiled = pattern
        elif isinstance(pattern, str):
            compiled = re.compile(pattern)
        else:
            raise HarnessError(f"wait() pattern must be str or re.Pattern, got {type(pattern).__name__}")

        deadline = time.monotonic() + timeout
        poll_interval = 0.1
        while time.monotonic() < deadline:
            if compiled.search(self._raw_snapshot()):
                return True
            time.sleep(poll_interval)
        # One last shot after the deadline (race-safe for tight timeouts).
        return bool(compiled.search(self._raw_snapshot()))

    def dump(self) -> list[str]:
        self._require_spawned("dump")
        rows: list[str] = []
        for raw_line in self._raw_snapshot().splitlines():
            line = raw_line.rstrip()
            if _BANNER_MARKER in line:
                continue
            rows.append(line)
        while rows and rows[-1] == "":
            rows.pop()
        while rows and rows[0] == "":
            rows.pop(0)
        return rows

    def screen(self) -> str:
        return "\n".join(self.dump())

    # ── Termination ──────────────────────────────────────────────────────

    def expect_exit(self, code: int = 0, timeout: float = 5.0) -> bool:
        self._require_spawned("expect_exit")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            actual = self._poll_exit_code(timeout=0.0)
            if actual is not None:
                return actual == code
            time.sleep(0.1)
        actual = self._poll_exit_code(timeout=0.0)
        return actual is not None and actual == code

    # ── Internal ─────────────────────────────────────────────────────────

    def _require_spawned(self, op: str) -> None:
        if self._session_id is None:
            raise HarnessError(f"Harness.{op}() called before spawn()")

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(_DEFAULT_ENV)
        env.update(self._env_overrides)
        return env

    def _run_tui_use(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = [_TUI_USE_BIN, *args]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
                cwd=self._cwd,
                timeout=_VERB_TIMEOUT_S,
            )
        except FileNotFoundError as e:
            raise ExtrasMissingError(f"`{_TUI_USE_BIN}` not on PATH: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"tui-use {args[0] if args else '?'} timed out after {_VERB_TIMEOUT_S}s") from e

        if check and result.returncode != 0:
            raise BackendError(
                f"tui-use {args[0] if args else '?'} failed "
                f"(exit {result.returncode}):\n"
                f"  stdout: {result.stdout.strip()}\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result

    def _raw_snapshot(self) -> str:
        return self._run_tui_use("snapshot", check=False).stdout or ""

    def _poll_exit_code(self, timeout: float = 0.0) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while True:
            info = self._run_tui_use("info", check=False).stdout or ""
            status_m = _INFO_STATUS_RE.search(info)
            if status_m and status_m.group(1).lower() == "exited":
                code_m = _INFO_EXIT_RE.search(info)
                if code_m:
                    return int(code_m.group(1))
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
