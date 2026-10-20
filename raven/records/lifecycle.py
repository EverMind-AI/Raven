"""Which state a record may move to next, and which move it may not make.

A record's state is the one thing about it that a second run has to agree
with: the brief that was built, the message that was sent, the reply that
came back. If two runs disagree about that, the store stops being a record
of what happened and becomes a record of what each run believed.

So the moves are declared here, in one place, and the store refuses the ones
that are not declared. A refused move is not a bug to be worked around: it is
the mechanism that stops a resumed run from sending a second email to
somebody who already answered.
"""

from __future__ import annotations

from raven.records.types import LifecycleError, Record, RecordType, now_stamp

__all__ = [
    "RECORD_STATES",
    "TERMINAL_STATES",
    "allowed_transitions",
    "can_transition",
    "check_transition",
    "is_terminal",
    "transition",
]


# The chain a prospect walks. It is the one the vertical's stages produce, in
# the order they produce it: a company is found, judged worth the research,
# researched, turned into a brief, offered, answered, chased once, and then
# either won or lost.
OPPORTUNITY_CHAIN: tuple[str, ...] = (
    "new",
    "discovered",
    "qualified",
    "researched",
    "artifact_built",
    "pitched",
    "engaged",
    "replied",
    "followed_up",
)

# The other record kinds walk shorter chains, named after the stage that
# produces them. They are here so that a mistake in one of them is refused the
# same way a mistake in the prospect chain is, rather than being noticed later
# by whoever reads the rows.
COMPANY_CHAIN: tuple[str, ...] = ("new", "discovered", "qualified", "researched")
CONTACT_CHAIN: tuple[str, ...] = ("new", "discovered", "verified")
ARTIFACT_CHAIN: tuple[str, ...] = ("new", "built", "qa_passed")
INTERACTION_CHAIN: tuple[str, ...] = ("new", "prepared", "sent")

# The states a run can reach when the run ends rather than when it advances.
# Where a run may reach them from is the whole point: a prospect is won or
# lost however far it got, but an artefact is delivered only after it passed
# the QA gate, and a message is delivered, bounced or unsubscribed only once
# it has been sent. A step that could be stepped over would not be a step.
OPPORTUNITY_OUTCOMES: tuple[str, ...] = ("won", "lost")
COMPANY_OUTCOMES: tuple[str, ...] = ()
CONTACT_OUTCOMES: tuple[str, ...] = ()
ARTIFACT_OUTCOMES: tuple[str, ...] = ("delivered",)
INTERACTION_OUTCOMES: tuple[str, ...] = ("delivered", "bounced", "unsubscribed")

# Where each chain's outcomes may be reached from: "any" for a chain whose
# run can end early, or the named step whose arrival makes the outcome the
# only thing left to say.
ANY: str = "any"
OUTCOME_SOURCES: dict[RecordType, str | tuple[str, ...]] = {
    RecordType.OPPORTUNITY: ANY,
    RecordType.COMPANY: ANY,
    RecordType.CONTACT: ANY,
    RecordType.ARTIFACT: ("qa_passed",),
    RecordType.INTERACTION: ("sent",),
}

RECORD_STATES: dict[RecordType, tuple[str, ...]] = {
    RecordType.OPPORTUNITY: OPPORTUNITY_CHAIN + OPPORTUNITY_OUTCOMES,
    RecordType.COMPANY: COMPANY_CHAIN + COMPANY_OUTCOMES,
    RecordType.CONTACT: CONTACT_CHAIN + CONTACT_OUTCOMES,
    RecordType.ARTIFACT: ARTIFACT_CHAIN + ARTIFACT_OUTCOMES,
    RecordType.INTERACTION: INTERACTION_CHAIN + INTERACTION_OUTCOMES,
}

# The chain a record walks, and the outcomes it may reach from it.
RECORD_CHAINS: dict[RecordType, tuple[tuple[str, ...], tuple[str, ...]]] = {
    RecordType.OPPORTUNITY: (OPPORTUNITY_CHAIN, OPPORTUNITY_OUTCOMES),
    RecordType.COMPANY: (COMPANY_CHAIN, COMPANY_OUTCOMES),
    RecordType.CONTACT: (CONTACT_CHAIN, CONTACT_OUTCOMES),
    RecordType.ARTIFACT: (ARTIFACT_CHAIN, ARTIFACT_OUTCOMES),
    RecordType.INTERACTION: (INTERACTION_CHAIN, INTERACTION_OUTCOMES),
}

# A state may move to the next step of its chain, or to one of the chain's
# outcomes when the chain says the outcome is reachable from there. It may
# not skip a step: the steps are the guarantee that a brief was researched
# before it was offered and an artefact passed QA before it was delivered.
# It may not move backwards either -- a record's state only ever moves
# forward, so a second run meeting the same thing cannot demote a prospect
# that has already been answered.
def _allowed_moves() -> dict[RecordType, dict[str, frozenset[str]]]:
    """The declared moves for every record kind."""
    moves: dict[RecordType, dict[str, frozenset[str]]] = {}
    for record_type, (chain, outcomes) in RECORD_CHAINS.items():
        by_state: dict[str, frozenset[str]] = {}
        for index, state in enumerate(chain):
            nxt = chain[index + 1 : index + 2]
            by_state[state] = frozenset(nxt)
        source = OUTCOME_SOURCES.get(record_type, ANY)
        reachable = chain if source is ANY else tuple(source)
        for state in reachable:
            by_state.setdefault(state, frozenset())
            by_state[state] = by_state[state] | frozenset(outcomes)
        for outcome in outcomes:
            by_state[outcome] = frozenset()
        moves[record_type] = by_state
    return moves


_ALLOWED: dict[RecordType, dict[str, frozenset[str]]] = _allowed_moves()

# A state nothing follows: the prospect is won or lost, the artefact was
# delivered or the message bounced. A record in one of these is finished, and
# a run that tries to move it is asking to rewrite history.
TERMINAL_STATES: frozenset[str] = frozenset({"won", "lost", "delivered", "bounced", "unsubscribed"})


def allowed_transitions(record_type: RecordType, state: str) -> frozenset[str]:
    """The states a record of this kind may move to from here."""
    moves = _ALLOWED.get(record_type, {})
    if state not in moves:
        raise LifecycleError(f"unknown state {state!r} for a {record_type.value} record")
    return moves[state]


def can_transition(record_type: RecordType, state: str, nxt: str) -> bool:
    """Whether the move is declared."""
    try:
        return nxt in allowed_transitions(record_type, state)
    except LifecycleError:
        return False


def is_terminal(state: str) -> bool:
    """Whether the state is one nothing follows."""
    return state in TERMINAL_STATES


def check_transition(record_type: RecordType, state: str, nxt: str) -> None:
    """Raise unless the move is declared."""
    if nxt not in allowed_transitions(record_type, state):
        raise LifecycleError(
            f"a {record_type.value} record in {state!r} may not move to {nxt!r} "
            f"(allowed: {sorted(allowed_transitions(record_type, state))})"
        )


def transition(record: Record, nxt: str) -> Record:
    """The record as it would be after the move, refused if the move is not declared.

    Returns a new record rather than changing the one it was given: the store
    is the thing that decides whether a move is written down, and it needs the
    record it was handed to stay as it was if it refuses.
    """
    check_transition(record.type, record.state, nxt)
    moved = Record(
        type=record.type,
        dedupe_key=record.dedupe_key,
        fields=dict(record.fields),
        provenance=list(record.provenance),
        state=nxt,
        id=record.id,
        created_at=record.created_at,
        updated_at=now_stamp(),
    )
    return moved
