"""The intent and ownership of a single request: the one input to submit()."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from raven.spine.message import Media, Source


class Origin(StrEnum):
    """Who the request belongs to — drives pool, control eligibility, accounting."""

    USER = "user"
    SENTINEL = "sentinel"
    CRON = "cron"
    HEARTBEAT = "heartbeat"
    SUBAGENT = "subagent"


# The origins the runtime starts on its own, which nobody is waiting on: they share the system pool and
# read the loose silence rule. SUBAGENT is the turn that re-injects a result; a sub-agent's own
# execution runs off the scheduler behind a separate gate.
PROACTIVE_ORIGINS = frozenset({Origin.SENTINEL, Origin.CRON, Origin.HEARTBEAT, Origin.SUBAGENT})


class AnswerlessTurnError(Exception):
    """A runner's own report that the turn it ran ended with no answer.

    Raised where an ordinary exception would misreport the failure: the runner
    has already put it into words a reader can act on (the model call it gave
    up on, and why), so ``describe_failure`` passes the text through unchanged
    instead of prefixing it with this class name, and the ``turn_ended`` marker
    that says so is filed before the failure leaves the loop.

    ``full_detail`` is the same report at length, for a reader who expands the
    row. The runner builds and scrubs it -- the kernel imports nothing to do
    that with -- and the lane copies it onto ``TurnFailed.full_detail``.
    """

    def __init__(self, *args: object, full_detail: str | None = None, no_output: bool = False) -> None:
        super().__init__(*args)
        self.full_detail = full_detail
        # The last reply had no text, no tool call and nothing else to show (no reasoning, refusal or cut at the
        # output ceiling) once recovery was spent, or no text with recovery off. Earlier attempts are not covered.
        self.no_output = no_output


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
    """One request to process."""

    origin: Origin
    source: Source
    text: str
    media: tuple[Media, ...] = ()
    # The turn's own identity, carried on its lifecycle events so a consumer can
    # tell WHICH turn just ended — not any inbound message's id: a turn the
    # runtime submits itself has no inbound message at all. A submitter that
    # must correlate the end with its own call sets this (``turn.send`` returns
    # it to the client); left unset, the lane mints one, so every turn is
    # identified whether or not its submitter cared.
    turn_id: str | None = None
    # When this request reached the server, ISO-8601. Set by a submitter whose
    # request does not run at once: an inject waits in the lane's mailbox until
    # the running turn's next gap, so the drain's own clock would file a
    # correction typed a minute in under the time that gap happened to come.
    # Unset everywhere else -- a turn that runs when it is submitted is stamped
    # as it starts (``turn_path``'s ``turn_received_at``).
    received_at: str | None = None
    conversation: str | None = None
    busy: BusyPolicy = BusyPolicy.APPEND
    sentinel: SentinelExtras | None = None
    # Verbatim delivery: when set, the turn skips the model entirely and emits
    # this text as-is (a background task pushing a finished result back to its
    # conversation). Runs through the lane so the session write stays serialized;
    # the model never sees it, so it cannot be rewritten. See AgentLoop.run_turn.
    deliver_text: str | None = None
    # What re-entered the conversation here, when this turn is a delegated
    # result coming back rather than something a person sent. Carried so the
    # STORED user entry can be marked: a reader replaying the session has no
    # other way to tell a re-injection from a question, and drew it as one --
    # prompt-injection fence and all -- while a client watching live drew the
    # delivery row instead. Shape: ``{"kind": "spawn"|"dag", "label": ...,
    # "status": ..., "run_id": ...}``, the same identity the
    # ``subagent.delivered`` event carries.
    delegated: dict[str, Any] | None = None
    # Direct sub-agent delivery: when set to ``(agent, handle)`` the turn skips
    # the model and runs against that instance instead, streaming its reply back
    # to this conversation. Runs on that instance's own lane (``direct_lane``),
    # so it is concurrent with the main agent's turn and with every other
    # instance's, and unlike every other turn it is NOT written to the session
    # transcript -- a direct chat exists to keep those exchanges out of the main
    # agent's context. See AgentLoop.run_turn.
    direct_target: tuple[str, str] | None = None
    # The original session key cannot be recovered by splitting a direct lane:
    # both the session and the instance handle may contain the lane separator.
    parent_session: str | None = None
    # Per-turn UI choice for Playbook generation. None inherits the configured
    # default; off/task/persona are resolved before the setup model call.
    playbook_mode: str | None = None
    # ``(trace_id, span_id)`` of the span that dispatched the work this turn
    # reports on, set only by a submitter announcing a dispatched result. A turn
    # no longer inherits its submitter's context, so where it came from has to
    # be stated here; the kernel carries the pair to the runner uninterpreted.
    dispatch_link: tuple[str, str] | None = None


# A lane is a serial domain, so an instance that is to answer while the main
# agent (or another instance) is answering needs a lane of its own. The
# scheduler already keys lanes on ``conversation`` and documents the case: a
# channel that keys by a sub-conversation within a chat formats that key itself.
_LANE_SEP = "#"


def direct_lane(session_key: str, agent: str, handle: str) -> str:
    """The lane a direct chat with one instance runs on.

    This identifies the queue, turn, and addressee per instance. The request
    carries its original parent session separately for permissions and records.
    """
    return f"{session_key}{_LANE_SEP}{agent}/{handle}"


def session_of(lane: str) -> str:
    """Legacy string-only lookup for parents without a separator. A request must
    use ``session_key_for`` instead, since a caller can choose a parent key
    containing ``#`` and a handle can contain it too.
    """
    return lane.split(_LANE_SEP, 1)[0]
