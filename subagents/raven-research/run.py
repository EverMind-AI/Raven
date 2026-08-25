#!/usr/bin/env python
"""Host-side launcher for the Raven-X deep-research agent.

One call is one turn. Two properties of the contract make a bare CLI
registration useless, and this wrapper exists to absorb both:

- **stdout must not be parsed.** It is rendered for a human (spinners, progress
  lines, tool hints), and `--no-markdown` only turns off the renderer, not the
  prose. The answer is read out of the turn's session JSONL instead.
- **exit 0 does not mean an answer was produced.** Only a config or credential
  error maps to a non-zero code; an answerless run still exits 0. Success is
  therefore decided by finding a committed answer, not by the exit status. That
  bucket gained a member upstream - a memory backend with
  `require_service=true` that finds no usable service also exits 1 - which is
  unreachable on this config, where the knob is unset and a missing everos
  degrades to a warning.

The same `--session <id>` across calls is one conversation. This build keeps a
session under the *workspace* it ran in (`<workspace>/sessions/cli/<id>.jsonl`),
so the workspace has to be a function of the id and nothing else: a per-process
workspace would file every turn under a fresh session and lose the history that
`drFlow.conversation` exists to use. Distinct ids therefore still get distinct
workspaces, which is what keeps concurrent conversations from interleaving.

stdout carries the answer and nothing else; raven's CLI backend takes stdout as
the reply. stderr is the progress lane: the backend streams it to the live
console beside the run and keeps it with the attempt record, without folding it
into the reply, so the child's output is mirrored there as it happens.
Diagnostics also land in `launcher.log` inside the run workspace; `--verbose`
mirrors this launcher's own lines to stderr too, for a human running it by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
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


def manifest_output_cap() -> int | None:
    """This agent's reply cap from subagent.json, or None when unreadable.

    Raven's CLI backend reads the whole of a child's stdout as the reply and
    tail-truncates it to this many chars (cli_agent.py). None means the
    manifest cannot be read and no reservation is possible.
    """
    try:
        return int(json.loads((HERE / "subagent.json").read_text(encoding="utf-8"))["maxOutputChars"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


_TRAIL_TRUNC_NOTE = "\n\n[report truncated to preserve the research trail]"
_TRAIL_DROP_NOTE = "\n\n[research trail dropped: exceeds the host's output cap]"


def compose_reply(answer: str, trail: str, cap: int | None) -> tuple[str, bool]:
    """Fit the answer and the trail under the host's output cap.

    Raven's CLI backend reads the whole of this stdout as the subagent reply
    and tail-truncates it to ``maxOutputChars``, and the trail is appended
    after the answer, so at the cap the host would cut the record clean away.
    Reserve room for it the way cli_agent.py reserves for its warning:
    shorten the answer (and say so) so the trail survives; when even the trail
    cannot fit, keep the answer and say it was dropped.

    The blank line before the trail is deliberate: the rendered trail opens
    with "\\n---\\n\\n", and markdown reads a `---` directly under text as a
    setext H2 rather than a rule.
    """
    body = answer.strip()
    if not trail:
        return body, False
    trail = trail.strip()
    if not trail:
        return body, False
    if cap is None:
        return f"{body}\n\n{trail}", True
    # The unmodified pair decides first: a report that fits the cap unchanged
    # is left alone, however the notice budget would have cut it.
    if len(body) + 2 + len(trail) <= cap:
        return f"{body}\n\n{trail}", True
    room = cap - 2 - len(trail) - len(_TRAIL_TRUNC_NOTE)
    if room > 0:
        return f"{body[:room].rstrip()}{_TRAIL_TRUNC_NOTE}\n\n{trail}", True
    drop = _TRAIL_DROP_NOTE
    return f"{body[: max(0, cap - len(drop))].rstrip()}{drop}", False


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


def _preview(text: str, limit: int = 110) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def tool_lines(entry: dict) -> list[str]:
    """Render one transcript entry as compact tool-activity lines, or nothing.

    Only the work is mirrored -- calls going out and results coming back. The
    narration around them already reaches stderr through the child's own
    stdout render; repeating it here would print every sentence twice.
    """
    if entry.get("role") == "assistant":
        lines = []
        for call in entry.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            if name := fn.get("name"):
                lines.append(f"→ {name}({_preview(fn.get('arguments') or '')})")
        return lines
    if entry.get("role") == "tool":
        content = str(entry.get("content") or "")
        # The untrusted fence is for the model reading the result, not for a
        # human glancing at a progress line.
        if content.startswith("[BEGIN UNTRUSTED") and "\n" in content:
            content = content.split("\n", 1)[1]
        return [f"← {entry.get('name') or 'tool'}: {_preview(content)}"]
    return []


def follow_partial(final: Path, stop: "threading.Event") -> None:
    """Mirror the turn's tool activity to stderr while the child runs.

    The child narrates on stdout (mirrored already) but draws tool calls as
    transient console lines that never survive a pipe, so the committed
    transcript is the only reliable account of them. This tails the turn's
    `.partial.jsonl` -- the file the writer appends this turn's events to --
    and stays quiet if it never appears; a writer that appends somewhere else
    loses nothing but this mirror. Whole lines only: the last line of an
    appended file may be mid-write, and parses on a later pass.
    """
    partial = final.with_name(final.stem + ".partial.jsonl")
    seen = 0
    while True:
        finished = stop.wait(0.5)
        try:
            text = partial.read_text(encoding="utf-8")
        except OSError:
            text = ""
        lines = text[: text.rfind("\n") + 1].splitlines()
        for raw in lines[seen:]:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for line in tool_lines(entry):
                print(line, file=sys.stderr, flush=True)
        seen = max(seen, len(lines))
        if finished:
            return


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

# A clarify handoff is the third shape of committed reply, and it arrives by the same
# route as the wrap-ups: `AskUserGate` short-circuits `before_execute_tools`, and the
# loop commits the questions through `add_assistant_message` with no `finish_reason`.
# So the two tests above discard it, and turn one of an `askUser` profile reports
# answerless while holding a perfectly good reply - the questions the user has to
# answer before research can start.
#
# Recognised through `observers`, not a message flag, because the signal already lives
# there: the loop stamps the turn's observer payload onto its last non-empty assistant
# message, which on a clarify turn IS the handoff.
#
# Read from `turn_end`, not from `ask_user.asked`, because a clarify arrives by TWO
# routes and only one of them is the tool. When the model writes its questions as
# ordinary prose instead of calling `ask_user`, `ClarifyExemptHook` stamps the same
# commit marker but leaves `asked` false - and that reply carries a normal
# `finish_reason == "stop"`, so keying on `asked` would file a page of questions as a
# finished research report. `turn_end.awaiting_user` is set on both routes and is the
# key the flow stamps for exactly this reader ("so a reader of `turn_end` alone can
# tell the two apart"); `awaiting_user_source` says which route it was.
_CLARIFY_OBSERVER = ("turn_end", "awaiting_user")
_CLARIFY_SOURCE = ("turn_end", "awaiting_user_source")


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
        # Clarify is tested FIRST: the prose route carries an ordinary
        # `finish_reason == "stop"`, so the "completed" branch would swallow it and
        # the questions would ship as a finished report.
        if (obs := (row.get("observers") or {})).get(_CLARIFY_OBSERVER[0], {}).get(
            _CLARIFY_OBSERVER[1]
        ):
            route = obs.get(_CLARIFY_SOURCE[0], {}).get(_CLARIFY_SOURCE[1]) or "unknown"
            answer, source = content, f"clarify_requested:{route}"
        elif row.get("finish_reason") == "stop":
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
        # Inert upstream since the everos v2 write path landed, and kept only
        # because dropping a flag from a call site is not free: the two builds
        # either side of a swap have to accept the same argv. Blocking on the
        # detached writes is now unconditional (`drain_backend_stores`).
        "--wait-skill-extract",
        # Live again, and the reason it is still passed: a lone `-m` turn does
        # not trip a boundary on its own, and a one-question spawn is exactly
        # that, so without a promotion nothing this run captured is ever
        # derived from. Two costs, both accepted deliberately. It blocks on one
        # request bounded by the adapter's `flush_timeout_s`, inside this
        # launcher's own `--timeout`; and on turn two of a conversation it
        # promotes a trajectory that is not finished, because the turns share
        # one backend-side session and this launcher has no "last turn" signal
        # to hold it back for - the gateway spawns one process per turn and
        # never says which is the last. Upstream's advice (promote on the last
        # `-m` only) therefore has no addressee here; the alternative is not
        # "promote later", it is "never".
        "--flush-skill-buffer",
        "-m", question,
    ]
    log(
        f"[run] workspace={workspace} turn={'resume' if pre_lines else 'first'} "
        f"timeout={args.timeout}s"
    )

    started = time.time()
    # The child's output is mirrored to stderr line by line as it happens:
    # raven's cli backend treats stderr as a log lane (streamed to the live
    # console beside the run, kept with the attempt record) and no longer folds
    # it into the reply, so a watcher sees the research work instead of a
    # silence that ends in a report. The reply contract is unchanged: stdout
    # still carries the answer, verbatim and alone. PYTHONUNBUFFERED because a
    # piped child block-buffers its stdout otherwise, and a mirror that lands
    # in 8 KiB bursts is not a live view; COLUMNS/TERM stop the child's console
    # from wrapping long paths mid-line.
    child_env = {
        **os.environ,
        "RAVEN_CLI_DEBUG": os.environ.get("RAVEN_CLI_DEBUG", ""),
        "PYTHONUNBUFFERED": "1",
        "COLUMNS": "400",
        "TERM": "dumb",
    }
    tail: deque[str] = deque(maxlen=5)
    timed_out = False
    try:
        proc = subprocess.Popen(
            argv, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", env=child_env,
        )
    except OSError as exc:
        rendered.unlink(missing_ok=True)
        log(f"[run] FAILED: could not start Raven-X ({exc})")
        print(f"FAILED: could not start Raven-X ({exc}). See {_LOG_FILE}", flush=True)
        return 1

    # A watchdog rather than a deadline checked inside the read loop: that loop
    # blocks on the pipe, so a check inside it only runs when the child happens
    # to say something, and a silent hang would never be killed.
    def _expire() -> None:
        nonlocal timed_out
        timed_out = True
        log(f"[run] timeout after {args.timeout}s, killing the agent")
        proc.kill()

    watchdog = threading.Timer(args.timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    follow_stop = threading.Event()
    follower = threading.Thread(target=follow_partial, args=(transcript, follow_stop), daemon=True)
    follower.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            log(line)
            if not _VERBOSE:
                print(line, file=sys.stderr, flush=True)
        rc: int | None = proc.wait()
    finally:
        watchdog.cancel()
        follow_stop.set()
        follower.join(timeout=2)
        rendered.unlink(missing_ok=True)
    if timed_out:
        rc, stderr_tail = None, ["timed out"]
    else:
        stderr_tail = list(tail)
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
    # Taken out before the observers line is logged: this is prose measured in
    # kilobytes and the rest of that payload is counters, so leaving it in makes the
    # one line a reader actually greps unreadable.
    trail = observers.pop("research_trail", "") if isinstance(observers, dict) else ""
    log(f"[run] session_log={transcript} exit={rc} elapsed={elapsed}s")
    if observers:
        log(f"[run] observers={json.dumps(observers, ensure_ascii=False)}")
    if source in _WRAPUP_FLAGS:
        # Worth its own line: a wrap-up is told to commit to the conclusion the turn had
        # already reached and not to reason further, so the answer is real but thinner
        # than a completed one.
        log(f"[run] answer came from the loop's wrap-up ({source}), not a normal completion")
    elif source and source.startswith("clarify_requested"):
        # Louder than the wrap-up note, because this reply is not an answer at all: it
        # is the questions, and research has not started. A caller that files it as a
        # report files the questions as findings. Resuming the same `--session` with the
        # user's reply is what turns it into one.
        log("[run] reply is a clarify handoff, NOT a research answer - resume this "
            "session with the user's answers to continue")

    if answer is None:
        log("[run] FAILED: the run committed no answer (exit 0 does not imply one)")
        print(f"FAILED: the research run committed no answer. See {_LOG_FILE}", flush=True)
        return 0 if args.keep_going else 1

    log(f"[run] answer_chars={len(answer)}")
    # The report template tells the model NOT to close with a list of sources,
    # promising that "the reply is followed by the full record of what was searched
    # and opened". On this path that record never arrived: the flow appends it to the
    # value its CLI returns, and this launcher reads the persisted message instead
    # (the CLI's stdout is rendered for a human and cannot be parsed). So the model
    # was made to omit its sources in exchange for a substitute the caller never got
    # - measured at 17 pages read and 8 cited on one live turn, with the other 9
    # recorded nowhere.
    #
    # Appended here, to the one string the host uses for BOTH the recorded `out.md`
    # and the reply it shows, so the record and the reader see the same sources. It
    # is derived, not generated: no tokens were spent on it and there is nothing in
    # it to invent.
    body, trail_kept = compose_reply(answer, trail, manifest_output_cap())
    if trail:
        log(f"[run] research trail {'appended' if trail_kept else 'dropped'} "
            f"({len(trail.strip())} chars)")
    # stdout is the answer plus that record, and nothing else.
    print(body, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
