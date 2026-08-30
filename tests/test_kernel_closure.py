"""The kernel imports nothing else -- checked by running it, not by reading it.

`the kernel stands alone` is an import-linter contract, and it reads the static
import graph. Two ways of reaching a module do not appear in that graph: an
``importlib.import_module`` on a name built at runtime, and a package face that
resolves attributes lazily through ``__getattr__`` (PEP 562), which this repo
uses for ``raven.memory_engine``. Either would let the kernel pull a shelf in
with the contract still green.

So this imports the kernel set in a subprocess of its own and asks what actually
landed in ``sys.modules``. The set is read from the contract rather than
restated, so a module seated in the kernel later is covered the moment it is.

The claim being pinned is the one CONTEXT.md makes about the raven-core wheel:
that this set is a closure, shippable on its own.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _kernel_modules() -> list[str]:
    """The contract's own source list: ["raven.contracts", "raven.home", ...]."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = data["tool"]["importlinter"]["contracts"]
    kernel = next(c for c in contracts if c["name"] == "the kernel stands alone")
    return sorted(kernel["source_modules"])


def test_the_contract_still_names_a_kernel() -> None:
    """A renamed contract would make the closure test below import nothing."""
    modules = _kernel_modules()

    assert len(modules) >= 4, modules
    assert all(m.startswith("raven.") for m in modules), modules


def test_the_kernel_pulls_in_no_other_raven_module() -> None:
    modules = _kernel_modules()
    program = "\n".join(
        [
            "import sys",
            *(f"import {m}" for m in modules),
            f"KERNEL = tuple({modules!r})",
            "loaded = sorted(m for m in sys.modules if m == 'raven' or m.startswith('raven.'))",
            "print('\\n'.join(m for m in loaded if m != 'raven' and not m.startswith(KERNEL)))",
        ]
    )

    run = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=REPO,
    )

    assert run.returncode == 0, run.stderr
    strays = [line for line in run.stdout.splitlines() if line.strip()]
    assert strays == [], f"importing the kernel pulled in shelves, so the raven-core wheel is not a closure: {strays}"
