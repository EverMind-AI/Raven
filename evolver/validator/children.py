"""Start the validator's own child entry points with this interpreter and this checkout.

``evolver`` lives beside the ``raven`` package in a source checkout and is not in the
wheel, so a child whose working directory is elsewhere finds it only through
``PYTHONPATH``. Children never write bytecode, so importing agent code leaves the
package snapshot byte-identical.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def child_env(base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    paths = [str(REPO_ROOT), *[p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def module_command(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", module, *args]
