"""Build the raven-core wheel: the kernel as its own distribution.

The roster is read from the "the kernel stands alone" import-linter contract
in pyproject.toml -- the same rows the closure guard reads -- so the wheel
cannot drift from the contract. The stage directory gets a generated
pyproject and a kernel __init__; the kernel modules are copied verbatim and
`uv build` does the rest.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

KERNEL_INIT = '''"""The Raven kernel: the papers, the spine, tracing and the home resolver.

Built by scripts/build_core_wheel.py from the same sources the full raven
wheel ships; the roster lives in pyproject.toml ("the kernel stands alone").
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("raven-core")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
'''

PYPROJECT = """\
[project]
name = "raven-core"
version = "{version}"
description = "The Raven kernel: the papers, the spine, tracing and home"
requires-python = ">=3.12"
dependencies = [
{deps}
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["raven"]
"""


def kernel_roster(pyproject: dict) -> list[str]:
    for contract in pyproject["tool"]["importlinter"]["contracts"]:
        if contract["name"] == "the kernel stands alone":
            return list(contract["source_modules"])
    raise SystemExit("pyproject.toml no longer names 'the kernel stands alone'")


def third_party_roots(files: list[Path]) -> set[str]:
    """Import roots outside the stdlib and raven itself, across the roster."""
    roots: set[str] = set()
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module or ""]
            else:
                continue
            roots |= {m.split(".")[0] for m in mods}
    return {r for r in roots if r and r not in sys.stdlib_module_names and r != "raven"}


def pinned_deps(pyproject: dict, roots: set[str]) -> list[str]:
    """The main distribution's pin for each kernel import root; a root the
    main dependency list does not pin is a hard error, not a guess."""
    specs = pyproject["project"]["dependencies"]
    out: list[str] = []
    for root in sorted(roots):
        want = root.replace("_", "-").lower()
        hits = [s for s in specs if re.split(r"[<>=!\[ ;]", s, 1)[0].strip().lower() == want]
        if not hits:
            raise SystemExit(f"kernel imports {root} but pyproject pins no such dependency")
        out.append(hits[0])
    return out


def stage(stage_dir: Path, roster: list[str], version: str, deps: list[str]) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    pkg = stage_dir / "raven"
    pkg.mkdir(parents=True)
    for module in roster:
        rel = Path(*module.split(".")[1:])
        src_dir, src_file = REPO / "raven" / rel, REPO / "raven" / rel.with_suffix(".py")
        if src_dir.is_dir():
            shutil.copytree(src_dir, pkg / rel, ignore=shutil.ignore_patterns("__pycache__"))
        elif src_file.is_file():
            shutil.copy2(src_file, pkg / rel.with_suffix(".py"))
        else:
            raise SystemExit(f"roster names {module} but raven/{rel} does not exist")
    (pkg / "__init__.py").write_text(KERNEL_INIT)
    dep_lines = "\n".join(f'    "{d}",' for d in deps)
    (stage_dir / "pyproject.toml").write_text(PYPROJECT.format(version=version, deps=dep_lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(REPO / "dist"))
    parser.add_argument("--stage-dir", default=str(REPO / "build" / "raven-core"))
    args = parser.parse_args()

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    roster = kernel_roster(pyproject)
    staged: list[Path] = []
    for module in roster:
        rel = Path(*module.split(".")[1:])
        target = REPO / "raven" / rel
        staged += sorted(target.rglob("*.py")) if target.is_dir() else [target.with_suffix(".py")]
    deps = pinned_deps(pyproject, third_party_roots(staged))

    stage_dir = Path(args.stage_dir)
    stage(stage_dir, roster, pyproject["project"]["version"], deps)
    run = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", args.out_dir],  # noqa: S607 -- uv off PATH, as the Makefile invokes it
        cwd=stage_dir,
    )
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
