"""Child entry for layer E: dispatch each case to the agent the way the host does.

Usage: ``python -m evolver.validator._dispatch <job.json>``. The job names the
isolated home's agents root, the agent's manifest name, and the cases (task,
workspace, timeout). The row comes from discovery and the backend from
``build_third_party_backend`` with its own connection pool, which is the host's own
path for a ``kind: acp`` row. Cases run one at a time; results go to the job's
``out`` file. Run with the isolated home's scrubbed environment: an ACP child
inherits this process's environment.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path


async def _run(job: dict) -> list[dict]:
    from raven.acp_client.pool import AcpConnectionPool
    from raven.agent.subagent.backends import build_third_party_backend
    from raven.agent.subagent.vendored_agents import discover_product_rows

    rows = [r for r in discover_product_rows(Path(job["agents_root"])) if r.name == job["agent_name"]]
    if len(rows) != 1:
        raise SystemExit(f"discovery found {len(rows)} rows named {job['agent_name']!r}")
    row = rows[0]
    pool = AcpConnectionPool()
    results: list[dict] = []
    try:
        for case in job["cases"]:
            chunks: list[str] = []

            async def on_delta(text: str, _chunks: list[str] = chunks) -> None:
                _chunks.append(text)

            backend = build_third_party_backend(row, pool=pool, timeout=case["timeout_s"])
            started = time.monotonic()
            reply, error = "", None
            try:
                reply = await asyncio.wait_for(
                    backend.run(
                        case["task"],
                        task_id=case["id"],
                        workspace=Path(case["workspace"]),
                        executor=None,
                        session_key=f"validate:{job['run_id']}:{case['id']}",
                        on_delta=on_delta,
                    ),
                    timeout=case["timeout_s"] + 30,
                )
            except (TimeoutError, asyncio.TimeoutError):
                error = f"timed out after {case['timeout_s']}s"
            except Exception as exc:  # noqa: BLE001 - the dispatch failure is the finding
                error = f"{type(exc).__name__}: {exc}"
            results.append(
                {
                    "case_id": case["id"],
                    "reply": reply or "",
                    "full_text": "".join(chunks),
                    "seconds": round(time.monotonic() - started, 3),
                    "error": error,
                }
            )
    finally:
        await pool.close_all()
    return results


def main(argv: list[str]) -> int:
    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    results = asyncio.run(_run(job))
    Path(job["out"]).write_text(json.dumps(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
