"""Child entry for rule C3: import a package's factory modules and report the first failure.

Usage: ``python -m evolver.validator._import_probe --root <dir> [--root <dir> ...] -- <module> ...``.
Each root is prepended to ``sys.path``. Exit 0 when every module imports; otherwise
print ``<module>: <traceback>`` to stdout and exit 1.
"""

from __future__ import annotations

import importlib
import sys
import traceback


def main(argv: list[str]) -> int:
    roots: list[str] = []
    modules: list[str] = []
    args = iter(argv)
    for arg in args:
        if arg == "--root":
            roots.append(next(args))
        elif arg == "--":
            modules.extend(args)
    sys.path[:0] = roots
    for module in modules:
        try:
            importlib.import_module(module)
        except BaseException:  # noqa: BLE001 - any import-time failure is the finding
            print(f"{module}: {traceback.format_exc(limit=3)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
