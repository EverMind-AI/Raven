#!/usr/bin/env python
"""Host-side launcher for the Raven-Design visual agent.

One host invocation is one conversation turn. Reusing the provisioned session
id resumes the same inner Raven session and its stable caller workspace. The
inner checkout owns visual Skill selection, tool use, same-turn compaction, and
artifact creation; this wrapper only renders credentials, starts that runtime,
and returns its committed assistant answer.

stdout carries only the answer. Diagnostics go to the per-conversation
launcher.log, with an optional stderr mirror for manual runs.
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
CHECKOUT = HERE / "Raven-Design"
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
    "DESIGN_API_KEY": ("providers", "custom", "apiKey"),
    "DESIGN_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "DESIGN_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}
REQUIRED_SECRETS = ("DESIGN_API_KEY",)

# Everything the runtime persists - transcripts above all - lands here rather
# than in this folder. See `render_config` for why writing the config here is
# what moves them.
STATE_ROOT = Path(
    env_value("DESIGN_STATE_ROOT") or Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-design"
)

# Our own per-conversation state: the workspace pointer and the launcher log.
# Distinct from the runtime's own data directory, and kept under the same root
# so the project folder holds only the files that ship; DESIGN_RUN_ROOT moves it.
RUN_ROOT = Path(env_value("DESIGN_RUN_ROOT") or STATE_ROOT / "runs")
_EXIT_TIMEOUT = 124


# The host raven's config file, read for the fallbacks below. Read as JSON, never
# imported from raven: this launcher is standard-library only and has to run
# under a bare python3 that may not have the runtime installed at all.
HOST_CONFIG = Path(os.environ.get("RAVEN_HOME", "").strip() or Path.home() / ".raven") / "config.json"


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


def put(data: dict, path: tuple, value: object) -> None:
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
    for key in ("provider", "model", "reasoningEffort"):
        if key in host_defaults:
            defaults[key] = host_defaults[key]
    if parent_model := os.environ.get("RAVEN_PARENT_MODEL", "").strip():
        defaults["model"] = parent_model
        defaults["provider"] = ""
    if parent_effort := os.environ.get("RAVEN_PARENT_REASONING_EFFORT", "").strip():
        defaults["reasoningEffort"] = parent_effort
    if not defaults.get("provider"):
        model = defaults.get("model") or ""
        for name, block in providers.items():
            if isinstance(block, dict) and model in (block.get("models") or []):
                defaults["provider"] = name
                break
    return (
        f"provider={defaults.get('provider')} model={defaults.get('model')} "
        f"reasoning_effort={defaults.get('reasoningEffort')}"
    )


def configure_image_generation(config: dict, host: dict) -> None:
    """Enable image generation only when a compatible credential exists."""
    own_image_key = env_value("DESIGN_IMAGE_API_KEY")
    own_llm_key = env_value("DESIGN_API_KEY")
    host_media = ((host.get("tools") or {}).get("media") or {}).get("image") or {}
    host_media_base = str(host_media.get("apiBase") or host_media.get("api_base") or "")
    host_media_key = str(host_media.get("apiKey") or host_media.get("api_key") or "")
    host_media_style = str(host_media.get("apiStyle") or host_media.get("api_style") or "")
    host_openrouter = (host.get("providers") or {}).get("openrouter") or {}
    host_openrouter_key = str(host_openrouter.get("apiKey") or host_openrouter.get("api_key") or "")

    image_key = own_image_key
    borrowed_host_media = False
    compatible_host_media = host_media_style in {"chat_modalities", "openrouter_images", "images"} or (
        not host_media_style and (not host_media_base or "openrouter.ai" in host_media_base)
    )
    if not image_key and host_media_key and compatible_host_media:
        image_key = host_media_key
        borrowed_host_media = True
    if not image_key:
        image_key = host_openrouter_key
    if not image_key and own_llm_key:
        configured_base = dig(config, ("providers", "custom", "apiBase"))
        if "openrouter.ai" in configured_base:
            image_key = own_llm_key

    if image_key:
        put(config, ("tools", "media", "image", "apiKey"), image_key)
        if borrowed_host_media:
            if host_media_base:
                put(config, ("tools", "media", "image", "apiBase"), host_media_base)
            if host_media_style:
                put(config, ("tools", "media", "image", "apiStyle"), host_media_style)
            if host_media.get("model"):
                put(config, ("tools", "media", "image", "model"), str(host_media["model"]))
            if host_media.get("bypassProxy") is not None:
                put(config, ("tools", "media", "image", "bypassProxy"), bool(host_media["bypassProxy"]))
            if host_media.get("allowModelOverride") is not None:
                put(
                    config,
                    ("tools", "media", "image", "allowModelOverride"),
                    bool(host_media["allowModelOverride"]),
                )
        return

    put(config, ("tools", "media", "image", "apiKey"), "")
    put(config, ("tools", "media", "image", "model"), "")


def render_config(source: Path) -> Path:
    """Write a copy of `source` with the `.env` secrets merged in, under STATE_ROOT.

    The location is the whole mechanism, not a detail. The runtime derives its
    data directory from the config file's own parent and offers no separate knob
    for the session directory, so wherever this file goes, `sessions/` goes with
    it - which is also exactly what `session_file` resolves against, so the two
    stay consistent by construction. Writing it under STATE_ROOT is the only way
    to keep conversation transcripts out of the project directory without
    patching the checkout, and the checkout is replaced wholesale on every
    upstream zip, so a patch would not survive.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = host_config()
    configure_image_generation(config, host)

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

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    # The normal path deletes this in a `finally`; only a SIGKILL strands one, so
    # sweep anything far older than a run could still be using.
    for stale in STATE_ROOT.glob(".config.rendered.*.json"):
        if time.time() - stale.stat().st_mtime > 86400:
            stale.unlink(missing_ok=True)

    rendered = STATE_ROOT / f".config.rendered.{os.getpid()}.json"
    # Create it unreadable to anyone else before a single secret byte is in it.
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, ensure_ascii=False)
    return rendered


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
        return subprocess.CompletedProcess(["git", *args], 127, "", "git not found")
    return subprocess.run([executable, *args], cwd=str(cwd), capture_output=True, text=True)


def session_file(python: Path, workspace: Path, chat_id: str) -> Path | None:
    """Ask the checkout where it will keep this session's transcript.

    Computed by the build rather than reproduced here so its own session layout
    and filename escaping remain authoritative.
    """
    code = (
        "import sys; from pathlib import Path;"
        "from raven.session.manager import SessionManager;"
        "print(SessionManager(Path(sys.argv[1]))._get_session_path(sys.argv[2]))"
    )
    proc = subprocess.run(
        [str(python), "-c", code, str(workspace), f"cli:{chat_id}"],
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
    # leading it stored "raven-design received a runtime context in an empty scratch
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Raven-Design visual design agent on one task")
    ap.add_argument("--task", help="The visual design task")
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
    # No wall-clock cap by default: a real visual design task has no predictable length,
    # and a killed run produces nothing at all - no answer, and no memory, since
    # extraction is what the drain below waits for. Pass a positive value to cap
    # it. The per-LLM-call (`agents.defaults.llmCallTimeout`) and per-command
    # (`tools.exec.maxTimeout`) limits stay: those bound a *stalled* backend or
    # command, which is a hang rather than a long task.
    ap.add_argument("--timeout", type=int, default=0, help="Seconds; 0 (default) means no limit")
    ap.add_argument("--checkout", default=str(CHECKOUT))
    ap.add_argument("--keep-going", action="store_true", help="Do not fail when no answer was committed")
    ap.add_argument("--verbose", action="store_true", help="Mirror diagnostics to stderr; never when spawned")
    args = ap.parse_args()

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
    state_dir = RUN_ROOT / "sessions" / safe_name(conversation)
    state_dir.mkdir(parents=True, exist_ok=True)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = state_dir / "launcher.log"

    config = render_config(Path(args.config).resolve())
    workspace = resolve_workspace(state_dir, args.workspace)
    runtime_workspace = state_dir / "runtime-workspace"
    runtime_workspace.mkdir(parents=True, exist_ok=True)

    transcript = session_file(root / ".venv" / "bin" / "python", runtime_workspace, conversation)
    pre_lines = count_lines(transcript) if transcript is not None else 0
    resuming = pre_lines > 0

    argv = [
        str(raven_bin),
        "agent",
        "--config",
        str(config),
        "--workspace",
        str(runtime_workspace),
        # The full `cli:<id>` form on every turn, first included: it is returned
        # unchanged by the resolver, so it creates the session when absent and
        # resumes it when present. Never `--continue` - it picks "the most recent
        # cli session" in the bucket, which is a race between parallel workers
        # sharing a workspace.
        "--session",
        f"cli:{conversation}",
        "--no-markdown",
        "-m",
        build_task(task, workspace, resuming=resuming),
    ]
    log(
        f"[run] workspace={workspace} runtime_workspace={runtime_workspace} cwd={Path.cwd()} "
        f"turn={'resume' if resuming else 'first'} "
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
        print(f"FAILED: Raven-Design exited 1 (config or credential error). See {_LOG_FILE}", flush=True)
        return 1

    answer = None
    if transcript is not None and transcript.is_file():
        answer = extract_answer(transcript, pre_lines)
    elif timed_out:
        log("[run] no transcript: killed before the agent wrote one")
    else:
        log("[run] no transcript found")
    committed = answer is not None
    log(f"[run] answer_chars={len(answer or '')}")

    changes = describe_changes(workspace) if (workspace / ".git").exists() else ""
    if changes:
        log(f"[run] {changes.splitlines()[0]}")

    if answer is None and timed_out:
        log(f"[run] TIMEOUT: killed after {args.timeout}s with nothing committed")
        if not args.keep_going:
            print(
                f"TIMEOUT: Raven-Design was killed after {args.timeout}s before it committed an answer. "
                f"See {_LOG_FILE}",
                flush=True,
            )
            return _EXIT_TIMEOUT
        answer = f"(killed after {args.timeout}s with nothing committed)"
    elif answer is None:
        log("[run] FAILED: no answer was committed")
        if not args.keep_going:
            print(f"FAILED: Raven-Design produced no answer (no answer was committed). See {_LOG_FILE}", flush=True)
            return 1
        answer = "(no answer committed)"

    out = [answer]
    if timed_out and committed:
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
