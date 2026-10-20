"""The record of a deployment accepting to keep a field it was not given free.

Some sources let you read their data and do not let you keep it -- Google
Places is the sharpest case, where the terms allow the place id to be stored
and nothing else. A record that holds such a field is a deployment that has
decided something the source did not agree to, and that decision has to be
visible.

So the acceptance is written down against the record it applies to, and the
store checks it before it writes. The check is the point: a grant that nothing
consults is a note, and a note does not stop a write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raven.records.types import RecordsError, now_stamp
from raven.utils.atomic_io import atomic_update

__all__ = [
    "delete_grant",
    "grants_path",
    "has_grant",
    "read_grants",
    "write_grant",
]


def grants_path(record_id: str) -> Path:
    """Where the grants for one record live."""
    from raven.config.paths import get_runtime_subdir

    if not record_id or any(character in record_id for character in "/\\"):
        raise RecordsError(f"{record_id!r} cannot be used as a record id")
    return get_runtime_subdir("records") / "grants" / f"{record_id}.json"


def read_grants(record_id: str) -> list[dict[str, Any]]:
    """The grants on record for one record."""
    path = grants_path(record_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecordsError(f"the grant record at {path} is not readable json ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        raise RecordsError(f"the grant record at {path} does not hold a list of grants")
    return list(data["grants"])


def has_grant(record_id: str, source: str) -> bool:
    """Whether this deployment has agreed to keep this source's data."""
    return any(grant.get("source") == source for grant in read_grants(record_id))


def write_grant(record_id: str, source: str, *, basis: str) -> dict[str, Any]:
    """Record the acceptance, and return the grant.

    The basis is required for the same reason the source opt-in requires one:
    an acceptance nobody can be shown to have made is not an acceptance.
    """
    if not source:
        raise RecordsError("a grant must name the source it is for")
    if not basis:
        raise RecordsError("a grant must record the reason it was given")
    grant: dict[str, Any] = {
        "source": source,
        "basis": basis,
        "granted_at": now_stamp(),
    }

    def update(current: str | None) -> tuple[str | None, dict[str, Any]]:
        grants: list[dict[str, Any]] = []
        if current:
            try:
                data = json.loads(current)
            except json.JSONDecodeError as exc:
                raise RecordsError(f"the grant record is not readable json ({exc})") from exc
            if isinstance(data, dict) and isinstance(data.get("grants"), list):
                grants = [g for g in data["grants"] if g.get("source") != source]
        grants.append(grant)
        payload = json.dumps({"record_id": record_id, "grants": grants}, ensure_ascii=False, indent=2, sort_keys=True)
        return payload, grant

    atomic_update(grants_path(record_id), update)
    return grant


def delete_grant(record_id: str, source: str) -> bool:
    """Withdraw an acceptance. Whether one was there."""
    removed = False

    def update(current: str | None) -> tuple[str | None, bool]:
        nonlocal removed
        if not current:
            return None, False
        try:
            data = json.loads(current)
        except json.JSONDecodeError as exc:
            raise RecordsError(f"the grant record is not readable json ({exc})") from exc
        if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
            return None, False
        grants = list(data["grants"])
        kept = [g for g in grants if g.get("source") != source]
        removed = len(kept) != len(grants)
        data["grants"] = kept
        # The file is rewritten even when it ends up empty: a replacement of
        # None means "leave the file alone", and leaving a withdrawn grant on
        # disk is the one outcome a withdrawal must not have.
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), removed

    result = atomic_update(grants_path(record_id), update)
    return bool(result)
