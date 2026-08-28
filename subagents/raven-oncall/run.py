#!/usr/bin/env python
"""Host-side launcher for the Raven-Oncall watch agent.

Raven-Oncall is the `feat/ops_round3_device` branch of Raven, which adds the
on-call line: you hand it a long-running job, it submits the job, schedules its
own next look, and comes back to decide. `docs/oncall-quickstart.zh-CN.md` in
the checkout is the branch's own account of what works and what does not. This
wrapper supplies the three things that line leaves to its host:

- **a resident process to fire the wakes.** `ops_submit` schedules the next look
  as a cron job on channel `cli`, and `raven agent -m` is one-shot: it exits as
  soon as it replies, so nothing fires that wake. The branch ships the missing
  piece as `raven.ops.wake_shell`; this launcher keeps exactly one of them alive
  per install, started under a lock, so a campaign keeps watching itself after
  the spawn that started it has returned.
- **the right raven for a woken turn.** `wake_shell` resolves the child turn
  through `shutil.which("raven")`, so with an unmodified PATH every wake would
  run the *host's* raven - a build with no ops tools at all - against this
  install's config. The shell is therefore started with this checkout's venv
  first on PATH.
- **an answer, and a verdict on whether there is one.** Exit 0 does not mean the
  agent produced anything: only a config or credential error maps to non-zero.

One call is one turn. The same `--session cli:<id>` across calls continues the
conversation, but continuity of the *campaign* deliberately does not live there:
a woken turn is a cold start with no history, and picks up from the campaign's
ledger on disk. That is the behaviour under test, so nothing here hands a wake a
conversation to lean on instead.

stdout carries the reply and nothing else. Raven's CLI backend uses the whole of
a child's output as the subagent's reply, so any progress line printed here would
be pasted into the conversation as if the agent had said it. Diagnostics go to
`launcher.log` in the conversation's state directory; `--verbose` mirrors them to
stderr for a human running this by hand.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The checkout lives beside this launcher, per the subagents/ convention.
CHECKOUT = HERE / "Raven-Oncall"
DEFAULT_CONFIG = HERE / "config.json"


def env_value(name: str) -> str | None:
    """Read a setting from the process environment, falling back to `.env`.

    The environment wins so a caller can override one value without editing a
    file that holds the others. Parsed by hand rather than with python-dotenv:
    this launcher must stay importable under a bare `python3`, since that is
    what the subagent entry invokes.
    """
    if value := os.environ.get(name):
        return value.strip()
    env_file = HERE / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


# Where each secret belongs in the config the agent loads. They stay out of
# `config.json` because that file is published; they are merged back in at
# launch (see `render_config`). Raven reads none of them from the environment -
# its config loader does no variable substitution - so a rendered file is the
# only way to get them in.
SECRET_SLOTS = {
    "ONCALL_API_KEY": ("providers", "custom", "apiKey"),
    "ONCALL_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "ONCALL_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}
REQUIRED_SECRETS = ("ONCALL_API_KEY",)

# Everything the runtime persists - transcripts, the cron store, the ops home,
# the wake shell's own files - lands here rather than in this folder. See
# `render_config` for why writing the config here is what moves them.
STATE_ROOT = Path(
    env_value("ONCALL_STATE_ROOT") or Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-oncall"
)
RENDERED_CONFIG = STATE_ROOT / "config.json"

# This launcher's own per-conversation state - the launcher log and the run
# record. Distinct from the runtime's data directory, which follows the rendered
# config to STATE_ROOT; see `render_config`.
RUN_ROOT = Path(env_value("ONCALL_RUN_ROOT") or STATE_ROOT / "runs")


# The host raven's config file, read for the fallbacks below. Read as JSON, never
# imported from raven: this launcher is standard-library only and has to run
# under a bare python3 that may not have the runtime installed at all.
HOST_CONFIG = Path(os.environ.get("RAVEN_HOME", "").strip() or Path.home() / ".raven") / "config.json"


def connections_registry() -> Path:
    """The machine registry this install reads: the owner's, not a copy of it.

    `raven.ops.connections` resolves its own path as
    `get_config_path().parent / "connections.json"`, which is STATE_ROOT here --
    so without an answer the on-call tools see no machines at all, and a task
    naming a remote path is read as naming a local one. Measured 2026-08-25: a
    `case_legA` under /home/cfd on the CPU box was looked for on this laptop,
    then attempted in a docker image pulled here.

    Answered with a pointer rather than a copy. A copy has to be made once and is
    then wrong from the first machine the owner adds, with nothing to say so --
    five byte-identical copies were on this computer when that was noticed. The
    machines belong to the owner, `raven ops connection add` writes to the
    owner's file, and the host's graph check reads that same file before it will
    dispatch an on-call node. Three readers, one file.

    An install that predates this and keeps its own list stays on it: stranding a
    working install to make the point is not worth it.
    """
    host = HOST_CONFIG.parent / "connections.json"
    if host.is_file():
        return host
    own = STATE_ROOT / "connections.json"
    return own if own.is_file() else host


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read."""
    try:
        return json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def dig(data: dict, path: tuple) -> str:
    for part in path:
        if not isinstance(data, dict):
            return ""
        data = data.get(part)
    return data if isinstance(data, str) else ""


def put(data: dict, path: tuple, value: str) -> None:
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def recommended_llm() -> str:
    """What this folder's manifest says this agent is tuned for."""
    try:
        rec = json.loads((HERE / "subagent.json").read_text(encoding="utf-8")).get("recommendedLlm") or {}
    except (OSError, ValueError):
        return "unrecorded"
    return f"{rec.get('model', '?')} via {rec.get('apiBase') or rec.get('provider', '?')}"


def inherit_llm(config: dict, host: dict) -> str:
    """Take the host raven's whole LLM configuration; return what was taken.

    Only reached when this agent has no key of its own. The host's provider block
    is copied wholesale rather than matched by name: a provider called `custom`
    here and one called `custom` there can be two different endpoints, so picking
    by name would silently point this agent at a gateway its model is not served
    on - a failure that looks like a bad answer rather than an error.

    What is inherited is which brains are reachable, which one is chosen, and how
    a model name routes to a provider. Deliberately not the rest of
    `agents.defaults`: the token ceiling, the tool-iteration cap and the timeouts
    are this agent's operating limits, tuned for its own job, and they have
    nothing to do with whose key is paying.
    """
    providers = host.get("providers") or {}
    if not any(isinstance(p, dict) and p.get("apiKey") for p in providers.values()):
        return ""
    for key in ("providers", "routing"):
        if key in host:
            config[key] = host[key]
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    host_defaults = (host.get("agents") or {}).get("defaults") or {}
    for key in ("provider", "model"):
        if key in host_defaults:
            defaults[key] = host_defaults[key]
    return f"provider={defaults.get('provider')} model={defaults.get('model')}"


def render_config(source: Path, dest: Path | None = None) -> Path:
    """Write a copy of `source` with the `.env` secrets merged in.

    Two properties fix the location and the lifetime, and getting either wrong
    breaks the watch rather than the run:

    - Its parent *is* the runtime's data directory (`config.paths.get_data_dir`
      returns the config file's own parent), so putting it under STATE_ROOT is
      what moves the transcripts, the cron store, the ops home and the wake
      shell's pid file and logs out of the project directory. There is no
      separate knob for any of them; they all hang off this one path. What
      matters is that every consumer agrees on it - `cron_store` and `ops_home`
      derive from the same config path, so they follow automatically.
    - It is a fixed name rather than a per-run one, and which hosting renders it
      decides whether anything removes it. Mode 600 either way, and outside any
      published tree.

      Under cli hosting the file is *not* deleted: `ensure_wake_shell` starts a
      resident process holding this path, that process outlives the spawn by
      design, and it re-reads the file on every wake -- so deleting it at the
      end of a run would strand the whole campaign.

      Under acp hosting `serve_acp` renders its OWN copy, named for this
      launcher's pid, and deletes that copy when the child ends. Per-pid because
      acp servers are not sequential: every TUI window's pool launches one, all
      on the same STATE_ROOT, and on one fixed name the first clean exit's
      unlink pulled the config out from under whichever server was still
      running -- whose `load_config` reads the file on every schema rebuild and
      silently boots on shipped defaults when it is gone (no key, oncall gate
      off). Same disease `a4bd394a` fixed for raven-ppt; `raven-code` and
      `raven-research` already name theirs by pid. The parent directory is
      unchanged, so the data directory every consumer derives from the config
      path stays the same one the cli hosting uses.

      The fixed name stays what it is: the CLI path's live contract, not a
      legacy file -- the resident wake shell re-reads exactly that path between
      runs, which is why the sweep below must never be able to reach it.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = host_config()

    # Each optional key falls back on its own: a missing Serper or Jina key is a
    # degradation, not a failure, and the host's is better than nothing.
    for name, path in SECRET_SLOTS.items():
        value = env_value(name) or ("" if name in REQUIRED_SECRETS else dig(host, path))
        if value:
            put(config, path, value)

    llm_key = REQUIRED_SECRETS[0]
    if env_value(llm_key):
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {llm_key} is not set and {HOST_CONFIG} has no provider key to inherit from; "
                f"put the key in {HERE / '.env'} (see .env.example), export it, or configure a "
                f"provider in the host raven"
            )
        log(f"[run] llm: inherited from {HOST_CONFIG} ({taken}); tuned for {recommended_llm()}")

    # `config.json` carries the workspace as a bare name so the published file
    # names no machine. Nothing passes --workspace, so this value is the one that
    # takes effect; it resolves under STATE_ROOT rather than beside this file,
    # because what the agent writes while working is no more the project's
    # business than its transcripts are.
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    if workspace := defaults.get("workspace"):
        defaults["workspace"] = str((STATE_ROOT / workspace).resolve())

    dest = dest or RENDERED_CONFIG
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    # Create it unreadable to anyone else before a single secret byte is in it.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return dest


_EXIT_TIMEOUT = 124
"""Exit code for a run the launcher killed on its own deadline.

GNU ``timeout``'s convention, and its own code rather than 1 because the two
are different terminal states: 1 is the agent reporting a config or credential
error, or finishing without committing an answer -- both of which say the run
*ran*. A kill says nothing about the work at all, and a caller that folds it
into the same code has no way back to that distinction.
"""

_LOG_FILE: Path | None = None
_VERBOSE = False


def log(message: str) -> None:
    """Record a diagnostic without contaminating the reply."""
    if _VERBOSE:
        print(message, file=sys.stderr, flush=True)
    if _LOG_FILE is not None:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def safe_name(value: str) -> str:
    """Fold a conversation id into one path segment.

    The gateway sends a uuid, but this is a CLI flag anyone can set, and it names
    a directory: `--session ../../elsewhere` must not escape the run root.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    cleaned = cleaned.strip(".") or "unnamed"
    return cleaned[:120]


def cron_store(config: Path) -> Path:
    return config.parent / "cron" / "jobs.json"


def ops_home(config: Path) -> Path:
    return config.parent / "ops"


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# the resident wake shell
# --------------------------------------------------------------------------


def pid_is_wake_shell(pid: int) -> bool:
    """True when `pid` is alive and is our shell, not a recycled pid.

    A pid file alone is not evidence: pids are reused, and a stale one pointing
    at some unrelated process would make every later run believe the watch is up
    while no wake ever fires.
    """
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno != errno.EPERM:
            return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        return "raven.ops.wake_shell" in cmdline.read_bytes().decode("utf-8", "replace")
    except OSError:
        # No procfs to check against: fall back to "the pid answers", which is
        # the best available and still better than trusting the file alone.
        return True


def ensure_wake_shell(
    config: Path, *, checkout: Path, dispatch_agent: str | None = None
) -> tuple[int | None, str]:
    """Guarantee exactly one wake shell is polling this install's cron store.

    Returns (pid, detail). Taken under an exclusive lock for the whole
    check-and-start: two spawns arriving together would otherwise both see no
    shell and start one each, and two claimers on one store is how a live run
    gets turns nobody asked for.
    """
    state_dir = config.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / "wake_shell.pid"
    lock_file = state_dir / "wake_shell.lock"
    shell_log = state_dir / "logs" / "wake_shell.log"
    shell_log.parent.mkdir(parents=True, exist_ok=True)

    with lock_file.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            recorded = read_json(pid_file) if pid_file.is_file() else None
            if isinstance(recorded, dict) and isinstance(recorded.get("pid"), int):
                pid = recorded["pid"]
                if pid_is_wake_shell(pid):
                    return pid, f"already running (pid {pid})"

            venv_bin = checkout / ".venv" / "bin"
            python = venv_bin / "python"
            if not python.is_file():
                return None, f"venv missing at {python}; run `uv sync` in {checkout}"

            env = {**os.environ}
            # The shell resolves the woken turn with shutil.which("raven"). Without
            # this the wake would run the host's raven - no ops tools - against this
            # config, and the failure is silent: the turn starts, finds no ops_*
            # tool, and improvises.
            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
            # LiteLLM otherwise fetches a remote price table on first completion in
            # every process, and this shell starts one process per wake.
            env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            env.setdefault("RAVEN_TRACING_DIR", str(state_dir / "traces"))
            # A woken turn reads the same machines as a spawned one. Without this
            # it falls back to whatever sits beside this install's own config,
            # and a wake is exactly where nobody is watching to notice.
            env.setdefault("RAVEN_CONNECTIONS", str(connections_registry()))

            argv = [
                str(python),
                "-m",
                "raven.ops.wake_shell",
                "--store",
                str(cron_store(config)),
                "--config",
                str(config),
                # A wake that came due while no shell was up has still not been
                # looked at. Without this the service drops it at startup and the
                # campaign stalls with nothing in any log saying why.
                "--fire-missed",
                # Where "this campaign concluded" surfaces for a person: the HOST
                # raven's cron store, whose TUI already polls it for reminders.
                # Without this, a campaign that ran and finished entirely in
                # wake-spawned turns ends in silence (measured 2026-08-25).
                "--notify-store",
                str(HOST_CONFIG.parent / "cron" / "jobs.json"),
            ]
            # Only when the host is holding an instance of us. Passed through from
            # the caller rather than decided here, because this file cannot tell
            # the two hostings apart: the argv is identical whether a person ran it
            # by hand for a benchmark or the host spawned it as a sub-agent, and
            # only the caller knows which. Off, a wake is answered in a child
            # process here, cold, exactly as before.
            if dispatch_agent:
                argv += ["--dispatch-agent", dispatch_agent]
            handle = shell_log.open("a", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(checkout),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=handle,
                    start_new_session=True,
                )
            finally:
                handle.close()

            # It refuses a shared store and exits 2; catching that here turns a
            # silently dead watch into a reported one.
            time.sleep(0.7)
            if proc.poll() is not None:
                tail = ""
                try:
                    tail = shell_log.read_text(encoding="utf-8").strip().splitlines()[-1][:200]
                except (OSError, IndexError):
                    pass
                return None, f"failed to start (exit {proc.returncode}) {tail}".strip()

            pid_file.write_text(
                json.dumps({"pid": proc.pid, "store": str(cron_store(config)), "started_at": int(time.time())}) + "\n",
                encoding="utf-8",
            )
            return proc.pid, f"started (pid {proc.pid}), log {shell_log}"
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# reading back what the run left on disk
# --------------------------------------------------------------------------


def session_file(python: Path, config: Path, workspace: Path, chat_id: str) -> Path | None:
    """Ask the checkout where it will keep this session's transcript.

    Computed by the build rather than reproduced here: the layout
    (`<workspace>/sessions/<channel>/<id>.jsonl`) and the filename escaping are
    its own, and a copy here would point at the wrong file the next time either
    changes. `_get_session_path` is private, but it is the single function that
    knows the answer.
    """
    code = (
        "import sys; from pathlib import Path;"
        "from raven.config.loader import set_config_path;"
        "from raven.session.manager import SessionManager;"
        "set_config_path(Path(sys.argv[1]));"
        "print(SessionManager(Path(sys.argv[2]))._get_session_path('cli:' + sys.argv[3]))"
    )
    proc = subprocess.run(
        [str(python), "-c", code, str(config), str(workspace), chat_id],
        capture_output=True,
        text=True,
        cwd=str(CHECKOUT),
    )
    if proc.returncode != 0:
        log(f"[run] could not resolve the session path: {proc.stderr.strip()[-300:]}")
        return None
    return Path(proc.stdout.strip())


def count_lines(path: Path) -> int:
    """Lines already in the transcript, so a resumed turn can skip them."""
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as stream:
        return sum(1 for _ in stream)


def extract_answer(path: Path, skip_lines: int = 0) -> str | None:
    """Return the last assistant answer written past `skip_lines`, or None.

    A row carrying `tool_calls` is a step, not an answer, so the last assistant
    row with text and no tool calls is the reply the agent committed.
    """
    answer: str | None = None
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index < skip_lines:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("_type") == "metadata":
                continue
            if row.get("role") != "assistant" or row.get("tool_calls"):
                continue
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                answer = content.strip()
    return answer


def pending_wakes(config: Path) -> list[str]:
    """Describe the wakes this install still owes, soonest first."""
    store = read_json(cron_store(config))
    if not isinstance(store, dict):
        return []
    rows: list[tuple[int, str]] = []
    now_ms = int(time.time() * 1000)
    for job in store.get("jobs", []):
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        # camelCase: that is how CronService serialises state (`nextRunAtMs`).
        # Read as `next_run_at_ms` this never matched, so every job fell through
        # the isinstance guard and the footer said "no wake is scheduled" on every
        # turn that had just scheduled one -- the one line the agent cannot write
        # about itself, and it was telling the operator the loop was dead while
        # the wake it had armed fired on time. The dataclass spelling is accepted
        # too, so a store written by anything using the python field names reads.
        state = job.get("state") or {}
        at = state.get("nextRunAtMs")
        if not isinstance(at, int):
            at = state.get("next_run_at_ms")
        if not isinstance(at, int):
            continue
        delta = (at - now_ms) // 1000
        when = time.strftime("%H:%M:%S", time.localtime(at / 1000))
        due = f"in {delta // 60}m{delta % 60:02d}s" if delta >= 0 else f"overdue by {-delta // 60}m"
        rows.append((at, f"{job.get('name', job.get('id', '?'))} at {when} ({due})"))
    return [text for _, text in sorted(rows)]


def campaign_state(config: Path) -> list[str]:
    """Summarise each campaign's trail: rounds, escalations, whether it closed.

    Read from the campaign's own files rather than from what the agent said,
    because the point of the trail is that it is not the agent's account.
    """
    home = ops_home(config)
    if not home.is_dir():
        return []
    lines: list[str] = []
    for campaign in sorted(p for p in home.iterdir() if p.is_dir()):
        ledger = read_json(campaign / "ledger.json")
        trials = len(ledger.get("records") or {}) if isinstance(ledger, dict) else 0
        closed = (campaign / "reports.jsonl").is_file()
        asked: str | None = None
        events = campaign / "events.jsonl"
        if events.is_file():
            try:
                for line in events.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line) if line.strip() else None
                    if isinstance(row, dict) and row.get("kind") == "ask_owner":
                        asked = str(row.get("question", ""))[:200]
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        state = "finished, report filed" if closed else "still open"
        lines.append(f"campaign '{campaign.name}': {trials} trial(s) in the ledger, {state}")
        if asked and not closed:
            # ops_ask_owner reaches nobody in this hosting - it is delivered to a
            # cli channel with no person on it - so the question would otherwise
            # sit unread in events.jsonl while the run waits on an answer.
            lines.append(f"  it asked the owner: {asked}")
    return lines


def build_task(task: str, *, config: Path, resuming: bool) -> str:
    """Prefix the task with the facts about this hosting the agent cannot discover.

    On a resumed turn the history is already in context, so the preamble states
    only what a fresh turn would not know.
    """
    where = (
        "You are continuing the same conversation as earlier."
        if resuming
        else (
            "You are on call, hosted headlessly: nobody is watching a terminal. Your wakes are "
            f"fired by a resident shell polling {cron_store(config)}, so scheduling one with "
            "ops_submit or ops_check_later really does bring you back - but a woken turn is a "
            "cold start with no memory of this conversation, so whatever the next turn needs "
            "must be in the campaign's ledger, not in your head."
        )
    )
    return (
        f"{task}\n\n"
        f"---\n"
        f"Environment: {where} Campaign state for this install lives under {ops_home(config)}. "
        f"ops_ask_owner records the question on the campaign's trail and the owner reads it "
        f"asynchronously - they may take a long time to answer, or never answer - so do not "
        f"stand still waiting for a reply. Your reply to this turn is the whole report the "
        f"caller receives right now."
    )


def sweep_stale_acp_renders() -> None:
    """Remove acp-rendered configs whose launcher is gone.

    Each is named for the pid of the launcher that wrote it, and that launcher
    lives exactly as long as the server it starts -- so a live pid is the one
    thing that says "some server still holds this". Liveness rather than age,
    for raven-ppt's reason: a server serves for as long as its sessions do, and
    any age short enough to be useful would take a config out from under one
    that is merely long-lived. `raven-code`, `raven-research` and `raven-ppt`
    sweep on the same rule.

    The glob cannot reach the fixed `config.json`: that name has no `.acp.<pid>`
    infix, and it is not legacy here but the CLI path's live contract -- the
    resident wake shell re-reads it between runs. Deleting it would strand a
    cli-hosted campaign, which is the exact opposite mistake of the one this
    sweep exists to clean up after.

    Failures are ignored per file: this is hygiene running ahead of a serve,
    and it must never be what stops one.
    """
    for stale in STATE_ROOT.glob("config.acp.*.json"):
        try:
            pid = int(stale.name.split(".")[2])
            os.kill(pid, 0)
        except (IndexError, ValueError, ProcessLookupError):
            stale.unlink(missing_ok=True)
        except (PermissionError, OSError):
            continue


def serve_acp(args: argparse.Namespace) -> int:
    """Serve this install to an ACP client, over the config the launcher renders.

    ``raven acp`` finds its config through ``RAVEN_HOME``. A vendored folder does
    not work that way: its config lives beside the checkout, and -- more to the
    point -- the published one holds no credentials. This launcher is what merges
    them, from ``.env`` or by inheriting the host's provider, into a 0600 copy
    under STATE_ROOT. Point an ACP row at the published file and the child comes
    up with no key at all, which surfaces as an internal error on the first
    prompt rather than as anything naming a config.

    So the same ``render_config`` the cli path uses runs here too, and the child
    is told to use its output. Secret merging is *adaptation*, and adaptation
    belongs in this file rather than in the checkout: the checkout is replaced
    wholesale by an upstream drop, and anything patched into it has to be patched
    in again every time.

    stdio is inherited untouched -- it IS the protocol transport, and a pipe of
    our own here would leave the client talking to a launcher that does not speak
    ACP. This process then exists only to hold the rendered config's lifetime:
    it waits, and deletes the file when the child is done.
    """
    root = Path(args.checkout).expanduser().resolve()
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: venv missing at {raven_bin}; run `uv sync` in {root}")

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    state_dir = RUN_ROOT / "acp"
    state_dir.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = state_dir / "launcher.log"

    sweep_stale_acp_renders()
    config = render_config(
        Path(args.config).expanduser().resolve(),
        dest=STATE_ROOT / f"config.acp.{os.getpid()}.json",
    )
    if not config.is_file():
        raise SystemExit(f"error: no config at {config}")
    log(f"[acp] serving from {config}")

    env = {**os.environ}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env.setdefault("RAVEN_TRACING_DIR", str(config.parent / "traces"))
    # The same registry the cli path gives a woken turn: the machines belong to
    # the owner, and an ACP-served turn reads them for the same reason.
    env.setdefault("RAVEN_CONNECTIONS", str(connections_registry()))

    try:
        proc = subprocess.Popen(
            [str(raven_bin), "acp", "--config", str(config)], cwd=str(root), env=env
        )
        return proc.wait()
    except KeyboardInterrupt:
        # The client went away. The child watches its own stdin for EOF, so it is
        # already on its way out; wait briefly rather than leaving it holding the
        # config we are about to delete.
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        return 0
    finally:
        # Deletes this launcher's OWN per-pid copy, never the fixed name the
        # cli path's wake shell re-reads. Before the pid naming, this unlink on
        # a shared name was how one window's clean exit dropped another
        # window's still-running server onto shipped defaults, silently. A
        # SIGKILL skips this finally and leaves the file; the sweep at the next
        # serve collects it by dead pid. See `render_config` for the lifetime
        # the two hostings each want.
        config.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Raven-Oncall watch agent for one turn")
    ap.add_argument("--task", help="The task")
    ap.add_argument("--prompt-file", help="File holding the task (alternative to --task)")
    # The gateway substitutes {agent_id} here: a uuid it mints on the first turn
    # of a conversation and replays on every later one.
    ap.add_argument("--session", help="Conversation id; turns sharing one continue the same session")
    ap.add_argument("--job", help="State directory name for a run by hand (default: a unique name)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--checkout", default=str(CHECKOUT))
    # No wall-clock cap by default: the first turn of a campaign submits a job and
    # returns, but a turn that decides to look before replying has no predictable
    # length. The per-LLM-call and per-command limits in config.json still apply.
    ap.add_argument("--timeout", type=int, default=0, help="Seconds; 0 (default) means no limit")
    ap.add_argument(
        "--no-wake-shell",
        action="store_true",
        help="Do not start the resident wake shell; scheduled wakes will not fire unless "
        "something else is polling this store (a TUI on this config, for instance)",
    )
    ap.add_argument("--keep-going", action="store_true", help="Do not fail when no answer was committed")
    ap.add_argument("--verbose", action="store_true", help="Mirror diagnostics to stderr; never when spawned")
    ap.add_argument(
        "--acp",
        action="store_true",
        help=(
            "Serve the Agent Client Protocol on stdio instead of running one turn. "
            "The host calls this for a `kind: \"acp\"` row; the credentials are "
            "merged into a rendered config exactly as they are for a cli turn."
        ),
    )
    ap.add_argument(
        "--dispatch-agent",
        default=None,
        help=(
            "The name the host raven knows this agent by. Set it in subagent.json's "
            "command templates and a woken turn runs in this instance's pane instead "
            "of in a child process nobody reads; leave it off for a headless "
            "benchmark run, where a wake is a cold start by design. NOTE: the wake "
            "shell is one per install and keeps whatever mode it was started in, so "
            "switching modes means stopping the running shell first."
        ),
    )
    args = ap.parse_args()

    # Before the task check: an ACP session carries its prompts over the wire,
    # so there is no --task to give and requiring one would refuse every start.
    if args.acp:
        return serve_acp(args)

    if args.prompt_file:
        task = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    elif args.task:
        task = args.task.strip()
    else:
        raise SystemExit("error: pass --task or --prompt-file")
    if not task:
        raise SystemExit("error: the task is empty")

    # Resolved: the interpreter path is handed to a subprocess whose cwd is the
    # checkout, so a relative --checkout would not survive the chdir.
    root = Path(args.checkout).expanduser().resolve()
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: venv missing at {raven_bin}; run `uv sync` in {root}")

    conversation = args.session or args.job or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    state_dir = RUN_ROOT / safe_name(conversation)
    state_dir.mkdir(parents=True, exist_ok=True)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = state_dir / "launcher.log"

    config = render_config(Path(args.config).expanduser().resolve())
    if not config.is_file():
        raise SystemExit(f"error: no config at {config}")

    # Before the turn, not after: the agent can schedule a wake inside this turn,
    # and a shell started afterwards would race the job it is meant to fire.
    # This is the cli path's answer to "what fires a wake after the spawn
    # returns", and it is the only caller. `serve_acp` needs no equivalent: see
    # `render_config` for why a pooled server is its own scheduler.
    shell_pid, shell_detail = (None, "not started (--no-wake-shell)")
    if not args.no_wake_shell:
        shell_pid, shell_detail = ensure_wake_shell(
            config, checkout=root, dispatch_agent=args.dispatch_agent
        )
    log(f"[run] wake shell: {shell_detail}")

    workspace = Path(
        json.loads(config.read_text(encoding="utf-8"))
        .get("agents", {})
        .get("defaults", {})
        .get("workspace", str(Path.home() / ".raven" / "workspace"))
    ).expanduser()

    transcript = session_file(root / ".venv" / "bin" / "python", config, workspace, conversation)
    pre_lines = count_lines(transcript) if transcript is not None else 0
    resuming = pre_lines > 0

    argv = [
        str(raven_bin),
        "agent",
        "--config",
        str(config),
        "--session",
        f"cli:{conversation}",
        "--no-markdown",
        # `--wait-skill-extract` / `--flush-skill-buffer` were dropped upstream by
        # PR #334 (059e1c1e, 2026-08-17) as inert, and this checkout is past that
        # commit -- passing them makes `raven agent` exit 2 before the turn starts.
        "-m",
        build_task(task, config=config, resuming=resuming),
    ]
    log(f"[run] turn={'resume' if resuming else 'first'} timeout={args.timeout or 'none'} transcript={transcript}")

    # Traces are the one thing that does not follow --config (they key off
    # RAVEN_TRACING_DIR / RAVEN_HOME, not the data dir), so without this every
    # turn here would write into the host raven's ~/.raven/traces.
    env = {
        **os.environ,
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "RAVEN_TRACING_DIR": os.environ.get("RAVEN_TRACING_DIR", str(config.parent / "traces")),
    }
    env.setdefault("RAVEN_CONNECTIONS", str(connections_registry()))
    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            timeout=args.timeout or None,
            capture_output=True,
            text=True,
            env=env,
        )
        rc: int | None = proc.returncode
        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
    except subprocess.TimeoutExpired:
        # Its own flag rather than `rc is None` read back at each later branch:
        # what the launcher owes the caller after a kill differs from what it
        # owes after an exit, and recovering that distinction from a sentinel
        # exit code is how the two came to be conflated below.
        timed_out = True
        rc, stderr_tail = None, ["timed out"]
    elapsed = int(time.time() - started)
    log(f"[run] exit={rc} elapsed={elapsed}s")
    for line in stderr_tail:
        log(f"[run]   {line}")

    if rc == 1:
        # The only documented non-zero exit: config or credential error.
        log("[run] FAILED: config/credential error")
        print(f"FAILED: Raven-Oncall exited 1 (config or credential error). See {_LOG_FILE}", flush=True)
        return 1

    answer = None
    if transcript is not None and transcript.is_file():
        answer = extract_answer(transcript, pre_lines)
    elif timed_out:
        # Not "no transcript found": after a kill the absence is a consequence
        # of the kill, and reporting it as the finding is what put "no
        # transcript" in front of a user whose run had actually timed out.
        log("[run] no transcript: killed before the agent wrote one")
    else:
        log("[run] no transcript found")
    # Held before the fallbacks below overwrite `answer` with a sentence about
    # the run: only a value that came out of the transcript is the agent's own
    # answer, and only that one can be described as having been committed.
    committed = answer is not None
    log(f"[run] answer_chars={len(answer or '')}")

    if answer is None and timed_out:
        # A terminal state of its own, with its own exit code: the run was cut
        # short, which is not the same claim as the agent having finished
        # without an answer, and only the caller can decide whether to rerun it
        # with a longer deadline or take the work elsewhere.
        log(f"[run] TIMEOUT: killed after {args.timeout}s with nothing committed")
        if not args.keep_going:
            print(
                f"TIMEOUT: Raven-Oncall was killed after {args.timeout}s before it committed an answer. "
                f"See {_LOG_FILE}",
                flush=True,
            )
            return _EXIT_TIMEOUT
        answer = f"(killed after {args.timeout}s with nothing committed)"
    elif answer is None:
        log("[run] FAILED: no answer was committed")
        if not args.keep_going:
            print(f"FAILED: Raven-Oncall produced no answer (no answer was committed). See {_LOG_FILE}", flush=True)
            return 1
        answer = "(no answer committed)"

    # The watch footer is the part of the reply the agent cannot write: whether
    # anything will actually come back for it, and what its own trail says.
    footer: list[str] = []
    wakes = pending_wakes(config)
    if wakes:
        watcher = f"pid {shell_pid}" if shell_pid else f"NO WATCHER - {shell_detail}"
        footer.append(f"next wake ({watcher}): " + "; ".join(wakes[:3]))
    elif shell_pid:
        footer.append("no wake is scheduled: nothing will come back on its own")
    footer.extend(campaign_state(config))

    out = [answer]
    if timed_out and committed:
        # The answer above is the agent's own -- it reached the transcript
        # before the kill landed. That gap is routine: the process goes on
        # doing its own end-of-turn work after committing an answer, so a
        # deadline lands on a run that already has one. (The flag this used to
        # cite, `--wait-skill-extract`, is no longer passed -- it was dropped
        # upstream as inert; see the argv above. The gap it named outlived it.)
        # But the run did not finish, so whatever it would have done next is
        # gone.
        # Returning 0 without saying so is how a timed-out run reported as a
        # clean success. The `--keep-going` case says it in `answer` instead:
        # there is no committed answer there for this line to qualify.
        out.append(f"\n--- timed out: killed after {args.timeout}s, after this answer was committed")
    if footer:
        out.append("\n--- watch state\n" + "\n".join(footer))
    print("\n".join(out), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        # A setup error the caller caused has to reach them as the reply. stderr
        # would too - the backend folds it in - but only when it decides stderr is
        # non-empty, so the reliable channel is stdout, same as every other
        # failure here. Numeric exits (argparse) pass through untouched.
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            sys.exit(1)
        raise
