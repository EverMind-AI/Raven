#!/usr/bin/env python3
"""Register this product in the host raven's roster.

Runs under an interpreter that imports raven -- inside this repo, the project
venv -- and writes through ``raven.config.update_subagents``, the same pinned
surface the vendored installers use. ``{PYTHON}`` and ``{SUBAGENT_DIR}`` in
``subagent.json`` resolve against this interpreter and this file's location,
so moving the folder and re-running is the whole migration story. A live
raven holds the roster it read at startup; restart it afterwards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    row = json.loads((HERE / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        value = row.get(field)
        if isinstance(value, str):
            row[field] = value.replace("{SUBAGENT_DIR}", str(HERE)).replace("{PYTHON}", sys.executable)

    from raven.config.update_subagents import add_third_party_subagent

    add_third_party_subagent(row)
    print(f"registered {row['name']}: {row['command']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
