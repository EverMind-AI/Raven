"""Every path, environment variable and helper name in the prompt, read back off the code.

`tests/ppt/_skill_claims.py` checks the skill, and only the skill. Everything else that
reaches a model -- the workspace templates the context builder injects on every turn,
the tool descriptions, the strings the tools and stages hand back, and the docstrings of
the helper modules written into the author's build directory -- had no reader at all,
and two of them had been wrong for long enough to survive a directory rename: `TOOLS.md`
sent the build script to `ppt_projects/<project>/build/build.py` after the layout had
become `deck/build/`, and the subagent launcher looked for the finished deck in
`exports/` after `Project.exports_dir` had become `out/`.

Nothing here is written down twice. The directory names come off `Project`'s own
properties, the environment variables off the assignments in the script runner, the tool
names off `build_ppt_tools`, and the helper symbols off an `ast` walk of the source the
asset service emits -- so renaming a directory breaks this file's expectations with it
rather than leaving the gate agreeing with a stale document.

What it deliberately does not do is read prose for meaning. A claim about behaviour
("the build refuses X") is not checkable here; what is checkable is that every name the
prose uses is a name the code answers to.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("pptx")

ROOT = Path(__file__).resolve().parents[2]

# The launcher and its README sit beside the package rather than inside it, and they
# carry the same layout claims -- the `exports/` bug lived in `run.py`. Present in the
# monorepo, absent from a standalone checkout of the package.
WRAPPER = ROOT.parent
WRAPPER_FILES = ("README.md", "subagent.json", "run.py", "install.py", ".env.example")

# The shipped config and the template that overrides it. Deliberately not in
# WRAPPER_FILES: those are read as prose for the name checks, and these two are read
# as data by the launcher checks at the foot of this file.
WRAPPER_CONFIG = WRAPPER / "config.json"
WRAPPER_ENV_EXAMPLE = WRAPPER / ".env.example"

# Names a path segment used to be. Nothing in the code can supply these -- they are gone
# from it, which is the point -- so they are listed, and `test_the_retired_names_are_not
# _in_use` fails if one of them ever becomes a real directory again and turns this list
# into a lie.
RETIRED_SEGMENTS = ("ppt_projects", "exports")

# Packages whose string constants are what a model reads back: the tool replies, the
# stage guidance, the message on a finding, and what the ingest, the render and the
# template menu say when they cannot do something.
TALKING_PACKAGES = (
    "tools",
    "stages",
    "backends/script",
    "services/gates",
    "services/measure",
    "services/publish",
    "services/template",
    "services/ingest",
    "services/render",
)

# A slash-bearing token: a bare word is prose ("the build directory"), a word with a
# child under it is a path. `<name>` and `*` appear inside documented paths.
_PATH = re.compile(r"(?<![\w./-])([A-Za-z_][\w.-]*(?:/[\w.@*<>-]+)+)")
_ENV = re.compile(r"\bPPT_[A-Z0-9_]+\b")
_HELPER_MODULE = re.compile(r"(?<![\w./-])(ppt_[a-z0-9_]+)\b")
_QUALIFIED = re.compile(r"(?<![\w./-])(ppt_[a-z0-9_]+)\.([A-Za-z_]\w*)")
_REFERENCE_DOC = re.compile(r"\breferences/[\w.-]+\.md\b")
# The idiom the documents use to say what a module holds: "`ppt_icons.py` (`add_icon`,
# `find_icons`, `ICON_NAMES`)". Only the backticked words in the bracket are names; the
# prose between them is prose.
_MODULE_CONTENTS = re.compile(r"`(ppt_[a-z0-9_]+)\.py`[^(]{0,40}\(([^)]*)\)", re.DOTALL)
_BACKTICKED = re.compile(r"`([A-Za-z_]\w*)`")
# Below this the idiom above has stopped matching and the check has quietly emptied.
_MIN_NAMES_LISTED = 6

# Suffixes that make `ppt_layout.py` a filename rather than a symbol lookup.
_EXTENSIONS = frozenset({"py", "md", "json", "pptx", "pdf", "png", "txt"})

# Three documents quote the wrong path on purpose, to say it is the wrong one ("a bare
# `build/build.py` lands somewhere the build does not look"). That idiom is the only
# thing that exempts a path here, and it has to sit right in front of it.
_COUNTEREXAMPLE = re.compile(r"\bbare\b", re.IGNORECASE)
_COUNTEREXAMPLE_REACH = 32


# ---------------------------------------------------------------------------
# Truth, taken from the code
# ---------------------------------------------------------------------------


def _project() -> object:
    from raven.ppt.contracts.project import Project

    return Project(workspace=Path("/workspace"), slug="deck")


def _layout_dirs() -> set[str]:
    """Every directory the deck keeps things in, relative to the workspace.

    Discovered from `Project`'s properties rather than listed, so a directory added
    there is covered here without anyone remembering to add it.
    """
    from raven.ppt.contracts.project import Project

    project = _project()
    found: set[str] = set()
    for name, member in vars(Project).items():
        if not isinstance(member, property):
            continue
        value = getattr(project, name)
        if isinstance(value, Path) and project.workspace in value.parents:
            found.add(value.relative_to(project.workspace).as_posix())
    return found


def _known_dirs() -> set[str]:
    """The layout's directories, plus the one `provision` adds inside the build dir."""
    from raven.ppt.services.assets import script_helpers

    dirs = _layout_dirs()
    project = _project()
    build = project.build_dir.relative_to(project.workspace).as_posix()
    return dirs | {f"{build}/{script_helpers.REFERENCE_DIRNAME}"}


def _layout_segments() -> set[str]:
    """The words that put a slash-bearing token in scope.

    Off `_known_dirs` rather than `_layout_dirs`, so `references/tables.md` is in scope
    too: the documents live at `deck/build/references/`, and a link written without that
    prefix resolves against the workspace, where there is no such directory.
    """
    return {segment for path in _known_dirs() for segment in path.split("/")}


def _env_names_the_runner_sets() -> set[str]:
    """The `env[...] = ...` keys in the script runner: what the author's program can read."""
    from raven.ppt.backends.script import runner

    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "env"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                found.add(target.slice.value)
    return found


def _env_names_anything_reads() -> set[str]:
    """Every `PPT_*` name read through `os.environ` / `os.getenv`, in the package and in
    the modules it emits. A documented variable nothing sets is a defect; one that is
    read but set elsewhere (the font overrides) is not."""
    from raven.ppt.services.assets import script_helpers
    from raven.ppt.services.template import compose

    sources = [path.read_text(encoding="utf-8") for path in sorted((ROOT / "raven" / "ppt").rglob("*.py"))]
    sources.extend(body for name, body in script_helpers.script_helper_files().items() if name.endswith(".py"))
    sources.append(compose.helper_source())

    found: set[str] = set()
    # The launcher routes its own variables into config keys rather than reading them
    # through `os.environ`, so what it knows about is the names its source spells out.
    for name in WRAPPER_FILES:
        path = WRAPPER / name
        if name.endswith(".py") and path.is_file():
            found.update(_ENV.findall(path.read_text(encoding="utf-8")))
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - an emitted module that does not parse
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if _is_environ(node.value) and isinstance(node.slice.value, str):
                    found.add(node.slice.value)
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                target = node.func
                if not isinstance(target, ast.Attribute):
                    continue
                reads = target.attr == "getenv" or (target.attr == "get" and _is_environ(target.value))
                if reads and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
    return {name for name in found if name.startswith("PPT_")}


def _is_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _tools() -> list:
    from raven.ppt.tools.assembly import build_ppt_tools

    tools = build_ppt_tools(Path("/workspace"))
    assert tools, "no ppt tools were registered, so nothing below is being checked"
    return tools


def _helper_modules() -> dict[str, str]:
    """Module name -> source, for every module written beside the author's script."""
    from raven.ppt.backends.script.workspace import TEMPLATE_HELPER
    from raven.ppt.services.assets import script_helpers
    from raven.ppt.services.template import compose

    modules = {
        name[: -len(".py")]: body for name, body in script_helpers.script_helper_files().items() if name.endswith(".py")
    }
    modules[TEMPLATE_HELPER[: -len(".py")]] = compose.helper_source()
    return modules


def _top_level_names(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return found


# ---------------------------------------------------------------------------
# The text that reaches a model
# ---------------------------------------------------------------------------


def _string_literals(source: str) -> str:
    """Every string constant in a module, joined. This is the mechanical stand-in for
    "what a tool or a stage hands back": the payloads, the refusals and the hints are
    all string constants, and reading them this way needs no list of which ones."""
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover
        return ""
    return "\n".join(
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def corpus() -> dict[str, str]:
    """Named pieces of text that reach a model, so a failure can say which one."""
    from raven.ppt.services.assets import script_helpers

    pieces: dict[str, str] = {}

    # Copied into the workspace by `sync_workspace_templates` and injected on every turn
    # by `ContextBuilder.BOOTSTRAP_FILES` / `context_engine.segments.render`.
    for path in sorted((ROOT / "raven" / "templates").glob("*.md")):
        pieces[f"raven/templates/{path.name}"] = path.read_text(encoding="utf-8")
    assert pieces, "no workspace templates were found, so none are being checked"

    skill = ROOT / "raven" / "memory_engine" / "skills" / "ppt-script-authoring"
    pieces["SKILL.md"] = (skill / "SKILL.md").read_text(encoding="utf-8")
    pieces.update(script_helpers.reference_files())

    for tool in _tools():
        pieces[f"{tool.name}.description"] = tool.description
        pieces[f"{tool.name}.parameters"] = json.dumps(tool.parameters, ensure_ascii=False)

    # The helper modules are written into the build directory whole, so their own text is
    # the reference the author reads.
    for name, source in _helper_modules().items():
        pieces[f"{name}.py"] = source

    # The tools' and stages' replies, the messages the gates and the measurements put on
    # a finding, and what the ingest, the render and the template menu say when they
    # cannot do something: all of them are string constants, and taking every constant in
    # these packages needs no list of which ones reach a model.
    #
    # `contracts` and `profiles` are deliberately not here. They are the vocabulary rather
    # than the text: `Project.exports_dir` documents itself by naming the directory it is
    # *not*, and `profiles/registry.py` names the tools of two routes that are declared
    # and unimplemented -- neither reaches a model, and both would read as defects.
    for where in TALKING_PACKAGES:
        for path in sorted((ROOT / "raven" / "ppt" / where).glob("*.py")):
            pieces[f"raven/ppt/{where}/{path.name}"] = _string_literals(path.read_text(encoding="utf-8"))

    if (WRAPPER / "subagent.json").is_file():
        for name in WRAPPER_FILES:
            path = WRAPPER / name
            assert path.is_file(), f"{path} is named here and does not exist"
            body = path.read_text(encoding="utf-8")
            pieces[f"subagents/raven-ppt/{name}"] = _string_literals(body) if name.endswith(".py") else body

    return pieces


def _fail(problems: list[str], what: str) -> None:
    assert not problems, f"{what}:\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_the_retired_names_are_not_in_use() -> None:
    """Guards the list above: a retired name that came back would make it a lie."""
    assert not (set(RETIRED_SEGMENTS) & _layout_segments())


def test_every_path_named_is_a_path_the_layout_has() -> None:
    """A slash-bearing token rooted at a layout directory has to name a directory the
    layout actually has -- not merely sit somewhere under one.

    This is the check that would have caught `build/build.py` surviving the move to
    `deck/build/`: the token's own first segment is what puts it in scope, so a document
    naming a real directory at the wrong depth fails while prose that happens to use the
    word `build` does not. The directory tested is the token's parent, which leaves one
    unknown component at the end for the file or the generated directory the path names --
    `deck/review/build_failures/failure-001` is a run's own, and cannot be listed here.
    """
    known = _known_dirs()
    segments = _layout_segments()
    problems = []
    for where, text in corpus().items():
        for match in _PATH.finditer(text):
            token = match.group(1).rstrip("/")
            if token.split("/")[0] not in segments:
                continue
            if token in known or token.rsplit("/", 1)[0] in known:
                continue
            if _COUNTEREXAMPLE.search(text[max(0, match.start() - _COUNTEREXAMPLE_REACH) : match.start()]):
                continue
            problems.append(f"{where}: {token!r} is not in {sorted(known)}")
    _fail(problems, "these paths are not where the layout puts them")


def test_no_retired_directory_name_is_still_documented() -> None:
    problems = []
    for where, text in corpus().items():
        for name in RETIRED_SEGMENTS:
            for match in re.finditer(rf"(?<![\w-]){re.escape(name)}/", text):
                problems.append(f"{where}: {name!r} is a retired directory name, at offset {match.start()}")
    _fail(problems, "these documents still name a directory that no longer exists")


def test_every_environment_variable_named_is_one_the_code_uses() -> None:
    known = _env_names_the_runner_sets() | _env_names_anything_reads()
    assert "PPT_OUTPUT" in known, "the runner's assignments were not found, so this check is empty"
    problems = []
    for where, text in corpus().items():
        for name in set(_ENV.findall(text)) - known:
            problems.append(f"{where}: {name} is set by nothing and read by nothing")
    _fail(problems, "these environment variables do not exist")


def test_the_program_is_told_every_variable_the_runner_sets() -> None:
    """The other direction: a variable the runner starts setting and no document mentions
    is one the author will never reach for."""
    text = "\n".join(corpus().values())
    missing = {name for name in _env_names_the_runner_sets() if name not in text}
    assert not missing, f"the runner sets {sorted(missing)} and no model-facing text names them"


def test_every_ppt_name_is_a_registered_tool_or_an_emitted_module() -> None:
    known = {tool.name for tool in _tools()} | set(_helper_modules())
    problems = []
    for where, text in corpus().items():
        for name in set(_HELPER_MODULE.findall(text)) - known:
            problems.append(f"{where}: {name!r} is neither a registered tool nor a module written beside the script")
    _fail(problems, "these names do not exist")


def test_every_helper_a_document_reaches_into_exists() -> None:
    """`ppt_layout.Box`, `ppt_template.clone_page`: the module has to hold the symbol."""
    modules = {name: _top_level_names(source) for name, source in _helper_modules().items()}
    problems = []
    for where, text in corpus().items():
        for module, symbol in set(_QUALIFIED.findall(text)):
            if module not in modules or symbol in _EXTENSIONS:
                continue
            if symbol not in modules[module]:
                problems.append(f"{where}: {module}.{symbol} does not exist")
    _fail(problems, "these helpers were renamed or never existed")


def test_every_name_a_document_lists_under_a_module_is_in_it() -> None:
    """ "`ppt_icons.py` (`add_icon`, `find_icons`, `ICON_NAMES`)" -- all three have to be
    in the module that is written out, not in the service that writes it."""
    modules = {name: _top_level_names(source) for name, source in _helper_modules().items()}
    listed = 0
    problems = []
    for where, text in corpus().items():
        for module, blob in _MODULE_CONTENTS.findall(text):
            if module not in modules:
                continue
            for symbol in _BACKTICKED.findall(blob):
                listed += 1
                if symbol not in modules[module]:
                    problems.append(f"{where}: {module} does not hold {symbol!r}")
    _fail(problems, "these names are listed under a module that does not hold them")
    assert listed >= _MIN_NAMES_LISTED, f"only {listed} names were checked; the documents changed shape"


def test_every_module_written_beside_the_script_is_named_somewhere() -> None:
    """A helper the author is never told about is a helper the author does not import."""
    text = "\n".join(corpus().values())
    missing = {name for name in _helper_modules() if name not in text}
    assert not missing, f"{sorted(missing)} are written into the build directory and no text names them"


def test_every_reference_document_named_is_one_that_ships() -> None:
    from raven.ppt.services.assets import script_helpers

    shipped = set(script_helpers.reference_files())
    assert shipped, "no reference documents were found, so this check is empty"
    problems = []
    for where, text in corpus().items():
        for named in set(_REFERENCE_DOC.findall(text)) - shipped:
            problems.append(f"{where}: {named!r} is not one of {sorted(shipped)}")
    _fail(problems, "these reference documents do not ship")


# ---------------------------------------------------------------------------
# The launcher's rendered config, read back off the runtime that loads it
# ---------------------------------------------------------------------------
#
# `config.json` makes claims to raven the same way the documents above make claims
# to a model, and they went unread for the same reason: nothing compared the shipped
# provider name against the registry that resolves it. Naming it `custom` cost a live
# 5-page deck $15.61, all of it uncached input, because `find_gateway` returns on its
# first step -- a `provider_name` that maps to a gateway spec -- and `custom` is one,
# so the api_base detection that would have found OpenRouter never ran, and
# `custom.supports_prompt_caching` is False. A name the registry has no spec for
# resolves to nothing and lets that detection through.


_needs_wrapper = pytest.mark.skipif(
    not (WRAPPER / "subagent.json").is_file(),
    reason="the launcher and its config are absent from a standalone checkout of the package",
)


def _shipped_env() -> dict[str, str]:
    """`.env.example`'s assignments, so these tests move with the template."""
    return {
        key.strip(): value.strip()
        for key, _, value in (
            line.partition("=") for line in WRAPPER_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        )
        if key.strip().startswith("PPT_") and value.strip()
    }


@pytest.fixture
def launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The launcher module, repointed at tmp_path so no real `.env`, host config,
    model catalog or state root can leak into a test."""
    spec = importlib.util.spec_from_file_location("raven_ppt_launcher", WRAPPER / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERE", tmp_path)
    monkeypatch.setattr(module, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(module, "HOST_CONFIG", tmp_path / "no-host-config.json")
    monkeypatch.setattr(module, "MODEL_CATALOG", tmp_path / "no-model-catalog.json")
    for name in ("PPT_API_KEY", "PPT_MODEL", "PPT_API_BASE", "PPT_SERPER_API_KEY", "PPT_JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return module


def _render(launcher, tmp_path: Path, **overrides: str) -> dict:
    """The shipped `config.json`, rendered the way `run.py` renders it, on a `.env`
    holding what `.env.example` ships plus a key that is a placeholder, not a secret."""
    values = {"PPT_API_KEY": "sk-placeholder-not-a-real-key", **_shipped_env(), **overrides}
    (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    return json.loads(launcher.render_config(WRAPPER_CONFIG).read_text(encoding="utf-8"))


@_needs_wrapper
def test_the_shipped_provider_name_leaves_prompt_caching_on(launcher, tmp_path: Path) -> None:
    """The regression test for the $15.61 deck. Everything else here explains it."""
    from raven.providers.prompt_cache import accepts_cache_control

    defaults = _render(launcher, tmp_path)["agents"]["defaults"]
    assert accepts_cache_control(defaults["model"], addressed_to=defaults["provider"]), (
        f"a request addressed to {defaults['provider']!r} may not carry cache_control, "
        f"so every call bills {defaults['model']} at full input price"
    )


@_needs_wrapper
def test_the_provider_block_is_keyed_by_the_name_that_selects_it(launcher, tmp_path: Path) -> None:
    """`agents.defaults.provider` selects a block by name, so a rename that reaches one
    and not the other leaves the runtime with no block at all."""
    config = _render(launcher, tmp_path)
    assert list(config["providers"]) == [config["agents"]["defaults"]["provider"]]


@_needs_wrapper
def test_the_shipped_provider_name_does_not_shadow_endpoint_detection(launcher, tmp_path: Path) -> None:
    """Step 1 of `find_gateway` returns on a name that maps to a gateway spec, so a
    shipped name has to be one the registry does not carry -- then the api_base and
    api_key steps run and place the endpoint themselves."""
    from raven.providers.registry import find_by_name, find_gateway

    config = _render(launcher, tmp_path)
    name = config["agents"]["defaults"]["provider"]
    base = config["providers"][name]["apiBase"]
    key = config["providers"][name]["apiKey"]

    assert find_by_name(name) is None, f"{name!r} resolves to a spec, which is what suppresses detection"
    detected = find_gateway(provider_name=name, api_key=key, api_base=base)
    assert detected is not None and detected.supports_prompt_caching, (
        f"{base} was not placed from the shipped name alone"
    )
    # The failure this replaced, stated as the mechanism rather than as a value: the
    # old name answered step 1 and the endpoint was never looked at.
    shadowed = find_gateway(provider_name="custom", api_key=key, api_base=base)
    assert shadowed is not None and shadowed.name == "custom"


@_needs_wrapper
def test_an_endpoint_the_registry_cannot_place_is_left_to_the_model_id(launcher, tmp_path: Path) -> None:
    """A base nobody claims resolves to no spec at all, which falls through to the
    model's own name rather than to a spec that answers no."""
    from raven.providers.prompt_cache import accepts_cache_control
    from raven.providers.registry import find_gateway

    defaults = _render(launcher, tmp_path, PPT_API_BASE="https://llm.internal.example.com/v1")["agents"]["defaults"]
    assert find_gateway(api_base="https://llm.internal.example.com/v1") is None
    assert accepts_cache_control(defaults["model"], addressed_to=defaults["provider"])


@_needs_wrapper
def test_the_context_window_is_the_catalogs_number_verbatim(launcher, tmp_path: Path, monkeypatch) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"version": 3, "fetched_at": 0.0, "models": {"vendor/tiny": {"context_length": 262144}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "MODEL_CATALOG", catalog)
    defaults = _render(launcher, tmp_path, PPT_MODEL="vendor/tiny")["agents"]["defaults"]
    assert defaults["contextWindowTokens"] == 262144


@_needs_wrapper
def test_a_catalog_that_is_not_there_leaves_the_shipped_window_alone(launcher, tmp_path: Path) -> None:
    shipped = json.loads(WRAPPER_CONFIG.read_text(encoding="utf-8"))["agents"]["defaults"]["contextWindowTokens"]
    assert _render(launcher, tmp_path)["agents"]["defaults"]["contextWindowTokens"] == shipped


@_needs_wrapper
def test_a_model_the_catalog_does_not_list_leaves_the_shipped_window_alone(
    launcher, tmp_path: Path, monkeypatch
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"version": 3, "fetched_at": 0.0, "models": {}}), encoding="utf-8")
    monkeypatch.setattr(launcher, "MODEL_CATALOG", catalog)
    shipped = json.loads(WRAPPER_CONFIG.read_text(encoding="utf-8"))["agents"]["defaults"]["contextWindowTokens"]
    rendered = _render(launcher, tmp_path, PPT_MODEL="vendor/unlisted")
    assert rendered["agents"]["defaults"]["contextWindowTokens"] == shipped


@_needs_wrapper
def test_the_shipped_reasoning_effort_is_medium(launcher, tmp_path: Path) -> None:
    """A constant nothing in `.env` supplies, so it stays a config choice rather than
    becoming a derivation with one possible answer."""
    assert _render(launcher, tmp_path)["agents"]["defaults"]["reasoningEffort"] == "medium"


@_needs_wrapper
def test_the_launcher_imports_nothing_outside_the_standard_library() -> None:
    """`run.py` runs under whatever python3 the host has, which may not be the one the
    runtime is installed under -- so every derivation in it reads JSON off disk rather
    than importing the module that owns the same answer."""
    tree = ast.parse((WRAPPER / "run.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, "no imports were found, so this check is empty"
    assert not imported - sys.stdlib_module_names


class _FinishedChild:
    """A child that starts, says nothing and exits 0 -- enough for `main` to get
    past the launch, which is the only thing the two tests below are asking."""

    def __init__(self) -> None:
        self.stdout: list[str] = []
        self.returncode = 0

    def wait(self) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover - only the watchdog calls this
        pass


def _launch(launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str, *material: Path) -> str:
    """Run `main` far enough to build the child's argv, and return the prompt in it.

    Material is named in the task text because that is the only channel left: the
    launcher has no argv slot for it, and `materials_from_prompt` is what reads the
    absolute paths back out.
    """
    shipped = "\n".join(f"{k}={v}" for k, v in _shipped_env().items())
    (tmp_path / ".env").write_text(f"PPT_API_KEY=sk-placeholder-not-a-real-key\n{shipped}", encoding="utf-8")
    recorded: list[list[str]] = []

    def _popen(argv, **_kwargs):
        recorded.append(list(argv))
        return _FinishedChild()

    monkeypatch.setattr(launcher.subprocess, "Popen", _popen)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--job",
            "t",
            "--config",
            str(WRAPPER_CONFIG),
            "--no-deliver",
            "--task",
            " ".join([task, *(str(path) for path in material)]),
        ],
    )
    launcher.main()
    assert recorded, "the child was never started, so the run did not reach the launch"
    argv = recorded[0]
    return argv[argv.index("-m") + 1]


@_needs_wrapper
def test_a_run_with_no_material_reaches_the_launch_and_is_told_to_gather(
    launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck whose material has to be found is a deck, not an error. The refusal
    this replaced turned "a 5-page guide to using GitLab" into an exit code."""
    prompt = _launch(launcher, tmp_path, monkeypatch, "make a 5-page guide to using GitLab")
    assert "No material staged for this run" in prompt
    for tool in ("web_search", "web_fetch", "ppt_fetch"):
        assert tool in prompt, f"{tool} finds material and the prompt does not name it"
    # The sentence that would forbid everything: it points at `materials/`, which on
    # this branch is empty.
    assert "Use only files under" not in prompt


@_needs_wrapper
def test_a_run_with_material_keeps_the_sentence_that_fences_it(
    launcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: staged files still carry the sourcing rule that stops the deck
    being grounded in the model's own log."""
    source = tmp_path / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")
    prompt = _launch(launcher, tmp_path, monkeypatch, "build a deck", source)
    assert "Material staged for this run" in prompt
    assert "Use only files under" in prompt
    assert "notes.md" in prompt


@_needs_wrapper
def test_both_entry_points_hand_the_agent_the_same_material_block(launcher, tmp_path: Path) -> None:
    """The one guard against the two paths drifting apart again.

    The launcher is standard-library-only and lives outside this package, so the
    text it builds cannot be imported into `raven.acp.materials` or shared with it.
    Nothing but this comparison can keep them equal, and they had already diverged
    once in both directions: over ACP a run with no material was refused outright
    while the launcher gathered, and a declared `template` was honoured over ACP
    while the launcher raised on it.
    """
    from raven.acp import materials

    mats, out = tmp_path / "materials", tmp_path / "out"
    for staged in ([], [("/elsewhere/notes.md", mats / "notes.md")]):
        expected = launcher.material_section(staged, mats) + (
            f" Compile the deck under {out}/ and end your final reply with the MEDIA line naming it."
        )
        assert materials.describe(staged, mats, out) == expected


@_needs_wrapper
def test_both_entry_points_refuse_a_declared_template(launcher, tmp_path: Path) -> None:
    """Same declaration, same answer. The channel is `ppt_template` on both."""
    from raven.acp import materials

    task = '```raven-ppt\n{"materials": ["/a.md"], "template": "/house.pptx"}\n```'
    with pytest.raises(SystemExit):
        launcher.inputs_from_prompt(task)
    with pytest.raises(materials.StagingError):
        materials.inputs_from_prompt(task)
    # And the same declaration without a path is a style name on both, not a channel.
    styled = '```raven-ppt\n{"materials": ["/a.md"], "template": "minimal"}\n```'
    assert launcher.inputs_from_prompt(styled) == materials.inputs_from_prompt(styled) == ["/a.md"]
