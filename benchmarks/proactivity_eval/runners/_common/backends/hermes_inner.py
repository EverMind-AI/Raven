#!/usr/bin/env python3
"""Hermes subprocess inner — runs one cron job inside a clean HERMES_HOME
with ``hermes_time.now`` patched to the benchmark's fake_now.

Reads env: HERMES_EVAL_FAKE_NOW, HERMES_EVAL_CRON_JOB, HERMES_HOME,
HERMES_AGENT_SRC. Emits one JSON object on its final stdout line.

Callers (``HermesBackend``) re-exec THIS file via
``python3 -m _common.backends.hermes_inner`` or with explicit path.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path


def main() -> None:
    try:
        fake_now_iso = os.environ["HERMES_EVAL_FAKE_NOW"]
        cron_job_spec = json.loads(os.environ["HERMES_EVAL_CRON_JOB"])
        hermes_home = Path(os.environ["HERMES_HOME"])
        hermes_src = os.environ["HERMES_AGENT_SRC"]
    except KeyError as exc:
        print(json.dumps({"success": False,
                          "error": f"missing env: {exc}"}), flush=True)
        return

    hermes_home.mkdir(parents=True, exist_ok=True)
    if hermes_src not in sys.path:
        sys.path.insert(0, hermes_src)

    fake_now = datetime.fromisoformat(fake_now_iso)
    if fake_now.tzinfo is None:
        print(json.dumps({
            "success": False,
            "error": "fake_now must be tz-aware ISO (e.g. '+08:00')",
        }), flush=True)
        return

    # CRITICAL — patch hermes_time.now BEFORE importing cron modules.
    # cron.scheduler / cron.jobs do `from hermes_time import now as _hermes_now`,
    # binding the name at import time; patching after would be a no-op.
    import hermes_time  # noqa: E402
    hermes_time.now = lambda: fake_now  # noqa: E731

    from cron import jobs as cron_jobs          # noqa: E402
    from cron import scheduler as cron_scheduler  # noqa: E402

    job = cron_jobs.create_job(
        prompt=cron_job_spec["prompt"],
        schedule=cron_job_spec.get("schedule", "* * * * *"),
        name=cron_job_spec.get("name"),
        deliver=cron_job_spec.get("deliver", "local"),
    )
    job_for_run = cron_jobs.get_job(job["id"])
    if job_for_run is None:
        print(json.dumps({"success": False,
                          "error": "job_lookup_failed"}), flush=True)
        return

    try:
        success, full_doc, final_response, error = cron_scheduler.run_job(job_for_run)
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "error": f"run_job_exception: {type(exc).__name__}: {exc}",
        }), flush=True)
        return

    print(json.dumps({
        "success": bool(success),
        "final_response": final_response or "",
        "full_doc": full_doc,
        "error": error,
        "job_id": job["id"],
        "fake_now": fake_now_iso,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
