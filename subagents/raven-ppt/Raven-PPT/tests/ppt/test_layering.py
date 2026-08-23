"""The dependency direction, checked instead of documented.

`raven/ppt/` is layered contracts <- services <- backends <- stages <- profiles
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

PPT_ROOT = Path(__file__).resolve().parents[2] / "raven" / "ppt"

# Left to right: each layer may import itself and anything to its left.
LAYERS = ["contracts", "services", "backends", "stages", "profiles", "tools"]

# Nothing under raven/ppt/ may import these, on any layer. The engine is
# callable from a script, a test or a different host; reaching into the agent
# loop or the TUI would make it callable from exactly one.
FORBIDDEN_EVERYWHERE = ("raven.agent.loop", "raven.spine", "raven.tui_rpc", "raven.channels")

# The vendored converter is reached through an adapter, never directly, so the
# snapshot can be re-pinned without hunting call sites.
VENDOR = "raven.ppt.vendor"
VENDOR_ADAPTER_LAYER = "backends"


def _modules() -> list[tuple[Path, str]]:
    found = []
    for path in sorted(PPT_ROOT.rglob("*.py")):
        rel = path.relative_to(PPT_ROOT)
        if rel.parts and rel.parts[0] == "vendor":
            continue
        found.append((path, rel.parts[0] if len(rel.parts) > 1 else ""))
    return found


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


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
            if not module.startswith("raven.ppt."):
                continue
            target = module.split(".")[2]
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
    allowed_prefixes = ("raven.ppt.contracts",)
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
