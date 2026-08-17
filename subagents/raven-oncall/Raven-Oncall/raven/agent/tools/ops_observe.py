"""Raw-output tools for on-call campaigns: the job's own lines, and its outputs.

Why these exist separately from ``ops_tune_status``. That tool compresses a
trial's progress to a single line (``fetch_progress(tail=2)``, printing only the
last sample), which is enough for a training run whose progress rows each carry
the metric. It is not enough for a solver: measured on a real interFoam job, one
timestep prints 18 lines and only 2 carry the phase-fraction statistics, so a
one-line sample lands on the informative row about one time in nine. An agent
could then observe twenty times and still see almost nothing -- observation count
non-zero, information near zero.

These two tools pass the backend's output through unchanged. They parse nothing,
rank nothing and judge nothing: which lines matter, and whether the numbers in
them are healthy, is the judgement under measurement.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.agent.tools.ops import _resolve_campaign_dir

_MAX_TAIL = 400


def _campaign_backend(campaign: str, ledger: str | None) -> tuple[Any, Any, Path] | str:
    from raven.ops import Ledger
    from raven.ops.backends import backend_from_meta

    cdir = _resolve_campaign_dir(campaign, ledger)
    ledger_path, meta_path = cdir / "ledger.json", cdir / "meta.json"
    if not ledger_path.exists() or not meta_path.exists():
        return f"No campaign state under {cdir} (need ledger.json + meta.json)."
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    return backend_from_meta(meta), Ledger(ledger_path), cdir


class OpsObserveTool(Tool):
    """Return a trial's own output lines, verbatim."""

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_observe"

    @property
    def description(self) -> str:
        return (
            "Read the last N output lines of a trial in an ops campaign, exactly as the job "
            "wrote them. Nothing is summarised, filtered or ranked -- you get the raw lines and "
            f"decide what they mean. Use a large N (up to {_MAX_TAIL}) when the job writes many "
            "lines per step, so that a single step's output is not cut in half."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name."},
                "trial": {"type": "string", "description": "Trial key, as shown in the ledger."},
                "tail": {"type": "integer", "description": f"How many trailing lines to return (1-{_MAX_TAIL})."},
                "ledger": {"type": "string", "description": "Ledger path (locates the campaign dir)."},
            },
            "required": ["campaign", "trial"],
        }

    async def execute(self, campaign: str, trial: str, tail: int = 80, ledger: str | None = None, **kwargs: Any) -> str:
        resolved = _campaign_backend(campaign, ledger)
        if isinstance(resolved, str):
            return resolved
        backend, led, _ = resolved
        rec = led.get(trial)
        if rec is None or rec.handle is None:
            known = ", ".join(r.idem_key for r in led.all()) or "none"
            return f"Unknown trial {trial!r}. Trials in this campaign: {known}."
        n = max(1, min(int(tail), _MAX_TAIL))
        rows = await backend.fetch_progress(rec.handle, tail=n)
        if not rows:
            return f"{trial}: no output yet."
        lines = [
            row.get("line")
            if isinstance(row, dict) and "line" in row
            else _json.dumps(row, ensure_ascii=False, default=str)
            for row in rows
        ]
        return f"{trial}: last {len(lines)} line(s) of output\n" + "\n".join(lines)


class OpsOutputsTool(Tool):
    """Return what a trial has written out, and whatever its result carries."""

    timeout_seconds = 120.0

    @property
    def name(self) -> str:
        return "ops_outputs"

    @property
    def description(self) -> str:
        return (
            "List what a trial has produced so far -- the outputs it has written and whatever its "
            "own result record contains -- without interpretation. Use it to see how far a job "
            "actually got, which its output lines alone may not tell you."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name."},
                "trial": {"type": "string", "description": "Trial key, as shown in the ledger."},
                "ledger": {"type": "string", "description": "Ledger path (locates the campaign dir)."},
            },
            "required": ["campaign", "trial"],
        }

    async def execute(self, campaign: str, trial: str, ledger: str | None = None, **kwargs: Any) -> str:
        resolved = _campaign_backend(campaign, ledger)
        if isinstance(resolved, str):
            return resolved
        backend, led, _ = resolved
        rec = led.get(trial)
        if rec is None or rec.handle is None:
            known = ", ".join(r.idem_key for r in led.all()) or "none"
            return f"Unknown trial {trial!r}. Trials in this campaign: {known}."
        result = await backend.fetch_result(rec.handle)
        # Printed whole, including an empty metrics map: a backend that refuses to
        # invent a metric must not be silently rendered as "nothing to report".
        payload = {
            "status": result.status.name,
            "metrics": result.metrics,
            "output": result.output,
            "error": result.error,
        }
        return f"{trial}:\n" + _json.dumps(payload, ensure_ascii=False, indent=2, default=str)


__all__ = ["OpsObserveTool", "OpsOutputsTool"]
