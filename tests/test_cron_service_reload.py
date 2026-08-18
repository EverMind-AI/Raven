"""External-modification reload detection in CronService._load_store."""

import dataclasses
import os
from pathlib import Path

from raven.proactive_engine.schedulers.cron.service import CronSchedule, CronService
from raven.proactive_engine.schedulers.cron.types import CronPayload


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
        channel="tui",
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


def test_every_payload_field_survives_a_store_round_trip(tmp_path: Path):
    """Whatever ``CronPayload`` declares has to reach disk and come back.

    Driven off ``dataclasses.fields`` rather than a written-out list: a field
    added to the payload and forgotten in ``_save_store`` / ``_load_store``
    reads back as its default, which no assertion on the fields anyone
    remembered would catch. ``deliver`` was lost exactly that way -- accepted
    by ``add_job``, dropped on the way to the store -- while a full suite
    stayed green.
    """
    store_path = tmp_path / "jobs.json"
    added = CronService(store_path).add_job(
        name="round trip",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="remind me",
        deliver=True,
        channel="tui",
        to="direct",
        topic_tag="meds",
    )

    # A field left at its default cannot show a loss: the value that survives
    # and the value a dropped field reads back as are the same. Assert the
    # setup first, so adding a payload field without extending the call above
    # fails here rather than passing vacuously.
    defaults = CronPayload()
    for field in dataclasses.fields(CronPayload):
        assert getattr(added.payload, field.name) != getattr(defaults, field.name), (
            f"{field.name} was left at its default, so this test cannot see it being dropped"
        )

    reloaded = CronService(store_path).list_jobs()
    assert len(reloaded) == 1
    for field in dataclasses.fields(CronPayload):
        assert getattr(reloaded[0].payload, field.name) == getattr(added.payload, field.name), (
            f"payload.{field.name} did not survive the store round trip"
        )
