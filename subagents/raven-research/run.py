#!/usr/bin/env python
"""Host-side launcher for the Raven-X deep-research agent.

One call is one turn. Two properties of the contract make a bare CLI
registration useless, and this wrapper exists to absorb both:

- **stdout must not be parsed.** It is rendered for a human (spinners, progress
  lines, tool hints), and `--no-markdown` only turns off the renderer, not the
  prose. The answer is read out of the turn's session JSONL instead.
- **exit 0 does not mean an answer was produced.** Only a config or credential
  error maps to a non-zero code; an answerless run still exits 0. Success is
  therefore decided by finding a committed answer, not by the exit status.

The same `--session <id>` across calls is one conversation. This build keeps a
session under the *workspace* it ran in (`<workspace>/sessions/cli/<id>.jsonl`),
so the workspace has to be a function of the id and nothing else: a per-process
workspace would file every turn under a fresh session and lose the history that
`drFlow.conversation` exists to use. Distinct ids therefore still get distinct
workspaces, which is what keeps concurrent conversations from interleaving.

stdout carries the answer and nothing else. Raven's CLI backend uses the whole
of a child's output as the subagent's reply (stdout plus stderr when stderr is
non-empty), so any progress or diagnostic line printed here would be pasted into
the conversation as if the agent had said it. Diagnostics go to `launcher.log`
inside the run workspace instead; `--verbose` additionally mirrors them to
stderr for a human running this by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The checkout lives beside this launcher, per the subagents/ convention.
RAVEN_X = HERE / "Raven-X"
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


# Where each secret belongs in the config Raven-X loads. The keys stay out of
# `config.json` because that file is published; they are merged back in at
# launch (see `render_config`). Raven-X reads none of them from the
# environment - its config loader does no variable substitution - so a rendered
# file is the only way to get them in.
SECRET_SLOTS = {
    "RESEARCH_API_KEY": ("providers", "custom", "apiKey"),
    "RESEARCH_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "RESEARCH_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}
# The LLM key is the only one whose absence is fatal: Serper failing degrades a
# run to no search, and Jina is optional by design (unauthenticated r.jina.ai
# works at a lower rate limit, and a dead key is worse than none - it 402s).
REQUIRED_SECRETS = ("RESEARCH_API_KEY",)

# Everything the runtime persists - transcripts above all - lands here rather
# than in this folder. See `render_config` for why writing the config here is
# what moves them.
STATE_ROOT = Path(
    env_value("RESEARCH_STATE_ROOT")
    or Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-research"
)

# Per-conversation workspaces. Under the same root, so the project folder holds
# only the files that ship; RESEARCH_RUN_ROOT moves them.
RUN_ROOT = Path(env_value("RESEARCH_RUN_ROOT") or STATE_ROOT / "runs")


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


def render_config(source: Path) -> Path:
    """Write a copy of `source` with the `.env` secrets merged in, under STATE_ROOT.

    The location is the whole mechanism, not a detail. Raven-X derives
    `get_data_dir()` from the config file's own parent and offers no separate
    knob for the session directory, so wherever this file goes, `sessions/`,
    `cache/`, `cron/` and `ledger/` go with it. Writing it under STATE_ROOT is
    therefore the only way to keep conversation transcripts out of the project
    directory without patching the checkout - and the checkout is replaced
    wholesale on every upstream zip, so a patch would not survive.

    The sibling runtime directories move too. They cannot be split from the
    transcripts, because all four hang off that one parent.
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
    a directory: `--session ../../elsewhere` must not escape the run root. The
    surviving character set is also a subset of what the build's own
    `safe_filename` leaves alone, which is what lets `session_log` name the
    transcript instead of hunting for it.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    cleaned = cleaned.strip(".") or "unnamed"
    return cleaned[:120]


def session_log(workspace: Path, chat_id: str) -> Path:
    """Where this conversation's transcript is, named rather than searched.

    `SessionManager` files a session at `<workspace>/sessions/<channel>/<chat
    id>.jsonl`. Picking the newest file in that directory instead would resume
    reading the wrong conversation the moment a workspace ever held two, and the
    stale answer it returned would be indistinguishable from a fresh one.
    """
    return workspace / "sessions" / "cli" / f"{chat_id}.jsonl"


def count_lines(path: Path) -> int:
    """Lines already in the transcript, so a resumed turn can skip them.

    A later turn appends to the file it continues, so without this the previous
    turn's answer is still the last committed one and a turn that produced
    nothing would be reported as a success carrying stale text.
    """
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as stream:
        return sum(1 for _ in stream)


# A committed answer is either the turn's normal final message, or a wrap-up the loop
# writes when a budget cut the answer off: the tool-iteration budget, or - since
# dr@2.9 - the completion budget, which strands a turn mid-sentence on turn one with no
# tool call ever made. Both wrap-ups are persisted through `add_assistant_message`
# without a `finish_reason`, so testing for "stop" alone silently discards them and
# reports the run as answerless.
#
# Recognise them by the flag the loop stamps on the message instead, which it sets only
# when the wrap-up produced real text rather than the static apology. That flag test is
# also what keeps this from over-accepting, and it has to: two other kinds of assistant
# row carry no `finish_reason` either and must stay rejected. Both were seen here - a
# draft the verify reviewer rejected before a revision replaced it, and an answerless
# turn whose entire 27k-character reply sat inside an unclosed think block.
_WRAPUP_FLAGS = ("synthesized_on_truncation", "synthesized_on_exhaustion")


def extract_answer(log: Path, skip_lines: int = 0) -> tuple[str | None, str | None, dict]:
    """Return the committed answer, how it was produced, and the flow's account of the turn.

    The answer is the last assistant row past `skip_lines` that either stopped
    normally or carries a wrap-up flag. `observers` carries the flow's self-report;
    `invariants.ok == false` in it is a counter, not a verdict, so it is reported
    rather than treated as a failure. It is collected independently of the answer,
    because an answerless run is exactly when its counters are worth reading.
    """
    answer: str | None = None
    source: str | None = None
    observers: dict = {}
    version = ""
    for index, line in enumerate(log.read_text(encoding="utf-8", errors="replace").splitlines()):
        if index < skip_lines:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("_type") == "metadata":
            continue
        version = row.get("flow_version") or version
        if row.get("observers"):
            observers = row["observers"]
        if row.get("role") != "assistant":
            continue
        content = row.get("content")
        if not (isinstance(content, str) and content.strip()):
            continue
        if row.get("finish_reason") == "stop":
            answer, source = content, "completed"
        elif flag := next((f for f in _WRAPUP_FLAGS if row.get(f)), None):
            answer, source = content, flag
    if version:
        observers = {**observers, "flow_version": version}
    return answer, source, observers


def main() -> int:
    ap = argparse.ArgumentParser(description="Answer a research question with Raven-X.")
    ap.add_argument("--question")
    ap.add_argument("--prompt-file", help="File holding the question (alternative to --question)")
    # The gateway substitutes {agent_id} here: a uuid it mints on the first turn
    # of a conversation and replays on every later one.
    ap.add_argument("--session", help="Conversation id; turns sharing one continue the same conversation")
    ap.add_argument("--job", help="Conversation name for a run by hand (default: a unique generated name)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--raven-x", default=str(RAVEN_X))
    ap.add_argument("--keep-going", action="store_true", help="Do not fail when no answer was committed")
    ap.add_argument("--verbose", action="store_true", help="Mirror diagnostics to stderr; never when spawned")
    args = ap.parse_args()

    if args.prompt_file:
        question = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    elif args.question:
        question = args.question.strip()
    else:
        raise SystemExit("error: pass --question or --prompt-file")
    if not question:
        raise SystemExit("error: the question is empty")

    # Resolved: the interpreter path is handed to a subprocess whose cwd is the
    # checkout, so a relative --raven-x would not survive the chdir - it would
    # resolve against the new cwd and fail as a bare FileNotFoundError, past the
    # venv check below that exists to report exactly that.
    root = Path(args.raven_x).expanduser().resolve()
    # The console script, not `python -m raven.cli`: raven.cli is a package with
    # no __main__, so the module form cannot be executed.
    raven_bin = root / ".venv" / "bin" / "raven"
    if not raven_bin.is_file():
        raise SystemExit(f"error: Raven-X venv missing at {raven_bin}; run `uv sync` in {root}")

    conversation = safe_name(
        args.session or args.job or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    )
    workspace = RUN_ROOT / conversation
    workspace.mkdir(parents=True, exist_ok=True)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = workspace / "launcher.log"

    transcript = session_log(workspace, conversation)
    pre_lines = count_lines(transcript)

    rendered = render_config(Path(args.config).resolve())
    argv = [
        str(raven_bin),
        "agent", "--config", str(rendered),
        "--workspace", str(workspace),
        # The full `cli:<id>` form on every turn, first included: a value carrying
        # a colon is returned by the resolver unchanged, so it creates the session
        # when absent and resumes it when present. Never `--continue` - that picks
        # "the most recent cli session" in the workspace, which is a guess where
        # this is a fact.
        "--session", f"cli:{conversation}",
        "--no-markdown",
        # Without this the process exits as soon as the answer is ready and
        # interpreter shutdown cancels the in-flight everos extraction, so
        # nothing reaches long-term memory.
        "--wait-skill-extract",
        # And without the flush, extraction never runs at all: a lone `-m` turn
        # does not trip a boundary on its own, and a one-question spawn is
        # exactly that. The cost is one extraction pass per turn rather than one
        # per detected boundary. Conversation continuity is unaffected - it
        # lives in the transcript, not in everos's buffer.
        "--flush-skill-buffer",
        "-m", question,
    ]
    log(
        f"[run] workspace={workspace} turn={'resume' if pre_lines else 'first'} "
        f"timeout={args.timeout}s"
    )

    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=str(root), timeout=args.timeout,
            capture_output=True, text=True,
            env={**os.environ, "RAVEN_CLI_DEBUG": os.environ.get("RAVEN_CLI_DEBUG", "")},
        )
        rc: int | None = proc.returncode
        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
    except subprocess.TimeoutExpired:
        rc, stderr_tail = None, ["timed out"]
    finally:
        rendered.unlink(missing_ok=True)
    elapsed = int(time.time() - started)

    if rc == 1:
        # The only documented non-zero exit: config or credential error.
        log(f"[run] FAILED: config/credential error (exit 1) after {elapsed}s")
        for line in stderr_tail:
            log(f"[run]   {line}")
        # A failure reason belongs in the reply: the caller has to know why.
        print(f"FAILED: Raven-X exited 1 (config or credential error). See {_LOG_FILE}", flush=True)
        return 1

    if not transcript.is_file():
        siblings = sorted(p.name for p in transcript.parent.glob("*.jsonl"))
        log(f"[run] FAILED: no transcript at {transcript} (exit={rc}); siblings={siblings}")
        print(f"FAILED: the run produced no session log (exit={rc}). See {_LOG_FILE}", flush=True)
        return 1

    answer, source, observers = extract_answer(transcript, pre_lines)
    log(f"[run] session_log={transcript} exit={rc} elapsed={elapsed}s")
    if observers:
        log(f"[run] observers={json.dumps(observers, ensure_ascii=False)}")
    if source in _WRAPUP_FLAGS:
        # Worth its own line: a wrap-up is told to commit to the conclusion the turn had
        # already reached and not to reason further, so the answer is real but thinner
        # than a completed one.
        log(f"[run] answer came from the loop's wrap-up ({source}), not a normal completion")

    if answer is None:
        log("[run] FAILED: the run committed no answer (exit 0 does not imply one)")
        print(f"FAILED: the research run committed no answer. See {_LOG_FILE}", flush=True)
        return 0 if args.keep_going else 1

    log(f"[run] answer_chars={len(answer)}")
    # stdout is the answer, verbatim and alone.
    print(answer.strip(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
