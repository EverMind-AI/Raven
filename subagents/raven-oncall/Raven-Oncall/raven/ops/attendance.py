"""How much of a watch has been kept: looks taken, wakes armed, time open.

Two separate needs read the same three numbers, which is why they are counted in
one place.

A budget metered off the machine (``budget.WALL_CLOCK``, ``budget.LOOKS``) has
nowhere else to get its spend: the host measures machine time, and a campaign
watching a price feed spends none of it. The reading has to come from the
campaign's own record.

And a delivered watch has to say how many times it looked. What the report gate
already requires is that the conclusion be checkable -- ``observed`` states what
was seen, ``baseline`` what it started from -- but for a campaign whose correct
outcome was to do nothing, those are equally satisfied by having watched
diligently and by never having looked at all. The count is the only thing that
separates them, and it is not the loop's to state: it is in the trail either way,
so it is read from there and attached (measured on SentinelBench: 20 of 100 tasks
have "stay silent" as the right answer).

Nothing here is a judgement. Whether twelve looks over four hours was attentive
enough depends on what was being watched, and that belongs to whoever reads the
report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from raven.ops.budget import LOOKS, WALL_CLOCK, Budget

# Events that mean "the loop arranged to come back". Both are the loop's own
# decision to keep the watch open; the wake that then fires is the cron
# service's, and it is not recorded in the campaign's trail.
_WAKE_KINDS = frozenset({"wake_scheduled", "check_later"})


@dataclass(frozen=True)
class Attendance:
    """What the campaign's own record says about the watch being kept.

    ``looks`` is the number of times the tool layer read the world for this
    campaign -- the probe counter written by ``ops_tune_status``, not a count of
    turns. A wake that read nothing did not look.
    """

    looks: int = 0
    wakes: int = 0
    opened_at: datetime | None = None
    minutes_open: float | None = None


def attendance(campaign_dir: str | Path, *, now: datetime | None = None) -> Attendance:
    """Read the watch's own record. Never raises: a campaign directory that is
    missing, half-written or unreadable reports what could be read of it."""
    cdir = Path(campaign_dir).expanduser()
    looks = 0
    try:
        from raven.ops.state_claims import read_facts

        looks = int(read_facts(cdir).probe_seq or 0)
    except Exception:  # noqa: BLE001 -- an unreadable probe record is zero looks, not a crash
        looks = 0

    wakes = 0
    first_ts: datetime | None = None
    try:
        from raven.ops.instrument import read_events

        for event in read_events(cdir):
            if event.get("kind") in _WAKE_KINDS:
                wakes += 1
            if first_ts is None:
                first_ts = _parse(event.get("ts"))
    except Exception:  # noqa: BLE001
        pass

    opened = _declared_at(cdir) or first_ts
    minutes = None
    if opened is not None:
        minutes = max(0.0, ((now or datetime.now()) - opened).total_seconds() / 60.0)
    return Attendance(looks=looks, wakes=wakes, opened_at=opened, minutes_open=minutes)


def off_machine_spend(
    campaign_dir: str | Path, declared: Budget, *, now: datetime | None = None
) -> float | None:
    """What has been spent against a budget the host cannot measure, or None.

    ``None`` for a compute budget -- not zero. This function is not the one that
    knows about machine time, and answering zero would put a figure in front of a
    loop that reads as "nothing spent yet".
    """
    if not declared.off_machine:
        return None
    kept = attendance(campaign_dir, now=now)
    if declared.meter == LOOKS:
        return float(kept.looks)
    if declared.meter == WALL_CLOCK:
        return kept.minutes_open
    return None


def _declared_at(cdir: Path) -> datetime | None:
    """When the campaign was declared, from its declaration.

    Preferred over the first event because an amended declaration is still the
    same watch: the clock a wall-clock budget runs against started when the owner
    asked for the watch, and re-declaring after a round 0 that died on a shell
    mismatch does not buy another day of it.
    """
    import json

    path = cdir / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamped = _parse(meta.get("declared_at"))
    if stamped is not None:
        return stamped
    try:
        # Campaigns declared before the field existed are on disk and running.
        # The file's own mtime is the closest thing to hand, and it is what a
        # reader would use.
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None
