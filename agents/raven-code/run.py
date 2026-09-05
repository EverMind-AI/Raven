#!/usr/bin/env python
"""Host-side launcher for Raven-Code -- the B side, on installed raven.

The vendored Raven-Code (subagents/raven-code) carries a whole fork
checkout; this product carries its assets only, and as of the exec-target
swap the process it starts is installed raven itself. Two hostings, the
fork launcher's own pair:

- ``--acp`` (what the roster row spawns): render the config, then exec
  ``python -m raven acp`` on this interpreter, so the server inherits this
  pid, process group and stdio, and every turn runs through the same
  assembly door (build_runtime) as the host's TUI and gateway.
- ``--task`` / ``--prompt-file``: the one-turn CLI adjudication hosting.
  One call is one turn; the same ``--session <id>`` across calls is a
  multi-turn conversation. The launcher renders, runs ``raven agent -m``
  on the installed raven, and owes the caller the fork launcher's five
  commitments: a task-first preamble, a transcript verdict (the answer is
  what the agent committed, never a replay of a previous turn's), a
  working-tree footprint footer, a workspace pinned per conversation with
  a contradicting ``--workspace`` refused, and exit 124 for a launcher
  kill kept distinct from exit 1.

The fork's coding conduct rides the rendered config now, not the process
environment: ``plugins.dirs`` names the code-flow plugin, and the
``workspaceGate`` block in its slice arms the first-write gate (the fork's
``RAVEN_WORKSPACE_ALLOC_*`` env contract respelled as config, per D6) --
multiplexed layout for the ACP server, one-conversation layout for a CLI
instance. The launch contract is otherwise the fork launcher's: refuse
without any LLM key before any process starts, seed the fork's TOOLS.md
wording into the state partition once, keep secrets out of the published
config and in a 0600 rendered copy whose parent decides the data dir.

Serving ACP, stdout belongs to the protocol and diagnostics go to stderr.
Hosting a CLI turn, stdout carries the answer and nothing else -- the
caller's backend folds non-empty stderr into the conversation -- so
diagnostics go to ``launcher.log`` in the conversation's state directory;
``--verbose`` mirrors them for a human running this by hand.
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

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_ID = "code-flow"
GUIDE = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "TOOLS_CODE.md"

PRODUCT = "raven-code"

# Where each secret belongs in the config the engine loads; keys stay out of
# config.json because that file is published.
SECRET_SLOTS = {
    "CODE_API_KEY": ("providers", "custom", "apiKey"),
    "CODE_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "CODE_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config), so it stays out of the
# optional fallback loop.
REQUIRED_SECRETS = ("CODE_API_KEY",)

_DEFAULT_HOST_WORKSPACE = "~/.raven/workspace"

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


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def host_agent_home() -> Path:
    """The Agent home the host raven is configured to use.

    Read from the host config rather than assumed: an operator who moved
    ``agents.defaults.workspace`` moved every product's state with it, and a
    launcher pinning ``raven_home()/workspace`` regardless would silently
    relocate this product's sessions. The schema-default spelling resolves to
    the place it means.
    """
    raw = ((render.host_config().get("agents") or {}).get("defaults") or {}).get("workspace")
    if isinstance(raw, str) and raw.strip():
        configured = raw.strip()
        if configured == _DEFAULT_HOST_WORKSPACE:
            return raven_home() / "workspace"
        return Path(configured).expanduser()
    return raven_home() / "workspace"


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    raw = env_value("CODE_STATE_ROOT")
    if raw:
        override = Path(raw).expanduser()
        return override if override.is_absolute() else host_agent_home() / override
    return host_agent_home() / "subagent_sessions" / PRODUCT


def log(message: str) -> None:
    """Record a diagnostic without contaminating the reply channel.

    Serving ACP, stdout is the protocol and stderr is free: diagnostics go
    straight to stderr. Hosting a CLI turn (``_LOG_FILE`` set), the caller's
    backend folds non-empty stderr into the conversation as if the agent had
    said it, so diagnostics go to the conversation's launcher.log and only
    ``--verbose`` mirrors them to stderr.
    """
    if _LOG_FILE is None or _VERBOSE:
        print(message, file=sys.stderr, flush=True)
    if _LOG_FILE is not None:
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def seed_guide(partition: Path) -> None:
    """Seed the partition's TOOLS.md with the fork's own wording, once.

    Raven writes workspace templates only for files still missing, so seeding
    first is what keeps the fork's tool guidance (exec sessions, background
    jobs, the 30k spill) in front of the model instead of the trunk template
    that would otherwise land there, and an operator's in-place edits are
    never overwritten.
    """
    render.seed_once(partition / "TOOLS.md", lambda: GUIDE.read_text(encoding="utf-8"))


def render_config(source: Path, partition: Path, gate: dict[str, str]) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under ``partition``.

    The partition is the rendered file's parent and the pinned Agent home, so
    the runtime data dir, transcripts and allocation records stay in this
    host-owned partition; the working directory repository work happens in
    stays a separate path (the ACP session cwd, or ``--workspace``). ``gate``
    is the workspaceGate arming rendered into the code-flow slice -- this
    render owns those spellings and the plugin conforms (verdict D6: the
    fork's ``RAVEN_WORKSPACE_ALLOC_*`` env contract, moved into config).
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=REQUIRED_SECRETS, lookup=env_value)

    llm_key = REQUIRED_SECRETS[0]
    if env_value(llm_key):
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = render.inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {llm_key} is not set and the host config has no provider key to "
                f"inherit from; put the key in {HERE / '.env'} (see .env.example), export "
                f"it, or configure a provider in the host raven"
            )
        log(f"[run] llm: inherited from the host ({taken})")

    partition = partition.resolve()
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    defaults["workspace"] = str(partition)

    plugins = config.setdefault("plugins", {})
    plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("workspaceGate", dict(gate))
    # The launcher that arms the gate also flips it on: rendering an armed
    # workspaceGate into a slice the plugin reads as enabled=False would be
    # these two lines disagreeing, and on a custom --config lacking the
    # code-flow slice it would deploy the very coding-agent-with-no-approval-
    # gate the swap ordering exists to prevent (the fork armed via env,
    # regardless of config file). setdefault, so an operator's explicit false
    # still opts out.
    flow_slice.setdefault("enabled", True)

    partition.mkdir(parents=True, exist_ok=True)
    seed_guide(partition)
    render.sweep_stale_renders(partition)
    return render.write_rendered(config, partition)


def render_acp_config(source: Path) -> Path:
    """The ACP hosting's render: the acp partition, the multiplexed arming.

    The partition is the engine's Agent home and must sit OUTSIDE the host
    Agent home (the host hands its home over as the session cwd, and the
    runtime refuses a cwd that contains the engine's home) -- so it comes
    from the shared placement helper, not from the state root. Everything
    else this product keeps (repos, per-instance buckets) is work and stays
    under CODE_STATE_ROOT; CODE_ACP_HOME overrides the home alone.
    """
    acp_state = render.product_acp_home(PRODUCT, override=env_value("CODE_ACP_HOME")).resolve()
    return render_config(
        source,
        acp_state,
        {
            "allocBase": str(acp_state),
            "reposRoot": str(state_root() / "repos"),
            "stateBucket": "acp",
        },
    )


def serve(args: argparse.Namespace) -> int:
    """Serve installed raven's ``raven acp`` on this process's stdio.

    After rendering, this process execs ``python -m raven acp`` on its own
    interpreter (the roster row's ``{PYTHON}`` resolves at install time to
    one that imports raven), so the server inherits this pid, process group
    and stdio untouched. No finally-unlink around it: the fork's own sweep
    docstring concedes that promise is one a SIGKILLed process group never
    keeps, and the pid-liveness sweep in render_config is the cleaner that
    actually runs.
    """
    rendered = render_acp_config(Path(args.config).resolve())
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


def safe_name(value: str) -> str:
    """Fold a conversation id into one path segment.

    The gateway sends a uuid, but this is a CLI flag anyone can set, and it
    names a directory: ``--session ../../elsewhere`` must not escape the
    state root.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    cleaned = cleaned.strip(".") or "unnamed"
    return cleaned[:120]


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required for Raven-Code workspace reporting")
    return subprocess.run([executable, *args], cwd=str(cwd), capture_output=True, text=True)


def session_file(partition: Path, workspace: Path, conversation: str) -> Path:
    """Where installed raven will keep this conversation's transcript.

    Computed by raven's own SessionManager rather than reproduced here: the
    grouping (the project slug of the launch directory) and the id escaping
    are its functions, and re-implementing them would silently point at the
    wrong file the next time either changes. The turn below runs with
    ``cwd=workspace`` so the subprocess derives the same group this
    computation does. The fork probed its checkout by subprocess for the same
    facts; installed raven can simply be imported.
    """
    from raven.session.manager import SessionManager
    from raven.utils.paths import project_slug

    manager = SessionManager(partition, project_slug=project_slug(workspace), project_dir=workspace)
    return manager.session_path(f"cli:{conversation}")


def count_lines(path: Path) -> int:
    """Lines already in the transcript, so a resumed turn can skip them.

    A resumed turn appends to the file it continues, so without this the
    previous turn's answer is still the last assistant row and a turn that
    produced nothing would be reported as a success carrying stale text.
    """
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as stream:
        return sum(1 for _ in stream)


def extract_answer(path: Path, skip_lines: int = 0) -> str | None:
    """Return the last assistant answer written past ``skip_lines``, or None.

    A row carrying ``tool_calls`` is a step, not an answer, and empty
    assistant rows are dropped before they reach disk, so the last assistant
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


def describe_changes(repo: Path) -> str:
    """Summarise the working-tree footprint of a run, when the workspace is a checkout.

    A name-and-count summary rather than a patch: the agent edits the
    caller's real files, so what the caller needs from the reply is where to
    look. The diff itself is already in their working tree, and ``git diff``
    shows it better than a copy pasted into conversation.
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

    On a resumed turn the history is already in context, so the preamble
    states only what changed. Left out, the agent reads a fresh "you are
    working in" line against files it remembers creating and treats it as a
    contradiction.
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
    # Task first, environment after. The order is not cosmetic: everos
    # extracts a memory from each turn by summarising the message, and with
    # the preamble leading it stored the runtime context instead of the work.
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
    workspace: the gateway starts a cli subagent there, and inheriting it is
    what puts this agent on the caller's files rather than in a private
    directory of its own. Sessions are bucketed by the workspace, so a later
    turn arriving with a different cwd would be a *different* conversation
    with no history - the first turn's path is recorded here and replayed.
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


def run_task(args: argparse.Namespace) -> int:
    """Host one adjudicated CLI turn on installed raven's ``raven agent -m``.

    The turn runs with ``cwd=workspace`` so raven groups the session by the
    project the work happens in, and the transcript this launcher judges is
    the one that subprocess writes. The fork passed two skill flags here
    (--wait-skill-extract / --flush-skill-buffer); installed raven has
    neither and this product ships skillForge off, so nothing is lost by
    their absence (verdict D5).
    """
    if args.prompt_file:
        task = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    elif args.task:
        task = args.task.strip()
    else:
        raise SystemExit("error: pass --task or --prompt-file")
    if not task:
        raise SystemExit("error: the task is empty")

    conversation = args.session or args.job or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    instance = f"instance-{safe_name(conversation)}"
    state_dir = (state_root() / instance).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = state_dir / "launcher.log"

    rendered = render_config(
        Path(args.config).resolve(),
        state_dir,
        {
            "allocDir": str(state_dir),
            "instance": instance,
            "reposRoot": str(state_root() / "repos"),
        },
    )
    workspace = resolve_workspace(state_dir, args.workspace)

    transcript = session_file(state_dir, workspace, conversation)
    pre_lines = count_lines(transcript)
    resuming = pre_lines > 0

    argv = [
        sys.executable,
        "-m",
        "raven",
        "agent",
        "--config",
        str(rendered),
        "--workspace",
        str(workspace),
        # The full `cli:<id>` form on every turn, first included: it is
        # returned unchanged by the resolver, so it creates the session when
        # absent and resumes it when present. Never `--continue` - it picks
        # "the most recent cli session", which is a race between parallel
        # workers sharing a workspace.
        "--session",
        f"cli:{conversation}",
        "--no-markdown",
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
            cwd=str(workspace),
            timeout=args.timeout or None,
            capture_output=True,
            text=True,
        )
        rc: int | None = proc.returncode
        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
    except subprocess.TimeoutExpired:
        # Its own flag rather than `rc is None` read back at each later
        # branch: what the launcher owes the caller after a kill differs from
        # what it owes after an exit, and recovering that distinction from a
        # sentinel exit code is how the two came to be conflated once.
        timed_out = True
        rc, stderr_tail = None, ["timed out"]
    finally:
        rendered.unlink(missing_ok=True)
    elapsed = int(time.time() - started)
    log(f"[run] exit={rc} elapsed={elapsed}s")
    for line in stderr_tail:
        log(f"[run]   {line}")

    if rc == 1:
        # The only documented non-zero exit: config or credential error.
        log("[run] FAILED: config/credential error")
        print(f"FAILED: Raven-Code exited 1 (config or credential error). See {_LOG_FILE}", flush=True)
        return 1

    answer = None
    if transcript.is_file():
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
        # without an answer, and only the caller can decide whether to rerun
        # it with a longer deadline or take the work elsewhere.
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
        # before the kill landed. But the run did not finish, so whatever it
        # would have done next is gone; returning 0 without saying so is how
        # a timed-out run once reported as a clean success.
        out.append(f"\n--- timed out: killed after {args.timeout}s, after this answer was committed")
    if changes:
        out.append(f"\n--- {changes}")
    print("\n".join(out), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Raven-Code: serve ACP on stdio, or host one CLI turn")
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (what the roster row spawns)")
    parser.add_argument("--task", help="the coding task (one-turn CLI hosting)")
    parser.add_argument("--prompt-file", help="file holding the task (alternative to --task)")
    # The gateway substitutes {agent_id} here: a uuid it mints on the first
    # turn of a conversation and replays on every later one.
    parser.add_argument("--session", help="conversation id; turns sharing one continue the same session")
    parser.add_argument("--job", help="state directory name for a run by hand (default: a unique name)")
    parser.add_argument("--workspace", help="work in this directory instead of the inherited cwd")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    # No wall-clock cap by default: a real coding task has no predictable
    # length, and a killed run produces nothing at all. The per-LLM-call and
    # per-command limits stay: those bound a stall, not a long task.
    parser.add_argument("--timeout", type=int, default=0, help="seconds; 0 (default) means no limit")
    parser.add_argument("--keep-going", action="store_true", help="do not fail when no answer was committed")
    parser.add_argument("--verbose", action="store_true", help="mirror diagnostics to stderr; never when spawned")
    args = parser.parse_args()
    if args.acp:
        return serve(args)
    return run_task(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        # A setup error the caller caused (no task, a workspace that
        # contradicts the conversation) has to reach them as the reply.
        # stderr would too - the backend folds it in - but only when it
        # decides stderr is non-empty, so the reliable channel is stdout,
        # same as every other failure here. Numeric exits pass through.
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            sys.exit(1)
        raise
