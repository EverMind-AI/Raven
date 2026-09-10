"""Agents scaffolding subcommands -- owns the ``agents_app`` Typer instance.

``raven agents new <name>`` instantiates the packaged scaffold templates
(``raven/templates/agents_scaffold/``) into a fresh agent folder: the roster
manifest, the launcher, the birth-certificate config, and a plugin package
carrying one tool and one hook factory. A folder dropped under the raven
home's ``agents/`` tree is discovered without registration, which is why
``--register`` defaults to off.

One name derives every identity the templates spell four ways: the kebab
machine id (``my-agent``), the Title-Case display name (``My-Agent``), the
python package (``my_agent``), and the env-var prefix (``MY_AGENT``, the
``api_key_var`` convention). Instantiation is a string replacement of those
four spellings over every template file and path.

After the write the command walks the rest of the closed loop itself:
doctor (the generated manifest re-validated, the plugin manifest re-parsed,
every .py compiled, discovery really scanned), then smoke (spawn the
discovered row's command and exchange one ACP ``initialize`` frame -- the S1
gate-B recipe), then the next-steps card. A launcher that refuses loudly for
want of a key is reported as the fail-closed design working, not as a broken
chain.

``commands.py`` wires this in through :func:`register`, the same door the
top-level commands use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console

# soft_wrap so an absolute target path prints as one line instead of being
# folded at terminal width mid-path.
console = Console(soft_wrap=True)

agents_app = typer.Typer(help="Create and manage agents")

_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# The display name is substituted verbatim into the JSON manifest, so the
# charset is fenced here rather than escaped there: a quote, backslash or
# control character would corrupt every file that spells the name.
_DISPLAY = re.compile(r"^[A-Za-z][A-Za-z0-9 -]*$")

# The template tree spells the agent's name in four case-distinct forms; the
# scaffold replaces each with the same form derived from the requested name.
# Case-distinct means no replacement can produce another token, so the order
# of the four replacements cannot cascade.
_PLACEHOLDER_FOLDER = "my-agent"
_PLACEHOLDER_DISPLAY = "My-Agent"
_PLACEHOLDER_PACKAGE = "my_agent"
_PLACEHOLDER_ENV_PREFIX = "MY_AGENT"

# The engine variant's template subtree, and the text rewrites that turn the
# agent-side templates into their wheel-shaped spelling. Applied BEFORE the
# placeholder replacement, so they are written in the templates' own token
# spellings; each anchor is pinned by the test family, so template drift
# breaks a test rather than producing a silently wrong scaffold.
_ENGINE_SUBTREE = "engine/"
_ENGINE_REWRITES = (
    # run.py: name the wheel's import package so the launcher refuses loudly
    # when it is not importable (the raven-design launcher's own convention).
    ('ENGINE_PACKAGE = ""', 'ENGINE_PACKAGE = "my_agent_engine"'),
    # subagent.json: declare the engine so discovery lists the row disabled,
    # naming the missing wheel, until the wheel is importable.
    (
        '"maxOutputChars": 60000,',
        '"maxOutputChars": 60000,\n  "engine": {\n    "package": "my_agent_engine",\n    "wheel": "my-agent-engine"\n  },',
    ),
    # Everywhere else: the harness id and package move from the plugins/
    # directory spelling to the engine spelling.
    ("my-agent-flow", "my-agent-engine"),
    ("my_agent_flow", "my_agent_engine"),
)

# The shipped agent folders (their manifests capitalize the same ids), plus
# the retired transitional spelling of the research folder -- configs written
# during its rename window may still hold rows under either name. Built-in
# seed names are not listed here: they come from the package module below, so
# a new seed stays covered without this list knowing about it.
_RESERVED_AGENT_IDS = frozenset(
    {
        "raven-code",
        "raven-design",
        "raven-oncall",
        "raven-ppt",
        "raven-research",
        "raven-research-ng",
    }
)


def _display_name(name: str) -> str:
    """``My-Agent`` for ``my-agent``: each kebab word capitalized."""
    return "-".join(part.capitalize() for part in name.split("-"))


def _env_prefix(name: str) -> str:
    """The env-var prefix the launcher templates read (``MY_AGENT`` for ``my-agent``).

    Derived through ``api_key_var`` rather than a local upper-case: that
    function owns the folder-name-to-variable convention (including the
    ``raven-`` prefix strip), and a second derivation here would be free to
    disagree with what onboarding prompts for.
    """
    from raven.agent.subagent.vendored_agents import api_key_var

    suffix = "_API_KEY"
    return api_key_var(name)[: -len(suffix)]


def _replace_tokens(text: str, name: str, display: str, package: str, env_prefix: str) -> str:
    return (
        text.replace(_PLACEHOLDER_FOLDER, name)
        .replace(_PLACEHOLDER_DISPLAY, display)
        .replace(_PLACEHOLDER_PACKAGE, package)
        .replace(_PLACEHOLDER_ENV_PREFIX, env_prefix)
    )


def _render_text(
    source: str, rewrites: tuple[tuple[str, str], ...], name: str, display: str, package: str, env_prefix: str
) -> str:
    for anchor, replacement in rewrites:
        source = source.replace(anchor, replacement)
    return _replace_tokens(source, name, display, package, env_prefix)


def _templates_root() -> Path:
    import raven

    return Path(raven.__file__).resolve().parent / "templates" / "agents_scaffold"


def _template_files() -> list[tuple[Path, str]]:
    """Every template file as ``(absolute path, relative posix path)``, sorted.

    Enumerated from disk rather than kept as a list: the template tree is the
    single source of what a scaffold contains, and a file added there ships
    without this module knowing its name.
    """
    root = _templates_root()
    files: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or rel.suffix == ".pyc" or rel.name == ".DS_Store":
            continue
        files.append((path, rel.as_posix()))
    return files


def _reserved_reason(candidate: str) -> str | None:
    """Why ``candidate`` cannot name a new agent, or ``None`` when it can."""
    from raven.agent.subagent.builtin_agents import BUILTIN_AGENT_NAMES, LEGACY_AGENT_ALIASES

    if candidate.lower() in _RESERVED_AGENT_IDS:
        return f"'{candidate}' is a shipped agent's name"
    # Case-folded like every other fence here: `is_builtin_agent_name` is
    # exact (the merge keys are exact), so 'RAVEN' would scaffold an enabled
    # impostor sitting beside the built-in 'Raven' on the dispatch table.
    builtin = {n.casefold() for n in BUILTIN_AGENT_NAMES} | {a.casefold() for a in LEGACY_AGENT_ALIASES}
    if candidate.casefold() in builtin:
        return f"'{candidate}' is a built-in agent's name"
    return None


def _discovered_identities(root: Path) -> set[str]:
    """Case-folded names and folder ids already discovered under ``root``.

    The tree is the scaffold's whole premise -- a folder is a roster row with
    no registration -- so a name check that read only config would let two
    enabled rows share one name (a --display collision, or a folder id
    written in another casing).
    """
    from raven.agent.subagent.vendored_agents import discover_product_rows

    if not root.is_dir():
        return set()
    taken: set[str] = set()
    for row in discover_product_rows(root):
        taken.add(str(row.name).casefold())
        cwd = str(getattr(row, "cwd", "") or "")
        if cwd:
            taken.add(Path(cwd).name.casefold())
    return taken


def _roster_names() -> set[str]:
    """Names the host roster already holds, lower-cased for the conflict check.

    An unreadable or malformed roster reads as empty rather than refusing: the
    scaffold itself writes no config, and ``--register``'s own write path still
    validates everything it is about to store.
    """
    from raven.config.update_subagents import get_agents

    try:
        rows = get_agents()
    except Exception:
        return set()
    return {str(row.get("name") or "").lower() for row in rows if isinstance(row, dict)}


def _refuse(message: str, *, code: int = 1) -> None:
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code)


def _sweep_stale_staging(root: Path) -> None:
    """Remove staging residue (``.*.partial-*``) a killed run left under ``root``.

    The in-process cleanup below cannot run after a SIGKILL, and discovery now
    skips dot-directories rather than advertising them -- but the bytes should
    not sit there forever, so every later scaffold run tidies its landing tree.
    """
    if not root.is_dir():
        return
    for stale in root.glob(".*.partial-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)


def _write_tree(
    target: Path,
    files: list[tuple[Path, str]],
    name: str,
    display: str,
    package: str,
    env_prefix: str,
    rewrites: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Instantiate ``files`` under ``target`` atomically; return the written rel paths.

    Assembled in a staging directory beside the target (same filesystem, so
    the final rename is atomic) and renamed as a whole: a failure mid-write
    leaves nothing at the target, never a partial agent the next scan would
    discover and advertise.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.partial-", dir=target.parent))
    written: list[str] = []
    try:
        for source, rel in files:
            rendered_rel = _replace_tokens(rel, name, display, package, env_prefix)
            dest = staging / rendered_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                _render_text(source.read_text(encoding="utf-8"), rewrites, name, display, package, env_prefix),
                encoding="utf-8",
            )
            written.append(rendered_rel)
        # Re-checked at the last moment because POSIX rename onto an existing
        # empty directory succeeds silently -- the refusal must not depend on
        # the earlier check still holding.
        if target.exists():
            raise FileExistsError(str(target))
        # mkdtemp is deliberately 0700; the landed folder should match a
        # hand-made or packaged-copied sibling, so open it up before the move.
        staging.chmod(0o755)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return written


def _resolved_python() -> str:
    """The interpreter the roster command resolves to: ``SUBAGENT_PYTHON``
    then this process's own -- discovery's own order
    (``vendored_agents._resolved_python``). One resolver for the whitespace
    guard AND the register writer, so the value the guard checked is the
    value that lands in the roster; two spellings here would let a clean
    ``SUBAGENT_PYTHON`` pass the gate while ``--register`` pinned a spacey
    ``sys.executable``.
    """
    return os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable


def _register_row(target: Path) -> str:
    """Pin the generated manifest into the host roster; the generated ``install.py``'s logic.

    The same two fields, the same substitutions, the same write door
    (``add_third_party_subagent``) -- so a ``--register`` run and a later
    ``python install.py`` cannot produce different rows for one folder.
    """
    row = json.loads((target / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        value = row.get(field)
        if isinstance(value, str):
            row[field] = value.replace("{SUBAGENT_DIR}", str(target)).replace("{PYTHON}", _resolved_python())

    from raven.config.update_subagents import add_third_party_subagent

    add_third_party_subagent(row)
    return str(row.get("name"))


def _doctor(
    target: Path, display: str, here: bool, engine_dir: Path | None = None
) -> tuple[list[str], object | None, object | None]:
    """The post-write self-check; returns (failures, the discovered row or None).

    Four verdicts, each printed as its own line: the roster manifest through
    the same pydantic model the loader uses, the plugin manifest through the
    same parser the registry uses, every generated .py compiled, and a real
    discovery scan. The scan is rooted at the target's own parent, so the
    answer is about this folder -- a home tree elsewhere can neither fake it
    ready nor hide it. Each failure message carries its fix.
    """
    import py_compile

    failures: list[str] = []

    try:
        from raven.config.schema import ThirdPartyAcpSubagentConfig

        row_raw = json.loads((target / "subagent.json").read_text(encoding="utf-8"))
        validated = ThirdPartyAcpSubagentConfig.model_validate(row_raw)
        console.print(f"  subagent.json: valid ({validated.name}, kind={validated.kind})")
    except Exception as exc:
        failures.append(
            f"subagent.json failed roster-schema validation ({exc}); fix the file -- the row schema "
            "is raven.config.schema.ThirdPartyAcpSubagentConfig -- or delete the folder and re-run"
        )
        console.print("  subagent.json: INVALID")

    # The wheel-shaped harness keeps its manifest inside the engine package
    # (importlib.resources reads it from there); the directory-shaped one
    # keeps it under the agent's plugins/ tree.
    manifest_root = engine_dir if engine_dir is not None else target / "plugins"
    manifests = sorted(manifest_root.glob("*/raven-plugin.toml"))
    if not manifests:
        failures.append(
            f"no */raven-plugin.toml was generated under {manifest_root}; without it the agent has no "
            "tool or hook contributions -- delete the folder and re-run, or restore the manifest by hand"
        )
        console.print("  raven-plugin.toml: MISSING")
    for manifest_path in manifests:
        try:
            from raven.plugins.manifest import PluginManifest

            manifest = PluginManifest.from_toml_path(manifest_path)
            contributes = manifest.contributes
            console.print(
                f"  raven-plugin.toml: parsed ({manifest.id}: "
                f"{len(contributes.tools)} tool(s), {len(contributes.hooks)} hook(s))"
            )
        except Exception as exc:
            failures.append(
                f"{manifest_path.relative_to(manifest_root)} failed manifest parsing ({exc}); "
                "fix the file or delete the folder and re-run"
            )
            console.print("  raven-plugin.toml: INVALID")

    py_files = sorted(target.rglob("*.py"))
    broken = []
    # Bytecode goes to a scratch directory so the check does not litter the
    # fresh folder with __pycache__ (py_compile refuses a devnull cfile).
    with tempfile.TemporaryDirectory(prefix="agents-doctor-") as scratch:
        for index, path in enumerate(py_files):
            try:
                py_compile.compile(str(path), cfile=str(Path(scratch) / f"{index}.pyc"), doraise=True, quiet=1)
            except py_compile.PyCompileError as exc:
                broken.append(f"{path.relative_to(target)}: {exc.msg}")
    if broken:
        failures.append(
            "generated python does not compile -- "
            + "; ".join(broken)
            + "; fix the file(s) or delete the folder and re-run"
        )
        console.print(f"  py_compile: {len(broken)} of {len(py_files)} file(s) BROKEN")
    else:
        console.print(f"  py_compile: {len(py_files)} file(s) OK")

    from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

    # Matched by the row's cwd, not by name: a same-named folder elsewhere in
    # the tree must not answer for this one (the write-time fence refuses new
    # duplicates, but the doctor must not misattribute pre-existing ones).
    row = next((r for r in discover_product_rows(target.parent) if str(getattr(r, "cwd", "")) == str(target)), None)
    state = product_state(target.parent).get(display) if row is not None else None
    if row is None or state is None:
        failures.append(
            f"discovery scanned {target.parent} and found no row named '{display}'; "
            "the manifest may be unreadable -- check subagent.json"
        )
        console.print(f"  discovery: no row named '{display}'")
    elif not state.ready and state.kind == "engine" and engine_dir is not None:
        # The designed state for a fresh engine scaffold, not a failure: the
        # row is listed, disabled, and names the wheel it waits for.
        console.print(f"  discovery: row '{display}' listed, disabled as designed ({state.detail})")
        console.print(f"  after `pip install -e {engine_dir}` (into raven's environment), the row turns ready")
    elif not state.ready:
        failures.append(f"discovery lists '{display}' but readiness is {state.kind!r}: {state.detail}")
        console.print(f"  discovery: row listed but NOT ready ({state.kind}: {state.detail})")
    else:
        console.print(f"  discovery: row '{display}' listed, enabled={row.enabled}, readiness=ready")
    if here:
        console.print(
            "  note: this scan read ./agents directly; a live raven reads $RAVEN_HOME/agents first, "
            "and an existing home tree shadows this checkout's tree"
        )
    return failures, row, state


def _smoke_handshake(row: object) -> tuple[str, str]:
    """Spawn the discovered row's command, send one ACP ``initialize``, read one frame.

    Returns ``(status, detail)`` with status ``green`` / ``refused`` /
    ``failed``. The S1 gate-B recipe against the row discovery itself
    resolved, so the smoke exercises exactly the command line a dispatch
    would. ``refused`` is the launcher's own loud no-key exit -- the
    fail-closed design working, so the caller reports it and moves on rather
    than failing the run.
    """
    import subprocess
    import threading

    frame = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )
        + "\n"
    )
    command = str(getattr(row, "command", "")).split()
    cwd = str(getattr(row, "cwd", "") or "") or None
    timeout = max(float(getattr(row, "ready_timeout_ms", 120000)) / 1000.0, 10.0)

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_reader: threading.Thread | None = None
    try:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None  # noqa: S101 - Popen(PIPE) contract
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        first_line: dict[str, str] = {}
        stderr_lines: list[str] = []

        def _read_stdout() -> None:
            first_line["line"] = proc.stdout.readline()

        def _drain_stderr() -> None:
            # Drained on its own thread so a chatty launcher can never fill
            # the pipe and deadlock the wait on stdout.
            for chunk in proc.stderr:
                stderr_lines.append(chunk.rstrip("\n"))

        stdout_reader = threading.Thread(target=_read_stdout, daemon=True)
        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stdout_reader.start()
        stderr_reader.start()
        stdout_reader.join(timeout)
        line = (first_line.get("line") or "").strip()
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if stderr_reader is not None:
        stderr_reader.join(2)
    stderr_text = "\n".join(stderr_lines)

    if line:
        try:
            reply = json.loads(line)
        except ValueError:
            return "failed", f"the launcher answered something that is not JSON-RPC: {line[:200]}"
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(result, dict) and "protocolVersion" in result:
            # A handshake that answered while the plugin registry failed to
            # build is a broken agent behind a green light: the boot continued
            # WITHOUT this agent's own tools, hooks and plugin memory backend.
            # The registry's own WARNING reaches the drained stderr.
            activation = next((ln.strip() for ln in stderr_lines if "plugin activation failed" in ln), "")
            if activation:
                return "failed", (
                    "the handshake answered but plugin activation failed -- the agent is running "
                    f"WITHOUT its own tools and hooks: {activation[:300]}"
                )
            info = result.get("agentInfo") or {}
            served = f"{info.get('name', 'agent')} {info.get('version', '')}".strip()
            return "green", f"initialize answered (protocolVersion={result['protocolVersion']}, {served})"
        return "failed", f"the reply is not a legal InitializeResponse: {line[:200]}"

    refused = proc.returncode not in (None, 0) and (
        "_API_KEY is not set" in stderr_text
        or "no provider key to inherit from" in stderr_text
        # The generated launcher's own pre-exec refusal when its declared
        # engine wheel is not importable (the raven-design convention).
        or "engine wheel is not installed" in stderr_text
    )
    if refused:
        refusal = next(
            (ln.strip() for ln in stderr_lines if ln.strip().startswith("error:")),
            stderr_lines[-1].strip() if stderr_lines else "the launcher exited before the handshake",
        )
        return "refused", refusal
    tail = "\n".join(ln for ln in stderr_lines[-6:])
    return "failed", (f"no reply within {int(timeout)}s (launcher exit code {proc.returncode}); stderr tail:\n{tail}")


@agents_app.command()
def new(
    name: str = typer.Argument(..., help="Machine id for the new agent: kebab-case (e.g. my-agent), never renamed"),
    display: str | None = typer.Option(None, "--display", help="Display name; defaults to Title-Case of NAME"),
    here: bool = typer.Option(False, "--here", help="Scaffold into ./agents/NAME instead of $RAVEN_HOME/agents/NAME"),
    kind: str = typer.Option("acp", "--kind", help="Agent protocol; only 'acp' today ('cli' is reserved for v2)"),
    register: bool = typer.Option(
        False,
        "--register/--no-register",
        help="Pin the row into the host roster (the generated install.py's logic); a home-tree folder is discovered without it",
    ),
    smoke: bool = typer.Option(True, "--smoke/--no-smoke", help="Spawn-and-handshake check after scaffolding"),
    engine_wheel: bool = typer.Option(
        False,
        "--engine-wheel",
        help="Ship the harness as an installable wheel skeleton (the raven-design shape) instead of a plugins/ directory",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the would-be tree and write nothing"),
) -> None:
    """Scaffold a new agent from the packaged templates."""
    if not _KEBAB.fullmatch(name):
        _refuse(
            f"'{name}' is not kebab-case: lowercase letters and digits in words separated by "
            "single hyphens, starting with a letter (e.g. my-agent)",
            code=2,
        )
    if kind == "cli":
        _refuse("--kind cli is reserved: the cli template lands in v2; only acp is supported today", code=2)
    if kind != "acp":
        _refuse(f"unknown --kind '{kind}': only acp is supported today", code=2)
    if display is not None and not _DISPLAY.fullmatch(display):
        _refuse(
            f"--display {display!r} is not a display name: letters, digits, spaces and hyphens only, "
            'starting with a letter (e.g. "Demo Deluxe"). The value is substituted verbatim into the '
            "JSON manifest, so quotes, backslashes or control characters would corrupt it",
            code=2,
        )

    shown = display or _display_name(name)
    package = name.replace("-", "_")
    env_prefix = _env_prefix(name)

    for candidate in dict.fromkeys((name, shown)):
        if reason := _reserved_reason(candidate):
            _refuse(f"{reason}; choose another name")

    roster = _roster_names()
    for candidate in dict.fromkeys((name, shown)):
        if candidate.lower() in roster:
            _refuse(
                f"the host roster already holds a row named '{candidate}' "
                "(raven agents is one namespace across casings); choose another name"
            )

    if here:
        target = Path.cwd() / "agents" / name
    else:
        from raven.home import raven_home

        target = raven_home() / "agents" / name
    if target.exists():
        _refuse(f"{target} already exists; there is no --force, move it away or choose another name")

    # The roster command template is split on whitespace -- the documented
    # platform constraint discovery and the ACP client share -- so a landing
    # path or interpreter path with whitespace can never be addressed as a
    # command. Refused whole here; quoting argv would be a seam across both
    # tokenizers and is not this command's to open.
    python = _resolved_python()
    for label, spelled in (("the agent folder path", str(target)), ("the python interpreter path", python)):
        if any(c.isspace() for c in spelled):
            _refuse(
                f"{label} '{spelled}' contains whitespace: the roster command template is split on "
                "whitespace, so the launcher could not be addressed; choose a location without spaces"
            )

    taken = _discovered_identities(target.parent)
    for candidate in dict.fromkeys((name, shown)):
        if candidate.casefold() in taken:
            _refuse(
                f"the agents tree at {target.parent} already holds an agent named '{candidate}' "
                "(raven agents is one namespace across casings); choose another name"
            )

    # The engine skeleton is a development project: inside a checkout (--here)
    # it lands on the shipped engines' shelf, otherwise in the caller's own
    # working directory, where `pip install -e` consumes it.
    engine_dir = None
    if engine_wheel:
        engine_dir = (Path.cwd() / "plugins-dist" / f"{name}-engine") if here else (Path.cwd() / f"{name}-engine")
        if engine_dir.exists():
            _refuse(f"{engine_dir} already exists; there is no --force, move it away or choose another name")

    everything = _template_files()
    if not everything:
        _refuse(f"no scaffold templates found under {_templates_root()}; this install is broken")
    engine_files = [(src, rel[len(_ENGINE_SUBTREE) :]) for src, rel in everything if rel.startswith(_ENGINE_SUBTREE)]
    agent_files = [
        (src, rel)
        for src, rel in everything
        if not rel.startswith(_ENGINE_SUBTREE) and not (engine_wheel and rel.startswith("plugins/"))
    ]
    rewrites = _ENGINE_REWRITES if engine_wheel else ()

    if dry_run:
        console.print(f"would create {target} ({len(agent_files)} files):")
        for _source, rel in agent_files:
            console.print(f"  {_replace_tokens(rel, name, shown, package, env_prefix)}")
        if engine_dir is not None:
            console.print(f"would create {engine_dir} ({len(engine_files)} files):")
            for _source, rel in engine_files:
                console.print(f"  {_replace_tokens(rel, name, shown, package, env_prefix)}")
        console.print("dry run: nothing written")
        return

    # Engine first, agent second: the agent folder is the discoverable half,
    # so a kill between the two writes leaves an inert engine checkout rather
    # than a discovered agent whose declared skeleton nothing will provide.
    # Sweeps live here, on the write path only: a dry run promises zero disk
    # mutation, and deleting stale residue is a mutation.
    _sweep_stale_staging(target.parent)
    engine_written: list[str] = []
    if engine_dir is not None:
        _sweep_stale_staging(engine_dir.parent)
        try:
            engine_written = _write_tree(engine_dir, engine_files, name, shown, package, env_prefix)
        except OSError as exc:
            _refuse(f"cannot write {engine_dir}: {exc}")
    try:
        written = _write_tree(target, agent_files, name, shown, package, env_prefix, rewrites=rewrites)
    except OSError as exc:
        if engine_dir is not None:
            # The agent half failed, so the fresh skeleton has no owner.
            shutil.rmtree(engine_dir, ignore_errors=True)
        _refuse(f"cannot write {target}: {exc}")
    if engine_dir is not None:
        console.print(f"1/4 wrote {engine_dir} ({len(engine_written)} files):")
        for rel in engine_written:
            console.print(f"  {rel}")
    console.print(f"1/4 wrote {target} ({len(written)} files):")
    for rel in written:
        console.print(f"  {rel}")

    console.print("2/4 doctor:")
    failures, row, state = _doctor(target, shown, here, engine_dir=engine_dir)
    if failures:
        console.print(f"[red]doctor: {len(failures)} check(s) failed[/red]")
        for message in failures:
            console.print(f"  - {message}")
        console.print(f"the folder is left at {target} for inspection; delete it to retry")
        raise typer.Exit(1)

    if not smoke:
        console.print("3/4 smoke: skipped (--no-smoke)")
    else:
        status, detail = _smoke_handshake(row)
        if status == "green":
            console.print(f"3/4 smoke: handshake GREEN -- {detail}")
        elif status == "refused":
            console.print("3/4 smoke: the launcher refused to start -- its fail-closed design, not a broken chain:")
            console.print(f"  {detail}")
            if "engine wheel" in detail and engine_dir is not None:
                console.print(
                    f"  after `pip install -e {engine_dir}` (into raven's environment), the handshake will run"
                )
            else:
                console.print(
                    "  provide a key (see .env.example) or configure a host provider, then the handshake will run"
                )
        else:
            console.print(f"[red]3/4 smoke: FAILED[/red] -- {detail}")
            console.print(f"the folder is left at {target} for inspection; delete it to retry")
            raise typer.Exit(1)

    if register:
        if state is not None and not state.ready:
            # A pinned row is a complete config row: it overrides the
            # discovered one wholesale, is never readiness-checked, and would
            # put an enabled name on the roster the dispatching model picks
            # and then fails on. Refusing (rather than pinning enabled=false)
            # keeps discovery the owner of readiness: the moment the wheel
            # installs, the discovered row turns ready on its own, while a
            # pinned false would stay off until someone hand-edits config.
            console.print(f"[red]--register refused:[/red] the row is not ready ({state.detail})")
            console.print("  the folder stays discovered; register after the row turns ready (python install.py)")
            raise typer.Exit(1)
        console.print(f"registered {_register_row(target)} in the host roster")

    console.print("4/4 next:")
    console.print(
        "  restart the gateway to make a live raven re-read the tree (the folder is discovered as a seed row)"
    )
    console.print(f"  fill in the description/owns TODOs in {target / 'subagent.json'}, then dispatch a first turn")
    if engine_dir is not None:
        console.print(
            f"  install the engine to turn the row ready: pip install -e {engine_dir} (where raven is installed)"
        )
    else:
        console.print("  optional: python install.py pins the row (needed only out-of-tree, or to customize the row)")


def register(app: typer.Typer) -> None:
    """Attach the ``agents`` group to ``app``."""
    app.add_typer(agents_app, name="agents")
