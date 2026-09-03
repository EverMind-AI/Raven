"""The dependency direction, checked instead of documented.

`raven_ppt/` is layered contracts <- services <- backends <- stages <- profiles
<- tools, and the layering is the whole design: the three routes exist so that
ingest, measurement, the gates, rendering and publication can be written once
and shared, and a service that reaches forward into a stage has quietly made
itself part of one route. That is exactly what happened to the predecessor --
two routes ended up with two text-measurement modules and two publish paths --
and it happened without anyone deciding to, one import at a time. An import is
easy to add and hard to notice, so this is a test.
"""

from __future__ import annotations

import ast
from pathlib import Path

PPT_ROOT = Path(__file__).resolve().parents[1] / "plugins-dist" / "ppt-engine" / "raven_ppt"

# Left to right: each layer may import itself and anything to its left.
LAYERS = ["contracts", "services", "backends", "stages", "profiles", "tools"]

# Nothing under raven/ppt/ may import these, on any layer. The engine is
# callable from a script, a test or a different host; reaching into the agent
# loop or the TUI would make it callable from exactly one.
FORBIDDEN_EVERYWHERE = ("raven.agent.loop", "raven.spine", "raven.tui_rpc", "raven.channels")

# The vendored converter is reached through an adapter, never directly, so the
# snapshot can be re-pinned without hunting call sites.
VENDOR = "raven_ppt.vendor"
VENDOR_ADAPTER_LAYER = "backends"


def _modules() -> list[tuple[Path, str]]:
    found = []
    for path in sorted(PPT_ROOT.rglob("*.py")):
        rel = path.relative_to(PPT_ROOT)
        if rel.parts and rel.parts[0] == "vendor":
            continue
        found.append((path, rel.parts[0] if len(rel.parts) > 1 else ""))
    return found


def _package(path: Path) -> str:
    """The dotted package a file's relative imports resolve against."""
    return ".".join(("raven_ppt", *path.relative_to(PPT_ROOT).parts[:-1]))


def _absolute(module: str | None, level: int, package: str) -> str | None:
    """`from ..stages import x`, written the way the rules below are written.

    A relative import reaches exactly as far as an absolute one and is invisible to a
    check that only reads `node.module`: `from ..stages import compose` inside a
    service is the same layer violation as `import raven_ppt.stages.compose`, and it
    used to pass. `raven/ppt/` happens to have none today, which is the argument for
    resolving them rather than against it -- the first one somebody writes is the one
    that would not have been caught.
    """
    if not level:
        return module
    parts = package.split(".")
    if level > 1:
        parts = parts[: -(level - 1)]
    if not parts:
        return None  # reaches above raven/, which nothing here does
    return ".".join([*parts, module]) if module else ".".join(parts)


def _imports(path: Path) -> list[tuple[str, int]]:
    """Every module this file imports, relative ones resolved to their absolute name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package(path)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _absolute(node.module, node.level, package)
            if resolved:
                out.append((resolved, node.lineno))
    return out


def test_a_relative_import_is_read_as_the_module_it_reaches() -> None:
    """The resolution itself, because every rule below is only as good as it.

    `raven/ppt/` is all absolute imports today, so nothing in the repository would
    fail if this quietly returned nothing at all -- which is exactly the state the
    check was in before.
    """
    package = "raven_ppt.services.assets"
    assert _absolute("shapes", 1, package) == "raven_ppt.services.assets.shapes"
    assert _absolute("stages", 2, package) == "raven_ppt.services.stages"
    assert _absolute("tools.outline", 3, package) == "raven_ppt.tools.outline"
    assert _absolute(None, 1, package) == package
    assert _absolute("json", 0, package) == "json"


def test_the_package_a_file_resolves_against_is_its_own_directory() -> None:
    assert _package(PPT_ROOT / "services" / "assets" / "icons.py") == "raven_ppt.services.assets"
    assert _package(PPT_ROOT / "services" / "__init__.py") == "raven_ppt.services"


def test_ppt_package_is_present() -> None:
    assert PPT_ROOT.is_dir(), f"{PPT_ROOT} missing"
    assert _modules(), "no modules found to check"


def test_layers_only_depend_leftwards() -> None:
    offences = []
    for path, layer in _modules():
        if layer not in LAYERS:
            continue
        allowed = set(LAYERS[: LAYERS.index(layer) + 1])
        for module, line in _imports(path):
            if not module.startswith("raven_ppt."):
                continue
            target = module.split(".")[1]
            if target in LAYERS and target not in allowed:
                offences.append(f"{path.name}:{line} ({layer}) imports {target}: {module}")
    assert not offences, "imports running against the layer order:\n  " + "\n  ".join(offences)


def test_nothing_reaches_into_the_agent_loop_or_the_ui() -> None:
    offences = []
    for path, _ in _modules():
        for module, line in _imports(path):
            if module.startswith(FORBIDDEN_EVERYWHERE):
                offences.append(f"{path.relative_to(PPT_ROOT)}:{line} imports {module}")
    assert not offences, "PPT code must stay callable outside the agent:\n  " + "\n  ".join(offences)


def test_vendor_is_reached_only_through_its_adapter() -> None:
    offences = []
    for path, layer in _modules():
        for module, line in _imports(path):
            if module.startswith(VENDOR) and layer != VENDOR_ADAPTER_LAYER:
                offences.append(f"{path.relative_to(PPT_ROOT)}:{line} imports {module}")
    assert not offences, f"vendor is reached only from {VENDOR_ADAPTER_LAYER}/:\n  " + "\n  ".join(offences)


def test_contracts_hold_no_dependencies_of_their_own() -> None:
    allowed_prefixes = ("raven_ppt.contracts",)
    stdlib_ok = {
        "__future__",
        "abc",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "hashlib",
        "json",
        "math",
        "pathlib",
        "re",
        "types",
        "typing",
        "pydantic",
    }
    offences = []
    for path, layer in _modules():
        if layer != "contracts":
            continue
        for module, line in _imports(path):
            root = module.split(".")[0]
            if root in stdlib_ok or module.startswith(allowed_prefixes):
                continue
            offences.append(f"{path.name}:{line} imports {module}")
    assert not offences, "contracts stay data-only:\n  " + "\n  ".join(offences)
