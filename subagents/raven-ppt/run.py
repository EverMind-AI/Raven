#!/usr/bin/env python3
"""Host-side launcher for the raven-ppt sub-agent.

The agent used to run in a container, for one reason that has since stopped
being true: the image carried LibreOffice and the rendering fonts because the
host had none of them. This host has `soffice` and 35 CJK-capable faces, so the
checkout runs directly and the 1.6 GB image, its registry and the docker SDK all
drop out of the path.

What the container contributed that still has to happen somewhere, and now
happens here:

- **Only the named files are visible.** Raven's CLI sub-agent contract
  substitutes `{prompt}`, `{prompt_file}` and `{agent_id}` and nothing else, so
  a dispatching agent has no argv slot for material -- naming absolute paths in
  the task text is the only channel. Those files are copied into the job's own
  `materials/`, and `tools.restrictToWorkspace` keeps the agent inside the job
  directory, so it reads the copies and cannot wander the host for more.
- **The key never touches a published file.** `config.json` ships and holds no
  secret; `.env` supplies it and the two are merged into a rendered config under
  the state root at launch.
- **stdout is the result.** Raven's CLI backend uses a child's whole output as
  the sub-agent's reply, so the agent's own narration goes to the job log and
  stdout carries only what was produced and where.

Success is a published deck rather than an exit code: a `MEDIA:` line whose file
exists and opens as a pptx with slides in it. That test was written because the
container exited 139 on fully successful runs; it survives the move because it
checks the artifact rather than the process.
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
    ".csv", ".tsv", ".json", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp",
}
_PATH_RE = re.compile(r"/[^\s'\"`,;()<>\[\]]+")

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

# Named as in the other three launchers so the four can be compared by grep. The
# LLM key is the only secret this folder takes, and the only one whose absence is
# fatal: there is nothing here to degrade to.
REQUIRED_SECRETS = ("PPT_API_KEY",)


def host_config() -> dict:
    """The host raven's config, or an empty dict when there is none to read."""
    try:
        return json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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


def materials_from_prompt(text: str) -> list[str]:
    """Absolute paths named in the prompt that exist and look like documents."""
    found: list[str] = []
    for match in _PATH_RE.findall(text):
        candidate = match.rstrip(".")
        path = Path(candidate)
        if path.suffix.lower() in MATERIAL_SUFFIXES and path.is_file() and candidate not in found:
            found.append(candidate)
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

    Returns ``(root, materials, exports, staged)``, where ``staged`` pairs each
    source with the path it was copied to -- the prompt is built from that pairing
    rather than re-deriving it, so the agent is told where each file actually is.
    ``root`` is handed to raven as the workspace, which is what puts both
    sub-directories inside the fence.

    A file that cannot be copied stops the run rather than being skipped. Two
    reasons: a deck built from part of its material is wrong in a way nothing
    downstream can see, and an uncaught copy error would put a Python traceback
    on stderr -- which the caller reads as this agent's reply. ``--material``
    reaches here without passing the prompt scan's ``is_file`` test, so this is
    the only place those paths are checked at all.

    A second source with the same basename is suffixed rather than allowed to
    overwrite the first. That collision loses material exactly as silently as a
    skipped copy would: ``copyfile`` succeeds, and the prompt would list two
    entries resolving to one file, so the agent believes it holds two documents
    and grounds the deck twice in one of them.
    """
    root = STATE_ROOT / "jobs" / job
    mats, exports = root / "materials", root / "exports"
    for directory in (mats, exports):
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
    return root, mats, exports, staged


def render_config(source: Path) -> Path:
    """`config.json` plus the secret, written where the runtime may read it.

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
        taken = inherit_llm(config, host_config())
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
    rendered.write_text(json.dumps(config, indent=2), encoding="utf-8")
    rendered.chmod(0o600)
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


def deck_mtimes(exports: Path) -> dict[Path, float]:
    """Every pptx under ``exports`` with its mtime, for the before/after compare."""
    found: dict[Path, float] = {}
    for path in exports.rglob("*.pptx"):
        try:
            found[path] = path.stat().st_mtime
        except OSError:
            continue
    return found


def verified_deck(exports: Path, tail: list[str], before: dict[Path, float]) -> tuple[Path | None, int]:
    """The deck the agent published, or ``(None, 0)``.

    The directory is searched rather than the transcript parsed, because neither
    half of the `MEDIA:` line can be trusted to arrive intact. Measured
    2026-08-20 on the first host run: the CLI hard-wraps its output at the
    terminal width, so the announced path was split across three lines and the
    marker line ended up empty -- and the deck itself was written to
    ``exports/<project>/<name>.pptx``, a level below where a basename lookup
    against ``exports`` would find it.

    So the launcher trusts what it owns. Every valid deck this run wrote or
    rewrote is a candidate; the announcement only chooses between them, and the
    newest wins when it names none of them.

    Scoped to this run rather than to the directory, which is the whole point of
    ``before``. ``--job`` is the sub-agent's ``agent_id``, minted once per
    conversation and replayed on every later turn, so one job directory
    accumulates every turn's exports while ``tail`` holds only this turn's
    output. Accepting any deck in it would let a turn that published nothing --
    a model error, the watchdog kill, a render that never finished -- match no
    announcement, fall through to the newest, and hand back an earlier turn's
    file as this turn's result.
    """
    candidates = [
        (path, count) for path, mtime in sorted(deck_mtimes(exports).items())
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
    ap.add_argument("--material", action="append", default=[], help="Source document; repeatable")
    ap.add_argument("--session", help="Enable multi-turn: reuse this key across runs")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--timeout", type=int, default=3600)
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

    materials = unique_sources(list(args.material) + materials_from_prompt(task))
    if not materials:
        raise SystemExit(
            "error: no source material. Pass --material, or name existing absolute "
            "paths to the source documents in the task text."
        )

    root, mats, exports, staged = stage(args.job, materials)

    global _LOG_FILE, _VERBOSE
    _VERBOSE = args.verbose
    _LOG_FILE = root / "launcher.log"

    task += "\n\n# Material staged for this run\n" + "\n".join(
        f"- {Path(source).name} (from {source}) -> {target}" for source, target in staged
    ) + (
        f"\nUse only files under {mats} as factual source material. "
        f"Compile the deck under {exports}/ and end your final reply with the "
        "MEDIA line naming it."
    )

    rendered = render_config(Path(args.config).resolve())
    argv = [
        str(raven), "agent",
        "--config", str(rendered),
        "--workspace", str(root),
        "--no-markdown",
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
    before = deck_mtimes(exports)
    tail: list[str] = []
    timed_out = False
    # Inherited wholesale on purpose: a proxy the host needs to reach the model
    # was plumbed by hand for the container, and here it simply arrives.
    # The CLI renders through a console that wraps at the terminal width, which
    # breaks long paths across lines in both the log and anything reading it.
    child_env = dict(os.environ, COLUMNS="400", TERM="dumb")
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
            tail.append(line)
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        rc = None
    finally:
        watchdog.cancel()

    deck, slides = verified_deck(exports, tail, before)
    log(f"[run] exit_code={rc} timed_out={timed_out} deck={deck} slides={slides}")

    if deck is None:
        print(f"FAILED: no verifiable deck was published. See {_LOG_FILE}", flush=True)
        return 1
    if rc != 0:
        log(f"[run] note: agent exited {rc} after publishing; deck verified, treating as success")

    delivered = None if args.no_deliver else deliver(deck, args.deliver_to)
    print(f"Published a {slides}-slide deck.", flush=True)
    print(f"Deck: {deck}", flush=True)
    print(f"MEDIA: {delivered if delivered is not None else deck}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
