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
import hashlib
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
# Effort overlays over the baseline config. `config.json` is the complete
# default (high) profile; each overlay carries the one knob its tier moves, so
# the provider wiring and the tool surface exist in exactly one place.
MODES_DIR = HERE / "modes"

# What a client's mode picker shows, and what the dispatching model reads when
# it chooses a tier for a task. The copy lives here rather than in the overlay
# files because it is product text about the choice, not config the agent reads.
MODE_LABELS = {
    "low": (
        "Low",
        "Light reasoning. For a small, well-specified change, a question about the code, "
        "or an explanation; the cheapest and fastest tier.",
    ),
    "high": (
        "High",
        "Standard reasoning, the default. Right for an ordinary fix, feature or failing test "
        "unless the request says otherwise.",
    ),
    "max": (
        "Max",
        "Deepest reasoning. For a hard bug, a cross-file refactor, or when the user has "
        "explicitly asked for the most thorough attempt; the slowest and most expensive tier.",
    ),
}
# The baseline IS the high profile, so it needs no overlay file; the others do.
BASELINE_MODE = "high"
# The one knob a tier moves. An overlay naming anything else would be declared
# and then ignored by the agent's mode profile, which is exactly the silent
# no-op the strict check here exists to refuse.
OVERLAY_KEYS = frozenset({"agents"})
EFFORT_PATH = ("agents", "defaults", "reasoningEffort")


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


def acp_home() -> Path:
    """Return the ACP engine's own Agent home -- never inside the host's.

    The host hands a session's working directory to whatever it dispatches to,
    and on a surface with no checkout of its own -- a web page -- that
    directory is the host's Agent home. A raven engine refuses to work in a
    directory that CONTAINS its own home, because the per-turn checkpoint runs
    `add -A` over the working directory and would commit its config and its
    provider tokens into a shadow repository.

    Homing this engine under the host's Agent home therefore made every
    dispatch to it fail before it started -- `cwd is not usable: working
    directory must not contain the agent home directory` -- while `Connect`
    still passed, because capability probing opens its session on a temporary
    directory instead. So the engine is homed under the raven data directory,
    which is never handed out as a working directory.

    Checked against the CONFIGURED host Agent home, not against the default one.
    `host_agent_home()` honours `agents.defaults.workspace`, so an operator who
    points it at `$RAVEN_HOME` -- or at any ancestor of it -- puts the raven data
    directory back inside the very tree being handed over, and the refusal
    returns. When the default lands inside it, the engine is homed beside the
    host's Agent home instead, which cannot be inside it whatever it is set to.

    Being outside the host Agent home is not on its own enough: the fallback
    lands beside whatever that home is, and beside a home like `~` that is
    `/Users`, which no run may write to. So a placement is refused when it is
    unusable as well as when it is inside -- otherwise the first `mkdir` raises a
    bare `PermissionError` naming nothing the operator can act on, in place of
    the `CODE_ACP_HOME` guidance the refusal beside it already gives.

    ``CODE_ACP_HOME`` overrides, for an operator who wants it elsewhere; it is
    their business then whether the host can reach it. ``CODE_STATE_ROOT``
    still governs everything else the product keeps -- its repos, its
    per-instance buckets -- which is work, and belongs where the work is.
    """
    raw = env_value("CODE_ACP_HOME")
    if raw:
        return Path(raw).expanduser()
    host = host_agent_home()
    candidate = _RAVEN_HOME / "subagent_sessions" / "raven-code" / "acp"
    if not outside(candidate, host):
        candidate = host.parent / f".raven-code-{instance_tag()}" / "acp"
    if not outside(candidate, host):
        raise SystemExit(
            "error: cannot place the Raven-Code ACP home outside the host Agent home "
            f"({host}); set CODE_ACP_HOME to a directory outside it"
        )
    if not creatable(candidate):
        raise SystemExit(
            f"error: the Raven-Code ACP home {candidate} cannot be created -- its nearest "
            "existing parent is not a directory this run may write to; set CODE_ACP_HOME "
            f"to a writable directory outside the host Agent home ({host})"
        )
    return candidate


def creatable(path: Path) -> bool:
    """True when ``path`` could be made -- asked without making anything.

    A check that created the directory to find out would answer the question by
    destroying it: `adopt_legacy_acp_home` reads an existing destination as "this
    run already has a home here, and it is the live one". So walk up to the
    nearest ancestor that does exist and ask whether it is a directory this
    process may create under.

    Advisory, not authoritative: a run with an effective uid that overrides the
    write bit is told yes and proceeds exactly as it did before this check. What
    it removes is the case where the answer is knowably no and the operator
    learns it from a bare `PermissionError`.
    """
    probe = Path(path).expanduser()
    for ancestor in (probe, *probe.parents):
        if ancestor.exists():
            return ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
    return False


def instance_tag() -> str:
    """A name for THIS raven instance, for a path shared with its siblings.

    Two instances on one machine are told apart by their `RAVEN_HOME`, and the
    sibling fallback below puts its directory beside the Agent home rather than
    inside it -- which is a place their siblings can reach. Without the instance
    in the name, `/srv/a` and `/srv/b` both land on `/srv/.raven-code`, and the
    ACP session store and the allocation base underneath it -- one instance's
    conversations -- are shared with the other. That is the isolation
    `RAVEN_HOME` exists to give.

    The directory's own name for legibility, and a digest of the resolved path
    because two instances can be named the same under different parents.
    """
    resolved = _RAVEN_HOME.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{safe_name(resolved.name)}-{digest}"


def outside(path: Path, home: Path) -> bool:
    """True when ``path`` is neither ``home`` nor anything under it.

    The one property the runtime's own guard cares about: a working directory
    must not contain the agent home it is handed to, and the host hands its Agent
    home over as the working directory.
    """
    p = Path(path).expanduser().resolve()
    h = Path(home).expanduser().resolve()
    return p != h and h not in p.parents


def adopt_legacy_acp_home(dest: Path) -> None:
    """Move a previous ACP home into ``dest``, once, if one is there.

    The engine's runtime data directory is derived from where its config sits, so
    moving the home moves the `sessions/` tree with it -- and the host's instance
    registry still holds the session ids the old tree knows. Left behind, the
    next turn's `session/load` is answered `unknown session`, the backend unbinds
    the handle, and a conversation the reader was in the middle of starts again
    from nothing.

    Only when the destination does not exist: a home already in the new place is
    the live one, and nothing may be written over it. A failed move is reported
    and not retried -- the run continues on the new home, which is the same
    outcome as before this function existed, and the operator is told where the
    old one is.
    """
    legacy = state_root() / "acp"
    if dest.exists() or not legacy.is_dir():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(dest)
    except OSError:
        try:
            shutil.move(str(legacy), str(dest))
        except OSError as exc:
            log(f"[run] acp: could not move the previous ACP home {legacy} -> {dest}: {exc}")
            return
    log(f"[run] acp: adopted the previous ACP home from {legacy}")


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


def overlay_effort(overlay_file: Path) -> str:
    """The effort one overlay file moves the tier to, refusing anything else.

    Strict on purpose: the agent's mode profile carries the effort and nothing
    more, so a key beyond it would be declared here and dropped there without a
    word. The failure has to be at launch, where a person sees it.
    """
    overlay = json.loads(overlay_file.read_text(encoding="utf-8"))
    unknown = sorted(set(overlay) - OVERLAY_KEYS)
    if unknown:
        raise SystemExit(
            f"{overlay_file}: unsupported top-level key(s) {', '.join(unknown)}; "
            f"an overlay carries only {', '.join(sorted(OVERLAY_KEYS))}"
        )
    defaults = (overlay.get("agents") or {}).get("defaults")
    extra = sorted(set(overlay.get("agents") or {}) - {"defaults"}) + sorted(set(defaults or {}) - {EFFORT_PATH[-1]})
    if extra:
        raise SystemExit(f"{overlay_file}: an overlay moves only {'.'.join(EFFORT_PATH)}; found {', '.join(extra)}")
    effort = dig(overlay, EFFORT_PATH)
    if not effort:
        raise SystemExit(f"{overlay_file}: an overlay must set {'.'.join(EFFORT_PATH)} to a non-empty string")
    return effort


def mode_catalogue() -> dict:
    """The `acp.modes` block: one entry per tier, the baseline carrying no effort.

    Declared rather than applied. The agent composes a profile per session, so
    these files become a catalogue a client picks from over `session/set_mode`,
    and `--mode` is only which entry a session starts in. The baseline entry
    inherits `agents.defaults.reasoningEffort` from the rendered config itself,
    which is what keeps the default tier byte-identical to today's behaviour.

    An empty dict when this folder ships no `modes/` directory, which leaves the
    rendered config without an `acp` key and the agent's `session/set_mode`
    method-not-found -- the pre-modes behaviour, and also what keeps this
    launcher runnable against a vendored tree whose schema knows no `acp` key.
    """
    if not MODES_DIR.is_dir():
        return {}
    catalogue = {}
    for mode, (name, description) in MODE_LABELS.items():
        entry = {"name": name, "description": description}
        if mode != BASELINE_MODE:
            overlay_file = MODES_DIR / f"{mode}.json"
            if not overlay_file.is_file():
                continue
            entry["reasoningEffort"] = overlay_effort(overlay_file)
        catalogue[mode] = entry
    return catalogue


def render_config(source: Path, state_dir: Path, mode: str | None = None) -> Path:
    """Write a rendered config into the state partition that will consume it.

    The runtime derives its data directory from the config file's parent but its
    Agent home from ``agents.defaults.workspace``. Point both at ``state_dir``
    so sessions, memory and skills stay in the same host-owned partition. The
    ACP session Working directory remains a separate path used for repository
    reads, edits and commands.

    ``mode`` is which tier sessions start in; the source stays the baseline
    every tier diffs against, so nothing is merged into it here.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    catalogue = mode_catalogue()
    if mode and mode not in catalogue:
        raise SystemExit(f"error: no overlay for mode {mode!r} at {MODES_DIR / f'{mode}.json'}")
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = mode or BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {acp['defaultMode']})")
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
    """Record a diagnostic without contaminating the reply.

    The directory is made here rather than assumed. On the ACP path the first
    thing recorded is `adopt_legacy_acp_home`'s report, and that runs before the
    home is created -- deliberately, since creating it first would make every run
    look like one that already had a home in the new place. A logger that raised
    in that window killed the run from inside the handler that exists to let it
    continue.

    Which makes the logger part of that ordering: the `mkdir` here is the same
    one the caller does moments later, so it is free where it stands, and fatal
    if a `log()` is ever moved above the adoption. See the note at that call.

    A write that still fails costs the line, not the run. On a `--run` turn
    stderr is folded into the subagent's reply, so an unwritable log has nowhere
    to report itself that would not paste launcher noise into the conversation as
    something the agent said. `--verbose` is the one mode where stderr is a
    human's terminal, and there the reason is given.
    """
    if _VERBOSE:
        print(message, file=sys.stderr, flush=True)
    if _LOG_FILE is None:
        return
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")
    except OSError as exc:
        if _VERBOSE:
            print(f"[run] log: {_LOG_FILE} is not writable: {exc}", file=sys.stderr, flush=True)


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
    acp_state = acp_home()
    _LOG_FILE = acp_state / "launcher.log"
    # Before the directory is made: `adopt_legacy_acp_home` will not move onto an
    # existing destination, and creating it first would make every run look like
    # one that already had a home in the new place. Nothing may log between the
    # line above and this one either -- `log()` creates the directory it writes
    # into, which is this one, so a diagnostic here would answer the question
    # before it is asked.
    adopt_legacy_acp_home(acp_state)
    acp_state.mkdir(parents=True, exist_ok=True)

    config = render_config(Path(args.config).resolve(), acp_state, mode=args.mode)
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
    ap.add_argument(
        "--mode",
        choices=tuple(MODE_LABELS),
        default=None,
        help="Which effort tier ACP sessions start in; a client may switch a live session "
        "with session/set_mode. `high` is the baseline as-is. Only with --acp.",
    )
    args = ap.parse_args()

    if args.acp:
        return _serve_acp(args)
    if args.mode:
        # A tier is a session-level choice the ACP client makes. The one-task
        # path has no session to put it on, and a flag accepted there would be
        # one that silently does nothing.
        raise SystemExit("error: --mode applies to --acp only")

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
