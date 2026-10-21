"""The outbound idempotency guard.

Raven's own documentation says the quiet thing out loud: *"A retry can repeat
external side effects."* A run that writes to somebody, dies before it
records that it did, and is retried writes to that somebody twice. For a
prospect that is not a retry, it is a stranger who contacted them twice.

This package is the guard against that. It gives an outbound effect three
things a bare send does not have:

* **A record before the effect.** A send is prepared as an ``interaction``
  record *before* anything leaves the machine, so the question "did we write
  to this person" is answered by the store rather than by a retry's
  optimism.
* **A way to tell a second attempt from the first.** A prepared send carries
  a **correlation token**, and a second attempt at the same subject is
  refused while the first attempt's outcome is still unknown.
* **A way to say "I do not know".** An attempt that was interrupted leaves
  the record in the ``prepared`` state, and a prepared record is an
  **unresolved send** -- an **ambiguous outcome**. It is surfaced, never
  auto-resent and never quietly forgiven.

The record store's own interaction chain already says what this needs:
``new -> prepared -> sent``, with ``delivered``, ``bounced`` and
``unsubscribed`` reachable only from ``sent``. A message is delivered only
once it was sent. The guard writes that chain rather than inventing a
parallel one.

The claim on a subject is kept in the store, not in the process. The check
and the claim are one transaction under the store's writer lock, and the
subject is unique in the claim table, so several processes racing on one
subject produce one send and several refusals rather than several sends.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from raven.outbound.token import new_token
from raven.records.store import RecordStore
from raven.records.types import Record, RecordType

__all__ = [
    "NOT_SENT",
    "PREPARED",
    "SENT",
    "UNKNOWN",
    "PreparedSend",
    "Reconciliation",
    "UnresolvedSendError",
    "complete_send",
    "find_by_token",
    "guard_send",
    "prepare_send",
    "reconcile",
    "unresolved",
]

#: The interaction state a send rests in until the outcome is known. A
#: prepared send is one that may or may not have happened.
PREPARED = "prepared"

#: The interaction state a send reaches once it is known to have gone out.
SENT = "sent"

#: What a probe can say about a prepared send.
SENT_VERDICT = "sent"
NOT_SENT = "not_sent"
UNKNOWN = "unknown"

#: The claims the guard keeps: one row per prepared send, and the subject
#: is unique in the table, so a subject is claimed by one send at a time.
#: The claim is what makes "one outbound send cannot be sent twice" a
#: property of the database rather than a property of one process's timing.
_CLAIMS_TABLE = """
CREATE TABLE IF NOT EXISTS prepared_sends (
    subject_key  TEXT PRIMARY KEY,
    subject      TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    token        TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    created_at   TEXT NOT NULL
)
"""


class UnresolvedSendError(RuntimeError):
    """A second attempt at a subject whose first attempt is still unresolved.

    Raised rather than allowed, because the alternative -- sending again and
    finding out later -- is the failure this package exists to prevent.
    """


@dataclass(frozen=True)
class PreparedSend:
    """One outbound message, prepared but not yet known to have happened.

    The token is the handle a reply quotes, and it is written into the
    message before the message is sent.
    """

    record_id: str
    token: str
    subject: str
    subject_type: str
    channel: str
    body: str
    fields: dict[str, Any] = field(default_factory=dict)

    def message_with_token(self) -> str:
        """The body as it goes out, with the token quoted at the end.

        The token is on its own line so a reader sees it as part of the
        message rather than as a stray string, and so a reply quoting it is
        quoting something they were meant to quote.
        """
        return f"{self.body}\n\n{self.token}"


@dataclass(frozen=True)
class Reconciliation:
    """What a reconciliation run decided about the prepared sends.

    ``sent`` were confirmed to have gone out. ``not_sent`` were confirmed
    not to have, and their records are gone -- a record of a message that
    was never sent is a claim that does not belong in the store.
    ``ambiguous`` are the ones nobody could decide, and they stay prepared
    until a person decides them.
    """

    sent: tuple[Record, ...] = ()
    not_sent: tuple[Record, ...] = ()
    ambiguous: tuple[Record, ...] = ()

    @property
    def resolved(self) -> int:
        return len(self.sent) + len(self.not_sent)

    @property
    def unresolved(self) -> int:
        return len(self.ambiguous)


def _interaction_fields(
    subject: str,
    subject_type: str,
    channel: str,
    body: str,
    token: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The fields an interaction record carries."""
    fields: dict[str, Any] = {
        "subject": subject,
        "subject_type": subject_type,
        "channel": channel,
        "body": body,
        "correlation_token": token,
    }
    if extra:
        fields.update(extra)
    return fields


def unresolved(
    store: RecordStore,
    subject: str | None = None,
    subject_type: str | None = None,
) -> list[Record]:
    """The prepared sends whose outcome nobody knows.

    A prepared send is one that may or may not have gone out. Until it is
    resolved, a second attempt at the same subject is refused.
    """
    prepared = store.list_records(record_type=RecordType.INTERACTION, state=PREPARED)
    out: list[Record] = []
    for interaction in prepared:
        if subject is not None and interaction.fields.get("subject") != subject:
            continue
        if subject_type is not None and interaction.fields.get("subject_type") != subject_type:
            continue
        out.append(interaction)
    return out


def find_by_token(store: RecordStore, token: str) -> Record | None:
    """The interaction a correlation token belongs to, or None.

    The token is the interaction's key, so this is a lookup rather than a
    scan -- and a reply that quotes a token resolves in one step.
    """
    if not token:
        return None
    return store.get(RecordType.INTERACTION, token)


def prepare_send(
    store: RecordStore,
    *,
    subject: str,
    subject_type: str,
    channel: str,
    body: str,
    extra: dict[str, Any] | None = None,
) -> PreparedSend:
    """Prepare an outbound message, and write down that it is about to happen.

    The interaction record is written **before** the caller gets the send,
    so nothing can leave the machine without the store already knowing it
    was going to.

    Refuses when the subject has an unresolved send. A second attempt at
    somebody whose first attempt is still in doubt is the double-send this
    package exists to prevent, and the refusal says which attempt is
    unresolved so the operator can reconcile it first.

    The check and the claim are one write. They happen in a single
    transaction under the store's writer lock, and the claim table holds one
    row per subject, so two processes racing on the same subject produce one
    send and one refusal rather than two sends.
    """
    if not subject:
        raise ValueError("an outbound message needs a subject")
    if not channel:
        raise ValueError("an outbound message needs a channel")

    subject_key = _subject_key(subject, subject_type)
    with store.writing() as conn:
        _ensure_claims_table(conn)
        _release_dead_claims(conn, subject_key)
        pending = unresolved(store, subject=subject, subject_type=subject_type)
        if pending:
            raise UnresolvedSendError(_refusal(subject, _pending_tokens(pending)))

        token = new_token()
        record = store.put(
            _build_interaction(
                subject=subject,
                subject_type=subject_type,
                channel=channel,
                body=body,
                token=token,
                extra=extra,
            )
        )
        try:
            conn.execute(
                "INSERT INTO prepared_sends "
                "(subject_key, subject, subject_type, token, record_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (subject_key, subject, subject_type, token, record.id, record.created_at),
            )
        except sqlite3.IntegrityError as exc:
            claimed = conn.execute(
                "SELECT token FROM prepared_sends WHERE subject_key = ?",
                (subject_key,),
            ).fetchone()
            raise UnresolvedSendError(_refusal(subject, claimed[0] if claimed else "another process")) from exc

    return PreparedSend(
        record_id=record.id,
        token=token,
        subject=subject,
        subject_type=subject_type,
        channel=channel,
        body=body,
        fields=dict(record.fields),
    )


def _subject_key(subject: str, subject_type: str) -> str:
    """The key a subject is claimed under.

    The subject and its kind together, because the check the guard makes is
    made against both: a claim on a company does not claim a person who
    happens to carry the same name.
    """
    return f"{subject_type}\x00{subject}"


def _ensure_claims_table(conn: sqlite3.Connection) -> None:
    """The claims table, made the first time a claim is asked for."""
    conn.execute(_CLAIMS_TABLE)


def _release_dead_claims(conn: sqlite3.Connection, subject_key: str) -> None:
    """Drop the claim on a subject whose send is no longer prepared.

    A send that was resolved, or removed, left its claim behind. The claim
    is about a send whose outcome nobody knows, and a resolved send is not
    that, so the claim goes with it.
    """
    conn.execute(
        "DELETE FROM prepared_sends WHERE subject_key = ? AND NOT EXISTS ("
        "SELECT 1 FROM records WHERE records.id = prepared_sends.record_id AND records.state = ?)",
        (subject_key, PREPARED),
    )


def _release_claim(store: RecordStore, token: str) -> None:
    """Let the subject be claimed again, now that the outcome is known."""
    with store.writing() as conn:
        _ensure_claims_table(conn)
        conn.execute("DELETE FROM prepared_sends WHERE token = ?", (token,))


def _pending_tokens(pending: list[Record]) -> str:
    """The tokens of the sends that are still unresolved."""
    return ", ".join(
        str(interaction.fields.get("correlation_token") or interaction.id)
        for interaction in pending
    )


def _refusal(subject: str, tokens: str) -> str:
    """Why a second attempt at this subject is refused."""
    return (
        f"there is already a send to {subject!r} whose outcome is unknown ({tokens}). "
        f"Reconcile it before sending again."
    )


def _build_interaction(
    *,
    subject: str,
    subject_type: str,
    channel: str,
    body: str,
    token: str,
    extra: dict[str, Any] | None = None,
):
    """The interaction record a prepared send is written as."""
    from raven.records.types import Record

    return Record(
        type=RecordType.INTERACTION,
        dedupe_key=token,
        fields=_interaction_fields(subject, subject_type, channel, body, token, extra),
        state=PREPARED,
    )


def complete_send(
    store: RecordStore,
    prepared: PreparedSend,
    *,
    message_id: str = "",
    outcome: str = SENT,
) -> Record:
    """Record that a prepared send went out.

    Idempotent: completing a send that is already sent returns the record
    as it is, so a caller that cannot tell whether it has already completed
    does not have to guess.
    """
    record = find_by_token(store, prepared.token)
    if record is None:
        raise UnresolvedSendError(
            f"the send {prepared.token!r} is not in the store, so it cannot be completed"
        )
    if record.state != PREPARED:
        return record

    from raven.records.types import now_stamp

    fields = dict(record.fields)
    if message_id:
        fields["message_id"] = message_id
    fields["sent_at"] = now_stamp()
    store.put(
        _rebuild(record, fields=fields, state=SENT if outcome == SENT else outcome)
    )
    _release_claim(store, prepared.token)
    return find_by_token(store, prepared.token) or record


def _rebuild(record: Record, *, fields: dict[str, Any], state: str) -> Record:
    """The same record with new fields and a new state."""
    from raven.records.types import Record

    return Record(
        type=record.type,
        dedupe_key=record.dedupe_key,
        fields=fields,
        provenance=list(record.provenance),
        state=state,
        id=record.id,
        created_at=record.created_at,
    )


def reconcile(
    store: RecordStore,
    probe: Callable[[Record], str] | None = None,
) -> Reconciliation:
    """Decide what happened to each prepared send.

    The probe is asked about one prepared send at a time and answers
    ``sent``, ``not_sent``, or anything else for "nobody can tell". The
    default probe cannot tell, so **nothing is ever resolved automatically**
    unless a caller says it knows.

    A confirmed send moves to ``sent``. A confirmed non-send is removed: a
    record of a message that never left is a claim that does not belong in
    the store, and leaving it would make the store say we wrote to somebody
    we did not write to. Anything else stays prepared and is reported as
    ambiguous.
    """
    verdicts: dict[str, str] = {}
    sent: list[Record] = []
    not_sent: list[Record] = []
    ambiguous: list[Record] = []

    for interaction in unresolved(store):
        verdict = UNKNOWN
        if probe is not None:
            try:
                verdict = str(probe(interaction) or UNKNOWN)
            except Exception:  # noqa: BLE001 - a probe that cannot answer is an unknown
                verdict = UNKNOWN
        verdicts[interaction.id] = verdict
        if verdict == SENT_VERDICT:
            from raven.records.types import now_stamp

            fields = dict(interaction.fields)
            fields["sent_at"] = now_stamp()
            store.put(_rebuild(interaction, fields=fields, state=SENT))
            _release_claim(store, interaction.dedupe_key)
            sent.append(interaction)
        elif verdict == NOT_SENT:
            store.delete(interaction.id)
            _release_claim(store, interaction.dedupe_key)
            not_sent.append(interaction)
        else:
            ambiguous.append(interaction)

    return Reconciliation(sent=tuple(sent), not_sent=tuple(not_sent), ambiguous=tuple(ambiguous))


@contextmanager
def guard_send(
    store: RecordStore,
    *,
    subject: str,
    subject_type: str,
    channel: str,
    body: str,
    extra: dict[str, Any] | None = None,
) -> Iterator[PreparedSend]:
    """Prepare a send, and record it as sent when the block returns.

    The record is written before the block runs, so the effect cannot
    happen without the store already knowing about it. If the block raises,
    the send is left **prepared** -- an ambiguous outcome -- rather than
    being marked sent or forgotten. An exception from a send does not mean
    the message did not go out; a timeout that lost its reply looks exactly
    like one that did.
    """
    prepared = prepare_send(
        store,
        subject=subject,
        subject_type=subject_type,
        channel=channel,
        body=body,
        extra=extra,
    )
    yield prepared
    complete_send(store, prepared)
