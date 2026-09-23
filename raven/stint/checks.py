"""The ledger of what a project runs for each check a playbook declares by description.

A leaf beside `verify` (which runs a check) and `bootstrap` (which detects what
a tree affords), and importing both, because the ledger needs both and neither
needs the ledger: `verify` reaching into `bootstrap` for the detection and
`bootstrap` reaching into `verify` for the check shape was the one mutual pair
this package had, and the import-cycle budget refused it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from raven.stint.backlog import STINT_DIR
from raven.stint.bootstrap import detect_checks
from raven.stint.verify import DEFAULT_TIMEOUT_SEC, CheckSpec

__all__ = ["CHECKS_FILE", "checks_path", "remember_check", "resolve_checks"]


CHECKS_FILE = "checks.json"
"""Where a project records what each declared check actually runs here.

Beside the backlog, under the directory named for the mode rather than for the
recipe that usually writes it: a backlog is the `stint` recipe's content, and
this is the mode's -- any `mode: stint` playbook that declares a check by
description needs somewhere to keep the answer, whether or not it also asked for
the recipe's layout.

Keyed ``<playbook>-<check>``. Not by check name alone: two playbooks may both
want a gate called `build` and mean different things by it, and the one that
arrived second would silently inherit the first one's command.
"""


def checks_path(project: Path) -> Path:
    return Path(project).expanduser() / STINT_DIR / CHECKS_FILE


def _ledger(project: Path) -> dict[str, dict[str, object]]:
    path = checks_path(project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_ledger(project: Path, rows: dict[str, dict[str, object]]) -> Path:
    path = checks_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def remember_check(project: Path, playbook: str, name: str, command: str, *, found: str = "person") -> Path:
    """Write down what this project runs for one of a playbook's declared checks."""
    rows = _ledger(project)
    rows[f"{playbook}-{name}"] = {
        "run": command,
        "from": found,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _write_ledger(project, rows)


def resolve_checks(project: Path, playbook: str, entries: Sequence[Any]) -> tuple[list[CheckSpec], list[str]]:
    """What each declared check runs here, and which ones nobody has answered.

    Resolved once per project and written down, not worked out per run: a command
    that is re-derived every time can change between two rounds of one stint
    without anybody having decided that it should, and a resolution that happens
    on every start is a resolution that can never stop to ask.

    Order: the playbook's own ``run`` if it has one -- a file that names its
    command is not asking a question -- then this project's ledger, then what the
    tree plainly affords. Anything still unanswered comes back as a name, for a
    caller to refuse over: a declared check that silently does not run is worse
    than no check, because the round reports a gate nobody measured.
    """
    rows = _ledger(project)
    afforded = {spec.name: spec for spec in detect_checks(project)}
    specs: list[CheckSpec] = []
    missing: list[str] = []
    for entry in entries:
        if literal := str(getattr(entry, "run", "") or "").strip():
            specs.append(_as_spec(entry, literal))
            continue
        recorded = rows.get(f"{playbook}-{entry.name}")
        if recorded and str(recorded.get("run") or "").strip():
            specs.append(_as_spec(entry, str(recorded["run"])))
            continue
        if (found := afforded.get(entry.name)) is not None:
            remember_check(project, playbook, entry.name, found.command, found="detected")
            specs.append(_as_spec(entry, found.command))
            continue
        missing.append(entry.name)
    return specs, missing


def _as_spec(entry: Any, command: str) -> CheckSpec:
    return CheckSpec(
        name=entry.name,
        command=command,
        timeout_sec=getattr(entry, "timeout_sec", DEFAULT_TIMEOUT_SEC),
        needs_display=bool(getattr(entry, "needs_display", False)),
    )
