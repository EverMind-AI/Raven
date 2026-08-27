#!/usr/bin/env python3
"""Host-side launcher for the raven-ppt sub-agent.

The agent used to run in a container, for one reason that has since stopped
being true: the image carried LibreOffice and the rendering fonts because the
host had none of them. This host has `soffice` and 35 CJK-capable faces, so the
checkout runs directly and the 1.6 GB image, its registry and the docker SDK all
drop out of the path.

What the container contributed that still has to happen somewhere, and now
happens here:

- **The named files arrive.** Raven's CLI sub-agent contract substitutes
  `{prompt}`, `{prompt_file}` and `{agent_id}` and nothing else, so a dispatching
  agent has no argv slot for material -- naming absolute paths in the task text
  is the only channel. Those files are copied into the job's own `materials/`
  and the prompt points at the copies.
- **The key never touches a published file.** `config.json` ships and holds no
  secret; `.env` supplies it and the two are merged into a rendered config under
  the state root at launch.
- **stdout is the result.** Raven's CLI backend uses a child's whole output as
  the sub-agent's reply, so the agent's own narration goes to the job log and
  stdout carries only what was produced and where.

Success is a published deck rather than an exit code: a pptx this run wrote under
the job's `out/` that opens as a zip carrying slide parts, with the `MEDIA:` line
only choosing between several. That test was written because the container exited
139 on fully successful runs; it survives the move because it checks the artifact
rather than the process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE / "Raven-PPT"
DEFAULT_CONFIG = HERE / "config.json"

STATE_ROOT = Path(
    os.environ.get("PPT_STATE_ROOT", "").strip()
    or Path.home() / ".raven" / "workspace" / "subagent_sessions" / "raven-ppt"
)

MATERIAL_SUFFIXES = {
    ".pdf", ".md", ".markdown", ".txt", ".rst", ".docx", ".doc", ".pptx",
    ".xlsx", ".xls", ".csv", ".tsv", ".json", ".html", ".htm", ".png", ".jpg",
    ".jpeg", ".webp",
}
_PATH_RE = re.compile(r"/[^\s'\"`,;()<>\[\]]+")
_INPUTS_FENCE = re.compile(r"```(?:raven-ppt|json)?\s*\n\s*(\{.*?\})\s*\n\s*```", re.DOTALL)

_EXIT_TIMEOUT = 124
"""Exit code for a run the watchdog killed on its own deadline.

GNU ``timeout``'s convention, and its own code rather than 1 because the two
are different terminal states: 1 is the agent having run and published nothing
verifiable. A kill says nothing about the work at all, and a caller that folds
it into the same code has no way back to that distinction.
"""

_LOG_FILE: Path | None = None
_VERBOSE = False


def log(message: str) -> None:
    if _VERBOSE:
        print(message, file=sys.stderr, flush=True)
    if _LOG_FILE is not None:
        with _LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def env_value(name: str) -> str | None:
    """`name` from the process environment, else from this folder's `.env`."""
    if value := os.environ.get(name, "").strip():
        return value
    env_file = HERE / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip()
    return None


# The host raven's config file, read for the fallback in `render_config`. Read
# as JSON, never imported from raven: this launcher is standard-library only and
# has to run under a bare python3 that may not have the runtime installed at all.
HOST_CONFIG = Path(os.environ.get("RAVEN_HOME", "").strip() or Path.home() / ".raven") / "config.json"

# Where each optional secret belongs in the config the agent loads. Named and
# pathed as in the other three launchers so the four can be compared by grep.
# The LLM key is not among them: it is written to every provider block rather
# than to one path, and its own branch below carries the model and base that the
# same key pays for.
SECRET_SLOTS = {
    "PPT_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "PPT_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The one secret whose absence is fatal: no pictures makes a poorer deck, no
# model makes no deck at all.
REQUIRED_SECRETS = ("PPT_API_KEY",)


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
    `agents.defaults`: the token ceiling, the tool-iteration cap and the render
    timeout are this agent's operating limits, tuned for its own job, and they
    have nothing to do with whose key is paying.
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


def inputs_from_prompt(text: str) -> list[str]:
    """What a dispatching agent declared, as a fenced JSON object: its
    materials. ``[]`` when it declared nothing, which leaves the prose scan
    below as the only reader.

    The sub-agent contract carries one content channel, the task text, and prose
    is not a reliable one. A path in prose is delimited by whitespace, so
    ``/tmp/my deck.pptx`` splits in two; a full stop in a script whose punctuation
    is not ASCII stays attached, so ``notes.md<CJK full stop>`` matches no known
    type. Either way the file is dropped without a word, which is exactly the
    silence ``stage`` refuses to allow for a file it cannot copy. A quoted string
    has neither problem.

    A declared ``template`` path is refused rather than ignored: the template
    channel was removed from the launcher, and a deck delivered without the
    house file the caller named would read as if it had been used. A non-path
    value under that key (a style name in an unrelated JSON block) is not a
    path declaration and is left alone.
    """
    for block in _INPUTS_FENCE.findall(text):
        try:
            declared = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(declared, dict) or not declared.keys() & {"materials", "template"}:
            continue
        template = declared.get("template")
        if isinstance(template, str) and template.startswith("/"):
            raise SystemExit(
                "error: the template channel was removed from the launcher. The "
                "deck is built in the file the run publishes; drop the template "
                "declaration and name materials only."
            )
        listed = declared.get("materials")
        return [item for item in listed if isinstance(item, str)] if isinstance(listed, list) else []
    return []


def materials_from_prompt(text: str) -> list[str]:
    """Absolute paths named in the prompt that exist and look like documents.

    The tail is trimmed a character at a time until what is left is a file,
    rather than by stripping ASCII full stops. Prose in any script puts its
    punctuation against the path, and a mark that is not ASCII stays attached:
    the suffix of `notes.md<CJK full stop>` is not `.md`, so the whole document
    used to be dropped without a word. The type list still applies, to the
    trimmed name -- prose names paths that are not material, and a run that
    staged every file mentioned in passing would ground the deck in its own log.
    A document whose kind is not on that list reaches this run through the
    declared block instead, which does not filter.
    """
    found: list[str] = []
    for match in _PATH_RE.findall(text):
        for end in range(len(match), 1, -1):
            candidate = match[:end]
            if Path(candidate).is_file():
                if Path(candidate).suffix.lower() in MATERIAL_SUFFIXES and candidate not in found:
                    found.append(candidate)
                break
    return found


def unique_sources(paths: list[str]) -> list[str]:
    """The given paths in order, one entry per real file.

    Deduplicated by real path rather than by spelling, because two names for one
    file would be staged twice under different names and listed as two entries,
    so the prompt would claim material the run does not have.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if (key := os.path.realpath(path)) not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def stage(job: str, materials: list[str]) -> tuple[Path, Path, Path, list[tuple[str, Path]]]:
    """The job directory, with the named files copied into it.

    Returns ``(root, materials, out_dir, staged)``, where ``staged`` pairs each
    source with the path it was copied to -- the prompt is built from that pairing
    rather than re-deriving it, so the agent is told where each file actually is.
    ``root`` is handed to raven as the workspace, which is what puts both
    sub-directories inside the fence.

    A file that cannot be copied stops the run rather than being skipped. Two
    reasons: a deck built from part of its material is wrong in a way nothing
    downstream can see, and an uncaught copy error would put a Python traceback
    on stderr -- which the caller reads as this agent's reply. A file named in
    the declared block reaches here without passing the prompt scan's
    ``is_file`` test, so this is the only place those paths are checked at all.

    A second source with the same basename is suffixed rather than allowed to
    overwrite the first. That collision loses material exactly as silently as a
    skipped copy would: ``copyfile`` succeeds, and the prompt would list two
    entries resolving to one file, so the agent believes it holds two documents
    and grounds the deck twice in one of them.
    """
    root = STATE_ROOT / "jobs" / job
    mats, out_dir = root / "materials", root / "out"
    for directory in (mats, out_dir):
        directory.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[str, Path]] = []
    taken: set[str] = set()
    for source in materials:
        stem, suffix = Path(source).stem, Path(source).suffix
        name, nth = stem + suffix, 2
        while name in taken:
            name, nth = f"{stem}-{nth}{suffix}", nth + 1
        taken.add(name)
        target = mats / name
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise SystemExit(
                f"error: cannot stage {source}: {exc.strerror or exc}. Name a readable "
                "absolute path, or drop it from the task."
            ) from None
        staged.append((source, target))
    return root, mats, out_dir, staged


def material_section(staged: list[tuple[str, Path]], mats: Path) -> str:
    """The prompt block that tells the agent what material it holds.

    The deck author runs with or without material, so the block names only
    what exists: with nothing staged there is no listing and no pointer at an
    empty ``materials/`` directory, and the prompt says the deck is built from
    the model's own account instead. Paths the task names that resolve to
    nothing are passed through untouched; the agent reads the task text itself.
    """
    if staged:
        listing = "\n".join(
            f"- {Path(source).name} (from {source}) -> {target}"
            for source, target in staged
        )
        return (
            f"\n\n# Material staged for this run\n{listing}"
            f"\nUse only files under {mats} as factual source material."
        )
    return (
        "\n\nNo source material was staged for this run: every fact in the deck "
        "is yours, and what you cannot verify is a guess. Present it as one."
    )


def render_config(source: Path) -> Path:
    """`config.json` plus its secrets, written where the runtime may read it.

    Raven derives its data directory from the config file's own parent, so this
    lands under the state root: rendering it beside `config.json` would put
    transcripts and caches inside a folder that ships.

    A folder with no key of its own runs on the host raven's, as the other three
    launchers do and as the onboarding step promises when it offers "this raven's
    LLM" for this folder. Neither branch may end with a provider block that has
    no key at all: the runtime makes `api_key` a hard requirement, so that config
    starts a child which cannot answer, and the caller reads the generic failure
    with nothing pointing at the missing credential.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = host_config()

    # Each optional key falls back on its own, and the host's is better than
    # none: with no Serper key `ppt_outline` keeps handing back gather errands
    # that `web_search` can only refuse, and the deck ships without its pictures.
    sources = []
    for name, path in SECRET_SLOTS.items():
        own = env_value(name)
        if value := own or dig(host, path):
            put(config, path, value)
            sources.append(f"{name}={'own' if own else 'host'}")
    if sources:
        log(f"[run] web: {', '.join(sources)}")
    else:
        # Not "web_search will refuse": the runtime resolves both keys from the
        # bare environment as a last resort, and the child is handed this
        # process's environment wholesale, so a host that exports
        # SERPER_API_KEY and stores nothing searches fine on this branch.
        log(
            "[run] web: no key in .env or the host config; the runtime still reads "
            "SERPER_API_KEY / JINA_API_KEY from the environment"
        )

    llm_key = REQUIRED_SECRETS[0]
    if api_key := env_value(llm_key):
        for provider in config.get("providers", {}).values():
            if isinstance(provider, dict) and not provider.get("apiKey"):
                provider["apiKey"] = api_key
        # Read on this branch only: both describe the endpoint this key pays for.
        # Applied on top of an inherited block they would point the host's gateway
        # at a model it may not serve, which surfaces as a bad answer rather than
        # an error - the same trap `inherit_llm` copies wholesale to avoid.
        if model := env_value("PPT_MODEL"):
            config.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
            for provider in config.get("providers", {}).values():
                if isinstance(provider, dict):
                    provider["models"] = [model]
        if api_base := env_value("PPT_API_BASE"):
            for provider in config.get("providers", {}).values():
                if isinstance(provider, dict):
                    provider["apiBase"] = api_base
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
        ignored = [name for name in ("PPT_MODEL", "PPT_API_BASE") if env_value(name)]
        log(
            f"[run] llm: inherited from {HOST_CONFIG} ({taken}); tuned for {recommended_llm()}"
            + (f"; ignored {', '.join(ignored)}, which need {llm_key}" if ignored else "")
        )
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    rendered = STATE_ROOT / ".config.rendered.json"
    fd = os.open(rendered, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode argument applies only on create, and this launcher never removes
    # the file, so a second run reopens whatever the first one left. `O_TRUNC`
    # has already emptied it here, which makes this the one moment the mode can
    # be fixed with no key on disk -- the window that writing first and calling
    # `chmod` second left open with every key already in the file.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
    return rendered


def _slide_count(deck: Path) -> int:
    """Slides in a pptx, or 0 if it is not one.

    A failed render leaves a truncated or empty file behind, so the check is
    that the archive opens and carries slide parts -- not that the name is right.
    """
    if not (deck.is_file() and zipfile.is_zipfile(deck)):
        return 0
    with zipfile.ZipFile(deck) as archive:
        return len([
            name for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ])


def deck_mtimes(out_dir: Path) -> dict[Path, float]:
    """Every pptx under ``out_dir`` with its mtime, for the before/after compare."""
    found: dict[Path, float] = {}
    for path in out_dir.rglob("*.pptx"):
        try:
            found[path] = path.stat().st_mtime
        except OSError:
            continue
    return found


def verified_deck(out_dir: Path, tail: list[str], before: dict[Path, float]) -> tuple[Path | None, int]:
    """The deck the agent published, or ``(None, 0)``.

    The directory is searched rather than the transcript parsed, because neither
    half of the `MEDIA:` line can be trusted to arrive intact. Measured
    2026-08-20 on the first host run: the CLI hard-wraps its output at the
    terminal width, so the announced path was split across three lines and the
    marker line ended up empty -- and the deck itself was written to
    a level below where a basename lookup against the directory would find it.

    So the launcher trusts what it owns. Every valid deck this run wrote or
    rewrote is a candidate; the announcement only chooses between them, and the
    newest wins when it names none of them.

    Scoped to this run rather than to the directory, which is the whole point of
    ``before``. ``--job`` is the sub-agent's ``agent_id``, minted once per
    conversation and replayed on every later turn, so one job directory
    accumulates every turn's decks while ``tail`` holds only this turn's
    output. Accepting any deck in it would let a turn that published nothing --
    a model error, the watchdog kill, a render that never finished -- match no
    announcement, fall through to the newest, and hand back an earlier turn's
    file as this turn's result.
    """
    candidates = [
        (path, count) for path, mtime in sorted(deck_mtimes(out_dir).items())
        if mtime > before.get(path, -1.0) and (count := _slide_count(path))
    ]
    if not candidates:
        return None, 0

    # Whitespace is stripped rather than normalised: the wrap breaks the path
    # mid-word, so "s\nubagents.pptx" only reads as one name once every space
    # and newline is gone.
    def squashed(lines: list[str]) -> str:
        return "".join("".join(lines).split())

    marker = [i for i, line in enumerate(tail) if line.strip().startswith("MEDIA:")]
    # The announcement first, and only then the rest of the run. A build writes
    # intermediates next to the deck -- measured 2026-08-20, `deck.pptx` beside
    # the requested `subagents.pptx` -- and both names appear somewhere in the
    # log, so anything wider than the announcement picks between them by luck.
    for scope in ([squashed(tail[marker[-1]:])] if marker else []) + [squashed(tail)]:
        for path, count in candidates:
            if path.name in scope:
                return path, count
    return max(candidates, key=lambda item: item[0].stat().st_mtime)


def deliver(deck: Path, dest_dir: str | None) -> Path | None:
    """Copy the deck next to the caller, keeping the job's own copy either way.

    Raven runs a CLI sub-agent with cwd set to the live session workspace, so
    the default destination puts the deck where that session's files are.
    """
    dest = Path(dest_dir).expanduser() if dest_dir else Path.cwd()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if dest.samefile(deck.parent):
            return deck
        target = dest / deck.name
        shutil.copyfile(deck, target)
        return target
    except OSError as exc:
        log(f"[run] warning: could not deliver the deck to {dest}: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the raven-ppt agent on staged material.")
    ap.add_argument("--task", help="Deck-building prompt")
    ap.add_argument("--prompt-file", help="File holding the prompt (alternative to --task)")
    ap.add_argument("--job", required=True, help="Job name; also the job directory name")
    ap.add_argument("--session", help="Enable multi-turn: reuse this key across runs")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    # An hour killed a run that had written all twenty pages and was on its last
    # pass over the renders, so nothing was published and the hour was spent for
    # nothing. A page costs a build, a render and a reading, and twenty of them
    # plus the review the deck is for do not fit in one.
    ap.add_argument("--timeout", type=int, default=10800)
    ap.add_argument("--deliver-to", default=None, help="Copy the finished deck here")
    ap.add_argument("--no-deliver", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="Mirror diagnostics to stderr")
    args = ap.parse_args()

    if args.prompt_file:
        task = Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.task:
        task = args.task
    else:
        raise SystemExit("error: pass --task or --prompt-file")

    # Executability, not existence: `subagents/install.sh` classifies the same
    # folder with `[ -x ]` and the onboarding step reads it the same way, so a
    # third reader disagreeing on a present-but-unexecutable file would have the
    # installer call this folder ready while the launcher calls it missing.
    raven = CHECKOUT / ".venv" / "bin" / "raven"
    if not (raven.is_file() and os.access(raven, os.X_OK)):
        raise SystemExit(
            f"error: {raven} is not an executable. Build the checkout's venv first:\n"
            f"  cd {CHECKOUT} && uv sync --extra ppt"
        )

    declared = inputs_from_prompt(task)
    materials = unique_sources(
        declared
        + materials_from_prompt(task)
    )
    root, mats, out_dir, staged = stage(args.job, materials)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = root / "launcher.log"

    task += material_section(staged, mats) + (
        f" Compile the deck under {out_dir}/ and end your final reply with the "
        "MEDIA line naming it."
    )

    rendered = render_config(Path(args.config).resolve())
    argv = [
        str(raven), "agent",
        "--config", str(rendered),
        "--workspace", str(root),
        "--no-markdown",
        # The runtime's own warnings, without which this log carries only what the
        # model said. Two failed runs were diagnosed off the absence of a
        # "Context overflow" line here, and the line was never going to be here:
        # `raven agent` suppresses loguru unless asked, so the log said nothing
        # about a recovery that may well have run. A launcher log is the only
        # record a finished run leaves.
        "--logs",
    ]
    if args.session:
        argv += ["--session", args.session]
    argv += ["-m", task]

    log(f"[run] job={args.job} workspace={root} materials={[target.name for _, target in staged]}")
    for source, target in staged:
        if target.name != Path(source).name:
            log(f"[run] staged {source} as {target.name}: its own basename was already taken")
    # Taken before the child starts, so what it publishes can be told apart from
    # what an earlier turn of this conversation left in the same job directory.
    before = deck_mtimes(out_dir)
    tail: list[str] = []
    timed_out = False
    # Inherited wholesale on purpose: a proxy the host needs to reach the model
    # was plumbed by hand for the container, and here it simply arrives.
    # The CLI renders through a console that wraps at the terminal width, which
    # breaks long paths across lines in both the log and anything reading it.
    # PYTHONUNBUFFERED so the piped child does not block-buffer its stdout --
    # the live mirror below is only live if lines arrive as they are printed.
    child_env = dict(os.environ, COLUMNS="400", TERM="dumb", PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(CHECKOUT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", env=child_env,
        )
    except OSError as exc:
        print(f"FAILED: could not start the agent ({exc}). See {_LOG_FILE}", flush=True)
        return 1
    # A watchdog rather than a deadline checked inside the read loop: that loop
    # blocks on the pipe, so a check inside it only runs when the child happens
    # to say something, and a silent hang -- a render waiting on a font, a model
    # call with no timeout of its own -- would never be killed.
    def _expire() -> None:
        nonlocal timed_out
        timed_out = True
        log(f"[run] timeout after {args.timeout}s, killing the agent")
        proc.kill()

    watchdog = threading.Timer(args.timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            log(line)
            # Mirrored to stderr as it happens: raven's cli backend streams
            # stderr to the live console beside the run without folding it
            # into the reply. Skipped under --verbose, where log() already
            # writes the same line there.
            if not _VERBOSE:
                print(line, file=sys.stderr, flush=True)
            tail.append(line)
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        rc = None
    finally:
        watchdog.cancel()

    deck, slides = verified_deck(out_dir, tail, before)
    log(f"[run] exit_code={rc} timed_out={timed_out} deck={deck} slides={slides}")

    if deck is None and timed_out:
        # A terminal state of its own: the watchdog cut the run short, which is
        # not the same claim as the agent having run and published nothing.
        # Only the caller can decide between a longer deadline and a smaller
        # deck, and it cannot decide that from "no verifiable deck".
        print(
            f"TIMEOUT: killed after {args.timeout}s before a verifiable deck was published. "
            f"See {_LOG_FILE}",
            flush=True,
        )
        return _EXIT_TIMEOUT
    if deck is None:
        print(f"FAILED: no verifiable deck was published. See {_LOG_FILE}", flush=True)
        return 1
    if rc != 0:
        log(f"[run] note: agent exited {rc} after publishing; deck verified, treating as success")

    delivered = None if args.no_deliver else deliver(deck, args.deliver_to)
    print(f"Published a {slides}-slide deck.", flush=True)
    if timed_out:
        # The deck is real -- `verified_deck` opened it -- so this stays a
        # success, but the run was killed on the way and whatever it would have
        # done after publishing is gone. Saying so is the difference between a
        # partial delivery and a silent one.
        print(f"Timed out: killed after {args.timeout}s, after this deck was published.", flush=True)
    print(f"Deck: {deck}", flush=True)
    print(f"MEDIA: {delivered if delivered is not None else deck}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
