"""Drive the ``raven`` agent via its CLI from a subprocess.

This is the contract enforced by ``tests/test_no_raven_imports.py``:
nothing in this module imports ``raven``. The agent under test is a
black box exposing the CLI ``raven {agent, sentinel tick, sentinel
discover-now, …}`` that we spawn fresh for each operation.

Why subprocess instead of in-process:

- Decouples eval from internal module reorgs in ``raven``.
- Lets us evaluate any agent that exposes a comparable CLI surface
  (hermes / openclaw / future entrants) with one driver shape.
- Per-case workspace isolation comes for free — each case points the
  CLI at its own ``--workspace <dir>`` and reads back the on-disk state.

Why one process per operation rather than a long-lived REPL pipe:

- ``raven agent`` in REPL mode uses ``prompt_toolkit`` which doesn't
  cleanly accept piped stdin (would deadlock waiting for terminal
  signals).
- ``raven agent -m "<msg>"`` non-interactive mode is one-shot and
  exits — that's the natural unit of work.
- All state we care about (memory, sessions, sentinel state) persists
  to disk, so a fresh subprocess sees the same state the previous one
  left.
"""

from __future__ import annotations

import json as _json
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .parse import parse_bool, parse_float, parse_two_column_table
from .state import AgentState


@dataclass
class AgentResponse:
    """Outcome of a one-shot ``raven agent --message ...`` invocation."""

    stdout: str
    stderr: str
    returncode: int
    duration_seconds: float
    cmd: list[str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class SentinelTickResult:
    """Parsed output of ``raven sentinel tick`` or one tick from
    ``raven sentinel ticks``.

    All optional because in some failure modes the table is partial; the
    caller can decide what's load-bearing for their case. ``fake_now``
    is populated only by the batch (``sentinel_ticks``) path, where the
    CLI echoes the per-tick timestamp back in the JSON record; the
    single-call path leaves it ``None`` since the caller already knows
    the time they passed in.
    """

    action: str | None              # skip / nudge / nudge_inject / nudge_defer / spawn_agent
    priority: str | None            # low / medium / high
    proactivity_score: float | None
    target_session: str | None
    reason: str | None
    nudge_message: str | None
    spawn_task: str | None
    route: str | None               # fast_path_skip / skip / dispatched / injected / deferred / spawn
    delivered: bool | None
    raw_stdout: str
    raw_stderr: str
    returncode: int
    fake_now: str | None = None     # populated only by sentinel_ticks (batch)
    topic_tag: str | None = None    # Planner-supplied stable topic key

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.action is not None


class RavenDriver:
    """Drive ``raven`` CLI commands at a single per-case workspace.

    Construction parameters
    -----------------------
    raven_repo
        Path to the ``raven`` checkout (the one containing
        ``raven/__main__.py``).
    workspace
        Per-case sandbox dir. Passed as ``--workspace`` to every
        subcommand. The driver does NOT create this directory or seed
        any fixtures — that's the case-setup step's responsibility.
    config
        Optional ``--config <path>`` override for ``raven {agent,
        gateway}``. Defaults to the user's normal config lookup.
    python_exe
        Defaults to ``<raven_repo>/.venv/bin/python``. Override when
        targeting a system interpreter or a non-uv venv.
    timeout_seconds
        Per-subprocess wall-clock cap. Sentinel tick on a cold workspace
        is ~5-30s; ``agent --message`` runs until the LLM responds, which
        can be long for tool-loops. Raise for slow models.
    """

    def __init__(
        self,
        *,
        raven_repo: Path,
        workspace: Path,
        config: Path | None = None,
        python_exe: Path | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.raven_repo = Path(raven_repo).resolve()
        self.workspace = Path(workspace).resolve()
        self.config = Path(config).resolve() if config else None
        if python_exe is None:
            import sys as _sys
            if _sys.platform == "win32":
                python_exe = self.raven_repo / ".venv" / "Scripts" / "python.exe"
            else:
                python_exe = self.raven_repo / ".venv" / "bin" / "python"
        self.python_exe = Path(python_exe)
        self.timeout_seconds = timeout_seconds

        if not self.python_exe.exists():
            raise FileNotFoundError(
                f"python_exe does not exist: {self.python_exe}. "
                f"Make sure the raven checkout has a venv (uv sync)."
            )

    # ------------------------------------------------------------------
    # CLI invocations

    def send_message(
        self,
        message: str,
        *,
        fake_now: str | None = None,
        session_id: str = "cli:direct",
        wait_skill_extract: bool = False,
    ) -> AgentResponse:
        """Run ``raven agent --message <m> [--fake-now ...]`` once.

        One-shot non-interactive invocation. Returns when the agent
        completes its response (including any tool calls). The agent's
        side effects — memory writes, sentinel state changes, session
        log appends — persist to the workspace and ``~/.raven``.
        """
        cmd = self._base_cmd("agent")
        cmd.extend(["--message", message])
        cmd.extend(["--session", session_id])
        cmd.append("--no-markdown")
        cmd.append("--no-logs")
        if wait_skill_extract:
            cmd.append("--wait-skill-extract")
        else:
            cmd.append("--no-wait-skill-extract")
        if fake_now:
            cmd.extend(["--fake-now", fake_now])
        return self._run(cmd)

    def sentinel_tick(
        self,
        *,
        fake_now: str | None = None,
        live: bool = False,
    ) -> SentinelTickResult:
        """Run ``raven sentinel tick``. Returns the parsed table.

        ``live=False`` (default) is dry-run — the Planner is still
        invoked (so an LLM call may fire unless the fast_path skips it),
        but no executor dispatch happens. ``live=True`` actually fires
        through dispatcher/injector/defer.
        """
        cmd = self._base_cmd("sentinel", "tick")
        cmd.append("--live" if live else "--dry-run")
        if fake_now:
            cmd.extend(["--fake-now", fake_now])
        r = self._run(cmd)
        fields = parse_two_column_table(r.stdout)
        return SentinelTickResult(
            action=fields.get("action"),
            priority=fields.get("priority"),
            proactivity_score=parse_float(fields.get("proactivity_score")),
            target_session=_none_if_dash(fields.get("target_session")),
            reason=fields.get("reason"),
            nudge_message=fields.get("nudge_message"),
            spawn_task=fields.get("spawn_task"),
            route=fields.get("route"),
            delivered=parse_bool(fields.get("delivered")),
            raw_stdout=r.stdout,
            raw_stderr=r.stderr,
            returncode=r.returncode,
        )

    def sentinel_ticks(
        self,
        *,
        from_iso: str,
        to_iso: str,
        interval_seconds: int = 1800,
        live: bool = False,
        timeout_seconds: float | None = None,
    ) -> list[SentinelTickResult]:
        """Run ``raven sentinel ticks --from <from> --to <to>`` in a
        single subprocess. Returns one :class:`SentinelTickResult` per
        tick in chronological order.

        Pairs with the ``sentinel ticks`` CLI added in branch
        ``feat/eval-fake-now`` of the raven repo. The CLI builds the
        Sentinel stack ONCE and reuses it across every tick — collapsing
        N cold-start subprocesses into 1. On fast_path_skip ticks
        (quiet_hours, dedup) the savings dominate (~30x); on ticks that
        actually hit the Planner LLM the LLM is still the wall-clock cost.

        ``timeout_seconds`` overrides the driver's default per-call cap.
        Pass a value large enough to fit N × LLM-call-budget; default
        (None) uses the driver-level ``timeout_seconds``.
        """
        cmd = self._base_cmd("sentinel", "ticks")
        cmd.extend(["--from", from_iso, "--to", to_iso,
                    "--interval-seconds", str(interval_seconds)])
        cmd.append("--live" if live else "--dry-run")
        r = self._run(cmd, override_timeout=timeout_seconds)
        ticks: list[SentinelTickResult] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                # Skip non-JSON noise (shouldn't happen, but don't crash
                # the whole batch on one bad line).
                continue
            ticks.append(SentinelTickResult(
                action=d.get("action"),
                priority=d.get("priority"),
                proactivity_score=d.get("proactivity_score"),
                target_session=d.get("target_session"),
                reason=d.get("reason"),
                nudge_message=d.get("nudge_message"),
                spawn_task=d.get("spawn_task"),
                route=d.get("route"),
                delivered=d.get("delivered"),
                raw_stdout=line,        # per-tick raw JSON, not the whole batch
                raw_stderr=r.stderr,
                returncode=r.returncode,
                fake_now=d.get("fake_now"),
                topic_tag=d.get("topic_tag"),
            ))
        return ticks

    def sentinel_discover_now(
        self,
        channel: str,
        to: str,
        *,
        fake_now: str | None = None,
    ) -> AgentResponse:
        """Force-trigger TaskDiscoverer via ``raven sentinel
        discover-now``. Real Planner LLM call inside; auto-confirmed
        with ``--yes`` because eval runs unattended.

        Passes ``--inproc`` so the LLM call happens in this subprocess
        rather than queuing a trigger for a gateway. The eval harness
        has no separate gateway — this subprocess IS the gateway-equivalent
        for the duration of the call.
        """
        cmd = self._base_cmd("sentinel", "discover-now", channel, to)
        cmd.extend(["--yes", "--inproc"])
        if fake_now:
            cmd.extend(["--fake-now", fake_now])
        return self._run(cmd)

    def read_state(self, sentinel_state_dir: Path | None = None) -> AgentState:
        """Read the on-disk state the agent left behind. See
        ``proactivity_eval.state.AgentState`` for the schema."""
        return AgentState.from_workspace(self.workspace, sentinel_state_dir)

    # ------------------------------------------------------------------
    # Internals

    # Subcommands that accept --config and use it to redirect the
    # raven data dir (~/.raven/...) to the config's parent. For
    # parallel longrun this is the per-persona isolation mechanism — see
    # MIGRATION_STATUS.md Phase D.
    _CONFIG_AWARE = frozenset({
        "agent", "gateway",
        ("sentinel", "ticks"),
        ("sentinel", "tick"),         # supported via set_config_path inside _load_sentinel_config
    })

    def _base_cmd(self, *subcommands: str) -> list[str]:
        """Build ``python -m raven <subcommand...>`` with --workspace
        and (if set) --config baked in.

        Both ``--workspace`` and ``--config`` are passed at the TOP-LEVEL
        subcommand position — they must come AFTER the subcommand path
        because typer is strict about flag placement.
        """
        cmd: list[str] = [str(self.python_exe), "-m", "raven", *subcommands]
        # All commands that accept --workspace use the same flag name,
        # so we can append uniformly.
        cmd.extend(["--workspace", str(self.workspace)])
        # --config: thread through to every config-aware subcommand so
        # the per-persona data dir (sentinel/state.json, cron/jobs.json,
        # etc.) is honored across the whole subprocess set, not just
        # ``agent`` calls. Without this, parallel personas share global
        # ~/.raven/sentinel/state.json and contaminate each other's
        # NudgePolicy dedup.
        if self.config:
            key: object = subcommands[0] if len(subcommands) == 1 else tuple(subcommands[:2])
            if key in self._CONFIG_AWARE:
                cmd.extend(["--config", str(self.config)])
        return cmd

    def _run(
        self, cmd: list[str], *, override_timeout: float | None = None,
    ) -> AgentResponse:
        """Spawn the subprocess and capture stdout/stderr/returncode.

        ``cwd`` is set to the raven repo so the package finds its
        config templates and bundled assets. Output streams are decoded
        as UTF-8 with ``errors='replace'`` so weird agent output never
        crashes the eval.

        ``override_timeout`` lets callers (notably ``sentinel_ticks()``,
        which packs N ticks into one subprocess) widen the cap above
        the driver-level ``timeout_seconds`` default without mutating
        the driver instance.
        """
        effective_timeout = (
            override_timeout if override_timeout is not None else self.timeout_seconds
        )
        # Disable Rich's terminal-width line wrapping. Without these env
        # vars the CLI rewraps long output (including JSON decision blocks)
        # to the parent terminal's width, which inserts literal newlines
        # INSIDE JSON string values — breaking ``json.loads`` downstream.
        import os
        env = {**os.environ, "COLUMNS": "200", "TERM": "dumb"}
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.raven_repo),
                env=env,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = (
                (exc.stderr or b"").decode("utf-8", errors="replace")
                + f"\n[proactivity-eval] timed out after {effective_timeout}s "
                + f"running: {' '.join(shlex.quote(c) for c in cmd)}"
            )
            return AgentResponse(
                stdout=stdout, stderr=stderr, returncode=-1,
                duration_seconds=time.monotonic() - t0, cmd=cmd,
            )
        return AgentResponse(
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode,
            duration_seconds=time.monotonic() - t0,
            cmd=cmd,
        )


def _none_if_dash(s: str | None) -> str | None:
    """The CLI prints '-' for empty target_session; normalise to None."""
    if s is None or s.strip() == "-":
        return None
    return s
