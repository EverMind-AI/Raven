"""What a campaign is allowed to spend, in whatever it is that gets spent.

The unit is the campaign's to declare and nothing here interprets it: it is
carried, printed and compared, never read. That is the whole point. This layer is
shared by every domain that runs jobs, and it used to price everything in GPU
minutes -- so a CFD campaign spending core-minutes inherited a field called
``budget_minutes_total`` whose value meant something else, and the two quantities
were indistinguishable to every reader including the loop.

How spend accumulates is declared too, because it differs by unit rather than by
backend:

  - **shared** -- two jobs overlapping on one device cost what one of them costs
    for that stretch. A GPU minute is occupancy: the card was busy, once.
  - **additive** -- each job pays for itself. A core-minute is work: eight cores
    and eight more cores for one minute is sixteen core-minutes, and without that
    a loop can buy speed by raising the decomposition and never pay for it.

Both rules run through one accumulator over weighted spans, so a platform that
prices in licence-hours or node-hours is a line of configuration rather than
another accumulator. The measurement -- when a job ran, how many units wide it
was -- stays with the backend, which is the only thing that can read it off the
machine.

*Which* measurement, though, is declared too. Everything above prices machine
time, and that is not what every watch spends. A campaign that sits on a price
feed for a day runs no jobs at all: what it spends is the span it was asked to
cover and the number of times it woke to look, and each of those wakes is a whole
cold start. Nothing on the machine can be asked for either figure -- the machine
was idle -- so the meter says who holds the reading, and a budget metered off the
machine is answered from the campaign's own record (``oncall_flow.attendance``).
"""

from __future__ import annotations

from dataclasses import dataclass

SHARED = "shared"
ADDITIVE = "additive"

# Who holds the reading for this budget's unit.
COMPUTE = "compute"  # the machine: core-minutes, gpu-minutes, licence-hours
WALL_CLOCK = "wall-clock"  # the clock: how long the watch has been open
LOOKS = "look"  # the campaign's own record: one per time it read the world

_METERS = frozenset({COMPUTE, WALL_CLOCK, LOOKS})

# The fields that predate this one, and what each meant where it was used. Kept
# because campaigns carrying them are on disk and running; a campaign's meta is
# the record of the device it ran under, so it is read rather than rewritten.
# Order matters: the CFD factory read budget_core_minutes_total in preference to
# budget_minutes_total, because that line inherited the second name while meaning
# the first quantity. A campaign carrying both is one of theirs.
_LEGACY = {
    "budget_core_minutes_total": ("core-minute", ADDITIVE),
    "budget_minutes_total": ("gpu-minute", SHARED),
}


@dataclass(frozen=True)
class Budget:
    """A declared allowance: how much of what, who measures it, and how
    concurrent use adds up."""

    unit: str
    total: float
    overlap: str = SHARED
    meter: str = COMPUTE

    @property
    def is_additive(self) -> bool:
        return self.overlap == ADDITIVE

    @property
    def off_machine(self) -> bool:
        """Whether the spend has to be read from the campaign rather than the host.

        The two cases this separates are not variants of one another: a host asked
        for the spend on a wall-clock budget answers with the machine time its jobs
        used, which for a watch that ran no jobs is zero however long it has been
        watching. Zero is the reading a loop acts on most freely.
        """
        return self.meter in (WALL_CLOCK, LOOKS)


def from_meta(meta: dict) -> Budget | None:
    """The campaign's declared budget, or ``None`` when it declared none.

    ``None`` is a first-class answer and not a zero: plenty of work has no budget
    (run this case once, reproduce that failure), and inventing a default would
    put a number nobody chose in front of a loop that cannot tell it from one the
    operator set. Requiring every domain to declare one would be the same mistake
    in the other direction. What the absence must not be is silent -- the callers
    say so, and go on reporting what has been spent.
    """
    declared = meta.get("budget")
    if isinstance(declared, dict) and declared.get("total") is not None:
        try:
            total = float(declared["total"])
        except (TypeError, ValueError):
            return None
        overlap = str(declared.get("overlap") or SHARED)
        meter = str(declared.get("meter") or COMPUTE)
        return Budget(
            unit=str(declared.get("unit") or "unit"),
            total=total,
            overlap=overlap if overlap in (SHARED, ADDITIVE) else SHARED,
            # Never inferred from the unit. "minute" is a wall-clock minute on a
            # watch and a core-minute on a solver campaign, and guessing puts a
            # spend nobody measured against a total somebody set.
            meter=meter if meter in _METERS else COMPUTE,
        )
    for key, (unit, overlap) in _LEGACY.items():
        value = meta.get(key)
        if value is None:
            continue
        try:
            return Budget(unit=unit, total=float(value), overlap=overlap)
        except (TypeError, ValueError):
            return None
    return None


def accumulate(spans: list[tuple[float, float, float]], *, overlap: str) -> float:
    """Total spend over ``(start, end, width)`` spans, in span-width x seconds / 60.

    ``width`` is how many units wide the job ran -- one for a whole-device job,
    the core count for a decomposed solver. Under ``additive`` every span pays;
    under ``shared`` an overlap is counted once, at the greatest width running
    across it, so a wide job overlapping a narrow one is not billed as the narrow
    one.

    A span whose start is unknown cannot be placed on the timeline, so callers
    pass those separately; over-charging an overlap that cannot be seen is the
    safe direction for a budget.
    """
    if not spans:
        return 0.0
    if overlap == ADDITIVE:
        return sum(max(0.0, end - start) * width for start, end, width in spans) / 60.0

    events: list[tuple[float, float, float]] = []
    for start, end, width in spans:
        if end > start:
            events.append((start, end, width))
    if not events:
        return 0.0
    edges = sorted({e for span in events for e in span[:2]})
    total = 0.0
    for left, right in zip(edges, edges[1:]):
        widths = [w for s, e, w in events if s < right and e > left]
        if widths:
            total += (right - left) * max(widths)
    return total / 60.0
