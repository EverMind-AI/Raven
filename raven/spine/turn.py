"""The intent and ownership of a single request: the one input to submit()."""

from dataclasses import dataclass
from enum import StrEnum

from raven.spine.message import Media, Source


class Origin(StrEnum):
    """Who the request belongs to — drives pool, control eligibility, accounting."""

    USER = "user"
    SENTINEL = "sentinel"
    CRON = "cron"
    HEARTBEAT = "heartbeat"
    SUBAGENT = "subagent"


class BusyPolicy(StrEnum):
    """What to do when the conversation's lane is already busy."""

    APPEND = "append"
    INJECT = "inject"
    INTERRUPT = "interrupt"


# A lane is a serial domain, so a turn that is to answer while another is
# answering needs a lane of its own. The scheduler already keys lanes on
# ``conversation`` and documents the case: a channel that keys by a
# sub-conversation within a chat formats that key itself.
#
# Nothing in this repo formats such a key yet, so every lane here IS its own
# session and ``session_of`` returns what it was given. It exists because a
# consumer that maps a lane to the subscription it belongs to must not have to
# know whether the lane happens to be a plain session key today.
_LANE_SEP = "#"


def session_of(lane: str) -> str:
    """The session a lane belongs to; a main-agent lane *is* its session.

    Splits on the **first** separator, which is what makes the encoding safe: a
    session key is ``channel:chat_id`` and never contains one, while a handle is
    free-form text the model chose and may contain anything at all.
    """
    return lane.split(_LANE_SEP, 1)[0]


@dataclass(frozen=True)
class SentinelExtras:
    """Sentinel's private per-turn extras namespace (canon v8).

    ``action_origin`` marks a turn the Sentinel injected as the execution of a
    menu pick the user accepted (action_executor's exec_kind=reply): it is the
    user's intent, so it runs the normal user path (Personalizer / after_send),
    but it must NOT re-trigger engagement detection (the accept was already
    recorded when the user picked) and the menu router must not re-consume it.
    Read only by Sentinel's own components (decision_consumer / on_user_inbound);
    the spine core never reads it.
    """

    action_origin: bool = False


@dataclass(frozen=True)
class TurnRequest:
    """One request to process.

    ``message_id`` is the inbound message's own id — the default anchor an
    outbound reply threads back to (the outbound side carries it as the
    reply_to field on Text).
    """

    origin: Origin
    source: Source
    text: str
    media: tuple[Media, ...] = ()
    message_id: str | None = None
    # The turn's own identity, carried on its lifecycle events so a consumer can
    # tell WHICH turn just ended. Distinct from ``message_id`` above, which is an
    # inbound channel message's id: one inbound message is one turn today, but a
    # turn the runtime submits itself has no inbound message at all. A submitter
    # that must correlate the end with its own call sets this (``turn.send``
    # returns it to the client); left unset, the lane mints one, so every turn is
    # identified whether or not its submitter cared.
    turn_id: str | None = None
    conversation: str | None = None
    busy: BusyPolicy = BusyPolicy.APPEND
    sentinel: SentinelExtras | None = None
    # Verbatim delivery: when set, the turn skips the model entirely and emits
    # this text as-is (a background task pushing a finished result back to its
    # conversation). Runs through the lane so the session write stays serialized;
    # the model never sees it, so it cannot be rewritten. See AgentLoop.run_turn.
    deliver_text: str | None = None
