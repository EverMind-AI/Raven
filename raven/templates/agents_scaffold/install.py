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


def main() -> int:
    python = os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable
    # The roster command is split on whitespace when it is spawned, so a
    # spacey interpreter or folder path can never be addressed -- refuse
    # loudly rather than pin a row the dispatcher would fail on.
    for label, spelled in (("the interpreter path", python), ("this folder's path", str(HERE))):
        if any(c.isspace() for c in spelled):
            raise SystemExit(
                f"error: {label} '{spelled}' contains whitespace; the roster command is split on "
                "whitespace, so the row could not be addressed -- move the folder or point "
                "SUBAGENT_PYTHON at a space-free interpreter"
            )

    row = json.loads((HERE / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        value = row.get(field)
        if isinstance(value, str):
            row[field] = value.replace("{SUBAGENT_DIR}", str(HERE)).replace("{PYTHON}", python)

    from raven.config.update_subagents import add_third_party_subagent

    add_third_party_subagent(row)
    print(f"registered {row['name']}: {row['command']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
