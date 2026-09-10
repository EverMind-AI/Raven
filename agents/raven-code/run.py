#!/usr/bin/env python
"""Host-side launcher for Raven-Code -- the B side, on installed raven.

The retired vendored Raven-Code carried a whole fork
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
environment: ``plugins.dirs`` names the code-flow plugin and its slice flips
the flow on (per D6). Worktree isolation retired with the write gate: nothing
locks a checkout, and a session learns from the flow when other sessions are
working beside it. The launch contract is otherwise the fork launcher's: refuse
without any LLM key before any process starts, seed the fork's TOOLS.md
wording into the state partition, refresh untouched seeds, keep secrets out of
the published config and in a 0600 rendered copy whose parent decides the data dir.

Serving ACP, stdout belongs to the protocol and diagnostics go to stderr.
Hosting a CLI turn, stdout carries the answer and nothing else -- the
caller's backend folds non-empty stderr into the conversation -- so
diagnostics go to ``launcher.log`` in the conversation's state directory;
``--verbose`` mirrors them for a human running this by hand.
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

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_ID = "code-flow"
GUIDE = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "TOOLS_CODE.md"
#: The coding conduct the model reads as ``agent.md`` beside the host identity:
#: one variant per model family, the fork's split (its per-family prompt files).
CONDUCT_DEFAULT = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "CODE_CONDUCT_default.md"
CONDUCT_ANTHROPIC = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "CODE_CONDUCT_anthropic.md"
#: The phased discipline for changing code, held apart from the conduct that
#: carries it and spliced in through a sentinel -- the fork's own shape
#: (``{{SE_DISCIPLINE}}``), and the reason is the same: it is the one section
#: whose effect is worth measuring on its own, so it has to be removable
#: without touching a word of the rest. Byte-identical in both variants, so
#: one file serves both.
CONDUCT_DISCIPLINE = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "CODE_DISCIPLINE.md"
DISCIPLINE_SENTINEL = "{{DISCIPLINE}}"
CONDUCT_RELATIVE = Path("agent_memory") / "profile" / "agent.md"
#: The product identity, seeded as ``soul.md`` the way raven-research seeds
#: its own. Bootstrap renders soul.md first; without a seed of ours the
#: one-turn CLI hosting's workspace sync writes trunk's template there -- a
#: personal assistant with a personality -- in front of the coding conduct,
#: while the ACP hosting (no sync) reads none, so the two hostings disagreed.
SOUL = HERE / "soul.md"
SOUL_RELATIVE = Path("agent_memory") / "profile" / "soul.md"

PRODUCT = "raven-code"

# Effort overlays over the baseline config. `config.json` is the complete
# default (high) profile; each overlay carries the one knob its tier moves, so
# the provider wiring and the tool surface exist in exactly one place.
MODES_DIR = HERE / "modes"
# The ids are the host's own tier ladder (medium / high / max): the host clamps
# its session tier onto the rungs an agent offers by name, and a rung it cannot
# rank would leave the agent on its default rather than guess. The baseline IS
# the high profile, so it needs no overlay file; the others do.
BASELINE_MODE = "high"
# What a client's mode picker shows, and what the dispatching model reads when
# it chooses a tier for a task. The copy lives here rather than in the overlay
# files because it is product text about the choice, not config the agent reads.
MODE_LABELS = {
    "medium": (
        "Medium",
        "Lighter reasoning. For a small, well-specified change, a question about the code, "
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
# The one top-level key an overlay may carry, and under it the one knob.
OVERLAY_KEYS = frozenset({"agents"})
EFFORT_KEY = "reasoningEffort"

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


# Pristine guide and conduct seeds from before per-file seed receipts existed.
# Digests recognize exact product text without overwriting an operator's edit.
_LEGACY_GUIDE_DIGESTS = frozenset(
    {
        "7d0dc36934316639000b486d2775e2581b1378472b29841bdc67231570576657",
        "bfd21b466b22dc3d851eedcfcc9219610d88b7622ba9fa84895a8379b35be6f9",
        "e64bb8409b14a883a075cb5afec9a1d56786cbfb502cb13b20e470e86d4b433d",
        "b1b3980cf6cd91b4ae78c9a5b9a9ec3d089147ecea23ae2dfe943e83cebe1c69",
    }
)

_LEGACY_CONDUCT_DIGESTS = frozenset(
    {
        "6702142378f6c7e65b1053a1c60b5fc826b0ae1f0efe03977fce9ea30fc807fe",
        "7a14f80c2e947f854d2282031ab61f065d2539f27576fac38e294e2c7ae6bc7f",
        "b9a8e109f77c36244cd16290b93488c985f461534ae3b9fd6f7a6d7d2541a667",
        "bad49cf7efb520c98ad55a1305d3f096be7a43c531ff3c4ab0500fb9e02dd781",
    }
)


def _seed_prompt(target: Path, wanted: str | None, pristine: set[str] | frozenset[str]) -> None:
    """Refresh an untouched product seed; keep edited files byte-for-byte.

    A small sibling receipt remembers the last text this launcher wrote, so
    later product updates do not need an ever-growing list of old templates.
    """
    receipt = target.with_name(f".{target.name}.code-flow.sha256")
    if target.exists():
        current = target.read_text(encoding="utf-8")
        digest = hashlib.sha256(current.encode()).hexdigest()
        previous = receipt.read_text(encoding="ascii").strip() if receipt.exists() else ""
        if current != wanted and digest not in pristine and digest != previous:
            log(f"[run] {target.name}: edited in place; kept unchanged")
            return
    if wanted is None:
        target.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8") != wanted:
        target.write_text(wanted, encoding="utf-8")
    receipt.write_text(hashlib.sha256(wanted.encode()).hexdigest() + "\n", encoding="ascii")


def seed_guide(partition: Path) -> None:
    """Seed or refresh the tool guide while preserving in-place edits."""
    _seed_prompt(partition / "TOOLS.md", GUIDE.read_text(encoding="utf-8"), _LEGACY_GUIDE_DIGESTS)


def conduct_source(model: str | None) -> Path:
    """Which conduct variant a model reads: the anthropic family gets the fork's
    anthropic prompt (tone, objectivity, todo guidance), everyone else the
    shared default. The family is read off the routed model id the way the
    fork's identity_prompts did: by substring, so a gateway prefix
    (``anthropic/claude-opus-5``) and a bare id both resolve."""
    lowered = (model or "").lower()
    if "claude" in lowered or "anthropic" in lowered:
        return CONDUCT_ANTHROPIC
    return CONDUCT_DEFAULT


def render_conduct(model: str | None, *, discipline: bool = True) -> str:
    """The conduct as the model reads it: the family's variant, with the
    discipline spliced in or left out. The sentinel never survives either way.
    """
    text = conduct_source(model).read_text(encoding="utf-8")
    block = ""
    if discipline:
        block = CONDUCT_DISCIPLINE.read_text(encoding="utf-8").rstrip("\n") + "\n"
    rendered = text.replace(DISCIPLINE_SENTINEL + "\n", block).replace(DISCIPLINE_SENTINEL, block.rstrip("\n"))
    if DISCIPLINE_SENTINEL in rendered:
        raise SystemExit(f"error: {conduct_source(model)} still carries {DISCIPLINE_SENTINEL} after rendering")
    return rendered


def conduct_settings() -> dict:
    """What this product's own settings say about the conduct.

    Same lookup as every other setting (the process environment, then this
    folder's ``.env``): the switch belongs to raven-code, and a host config
    never has to know this prompt exists.
    """
    out: dict = {}
    for name, key in (("CODE_CONDUCT", "enabled"), ("CODE_CONDUCT_DISCIPLINE", "discipline")):
        raw = env_value(name)
        if raw is None or not raw.strip():
            continue
        out[key] = raw.strip().lower() not in _OFF
    return out


def seed_soul(partition: Path) -> None:
    """Seed the product identity as the partition's ``soul.md``, once.

    The workspace sync only creates what is missing, so a file seeded before
    the engine starts is the one bootstrap reads on both hostings. Once is the
    contract (``seed_once``): an operator's edit is never overwritten.
    """
    render.seed_once(partition / SOUL_RELATIVE, lambda: SOUL.read_text(encoding="utf-8"))


def seed_conduct(partition: Path, model: str | None) -> None:
    """Seed the configured conduct, replacing only untouched product text.

    Bootstrap reads agent.md after the product identity. Model-family,
    discipline and product updates refresh managed seeds; edited text stays.
    Turning conduct off removes a managed seed instead of leaving it resident.
    """
    settings = conduct_settings()
    wanted = (
        render_conduct(model, discipline=settings.get("discipline", True)) if settings.get("enabled", True) else None
    )
    pristine = _LEGACY_CONDUCT_DIGESTS | {hashlib.sha256(text.encode()).hexdigest() for text in pristine_conducts()}
    _seed_prompt(partition / CONDUCT_RELATIVE, wanted, pristine)


def pristine_conducts() -> set[str]:
    """Every composition this product could have written, so a file that is
    still one of them can be replaced and a file that is not is left alone."""
    return {
        render_conduct(model, discipline=discipline)
        for model in (None, "anthropic/claude")
        for discipline in (True, False)
    }


#: Off switch for the working directory's own instruction files (``AGENTS.md``
#: and its equivalents), contributed to each turn's system message. On by
#: default -- a person joining a repository reads them, and so should this.
#: A run that must judge the model alone turns them off here,
#: without editing a config.
PROJECT_FILES_SETTING = "CODE_PROJECT_FILES"
#: The product's spelling of off, for every on/off setting it reads.
_OFF = frozenset({"0", "false", "no", "off", "disabled"})


def superseded_host_tools() -> dict[str, str]:
    """The host names this product's tool face replaces under another name.

    Imported from the plugin that owns the table rather than restated here:
    the launcher stays stdlib-only (this is the product's own module, the same
    seam raven-research uses to render its prompts) and there is one place to
    edit when the face changes.
    """
    if str(PLUGINS_DIR / FLOW_PLUGIN_ID) not in sys.path:
        sys.path.insert(0, str(PLUGINS_DIR / FLOW_PLUGIN_ID))
    from code_flow.tools import SUPERSEDED_HOST_TOOLS

    return dict(SUPERSEDED_HOST_TOOLS)


def default_project_files() -> list[str]:
    """The coding set of instruction-file names, from the plugin that reads them."""
    if str(PLUGINS_DIR / FLOW_PLUGIN_ID) not in sys.path:
        sys.path.insert(0, str(PLUGINS_DIR / FLOW_PLUGIN_ID))
    from code_flow.flow import PROJECT_FILES

    return list(PROJECT_FILES)


def project_files_off() -> bool:
    """Whether this product's settings turn the checkout's own files off.

    Absent means on: the slice decides the names. Only an explicit off value
    empties the list.
    """
    raw = env_value(PROJECT_FILES_SETTING)
    return bool(raw and raw.strip()) and raw.strip().lower() in _OFF


def overlay_effort(overlay: dict) -> str | None:
    """The one knob a tier moves, read strictly; ``None`` for the baseline's empty diff.

    An overlay naming anything else would be declared and then ignored by the
    engine's mode profile, which is exactly the silent no-op the refusal here
    exists to prevent.
    """
    if not overlay:
        return None
    defaults = (overlay.get("agents") or {}).get("defaults") or {}
    extra = sorted(set(overlay.get("agents") or {}) - {"defaults"}) + sorted(set(defaults) - {EFFORT_KEY})
    if extra:
        raise SystemExit(
            f"error: a raven-code mode overlay moves {', '.join(extra)}; a tier moves only agents.defaults.{EFFORT_KEY}"
        )
    effort = defaults.get(EFFORT_KEY)
    if not isinstance(effort, str) or not effort.strip():
        raise SystemExit(f"error: a raven-code mode overlay names no agents.defaults.{EFFORT_KEY}")
    return effort.strip()


def resolve_mode(overlay: dict) -> tuple[None, dict]:
    overlay_effort(overlay)
    return None, {}


def render_config(source: Path, partition: Path, mode: str | None = None, *, unattended: bool = False) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under ``partition``.

    The partition is the rendered file's parent and the pinned Agent home, so
    the runtime data dir and transcripts stay in this host-owned partition; the
    working directory repository work happens in stays a separate path (the ACP
    session cwd, or ``--workspace``). ``mode`` is which effort tier sessions
    start in; the source stays the baseline whatever the tier. ``unattended``
    says this hosting has nobody to answer a permission prompt -- see the
    ask-tier note below.
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
    # The launcher flips the flow on for its own renders, so a custom --config
    # lacking the code-flow slice still gets the product's conduct. setdefault,
    # so an operator's explicit false still opts out.
    flow_slice.setdefault("enabled", True)
    # The tool face rides the same switch shape as the flow: on for the
    # product's own renders, an explicit false in a custom config opts out. It
    # is a section of the same slice, so the flow and the tools have one switch
    # each and neither drags the other along.
    tools_slice = flow_slice.setdefault("tools", {})
    tools_slice.setdefault("enabled", True)
    # The tool fence travels through the slice because a plugin factory cannot
    # read it: these tools replace the host's own by name, and the host grants
    # its own the workspace root only when tools.restrictToWorkspace is on.
    # Rendered rather than defaulted, so the replacements are fenced exactly
    # when the originals would have been.
    tools_slice["restrictToWorkspace"] = bool((config.get("tools") or {}).get("restrictToWorkspace", False))
    # The shell's knobs travel the same way as the fence: the host's own
    # tools.exec values and the sandbox backend, rendered into the slice so the
    # exec factory can match the host's defaults and refuse to replace a
    # sandboxed shell. maxTimeout is the product's own knob (trunk has none)
    # and stays whatever config.json's slice says.
    exec_slice = tools_slice.setdefault("exec", {})
    host_tools = config.get("tools") or {}
    host_exec = host_tools.get("exec") or {}
    exec_slice["timeout"] = host_exec.get("timeout", 60)
    exec_slice["pathAppend"] = host_exec.get("pathAppend", "")
    exec_slice["sandboxBackend"] = (host_tools.get("sandbox") or {}).get("backend", "none")
    if tools_slice.get("enabled"):
        # One capability, one name. Where this face serves a host capability
        # under a different name, the host's name is withheld -- read from the
        # plugin's own table so adding a tool is a line beside the tool, not an
        # edit here, and only while the face is actually being served: written
        # into config.json it would outlive the switch and leave a product with
        # the face off unable to find a file at all.
        disabled = config.setdefault("tools", {}).setdefault("disabledTools", [])
        for host_name in superseded_host_tools():
            if host_name not in disabled:
                disabled.append(host_name)
    # The checkout's own instruction files ride the flow slice like the tool
    # face does: named for the product's own renders, an explicit list in a
    # custom config wins, and the off setting empties the list rather than
    # deleting the key (a missing key would fall back to the names below).
    flow_slice.setdefault("projectFiles", default_project_files())
    if project_files_off():
        flow_slice["projectFiles"] = []
        log(f"[run] project files: off ({PROJECT_FILES_SETTING})")
    if unattended:
        # The ask tier has nobody to ask on this hosting: one CLI turn prints
        # a reply and exits, so the gate refuses every write and every command
        # instead of prompting (measured 2026-09-08: the model could not edit
        # one line and reported the task incomplete), and trunk's own one-shot
        # spine names this the operator's call. The ACP hosting keeps the
        # default: raven dispatching a sub-agent answers those prompts itself,
        # and a person in an editor should still be asked. Builtin refusals
        # (the catastrophic-command list) hold in every mode, and an explicit
        # permissions block in a custom config wins.
        config.setdefault("permissions", {}).setdefault("mode", "full")

    # Declared, not merged: the engine composes a profile per session from the
    # catalogue over session/set_mode, and --mode only picks the starting entry.
    catalogue = render.mode_catalogue(
        MODES_DIR,
        MODE_LABELS,
        baseline=BASELINE_MODE,
        overlay_keys=OVERLAY_KEYS,
        resolve=resolve_mode,
    )
    if mode and mode not in catalogue:
        raise SystemExit(f"error: no overlay for mode {mode!r} under {MODES_DIR}")
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = mode or BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {acp['defaultMode']})")

    partition.mkdir(parents=True, exist_ok=True)
    seed_guide(partition)
    seed_soul(partition)
    seed_conduct(partition, defaults.get("model"))
    render.sweep_stale_renders(partition)
    return render.write_rendered(config, partition)


def render_acp_config(source: Path, mode: str | None = None) -> Path:
    """The ACP hosting's render: the acp partition.

    The partition is the engine's Agent home and must sit OUTSIDE the host
    Agent home (the host hands its home over as the session cwd, and the
    runtime refuses a cwd that contains the engine's home) -- so it comes
    from the shared placement helper, not from the state root. Everything
    else this product keeps (repos, per-instance buckets) is work and stays
    under CODE_STATE_ROOT; CODE_ACP_HOME overrides the home alone.
    """
    acp_state = render.product_acp_home(PRODUCT, override=env_value("CODE_ACP_HOME")).resolve()
    return render_config(source, acp_state, mode=mode)


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
    rendered = render_acp_config(Path(args.config).resolve(), mode=getattr(args, "mode", None))
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

    rendered = render_config(Path(args.config).resolve(), state_dir, unattended=True)
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
    parser.add_argument(
        "--mode", choices=sorted(MODE_LABELS), help="the effort tier sessions start in (with --acp only)"
    )
    # No wall-clock cap by default: a real coding task has no predictable
    # length, and a killed run produces nothing at all. The per-LLM-call and
    # per-command limits stay: those bound a stall, not a long task.
    parser.add_argument("--timeout", type=int, default=0, help="seconds; 0 (default) means no limit")
    parser.add_argument("--keep-going", action="store_true", help="do not fail when no answer was committed")
    parser.add_argument("--verbose", action="store_true", help="mirror diagnostics to stderr; never when spawned")
    args = parser.parse_args()
    if args.mode and not args.acp:
        raise SystemExit("error: --mode applies to --acp only; a one-turn CLI run has no session to put a tier on")
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
