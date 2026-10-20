"""Merging what a record already knew with what this run just learned.

Two runs that look at the same company usually agree on most of it and
disagree on some. The disagreements are the interesting part, and they have to
be merged in a way that keeps the record honest: a field that two runs read
differently needs both readings, each still tied to the source it came from,
rather than one of them silently winning.

So a merge never overwrites a source. It adds the new one, and where the same
source was read again, the later reading replaces the earlier one for that
field. Everything else is kept.
"""

from __future__ import annotations

from raven.records.types import Provenance

__all__ = [
    "merge_provenance",
    "provenance_for",
    "source_urls",
    "sourced_fields",
]


def _key(entry: Provenance) -> tuple[str, str, str]:
    """What makes two readings the same reading.

    The field, the source and the method together: the same page read as a
    fetch and read as a licensed feed are two readings, and the same page
    read twice is one reading with a later time on it.
    """
    return (entry.field, entry.source_url, entry.method)


def merge_provenance(existing: list[Provenance], incoming: list[Provenance]) -> list[Provenance]:
    """Both lists, with the later reading of the same source winning.

    Order is the existing order first, then whatever the new reading adds.
    A record's provenance is read in order by a person checking it, so the
    order it was collected in is worth keeping.
    """
    merged: dict[tuple[str, str, str], Provenance] = {}
    order: list[tuple[str, str, str]] = []
    for entry in list(existing) + list(incoming):
        key = _key(entry)
        if key not in merged:
            order.append(key)
        merged[key] = entry
    return [merged[key] for key in order]


def provenance_for(provenance: list[Provenance], name: str) -> list[Provenance]:
    """The sources backing one field."""
    return [entry for entry in provenance if entry.field == name]


def sourced_fields(provenance: list[Provenance]) -> set[str]:
    """The names of the fields that have at least one source."""
    return {entry.field for entry in provenance}


def source_urls(provenance: list[Provenance]) -> list[str]:
    """Every source the record leans on, in order, without repeats."""
    seen: list[str] = []
    for entry in provenance:
        if entry.source_url not in seen:
            seen.append(entry.source_url)
    return seen
