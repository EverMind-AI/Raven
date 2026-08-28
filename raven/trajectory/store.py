"""Pin registry, attempt definitions, and rotation-transparent span reading.

Retention contract
------------------

The tracing store keeps logs for troubleshooting: the active log rotates into
``logs/archive/<date>/`` by size or day, and nothing deletes data today. A
trajectory referenced as corpus (a bug report, a regression case, evolver
evidence) needs a stronger promise: **pinned ids are never purged**. The pin
registry (``pins.json`` in the trace state dir) records that promise; any
future purge tooling MUST keep every span whose ``traceId``, legacy
``attempt.id``, owning definition id, or any of that definition's aliases is
pinned, and must keep every artifact such a span references.

Attempt definitions
-------------------

An attempt id equals the trace id unless ``attempts.json`` (the mutable
sidecar in the trace state dir) defines otherwise: a definition maps a minted
``att-*`` id to its member trace ids plus the historical ids it absorbed
(``aliases``), so span logs stay append-only while attempt grouping stays
editable. :func:`merge_attempts` creates a definition (migrating member-level
pins up to it); :func:`split_attempt` deletes one (migrating its pin down to
the members inside the same transaction). Verdicts and pins recorded under an
absorbed id stay addressable through the aliases.

Pin migration spans two files that cannot be committed together; every path
orders its writes so any crash or concurrent interleaving over-protects, never
under-protects. Known residue: a process dying between merge's definition pin
and the attempts commit leaves one dangling over-protective pin.

Reading
-------

:func:`iter_spans` yields spans across archived + active logs in write order,
so callers address trajectories without knowing where rotation put them.
Filter ids match a definition's member set when one exists, else ``attempt.id``
OR ``traceId`` — the names a trajectory is known by without a definition.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

from loguru import logger

from raven.tracing import config as tracing_config
from raven.utils.atomic_io import atomic_update

T = TypeVar("T")

_PINS_FILE = "pins.json"
_DEFS_FILE = "attempts.json"


def _pins_path(state_dir: Path | None = None) -> Path:
    return (state_dir or tracing_config.state_dir()) / _PINS_FILE


def _defs_path(state_dir: Path | None = None) -> Path:
    return (state_dir or tracing_config.state_dir()) / _DEFS_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _span_attrs(span: dict[str, Any]) -> dict[str, Any]:
    """The span's attributes mapping, or {} — a JSON-legal record whose
    "attributes" is a truthy non-object must degrade, not crash readers."""
    attrs = span.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _str_value(value: Any) -> str | None:
    """``value`` when it is a non-empty string, else None.

    Span records are unvalidated history: a list/dict id or key would blow up
    hashing, sorting, or rendering downstream, so readers treat any non-string
    field as absent."""
    return value if isinstance(value, str) and value else None


def pins(state_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """The pin registry: id -> {reason, ts}. Empty on missing/corrupt file."""
    path = _pins_path(state_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_pins(text: str | None) -> dict[str, dict[str, Any]]:
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _dump_pins(registry: dict[str, dict[str, Any]]) -> str:
    return json.dumps(registry, ensure_ascii=False, indent=2) + "\n"


def _update_pins(state_dir: Path | None, mutate: Callable[[dict[str, dict[str, Any]]], T]) -> T:
    """Run one atomic read-mutate-write transaction over the pin registry."""

    def update(text: str | None) -> tuple[str, T]:
        registry = _parse_pins(text)
        result = mutate(registry)
        return _dump_pins(registry), result

    return atomic_update(_pins_path(state_dir), update)


def pin(id_: str, *, reason: str = "", state_dir: Path | None = None) -> None:
    """Protect the literal ``id_`` from any future purge. Idempotent.

    Low-level: writes the id as given. To pin an attempt *address* (a
    definition id, alias, member, or turn trace), use :func:`pin_attempt` —
    it re-resolves under the attempts lock, so it cannot race a concurrent
    merge/split into pinning an id that no longer owns anything.
    """
    if not id_:
        raise ValueError("id is required")

    def mutate(registry: dict[str, dict[str, Any]]) -> None:
        registry[id_] = {"reason": reason, "ts": _now_iso()}

    _update_pins(state_dir, mutate)


def unpin(id_: str, state_dir: Path | None = None) -> bool:
    """Remove a pin. Returns whether it existed."""

    def update(text: str | None) -> tuple[str | None, bool]:
        current = _parse_pins(text)
        if id_ not in current:
            return None, False
        del current[id_]
        return _dump_pins(current), True

    return atomic_update(_pins_path(state_dir), update)


def _join_reasons(reasons: Iterable[str | None]) -> str:
    """Combine pin reasons: distinct non-empty ones joined in first-seen order."""
    seen: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return "; ".join(seen)


def _valid_id(id_: Any) -> bool:
    """Whether ``id_`` is usable as a definition/alias key (also path-safe)."""
    if not isinstance(id_, str) or not id_:
        return False
    return "/" not in id_ and "\\" not in id_ and ".." not in id_


def _check_defs_invariants(defs: dict[str, dict[str, Any]]) -> bool:
    """Global address-space check: definition ids, aliases, and member traces
    must be three mutually disjoint sets (aliases/traces also unique across
    entries) — otherwise resolution order would silently pick an owner."""
    def_ids = set(defs)
    aliases: set[str] = set()
    traces: set[str] = set()
    for entry in defs.values():
        for alias in entry.get("aliases") or []:
            if alias in aliases:
                return False
            aliases.add(alias)
        for trace in entry.get("traces") or []:
            if trace in traces:
                return False
            traces.add(trace)
    return not (def_ids & aliases) and not (def_ids & traces) and not (aliases & traces)


def _parse_defs(text: str | None) -> dict[str, dict[str, Any]]:
    """Parse + validate attempts.json content.

    Entries with invalid shape are dropped individually; any cross-entry
    address collision rejects the whole file (never silently pick an owner).
    The result is either fully trustworthy or empty.
    """
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    defs: dict[str, dict[str, Any]] = {}
    for def_id, entry in data.items():
        if not _valid_id(def_id) or not isinstance(entry, dict):
            continue
        raw_traces = entry.get("traces")
        if not isinstance(raw_traces, list) or any(not isinstance(t, str) or not t for t in raw_traces):
            continue
        traces = list(dict.fromkeys(raw_traces))
        if len(traces) < 2:
            continue
        raw_aliases = entry.get("aliases", [])
        if (
            not isinstance(raw_aliases, list)
            or any(not _valid_id(a) for a in raw_aliases)
            or len(set(raw_aliases)) != len(raw_aliases)
        ):
            continue
        normalized: dict[str, Any] = {"traces": traces}
        if raw_aliases:
            normalized["aliases"] = list(raw_aliases)
        if isinstance(entry.get("ts"), str):
            normalized["ts"] = entry["ts"]
        defs[def_id] = normalized
    return defs if _check_defs_invariants(defs) else {}


def _dump_defs(defs: dict[str, dict[str, Any]]) -> str:
    return json.dumps(defs, ensure_ascii=False, indent=2) + "\n"


def definitions(state_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Attempt definitions: id -> {traces, aliases?, ts?}. Empty on any defect."""
    path = _defs_path(state_dir)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_defs(text)


def new_attempt_id() -> str:
    return f"att-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def _definition_for(id_: str, defs: dict[str, dict[str, Any]]) -> str | None:
    """Current definition id addressed by ``id_`` as itself or as an alias."""
    if id_ in defs:
        return id_
    for def_id, entry in defs.items():
        if id_ in (entry.get("aliases") or []):
            return def_id
    return None


def _owner_of(id_: str, defs: dict[str, dict[str, Any]]) -> str | None:
    """Definition id owning ``id_`` — as itself, an alias, or a member trace."""
    owner = _definition_for(id_, defs)
    if owner is not None:
        return owner
    for def_id, entry in defs.items():
        if id_ in (entry.get("traces") or []):
            return def_id
    return None


def attempt_members(id_: str, state_dir: Path | None = None) -> tuple[str, ...] | None:
    """Member trace ids of the definition ``id_`` names (or aliases), else None."""
    defs = definitions(state_dir)
    owner = _definition_for(id_, defs)
    return tuple(defs[owner]["traces"]) if owner else None


def owning_attempt(trace_id: str, state_dir: Path | None = None) -> str | None:
    """The definition id whose members include ``trace_id``, else None."""
    defs = definitions(state_dir)
    for def_id, entry in defs.items():
        if trace_id in (entry.get("traces") or []):
            return def_id
    return None


def attempt_alias_ids(id_: str, state_dir: Path | None = None) -> tuple[str, ...]:
    """Every id the attempt addressed by ``id_`` is known by.

    For a defined attempt: (definition id, *aliases, *member traces) — the set
    verdict/pin readers must consult so labels recorded under absorbed or
    member ids stay visible. For anything else: just (id_,).
    """
    defs = definitions(state_dir)
    owner = _owner_of(id_, defs)
    if owner is None:
        return (id_,)
    entry = defs[owner]
    return (owner, *(entry.get("aliases") or []), *entry["traces"])


def _expand_group(
    id_: str, defs: dict[str, dict[str, Any]], state_dir: Path | None
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """Resolve ``id_`` to its whole attempt group against a defs snapshot.

    Returns (group key, member trace ids, alias ids to carry) — a definition
    (addressed by id, alias, or member) expands to its members; a legacy
    attempt id, or any turn trace of one, expands to the entire legacy group
    (matching how resolve_attempt_id addresses whole attempts by one turn);
    a bare trace expands to itself. None when nothing matches.
    """
    owner = _owner_of(id_, defs)
    if owner is not None:
        entry = defs[owner]
        return owner, tuple(entry["traces"]), (owner, *(entry.get("aliases") or []))
    canonical = id_
    traces: list[str] = []
    for span in iter_spans(state_dir, attempt_id=id_):
        legacy = _str_value(_span_attrs(span).get("attempt.id"))
        if legacy and legacy != id_:
            canonical = legacy
            break
        trace_id = _str_value(span.get("traceId"))
        if trace_id and trace_id not in traces:
            traces.append(trace_id)
    if canonical != id_:
        traces = []
        for span in iter_spans(state_dir, attempt_id=canonical):
            trace_id = _str_value(span.get("traceId"))
            if trace_id and trace_id not in traces:
                traces.append(trace_id)
    if not traces:
        return None
    aliases = (canonical,) if canonical not in traces else ()
    return canonical, tuple(traces), aliases


def merge_attempts(ids: Sequence[str], state_dir: Path | None = None) -> str:
    """Combine the attempts addressed by ``ids`` into one new definition.

    Inputs may be definition ids, aliases, member/legacy/bare trace ids; each
    expands to its whole group against one snapshot (order-independent), and
    at least two distinct groups from one session are required. Member-level
    pins migrate up to the new definition id; pins and verdicts recorded under
    absorbed ids stay visible through the aliases. Returns the new id.
    """
    new_id, snapshot = _merge_publish(ids, state_dir)
    _merge_cleanup(new_id, snapshot, state_dir)
    return new_id


def _merge_publish(ids: Sequence[str], state_dir: Path | None = None) -> tuple[str, dict[str, dict[str, Any]]]:
    """Validate, pin the new definition, and commit it to attempts.json.

    All pure validation runs before any pins write, so a rejected merge writes
    nothing. The definition pin is written just before the attempts commit so
    a concurrent split can never observe an unpinned definition; if the commit
    itself then fails, the pin is rolled back (compare-and-delete).
    """
    if not ids:
        raise ValueError("at least two attempt ids are required")
    if any(not id_ for id_ in ids):
        raise ValueError("attempt ids must be non-empty")
    inputs = list(ids)
    outcome: dict[str, Any] = {}

    def update(text: str | None) -> tuple[str, None]:
        defs = _parse_defs(text)
        groups: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        for id_ in inputs:
            expanded = _expand_group(id_, defs, state_dir)
            if expanded is None:
                raise ValueError(f"unknown id {id_!r}: not a definition, alias, or recorded trace")
            key, traces, aliases = expanded
            groups.setdefault(key, (traces, aliases))
        if len(groups) < 2:
            raise ValueError("merge needs at least two distinct attempts")

        members: list[str] = []
        alias_ids: list[str] = []
        for traces, aliases in groups.values():
            members.extend(t for t in traces if t not in members)
            alias_ids.extend(a for a in aliases if a not in alias_ids)
        if len(members) < 2:
            raise ValueError("merge needs at least two distinct traces")
        for alias in alias_ids:
            if not _valid_id(alias):
                raise ValueError(f"legacy attempt id {alias!r} is not a safe identifier and cannot be merged")

        member_set = set(members)
        session_keys: dict[str, str] = {}
        for span in iter_spans(state_dir):
            trace_id = _str_value(span.get("traceId"))
            if trace_id and trace_id in member_set and trace_id not in session_keys:
                key = _str_value(_span_attrs(span).get("session.key"))
                if key:
                    session_keys[trace_id] = key
        distinct_sessions = sorted(set(session_keys.values()))
        if len(distinct_sessions) > 1:
            raise ValueError(
                f"members belong to different sessions {distinct_sessions}; cross-session merge is not supported"
            )

        remaining = {def_id: entry for def_id, entry in defs.items() if def_id not in groups}
        occupied = set(remaining) | member_set | set(alias_ids)
        for entry in remaining.values():
            occupied.update(entry.get("aliases") or [])
            occupied.update(entry["traces"])
        new_id = new_attempt_id()
        while new_id in occupied:
            new_id = new_attempt_id()

        entry: dict[str, Any] = {"traces": members, "ts": _now_iso()}
        if alias_ids:
            entry["aliases"] = alias_ids
        candidate = {**remaining, new_id: entry}
        if not _check_defs_invariants(candidate):
            raise ValueError("merge would violate attempt definition invariants")

        current_pins = pins(state_dir)
        snapshot = {m: dict(current_pins[m]) for m in members if m in current_pins}
        if snapshot:
            pin(new_id, reason=_join_reasons(r.get("reason") for r in snapshot.values()), state_dir=state_dir)
            outcome["definition_pin"] = pins(state_dir).get(new_id)
        outcome["new_id"] = new_id
        outcome["snapshot"] = snapshot
        return _dump_defs(candidate), None

    try:
        atomic_update(_defs_path(state_dir), update)
    except Exception:
        written = outcome.get("definition_pin")
        if written is not None:
            try:

                def rollback(registry: dict[str, dict[str, Any]]) -> None:
                    if registry.get(outcome["new_id"]) == written:
                        del registry[outcome["new_id"]]

                _update_pins(state_dir, rollback)
            except Exception:  # noqa: BLE001 — rollback is best-effort
                logger.debug("trajectory: merge pin rollback failed", exc_info=True)
        raise
    return outcome["new_id"], outcome["snapshot"]


def _merge_cleanup(new_id: str, snapshot: dict[str, dict[str, Any]], state_dir: Path | None = None) -> None:
    """Drop the member pins the publish migrated up. Best-effort by design:
    the merge is already committed, so a failure here only leaves redundant
    over-protective pins (unpin_attempt converges them). Re-checks under the
    attempts lock and compare-and-deletes, so a concurrent split (protection
    moved back down) or a fresh manual pin is never clobbered."""
    if not snapshot:
        return
    try:

        def check(text: str | None) -> tuple[None, None]:
            defs = _parse_defs(text)
            if new_id in defs:

                def clean(registry: dict[str, dict[str, Any]]) -> None:
                    for member, record in snapshot.items():
                        if registry.get(member) == record:
                            del registry[member]

                _update_pins(state_dir, clean)
            return None, None

        atomic_update(_defs_path(state_dir), check)
    except Exception:  # noqa: BLE001 — cleanup must not fail a committed merge
        logger.debug("trajectory: merge cleanup failed; redundant pins remain", exc_info=True)


def split_attempt(id_: str, state_dir: Path | None = None) -> tuple[str, ...] | None:
    """Delete the definition owning ``id_``; members revert to per-trace attempts.

    Protection carried by the definition or its aliases migrates down: one
    pins transaction (inside the attempts lock) pins every member (joining any
    existing member reason) and drops the definition/alias pins, then the
    deletion commits — so a failed commit leaves members protected and the
    definition still discoverable. Merged-attempt verdicts do not transfer to
    members. Returns the member trace ids, or None when nothing owns ``id_``.
    """

    def update(text: str | None) -> tuple[str | None, tuple[str, ...] | None]:
        defs = _parse_defs(text)
        owner = _owner_of(id_, defs)
        if owner is None:
            return None, None
        entry = defs[owner]
        members = list(entry["traces"])
        protected_ids = (owner, *(entry.get("aliases") or []))

        # The definition/alias pins must be read inside the pins lock: a
        # lock-free read races a concurrent pin(), skipping the migration and
        # leaving members unprotected after the definition is deleted.
        def mutate(registry: dict[str, dict[str, Any]]) -> None:
            pinned_records = {pid: registry[pid] for pid in protected_ids if pid in registry}
            if pinned_records:
                inherited = [record.get("reason") for record in pinned_records.values()]
                for member in members:
                    existing = (registry.get(member) or {}).get("reason")
                    registry[member] = {"reason": _join_reasons([existing, *inherited]), "ts": _now_iso()}
            for pid in protected_ids:
                registry.pop(pid, None)

        _update_pins(state_dir, mutate)
        remaining = {def_id: e for def_id, e in defs.items() if def_id != owner}
        return _dump_defs(remaining), tuple(members)

    return atomic_update(_defs_path(state_dir), update)


def pin_attempt(id_: str, *, reason: str = "", state_dir: Path | None = None) -> str:
    """Pin the attempt addressed by ``id_``; returns the id actually pinned.

    Resolves the current owner (definition, alias, member, legacy group, or
    the trace itself) inside the attempts lock and writes the pin while still
    holding it, so the operation is linearized against merge/split: a
    concurrent split either sees this pin and migrates it, or completed first
    and this call re-resolves (or refuses). Raises ``LookupError`` for an
    unresolvable id — never writes a dangling pin.
    """

    def check(text: str | None) -> tuple[None, str]:
        defs = _parse_defs(text)
        owner = _owner_of(id_, defs)
        if owner is None:
            expanded = _expand_group(id_, defs, state_dir)
            if expanded is None:
                raise LookupError(f"no spans found for id {id_!r}")
            owner = expanded[0]
        pin(owner, reason=reason, state_dir=state_dir)
        return None, owner

    return atomic_update(_defs_path(state_dir), check)


def unpin_attempt(id_: str, state_dir: Path | None = None) -> bool:
    """Remove every pin protecting the attempt addressed by ``id_``.

    Resolves the current owner under the attempts lock (a lock-free resolve
    races re-merges), then clears the definition id, its aliases, all member
    traces, and the literal id in one pins transaction. Without a definition
    the id expands through spans, so a legacy group is cleared whole. Note
    this removes member-level protection too. Returns whether any pin existed.
    """

    def check(text: str | None) -> tuple[None, bool]:
        defs = _parse_defs(text)
        owner = _owner_of(id_, defs)
        if owner is not None:
            entry = defs[owner]
            targets = {owner, *(entry.get("aliases") or []), *entry["traces"]}
        else:
            expanded = _expand_group(id_, defs, state_dir)
            if expanded is None:
                targets = {id_}
            else:
                key, traces, aliases = expanded
                targets = {key, *aliases, *traces}
        targets.add(id_)

        def clean(registry: dict[str, dict[str, Any]]) -> bool:
            removed = False
            for target in targets:
                if target in registry:
                    del registry[target]
                    removed = True
            return removed

        return None, _update_pins(state_dir, clean)

    return atomic_update(_defs_path(state_dir), check)


def is_pinned(
    span: dict[str, Any],
    state_dir: Path | None = None,
    *,
    _pins: dict | None = None,
    _defs: dict | None = None,
) -> bool:
    """Whether a span record is protected — directly (traceId / legacy
    attempt.id) or through its owning definition or any of its aliases."""
    registry = pins(state_dir) if _pins is None else _pins
    if not registry:
        return False
    trace_id = _str_value(span.get("traceId"))
    legacy = _str_value(_span_attrs(span).get("attempt.id"))
    if (trace_id and trace_id in registry) or (legacy and legacy in registry):
        return True
    defs = definitions(state_dir) if _defs is None else _defs
    for def_id, entry in defs.items():
        if trace_id in (entry.get("traces") or []):
            return def_id in registry or any(alias in registry for alias in entry.get("aliases") or [])
    return False


def resolve_attempt_id(id_: str, state_dir: Path | None = None) -> str | None:
    """Canonical attempt id for ``id_`` (a definition id, alias, or trace id).

    A definition (addressed by its id, an absorbed alias, or a member trace)
    wins; otherwise a turn's trace id resolves through its spans' legacy
    ``attempt.id`` so pre-definition records stay addressable, falling back to
    the id itself. Returns ``None`` when nothing matches at all.
    """
    owner = _owner_of(id_, definitions(state_dir))
    if owner is not None:
        return owner
    found = False
    for span in iter_spans(state_dir, attempt_id=id_):
        found = True
        attempt_id = _str_value(_span_attrs(span).get("attempt.id"))
        if attempt_id:
            return attempt_id
    return id_ if found else None


def span_log_paths(state_dir: Path | None = None) -> list[Path]:
    """All span log files in write order: archived (oldest first), then active."""
    base = state_dir or tracing_config.state_dir()
    logs_dir = base / "logs"
    archived = sorted((logs_dir / "archive").glob("*/audit-spans-*.log"))
    active = logs_dir / "audit-spans.log"
    return archived + ([active] if active.exists() else [])


def iter_spans(
    state_dir: Path | None = None,
    *,
    attempt_id: str | None = None,
    trace_id: str | None = None,
    session_key: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield span records across all logs in write order, filtered.

    ``attempt_id`` naming a definition (directly, via an alias, or via a
    member trace) matches the definition's member trace ids — one member
    addresses the whole attempt, like resolve_attempt_id; use ``trace_id`` for
    a single trace. Otherwise it matches spans whose ``attempt.id`` OR
    ``traceId`` equals it (single-turn attempts are addressed by their trace
    id). Unparseable lines are skipped — one bad line must not hide a whole
    trajectory.
    """
    match_ids: set[str] | None = None
    if attempt_id is not None:
        defs = definitions(state_dir)
        owner = _owner_of(attempt_id, defs)
        if owner is not None:
            match_ids = set(defs[owner]["traces"])
    for path in span_log_paths(state_dir):
        try:
            raw_lines = path.read_bytes().splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            # Decode per line: one invalid-UTF-8 byte must hide one record,
            # not the whole log file.
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(span, dict):
                continue
            attrs = _span_attrs(span)
            span_trace_id = _str_value(span.get("traceId"))
            if attempt_id is not None:
                if match_ids is not None:
                    if span_trace_id not in match_ids:
                        continue
                elif attrs.get("attempt.id") != attempt_id and span_trace_id != attempt_id:
                    continue
            if trace_id is not None and span_trace_id != trace_id:
                continue
            if session_key is not None and attrs.get("session.key") != session_key:
                continue
            yield span
