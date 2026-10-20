"""The shapes a record takes: what it is, what it claims, and where that came from.

A record is one durable thing Raven believes about the world -- a company, a
person, a file it produced, a message it sent -- and it carries the evidence
for that belief next to the belief itself. The two are kept apart on purpose:
``fields`` is what the record says, ``provenance`` is why it is entitled to say
it. A field with no source is a guess, and the store is shaped so that a guess
stays visible instead of being blended in with the sourced fields around it.

That split is what lets a later reader ask "how do you know this?" about one
field without having to trust the whole record, and it is what lets a corrected
field replace a wrong one without discarding the record that was right about
everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "LifecycleError",
    "Provenance",
    "Record",
    "RecordsError",
    "RecordsIdError",
    "RecordType",
    "now_stamp",
]


class RecordType(str, Enum):
    """What kind of thing a record is about.

    A plain string enum so a record row round-trips through JSON without a
    custom encoder, and so a comparison against a stored value does not need
    the enum imported at the far end.
    """

    COMPANY = "company"
    CONTACT = "contact"
    ARTIFACT = "artifact"
    INTERACTION = "interaction"
    OPPORTUNITY = "opportunity"


class RecordsError(RuntimeError):
    """A record could not be read, written or moved."""


class RecordsIdError(ValueError):
    """The id could not be used as a key or as a file name."""


class LifecycleError(RecordsError):
    """The record was asked to make a move its state does not allow."""


def now_stamp() -> str:
    """The current UTC time, second resolution, as an ISO-8601 string.

    Second resolution because a record's timestamps answer "when was this
    believed", not "when exactly" -- and a second is what a person reading a
    ledger later expects to see.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Provenance:
    """Why one field of a record is entitled to say what it says.

    ``field`` names the field the source supports. A source that backs three
    fields is three entries rather than one, because the support is per field:
    a pricing page supports a price and a feature list, and it says nothing
    about a headcount.

    ``method`` records how the source was reached, so a reader can tell a
    fetched page from a licensed feed from a value a model asserted.
    ``confidence`` is the source's own or the collector's assessment, on a
    zero-to-one scale, and is deliberately separate from ``tier``: the tier
    says what the source is allowed to be used for, the confidence says how much
    this particular reading is worth.
    """

    field: str
    source_url: str
    source: str = ""
    method: str = "fetch"
    retrieved_at: str = ""
    tier: str = ""
    confidence: float | None = None
    reason_kind: str = ""
    metric_basis: str = ""
    timeframe: str = ""

    def __post_init__(self) -> None:
        if not self.field:
            raise RecordsError("provenance must name the field it supports")
        if not self.source_url:
            raise RecordsError("provenance must name the source it came from")

    def to_dict(self) -> dict[str, Any]:
        """The row shape, with the unset parts left out."""
        data: dict[str, Any] = {
            "field": self.field,
            "source_url": self.source_url,
            "method": self.method,
        }
        if self.source:
            data["source"] = self.source
        if self.retrieved_at:
            data["retrieved_at"] = self.retrieved_at
        if self.tier:
            data["tier"] = self.tier
        if self.confidence is not None:
            data["confidence"] = self.confidence
        if self.reason_kind:
            data["reason_kind"] = self.reason_kind
        if self.metric_basis:
            data["metric_basis"] = self.metric_basis
        if self.timeframe:
            data["timeframe"] = self.timeframe
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provenance":
        """Read one row back, tolerating the parts an older row never had."""
        return cls(
            field=str(data.get("field", "")),
            source_url=str(data.get("source_url", "")),
            source=str(data.get("source", "")),
            method=str(data.get("method", "fetch")),
            retrieved_at=str(data.get("retrieved_at", "")),
            tier=str(data.get("tier", "")),
            confidence=data.get("confidence"),
            reason_kind=str(data.get("reason_kind", "")),
            metric_basis=str(data.get("metric_basis", "")),
            timeframe=str(data.get("timeframe", "")),
        )


@dataclass
class Record:
    """One durable belief, with the evidence for it.

    ``dedupe_key`` is the identity of the thing, not of the record: two
    records for the same company carry the same key, and the store keeps one
    of them. It is what stops a second run from telling a prospect they were
    never contacted, which is the failure a scraper plus a mailer cannot
    recover from.

    ``fields`` holds the record's own values, and ``provenance`` holds the
    sources for them. The two are separate so a field can be corrected, and
    re-sourced, without rewriting the record's identity.
    """

    type: RecordType
    dedupe_key: str
    fields: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    state: str = ""
    id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            raise RecordsError("a record needs a dedupe key")
        if not self.state:
            self.state = "new"
        if not self.created_at:
            self.created_at = now_stamp()
        if not self.updated_at:
            self.updated_at = self.created_at

    def sources_for(self, name: str) -> list[Provenance]:
        """The sources backing one field."""
        return [p for p in self.provenance if p.field == name]

    def unsourced_fields(self) -> list[str]:
        """The fields no source backs.

        A field with no source is a claim the record cannot defend, and this
        is the question an auditor asks first, so it is worth being able to
        ask it without walking the provenance list by hand.
        """
        sourced = {p.field for p in self.provenance}
        return sorted(name for name in self.fields if name not in sourced)

    def to_dict(self) -> dict[str, Any]:
        """The row shape, with the unset parts left out."""
        data: dict[str, Any] = {
            "type": self.type.value if isinstance(self.type, RecordType) else str(self.type),
            "dedupe_key": self.dedupe_key,
            "state": self.state,
            "fields": dict(self.fields),
            "provenance": [p.to_dict() for p in self.provenance],
        }
        if self.id:
            data["id"] = self.id
        if self.created_at:
            data["created_at"] = self.created_at
        if self.updated_at:
            data["updated_at"] = self.updated_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        """Read one row back, tolerating the parts an older row never had."""
        raw_type = data.get("type", "")
        try:
            record_type = RecordType(raw_type)
        except ValueError as exc:
            raise RecordsError(f"unknown record type {raw_type!r}") from exc
        provenance = [Provenance.from_dict(p) for p in data.get("provenance", [])]
        return cls(
            type=record_type,
            dedupe_key=str(data.get("dedupe_key", "")),
            fields=dict(data.get("fields", {})),
            provenance=provenance,
            state=str(data.get("state", "")),
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
