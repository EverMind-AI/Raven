"""``records.*`` RPC handlers -- the durable record store.

Five methods back the store:

* ``records.get``        -- one record, by id.
* ``records.upsert``     -- write a record, merging into the one already there.
* ``records.list``       -- the records, filtered by kind and by state.
* ``records.transition`` -- move a record along its chain.
* ``records.grants.set`` -- record, or withdraw, why one source's terms may be
  kept for one record.

The store is the process-wide one, so a surface and a tool see the same
records. Opening the database per call would mean a file handle and a lock per
call, and two handles writing one file is what the store's own lock exists to
prevent.

A refusal carries its reason. A record that is not there answers -32032
rather than an empty result, because "we have never heard of this record" and
"this record is empty" are different answers, and a client that guesses
between them asks a prospect twice. A move the chain does not allow answers
-32033 with the chain's own reason, because a refused move is information
rather than a failure to work around.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.rpc.errors import (
    InvalidParamsError,
    RecordNotFoundError,
    RecordTransitionRefusedError,
    SourcePolicyRefusedError,
)
from raven.rpc.models import (
    RecordGrantPayload,
    RecordPayload,
    RecordProvenancePayload,
    RecordsGetParams,
    RecordsGrantsSetParams,
    RecordsListParams,
    RecordsTransitionParams,
    RecordsUpsertParams,
)

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

#: The most records one call returns. A list without a cap is a way to pull
#: the whole store into a client that cannot show it.
_LIST_CAP = 200


def _store() -> Any:
    """The process-wide record store."""
    from raven.records.library import records_store

    return records_store()


def _parse(model: Any, params: dict | None) -> Any:
    """The params, validated.

    A frame the handler can reject on sight is -32602 rather than a
    traceback: the caller sent something the contract does not allow, and
    saying so is the whole answer.
    """
    try:
        return model.model_validate(params if params is not None else {})
    except Exception as exc:  # noqa: BLE001 - pydantic's message is the report
        raise InvalidParamsError(f"{model.__name__}: {str(exc)[:400]}") from exc


def _provenance_payload(entry: Any) -> RecordProvenancePayload:
    return RecordProvenancePayload(
        field=entry.field,
        source_url=entry.source_url,
        source=entry.source,
        method=entry.method,
        retrieved_at=entry.retrieved_at,
        tier=entry.tier,
        confidence=entry.confidence,
        reason_kind=entry.reason_kind,
        metric_basis=entry.metric_basis,
        timeframe=entry.timeframe,
    )


def _payload(record: Any) -> RecordPayload:
    """One record as the wire carries it."""
    return RecordPayload(
        id=record.id,
        type=record.type.value,
        dedupe_key=record.dedupe_key,
        state=record.state,
        fields=dict(record.fields),
        provenance=[_provenance_payload(entry) for entry in record.provenance],
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def records_get(params: dict) -> dict:
    """``records.get`` -- one record, by id."""
    parsed = _parse(RecordsGetParams, params)
    record = _store().get_by_id(parsed.id)
    if record is None:
        raise RecordNotFoundError(f"no record carries the id {parsed.id!r}")
    return {"record": _payload(record).model_dump()}


async def records_upsert(params: dict) -> dict:
    """``records.upsert`` -- write a record, merging into the one already there.

    The merge is the point: a second run meeting the same thing adds its
    sources to the ones already held rather than starting a second row, and
    the state only ever moves forward.
    """
    parsed = _parse(RecordsUpsertParams, params)
    if not parsed.dedupe_key.strip():
        raise InvalidParamsError("a record needs a dedupe key")

    from raven.records.types import Provenance, Record, RecordsError, RecordsIdError, RecordType

    try:
        record_type = RecordType(parsed.type)
    except ValueError as exc:
        raise InvalidParamsError(f"unknown record type {parsed.type!r}") from exc

    record = Record(
        type=record_type,
        dedupe_key=parsed.dedupe_key,
        fields=dict(parsed.fields or {}),
        provenance=[
            Provenance(
                field=entry.field,
                source_url=entry.source_url,
                source=entry.source,
                method=entry.method,
                retrieved_at=entry.retrieved_at,
                tier=entry.tier,
                confidence=entry.confidence,
                reason_kind=entry.reason_kind,
                metric_basis=entry.metric_basis,
                timeframe=entry.timeframe,
            )
            for entry in (parsed.provenance or [])
        ],
        state=parsed.state or "",
    )
    try:
        stored = _store().put(record)
    except RecordsIdError as exc:
        # The key cannot be used as a key. That is the frame's problem, not
        # the store's, and it is answered as one.
        raise InvalidParamsError(str(exc)) from exc
    except RecordsError as exc:
        # The store refuses a field the source's terms do not let us keep.
        # That is a refusal by the registry, and it says what would change
        # the answer -- including the id and the source to record.
        raise SourcePolicyRefusedError(str(exc)) from exc
    return {"record": _payload(stored).model_dump()}


async def records_list(params: dict) -> dict:
    """``records.list`` -- the records, filtered by kind and by state."""
    parsed = _parse(RecordsListParams, params)
    record_type = None
    if parsed.type:
        from raven.records.types import RecordType

        try:
            record_type = RecordType(parsed.type)
        except ValueError as exc:
            raise InvalidParamsError(f"unknown record type {parsed.type!r}") from exc

    records = _store().list_records(record_type, parsed.state)
    limit = min(_LIST_CAP, parsed.limit) if parsed.limit else _LIST_CAP
    return {
        "items": [_payload(record).model_dump() for record in records[:limit]],
        "total": len(records),
    }


async def records_transition(params: dict) -> dict:
    """``records.transition`` -- move a record along its chain."""
    parsed = _parse(RecordsTransitionParams, params)
    from raven.records.types import LifecycleError, RecordsError

    try:
        moved = _store().transition(parsed.id, parsed.to_state, parsed.reason or "")
    except LifecycleError as exc:
        # The chain refused. A subclass of RecordsError, so it is caught
        # first: the reason belongs to the chain and is worth keeping.
        raise RecordTransitionRefusedError(str(exc)) from exc
    except RecordsError as exc:
        raise RecordNotFoundError(str(exc)) from exc
    return {"record": _payload(moved).model_dump()}


async def records_grants_set(params: dict) -> dict:
    """``records.grants.set`` -- record, or withdraw, an acceptance.

    A grant says the terms of one source may be kept for one record. It needs
    a basis: an acceptance nobody can be shown to have made is not an
    acceptance, and a reader who cannot see the reason cannot tell a checked
    deployment from an unchecked one.
    """
    parsed = _parse(RecordsGrantsSetParams, params)
    from raven.records.grants import delete_grant, read_grants, write_grant

    if parsed.allowed:
        if not (parsed.basis or "").strip():
            raise InvalidParamsError(
                "recording an acceptance needs a basis: say why this deployment may keep the data"
            )
        write_grant(parsed.record_id, parsed.source, basis=parsed.basis or "")
        changed = True
    else:
        changed = delete_grant(parsed.record_id, parsed.source)

    grants = read_grants(parsed.record_id)
    return {
        "grants": [
            RecordGrantPayload(
                source=grant.get("source", ""),
                basis=grant.get("basis", ""),
                granted_at=grant.get("granted_at", ""),
            ).model_dump()
            for grant in grants
        ],
        "changed": changed,
    }


def register_records_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("records.get", records_get)
    dispatcher.register("records.upsert", records_upsert)
    dispatcher.register("records.list", records_list)
    dispatcher.register("records.transition", records_transition)
    dispatcher.register("records.grants.set", records_grants_set)


__all__ = [
    "records_get",
    "records_grants_set",
    "records_list",
    "records_transition",
    "records_upsert",
    "register_records_methods",
]
