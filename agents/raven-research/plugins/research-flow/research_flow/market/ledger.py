"""The entity ledger: the machine-readable half of a market scan.

An entity is one company in the market, its fields, and the sources for
those fields.

The field/provenance split is the one the records store already uses, so
the same vocabulary carries a research finding and a business record: a
field is separated from the source that backs it, so a field can be
corrected and re-sourced without rewriting the entity's identity, and so a
reader can ask about one field without trusting the whole record.

A field with no source is a claim the entity cannot defend, and
:func:`MarketEntity.unsourced_fields` is how you ask that question
without walking the provenance list by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from raven.records.types import Provenance

#: The default use a scan's sources are consulted for. It is the use the
#: sources policy is asked about, so it is a parameter and not a constant
#: buried in the policy.
DEFAULT_USE = "competitive_brief"


@dataclass
class MarketEntity:
    """One company in the market, with the sources for its fields."""

    key: str
    name: str
    fields: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    use: str = DEFAULT_USE

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("an entity needs a key")
        if not self.name:
            raise ValueError("an entity needs a name")

    def unsourced_fields(self) -> list[str]:
        """The fields no source backs."""
        sourced = {p.field for p in self.provenance}
        return sorted(name for name in self.fields if name not in sourced)

    def source_urls(self) -> list[str]:
        """The distinct pages this entity was read from, in the order they were
        first cited."""
        seen: list[str] = []
        for item in self.provenance:
            if item.source_url and item.source_url not in seen:
                seen.append(item.source_url)
        return seen

    def fields_from(self, source_url: str) -> list[str]:
        """The fields one page backs."""
        return sorted(p.field for p in self.provenance if p.source_url == source_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "use": self.use,
            "fields": dict(self.fields),
            "provenance": [p.to_dict() for p in self.provenance],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketEntity":
        return cls(
            key=str(data.get("key", "")),
            name=str(data.get("name", "")),
            fields=dict(data.get("fields") or {}),
            provenance=[Provenance.from_dict(p) for p in (data.get("provenance") or [])],
            use=str(data.get("use") or DEFAULT_USE),
        )


def entities_payload(
    entities: Sequence[MarketEntity],
    *,
    as_of: str = "",
    use: str = DEFAULT_USE,
) -> dict[str, Any]:
    """The one shape the entities file has.

    It carries the scan's date and the use its sources were cleared for,
    because a ledger read on its own has to say when it was true and what it
    was collected for - the same two facts the report carries in its own
    header.
    """
    return {
        "schema": "raven_market_entities",
        "version": 1,
        "as_of": as_of,
        "use": use,
        "entities": [entity.to_dict() for entity in entities],
    }


def write_entities(
    entities: Sequence[MarketEntity],
    path: Path,
    *,
    as_of: str = "",
    use: str = DEFAULT_USE,
) -> Path:
    """Write the ledger.

    The shape is deliberately the one a reader can check with ``jq``: the
    entities, the fields, and the source for every field, with the moment
    it was read.
    """
    payload = entities_payload(entities, as_of=as_of, use=use)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_entities(path: Path) -> list[MarketEntity]:
    """Read the ledger back."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MarketEntity.from_dict(row) for row in (data.get("entities") or [])]


__all__ = [
    "DEFAULT_USE",
    "MarketEntity",
    "entities_payload",
    "read_entities",
    "write_entities",
]
