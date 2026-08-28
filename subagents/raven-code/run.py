#!/usr/bin/env python
"""Host-side launcher for the Raven-Code coding agent.

Raven-Code is the `feat/acp_worktree_integration` branch of Raven-X, which is
built for exactly this shape: an external orchestrator hands work to it as a
worker. See `Raven-main/README.SWARM.md` for the branch's own contract. This
wrapper supplies the three things that contract leaves to the caller:

- **a session identity per conversation.** One call is one turn; the same
  `--session cli:<id>` across calls is a multi-turn conversation. The gateway's
  `{agent_id}` is that id.
- **a stable workspace per conversation.** The gateway spawns a cli subagent with
  the host agent's session workspace as cwd, so that is the workspace the inner
  agent gets: the sub-agent works where the caller works, on the caller's files,
  with nothing to declare and nothing to copy. Sessions are bucketed by the
  workspace's absolute path, so the same id against a different one is a
  *different* conversation; the first turn's path is recorded and replayed.
- **an answer, and a verdict on whether there is one.** Exit 0 does not mean the
  agent produced anything: only a config or credential error maps to non-zero.

stdout carries the answer and nothing else. Raven's CLI backend uses the whole of
a child's output as the subagent's reply (stdout plus stderr when stderr is
non-empty), so any progress line printed here would be pasted into the
conversation as if the agent had said it. Diagnostics go to `launcher.log` in the
conversation's state directory; `--verbose` mirrors them to stderr for a human
running this by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The checkout lives beside this launcher, per the subagents/ convention.
CHECKOUT = HERE / "Raven-main"
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
    "CODE_API_KEY": ("providers", "custom", "apiKey"),
    "CODE_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "CODE_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}
REQUIRED_SECRETS = ("CODE_API_KEY",)

# The host raven's config file, read for the fallbacks below. Read as JSON, never
# imported from raven: this launcher is standard-library only and has to run
# under a bare python3 that may not have the runtime installed at all.
_RAVEN_HOME = Path(os.environ.get("RAVEN_HOME", "").strip() or Path.home() / ".raven")
HOST_CONFIG = _RAVEN_HOME / "config.json"
_DEFAULT_HOST_WORKSPACE = "~/.raven/workspace"


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read."""
    try:
        return json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def host_agent_home() -> Path:
    """Return the Agent home configured by the host raven."""
    host = host_config()
    raw = None
    if isinstance(host, dict):
        raw = ((host.get("agents") or {}).get("defaults") or {}).get("workspace")
    if isinstance(raw, str) and raw.strip():
        configured = raw.strip()
        if configured == _DEFAULT_HOST_WORKSPACE:
            return _RAVEN_HOME / "workspace"
        return Path(configured).expanduser()
    return _RAVEN_HOME / "workspace"


def state_root() -> Path:
    """Return the root reserved for Raven-Code state under the host Agent home."""
    raw = env_value("CODE_STATE_ROOT")
    if raw:
        override = Path(raw).expanduser()
        return override if override.is_absolute() else host_agent_home() / override
    return host_agent_home() / "subagent_sessions" / "raven-code"


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


def sweep_stale_renders(state_dir: Path) -> None:
    """Remove rendered configs whose launcher is gone.

    Each file is named for the pid of the launcher that wrote it, and that
    launcher outlives the agent it starts in both modes, so a live pid is the
    one thing that says "some run still holds this".

    Liveness rather than age, because age is wrong in the `--acp` mode this
    launcher gained: the host tears a served session down by SIGKILLing its
    process group, which leaves no chance to clean up from inside, so
    `_serve_acp`'s own `finally` never runs -- the promise in its comment is one
    the process cannot keep. The 24-hour sweep it fell back on was sized for a
    stranding that only happened when something went wrong; under ACP that
    stranding is how every session ends.

    No age fallback was kept beside the liveness test, though a recycled pid can
    hold a stranded file past its welcome. Removing a config once it is old
    enough would take it out from under a session that has simply been up a long
    time, which is the worse of the two failures. `raven-research` sweeps the
    same file name in the same directory on liveness alone and says the same
    thing in its own docstring; two launchers disagreeing about when this file
    may be removed would be worse than either rule.

    Failures are ignored per file: this is hygiene running ahead of a run, and
    it must never be what stops one.
    """
    for stale in state_dir.glob(".config.rendered.*.json"):
        try:
            pid = int(stale.name.split(".")[3])
            os.kill(pid, 0)
        except (IndexError, ValueError, ProcessLookupError):
            stale.unlink(missing_ok=True)
        except (PermissionError, OSError):
            continue


def render_config(source: Path, state_dir: Path) -> Path:
    """Write a rendered config into the state partition that will consume it.

    The runtime derives its data directory from the config file's parent but its
    Agent home from ``agents.defaults.workspace``. Point both at ``state_dir``
    so sessions, memory and skills stay in the same host-owned partition. The
    ACP session Working directory remains a separate path used for repository
    reads, edits and commands.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    # Config location controls runtime data, but does not change workspace_path.
    # Pin it explicitly so Raven-Code does not fall back to ~/.raven/workspace.
    put(config, ("agents", "defaults", "workspace"), str(state_dir.resolve()))
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

    state_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale_renders(state_dir)

    rendered = state_dir / f".config.rendered.{os.getpid()}.json"
    # Create it unreadable to anyone else before a single secret byte is in it.
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered


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
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def safe_name(value: str) -> str:
    """Fold a conversation id into one path segment.

    The gateway sends a uuid, but this is a CLI flag anyone can set, and it names
    a directory: `--session ../../elsewhere` must not escape the run root.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    cleaned = cleaned.strip(".") or "unnamed"
    return cleaned[:120]


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required for Raven-Code workspace allocation")
    return subprocess.run([executable, *args], cwd=str(cwd), capture_output=True, text=True)


def session_file(python: Path, config: Path, workspace: Path, chat_id: str) -> Path | None:
    """Ask the checkout where it will keep this session's transcript.

    Computed by the build rather than reproduced here: the location is
    `<config dir>/sessions/<escaped workspace path>/cli/<chat id>.jsonl`, and both
    the bucketing and the escaping are its own functions. Re-implementing them
    would silently point at the wrong file the next time either changes.
    """
    # set_config_path, not load_config: only the setter moves the data dir off
    # ~/.raven, and it is what the CLI itself calls before loading (see
    # `load_runtime_config`). Getting this wrong resolves to the *host* Raven's
    # directory and the answer is never found.
    code = (
        "import sys; from pathlib import Path;"
        "from raven.config.loader import set_config_path;"
        "from raven.config.paths import get_workspace_state_dir;"
        "from raven.utils.helpers import safe_filename;"
        "set_config_path(Path(sys.argv[1]));"
        "print(get_workspace_state_dir(Path(sys.argv[2]), 'sessions') / 'cli' /"
        " (safe_filename(sys.argv[3]) + '.jsonl'))"
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
    """Lines already in the transcript, so a resumed turn can skip them.

    A resumed turn appends to the file it continues, so without this the previous
    turn's answer is still the last assistant row and a turn that produced
    nothing would be reported as a success carrying stale text.
    """
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as stream:
        return sum(1 for _ in stream)


def extract_answer(path: Path, skip_lines: int = 0) -> str | None:
    """Return the last assistant answer written past `skip_lines`, or None.

    A row carrying `tool_calls` is a step, not an answer, and empty assistant
    rows are dropped before they reach disk, so the last assistant row with text
    and no tool calls is the reply the agent committed.
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


def describe_changes(repo: Path) -> str:
    """Summarise the working-tree footprint of a run, when the workspace is a checkout.

    A name-and-count summary rather than a patch: the agent edits the caller's
    real files, so what the caller needs from the reply is where to look. The
    diff itself is already in their working tree, and `git diff` shows it better
    than a copy pasted into conversation.
    """
    status = git("status", "--porcelain", cwd=repo)
    if status.returncode != 0:
        return ""
    lines = [line for line in status.stdout.splitlines() if line.strip()]
    if not lines:
        return "no files were changed in the working tree"
    stat = git("diff", "--stat", cwd=repo)
    body = "\n".join(lines[:40])
    if len(lines) > 40:
        body += f"\n... and {len(lines) - 40} more"
    tail = stat.stdout.strip().splitlines()[-1:] if stat.returncode == 0 else []
    summary = f"working tree of {repo} after this run:\n{body}"
    if tail:
        summary += f"\n{tail[0].strip()}"
    return summary


def build_task(task: str, workspace: Path, *, resuming: bool) -> str:
    """Prefix the task with the facts about this run the agent cannot discover.

    On a resumed turn the history is already in context, so the preamble states
    only what changed. Left out, the agent reads a fresh "you are working in"
    line against files it remembers creating and treats it as a contradiction.
    """
    where = (
        f"You are continuing in {workspace}, the same directory as earlier in this conversation, "
        f"so anything you left there is still present."
        if resuming
        else (
            f"You are working in {workspace}, the directory the caller works in. Files already "
            f"there are theirs and your edits change them for real."
        )
    )
    # Task first, environment after. The order is not cosmetic: everos extracts a
    # memory from each turn by summarising the message, and with the preamble
    # leading it stored "raven-code received a runtime context in an empty scratch
    # directory" instead of the work. Leading with the task also matches what the
    # turn is actually about.
    return (
        f"{task}\n\n"
        f"---\n"
        f"Environment: {where} You can read and write anywhere on this host. Nobody is watching "
        f"this run - there is no channel to ask a question on, so where the task is ambiguous "
        f"choose the most defensible option and say which assumption you made. Your reply is the "
        f"whole report the caller receives."
    )


def resolve_workspace(state_dir: Path, override: str | None) -> Path:
    """Return the workspace, keeping it stable across turns.

    The default is this process's cwd, which is the host agent's session
    workspace: the gateway starts a cli subagent there, and inheriting it is what
    puts this agent on the caller's files rather than in a private directory of
    its own. Sessions are bucketed by the workspace's absolute path, so a later
    turn arriving with a different cwd would be a *different* conversation with
    no history - the first turn's path is recorded here and replayed.
    """
    pointer = state_dir / "workspace"
    recorded = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() else None

    if override:
        workspace = Path(override).expanduser().resolve()
        if recorded and recorded != str(workspace):
            raise SystemExit(
                f"error: this conversation is already bound to {recorded}; {workspace} would be a "
                f"new conversation with no history. Drop --workspace to continue it."
            )
        workspace.mkdir(parents=True, exist_ok=True)
    elif recorded:
        return Path(recorded)
    else:
        workspace = Path.cwd().resolve()

    pointer.write_text(str(workspace) + "\n", encoding="utf-8")
    return workspace


def _serve_acp(args) -> int:
    """Serve `raven acp` on this process's stdio, under a rendered config.

    The adaptation the checkout cannot carry: `raven acp` finds its config
    through RAVEN_HOME, and a vendored folder does not work that way -- its
    secrets live in this folder's `.env` and have to be merged into a copy
    first. Rendering here rather than in the checkout is what keeps the next
    upstream drop from wiping it.

    stdio is inherited untouched: fd 0/1/2 ARE the protocol channel, so the
    child must own exactly the descriptors this process was handed.
    """
    root = Path(args.checkout).expanduser().resolve()
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: venv missing at {raven_bin}; run `uv sync` in {root}")

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    acp_state = state_root() / "acp"
    _LOG_FILE = acp_state / "launcher.log"
    acp_state.mkdir(parents=True, exist_ok=True)

    config = render_config(Path(args.config).resolve(), acp_state)
    log(f"[run] acp: serving on stdio under {config}")
    # Arm the first-write workspace gate for the multiplexed server: per-session
    # allocation records under this partition, repo-level owners/locks/worktrees
    # under `repos`, and one pinned state bucket so runtime state stays in this
    # partition no matter which checkout a session serves. Without these the
    # vendored gate stays unarmed and Raven-Code writes wherever it lands.
    env = dict(os.environ)
    env["RAVEN_WORKSPACE_ALLOC_BASE"] = str(acp_state)
    env["RAVEN_WORKSPACE_ALLOC_REPOS"] = str(state_root() / "repos")
    env.setdefault("RAVEN_WORKSPACE_STATE_BUCKET", "acp")
    try:
        proc = subprocess.Popen([str(raven_bin), "acp", "--config", str(config)], cwd=str(root), env=env)
        return proc.wait()
    finally:
        # The rendered copy holds the merged secrets at 0600; a served session
        # ending must not leave it behind.
        config.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Raven-Code coding agent on one task")
    ap.add_argument("--task", help="The coding task")
    ap.add_argument("--prompt-file", help="File holding the task (alternative to --task)")
    # The gateway substitutes {agent_id} here: a uuid it mints on the first turn
    # of a conversation and replays on every later one.
    ap.add_argument("--session", help="Conversation id; turns sharing one continue the same session")
    ap.add_argument("--job", help="State directory name for a run by hand (default: a unique name)")
    ap.add_argument(
        "--workspace",
        help="Work in this directory instead of the inherited cwd (for a run by hand)",
    )
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    # No wall-clock cap by default: a real coding task has no predictable length,
    # and a killed run produces nothing at all - no answer, and no memory, since
    # extraction is what the drain below waits for. Pass a positive value to cap
    # it. The per-LLM-call (`agents.defaults.llmCallTimeout`) and per-command
    # (`tools.exec.maxTimeout`) limits stay: those bound a *stalled* backend or
    # command, which is a hang rather than a long task.
    ap.add_argument("--timeout", type=int, default=0, help="Seconds; 0 (default) means no limit")
    ap.add_argument("--checkout", default=str(CHECKOUT))
    ap.add_argument("--keep-going", action="store_true", help="Do not fail when no answer was committed")
    ap.add_argument("--verbose", action="store_true", help="Mirror diagnostics to stderr; never when spawned")
    ap.add_argument(
        "--acp",
        action="store_true",
        help="Serve the Agent Client Protocol on stdio instead of running one task",
    )
    args = ap.parse_args()

    if args.acp:
        return _serve_acp(args)

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
    # The console script, not `python -m raven.cli`: raven.cli is a package with
    # no __main__, so the module form cannot be executed.
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: venv missing at {raven_bin}; run `uv sync` in {root}")

    conversation = args.session or args.job or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    state_dir = state_root() / f"instance-{safe_name(conversation)}"
    state_dir.mkdir(parents=True, exist_ok=True)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = state_dir / "launcher.log"

    config = render_config(Path(args.config).resolve(), state_dir)
    workspace = resolve_workspace(state_dir, args.workspace)

    transcript = session_file(root / ".venv" / "bin" / "python", config, workspace, conversation)
    pre_lines = count_lines(transcript) if transcript is not None else 0
    resuming = pre_lines > 0

    argv = [
        str(raven_bin),
        "agent",
        "--config",
        str(config),
        "--workspace",
        str(workspace),
        # The full `cli:<id>` form on every turn, first included: it is returned
        # unchanged by the resolver, so it creates the session when absent and
        # resumes it when present. Never `--continue` - it picks "the most recent
        # cli session" in the bucket, which is a race between parallel workers
        # sharing a workspace.
        "--session",
        f"cli:{conversation}",
        "--no-markdown",
        # Without this the process exits as soon as the answer is ready and
        # interpreter shutdown cancels the in-flight everos extraction, so
        # nothing is ever written to long-term memory - measured: no store file
        # touched in 15 minutes across three conversations.
        "--wait-skill-extract",
        # And without the flush, case + skill extraction never runs at all: a
        # lone `-m` turn does not trip a boundary on its own, and most spawns are
        # exactly one turn, so the agent track (reusable coding cases) stayed
        # empty even after a real fix-the-bug task. The cost is one extraction
        # pass per turn instead of one per detected boundary. Raven's own session
        # continuity is unaffected - it lives in the transcript, not in everos's
        # buffer - so multi-turn conversations still resume normally.
        "--flush-skill-buffer",
        "-m",
        build_task(task, workspace, resuming=resuming),
    ]
    log(
        f"[run] workspace={workspace} cwd={Path.cwd()} turn={'resume' if resuming else 'first'} "
        f"timeout={args.timeout or 'none'} transcript={transcript}"
    )

    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            timeout=args.timeout or None,
            capture_output=True,
            text=True,
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
    finally:
        config.unlink(missing_ok=True)
    elapsed = int(time.time() - started)
    log(f"[run] exit={rc} elapsed={elapsed}s")
    for line in stderr_tail:
        log(f"[run]   {line}")

    if rc == 1:
        # The only documented non-zero exit: config or credential error.
        log("[run] FAILED: config/credential error")
        # A failure reason belongs in the reply: the caller has to know why.
        print(f"FAILED: Raven-Code exited 1 (config or credential error). See {_LOG_FILE}", flush=True)
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

    changes = describe_changes(workspace) if (workspace / ".git").exists() else ""
    if changes:
        log(f"[run] {changes.splitlines()[0]}")

    if answer is None and timed_out:
        # A terminal state of its own, with its own exit code: the run was cut
        # short, which is not the same claim as the agent having finished
        # without an answer, and only the caller can decide whether to rerun it
        # with a longer deadline or take the work elsewhere.
        log(f"[run] TIMEOUT: killed after {args.timeout}s with nothing committed")
        if not args.keep_going:
            print(
                f"TIMEOUT: Raven-Code was killed after {args.timeout}s before it committed an answer. See {_LOG_FILE}",
                flush=True,
            )
            return _EXIT_TIMEOUT
        answer = f"(killed after {args.timeout}s with nothing committed)"
    elif answer is None:
        log("[run] FAILED: no answer was committed")
        if not args.keep_going:
            print(f"FAILED: Raven-Code produced no answer (no answer was committed). See {_LOG_FILE}", flush=True)
            return 1
        answer = "(no answer committed)"

    out = [answer]
    if timed_out and committed:
        # The answer above is the agent's own -- it reached the transcript
        # before the kill landed, and `--wait-skill-extract` alone means the
        # process routinely outlives the answer it already committed. But the
        # run did not finish, so whatever it would have done next is gone.
        # Returning 0 without saying so is how a timed-out run reported as a
        # clean success. The `--keep-going` case says it in `answer` instead:
        # there is no committed answer there for this line to qualify.
        out.append(f"\n--- timed out: killed after {args.timeout}s, after this answer was committed")
    if changes:
        out.append(f"\n--- {changes}")
    print("\n".join(out), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        # A setup error the caller caused (no task, a workspace that contradicts
        # the conversation) has to reach them as the reply.
        # stderr would too - the backend folds it in - but only when it decides
        # stderr is non-empty, so the reliable channel is stdout, same as every
        # other failure here. Numeric exits (argparse) pass through untouched.
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            sys.exit(1)
        raise
