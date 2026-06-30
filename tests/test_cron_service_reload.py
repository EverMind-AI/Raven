"""External-modification reload detection in CronService._load_store."""

import os
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronSchedule, CronService


def test_reload_detects_rewrite_within_float_mtime_collision(tmp_path: Path):
    """A rewrite whose mtime collides at float precision must still be seen.

    After a save, `_last_mtime` caches the store's timestamp. float64
    st_mtime has ~238ns resolution at current epoch values, so an external
    rewrite landing inside the same ulp bucket compares equal as a float
    while st_mtime_ns still differs. Pin that the staleness check uses
    nanosecond precision (the historical flake in
    test_cron_delete_with_yes_removes_job was this race).
    """
    store_path = tmp_path / "jobs.json"
    svc = CronService(store_path)
    job = svc.add_job(
        name="first",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="x",
        channel="cli",
        to="direct",
    )

    base_ns = 1_780_000_000 * 10**9
    os.utime(store_path, ns=(base_ns + 10, base_ns + 10))
    # Mirror what save() records, in whatever precision the impl uses,
    # as if the save itself had produced this controlled timestamp.
    stat = store_path.stat()
    svc._last_mtime = stat.st_mtime_ns if isinstance(svc._last_mtime, int) else stat.st_mtime
    assert [j.id for j in svc.list_jobs()] == [job.id]

    external = CronService(store_path)
    external.remove_job(job.id)
    os.utime(store_path, ns=(base_ns + 50, base_ns + 50))
    # Sanity: indistinguishable at float precision (same ulp bucket) —
    # exactly the collision window of the old float check.
    assert store_path.stat().st_mtime == float(base_ns + 10) / 10**9

    assert svc.list_jobs() == []
