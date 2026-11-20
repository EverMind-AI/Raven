"""The compacting strategy stays a self-contained unit.

Inward: nothing in ``history/compacting`` reaches into the assembler or the
loop -- the shared pieces it needs (the positional tool pairing, the pinned
lookup, the reply headroom) live in modules that belong to neither. Outward:
importing the context engine package, or the marker keys the loop strips,
loads no strategy at all.

Read from source for the first, observed in a fresh interpreter for the second:
a shared ``sys.modules`` would already hold whatever an earlier test imported.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import raven.context_engine as context_engine_pkg

ROOT = Path(context_engine_pkg.__file__).parent
COMPACTING = ROOT / "history" / "compacting"
STRATEGY = "raven.context_engine.history.compacting"


def _imported(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _loaded_after(snippet: str, prefixes: tuple[str, ...]) -> list[str]:
    program = textwrap.dedent(snippet) + textwrap.dedent(
        f"""
        import json as _json
        import sys as _sys

        print("LOADED" + _json.dumps(sorted(n for n in _sys.modules if n.startswith({prefixes!r}))), flush=True)
        """
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=False, timeout=180)
    marker = result.stdout.rfind("LOADED")
    assert marker >= 0, f"the child reported nothing (exit {result.returncode})\n{result.stderr}"
    return json.loads(result.stdout[marker + len("LOADED") :].partition("\n")[0])


def test_the_strategy_package_exists_and_is_not_empty() -> None:
    assert {path.stem for path in COMPACTING.glob("*.py")} >= {
        "ledger",
        "trigger",
        "retention",
        "pruner",
        "checkpoint",
        "writer",
        "store",
        "runtime",
    }


def test_the_marker_keys_load_no_strategy() -> None:
    assert not [name for name in _imported(ROOT / "history" / "markers.py") if name.startswith("raven")]
    assert _loaded_after("import raven.context_engine.history.markers", (STRATEGY,)) == []


def test_importing_the_context_engine_loads_no_strategy() -> None:
    assert _loaded_after("import raven.context_engine", (STRATEGY,)) == []
