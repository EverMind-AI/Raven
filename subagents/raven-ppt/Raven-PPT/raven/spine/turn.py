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
    conversation: str | None = None
    busy: BusyPolicy = BusyPolicy.APPEND
    sentinel: SentinelExtras | None = None
    # Verbatim delivery: when set, the turn skips the model entirely and emits
    # this text as-is (a background task pushing a finished result back to its
    # conversation). Runs through the lane so the session write stays serialized;
    # the model never sees it, so it cannot be rewritten. See AgentLoop.run_turn.
    deliver_text: str | None = None
