"""Per-trial activation ledger + beacon.

Every runtime activation event (hook fire, skill injection mirror, code
beacon, presence assert) appends one JSON line to
``<workspace>/activation_ledger.jsonl``. Writes are best-effort: a ledger
failure must never affect the trial.

``activation_beacon`` is the mandatory one-liner inside every evolved
code path (design section 3, code class). It resolves the workspace from
``RAVEN_ACTIVATION_WORKSPACE`` and silently no-ops when unset, so
product runtime never pays for it.
"""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from pathlib import Path

LEDGER_FILENAME = "activation_ledger.jsonl"
WORKSPACE_ENV = "RAVEN_ACTIVATION_WORKSPACE"

_workspace_var: ContextVar[str | None] = ContextVar(
    "raven_activation_workspace", default=None)


def set_activation_workspace(workspace: "Path | str"):
    """Bind the activation workspace to the current asyncio context.

    Called once per trial by the benchmark harness. Child tasks inherit
    the binding, so concurrent trials in one process do not cross-write
    (the process-global env var cannot guarantee that).
    """
    return _workspace_var.set(str(workspace))


class ActivationLedger:
    def __init__(self, workspace: Path | str):
        self._path = Path(workspace) / LEDGER_FILENAME

    def record(self, *, kind: str, source: str, detail: dict | None = None) -> None:
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "kind": kind,
                    "source": source,
                    "detail": detail or {},
                }) + "\n")
        except Exception:
            pass


def activation_beacon(node_id: str, site: str = "", **detail: object) -> None:
    workspace = _workspace_var.get() or os.environ.get(WORKSPACE_ENV)
    if not workspace:
        return
    d: dict = dict(detail)
    if site:
        d["site"] = site
    ActivationLedger(workspace).record(kind="beacon", source=node_id, detail=d)


# ---- per-task collection (the Gate-b read-back side) -------------------------

BEACONS_DIRNAME = "beacons"
ENABLED_MARKER = ".enabled"


def beacon_workspace(out_dir: "Path | str", task_id: str, k: int) -> Path:
    """The canonical per-attempt beacon workspace under an eval out-dir.

    The batch runner points ``WORKSPACE_ENV`` here for each task-attempt
    subprocess, so beacon lines land pre-split by task; the reader below
    globs the same layout. Keep writer and reader on this one function.
    """
    return Path(out_dir) / BEACONS_DIRNAME / f"{task_id}_k{k}"


def mark_beacons_enabled(out_dir: "Path | str") -> None:
    """Drop the collection marker distinguishing "instrumentation ran, nothing
    fired" (an honest zero) from "collection was never wired" (no data)."""
    root = Path(out_dir) / BEACONS_DIRNAME
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / ENABLED_MARKER).touch()
    except OSError:
        pass


def read_fired_tasks(
    out_dirs: "list[Path | str]", task_ids: "list[str]"
) -> "set[str] | None":
    """Which of ``task_ids`` have at least one beacon line under any of the
    given eval out-dirs (the confirm dir plus its infra-rerun ladder siblings).

    Returns None when NO out-dir carries the collection marker — no
    instrumentation data means Gate-b must fail OPEN (skip attribution), not
    reject everything. With the marker present, an empty set is an honest
    "the mechanism never fired anywhere" and Gate-b correctly credits nothing.
    """
    roots = [Path(d) / BEACONS_DIRNAME for d in out_dirs]
    if not any((r / ENABLED_MARKER).exists() for r in roots):
        return None
    fired: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for tid in task_ids:
            if tid in fired:
                continue
            for ws in root.glob(f"{tid}_k*"):
                lf = ws / LEDGER_FILENAME
                try:
                    if lf.is_file() and lf.stat().st_size > 0:
                        fired.add(tid)
                        break
                except OSError:
                    continue
    return fired
