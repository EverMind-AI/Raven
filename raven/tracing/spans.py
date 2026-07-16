"""audit.span.v1 span construction + emission.

One viewer renders any framework's traces because every collector writes this
same schema. Raven adds ``span.type`` values beyond the original five
(``memory``, ``plugin``, ``skill`` for skill.read/inject) — see the design doc.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import config
from .store import TraceStore

SCHEMA_VERSION = "audit.span.v1"
FRAMEWORK = "raven"

_store: TraceStore | None = None


def _get_store() -> TraceStore:
    global _store
    if _store is None:
        _store = TraceStore(config.state_dir())
    return _store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_span(
    name: str,
    span_type: str,
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    session_key: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    start_time: str,
    end_time: str | None = None,
    status_code: str = "OK",
    status_message: str = "",
    attributes: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "span.type": span_type,
        "framework": FRAMEWORK,
        # session.id + channel.id are the keys the shared viewer groups on
        # (audit.span.v1 common attrs). Mirror session_key/channel into them
        # so raven traces group by conversation → turn like the others.
        "session.id": session_key,
        "session.key": session_key,
        "channel": channel,
        "channel.id": channel,
        "chat_id": chat_id,
        "audit.schema_version": SCHEMA_VERSION,
    }
    if attributes:
        attrs.update(attributes)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": name,
        "kind": "INTERNAL",
        "startTime": start_time,
        "endTime": end_time or start_time,
        "status": {"code": status_code, "message": status_message},
        "attributes": attrs,
        "events": events or [],
    }


def emit(span: dict[str, Any]) -> None:
    try:
        _get_store().append_span(span)
    except Exception:  # noqa: BLE001 — tracing must never break the host
        pass


def persist_artifact(kind: str, meta: dict[str, Any], payload: Any, *, label: str | None = None):
    try:
        return _get_store().persist_artifact(kind, meta, payload, label=label, preview_length=config.preview_len())
    except Exception:  # noqa: BLE001
        return None


def artifact_attributes(prefix: str, artifact: dict[str, Any] | None) -> dict[str, Any]:
    return TraceStore.artifact_attributes(prefix, artifact)
