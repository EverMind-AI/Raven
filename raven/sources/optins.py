"""The record of why this deployment may use a source it may not use as it stands.

A source whose terms do not permit a use can still be used by an operator who
has a reason: a licence they hold, a contract they are party to, a decision
they made and can point at later. What the registry cannot do is decide that
for them, and what it must not do is let the use happen with no trace.

So the reason is written down, next to the source and the use it covers, and
the registry reads it back before it allows the call. The record is the
answer to "how do you know this was allowed" -- which is the question an
examiner asks, and which an operator has to be able to answer about their own
deployment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from raven.sources.policy import SourcePolicyError
from raven.utils.atomic_io import atomic_update

__all__ = [
    "delete_optin",
    "has_opt_in",
    "optins_path",
    "read_optins",
    "write_optin",
]


def _now() -> str:
    """The current UTC time, second resolution, as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def optins_path() -> Path:
    """Where the opt-in record lives for the active agent home."""
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("sources") / "optins.json"


def read_optins() -> dict[str, list[dict[str, Any]]]:
    """Every opt-in on record, keyed by source.

    A file that cannot be read is reported rather than treated as empty. An
    empty record and an unreadable record look the same to a caller that
    only asks "may I", and one of them means "nobody has said yes" while the
    other means "the answer is somewhere we cannot reach".
    """
    path = optins_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourcePolicyError(f"the opt-in record at {path} is not readable json ({exc})") from exc
    if not isinstance(data, dict):
        raise SourcePolicyError(f"the opt-in record at {path} is not a mapping of sources")
    return {str(source): list(grants) for source, grants in data.items()}


def has_opt_in(source: str, use: str | None = None) -> bool:
    """Whether the operator has recorded why this source may be used.

    A grant that names a use covers that use and nothing else; a grant that
    names no use covers the source outright. An operator who has said "yes to
    this source, in this deployment" has not said "yes to every use of it".
    """
    for grant in read_optins().get(source, []):
        covered = grant.get("use") or ""
        if use is None or not covered or covered == use:
            return True
    return False


def write_optin(source: str, *, basis: str, use: str = "", granted_by: str = "") -> dict[str, Any]:
    """Record why this source may be used, and return the grant.

    The basis is required. An opt-in with no reason behind it is a decision
    nobody can be shown to have made, and it would let a later reader conclude
    that the deployment was checked when it was not.
    """
    if not source:
        raise SourcePolicyError("an opt-in must name the source it is for")
    if not basis:
        raise SourcePolicyError("an opt-in must record the reason it was given")
    grant: dict[str, Any] = {
        "source": source,
        "use": use,
        "basis": basis,
        "granted_at": _now(),
    }
    if granted_by:
        grant["granted_by"] = granted_by

    def update(current: str | None) -> tuple[str | None, dict[str, Any]]:
        data: dict[str, Any] = {}
        if current:
            try:
                loaded = json.loads(current)
            except json.JSONDecodeError as exc:
                raise SourcePolicyError(f"the opt-in record is not readable json ({exc})") from exc
            if isinstance(loaded, dict):
                data = loaded
        grants = list(data.get(source, []))
        grants = [g for g in grants if g.get("use") != use]
        grants.append(grant)
        data[source] = grants
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), grant

    atomic_update(optins_path(), update)
    return grant


def delete_optin(source: str, use: str | None = None) -> bool:
    """Withdraw an opt-in. Whether one was there."""
    removed = False

    def update(current: str | None) -> tuple[str | None, bool]:
        nonlocal removed
        if not current:
            return None, False
        try:
            data = json.loads(current)
        except json.JSONDecodeError as exc:
            raise SourcePolicyError(f"the opt-in record is not readable json ({exc})") from exc
        if not isinstance(data, dict):
            return None, False
        grants = list(data.get(source, []))
        if use is None:
            kept = []
        else:
            kept = [g for g in grants if g.get("use") != use]
        removed = len(kept) != len(grants)
        if kept:
            data[source] = kept
        else:
            data.pop(source, None)
        # The file is rewritten even when it ends up empty: a replacement of
        # None means "leave the file alone", and leaving a withdrawn opt-in on
        # disk is the one outcome a withdrawal must not have.
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), removed

    result = atomic_update(optins_path(), update)
    return bool(result)
