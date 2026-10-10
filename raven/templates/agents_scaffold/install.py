#!/usr/bin/env python3
"""Register this agent in the host raven's roster.

Runs under an interpreter that imports raven -- inside this repo, the project
venv -- and writes through ``raven.config.update_subagents``, the same pinned
surface the retired vendored installers used. ``{PYTHON}`` and ``{SUBAGENT_DIR}`` in
``subagent.json`` resolve against ``SUBAGENT_PYTHON`` (falling back to this
interpreter -- discovery's own resolution order, so a pinned row and a
discovered row name the same interpreter) and this file's location, so moving
the folder and re-running is the whole migration story. A live raven holds
the roster it read at startup; restart it afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _resolve(value: str, *, python: str, quote: bool) -> str:
    """``value`` with ``{PYTHON}`` and ``{SUBAGENT_DIR}`` substituted for this folder."""
    try:
        from raven.utils.commands import resolve_subagent_command
    except ImportError:
        # Older launchers cannot keep a whitespace path together after substitution.
        for placeholder, path in (("{SUBAGENT_DIR}", str(HERE)), ("{PYTHON}", python)):
            if quote and placeholder in value and any(char.isspace() for char in path):
                raise SystemExit(
                    f"Cannot register a command path containing whitespace with this Raven version: {path!r}. "
                    "Upgrade Raven or use paths without whitespace."
                )
        return value.replace("{SUBAGENT_DIR}", str(HERE)).replace("{PYTHON}", python)
    return resolve_subagent_command(value, python=python, subagent_dir=str(HERE), quote=quote)


def main() -> int:
    python = os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable

    row = json.loads((HERE / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        value = row.get(field)
        if isinstance(value, str):
            row[field] = _resolve(value, python=python, quote=field != "cwd")

    from raven.config.update_subagents import add_third_party_subagent

    add_third_party_subagent(row)
    print(f"registered {row['name']}: {row['command']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
