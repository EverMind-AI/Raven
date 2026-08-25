"""Translate raven's outbound wire events into ACP ``session/update`` frames.

This is the sink handed to ``build_rpc_stack``, which threads the *same*
``send_frame`` through the subscription emitter, all three brokers, the MCP
event bridge and the system methods. One sink therefore intercepts the entire
outbound surface -- which is the reason to sit here rather than to register an
outlet: ``spine/delivery.py``'s hub sink returns early on ``TurnStarted`` /
``TurnFailed`` / ``TurnEnded``, so an object registered only as an outlet never
sees a turn end, and a suspended ``session/prompt`` would never be answered.

The cost, stated rather than hidden: this parses a dict that was serialised one
layer up, and whatever ``TuiOutlet`` dropped is dropped for good (a progress
notice has no wire event at all, so no branch here can recover it).

Two invariants hold the turn together:

* **Exactly one terminating event resolves a prompt.** The gate on
  :class:`_Turn` makes a second one a no-op rather than an error, because a
  cancel followed by the sink's own failure event is the normal shape, not a bug.
* **A prompt is never answered with a JSON-RPC error.** A turn that failed still
  ends with a ``stopReason``; the failure is surfaced as message content. The
  narrower rule this replaces -- "suppress the -32099 cancel event" -- would
  have been wrong: that event is the *only* signal a cancelled turn produces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote

from loguru import logger

from raven.acp import protocol
from raven.acp.redact import redact
from raven.acp.tool_kinds import absolute_path, locations, title_for, tool_kind

# Every event type ``TuiOutlet``, the spine sink, the DAG bridge and the cron
# fan-out can put on a subscription. Pinned as a set so a new wire event fails
# the test that asserts this covers them, rather than being silently dropped by
# a translator that has no branch for it -- the failure mode where a client is
# missing information nobody notices is absent.
KNOWN_EVENT_TYPES = frozenset(
    {
        "token.delta",
        "thinking.delta",
        "tool.start",
        "tool.complete",
        "message.start",
        "message.complete",
        "error",
        "notice",
        "episode.start",
        "dag.run_started",
        "dag.node_updated",
        "dag.run_completed",
        "cron.delivered",
        "cron.missed",
        "media",
    }
)

# Outbound frames on ``send_frame`` that are not subscription events. Each one is
# a surface this translator does not serve; listed so that "we drop it" is a
# decision with a name on it, and so a newly-added notification shows up as an
# unknown method in the log instead of vanishing.
#
# ``approval.request`` and ``approval.closed`` stay on this list even though shell
# approvals *are* served now: they are served on the other side, by
# ``AcpPermissionBroker`` answering ``session/request_permission``, and the RPC
# broker that emits these is still constructed. So a frame arriving here means
# something reached the wrong broker, and dropping it is right -- a client that
# received one would have no method to route it to.
SIDE_CHANNEL_METHODS = frozenset(
    {
        "approval.request",
        "approval.closed",
        "clarify.request",
        "confirm.request",
        "system.update_available",
        "mcp.status",
        "memory.health",
        "oauth.pending",
        "oauth.done",
    }
)

# The reason string ``turn.cancel`` stamps on its one error event. Matched on the
# reason and not on the code: -32099 is also what a build failure and a draining
# scheduler use, and those are not cancellations.
_CANCELLED_REASON = "cancelled_by_client"

# A tool result preview is written for a person to read in a panel. The runtime
# already truncates and sets ``truncated``; this is the backstop for a tool that
# does not, so one runaway result cannot become a multi-megabyte frame.
MAX_RESULT_PREVIEW = 64 * 1024

# Terminal events held while a turn's own id is still unknown. The window is one
# RPC round trip wide (``turn.send`` emits ``message.start`` before it returns),
# so a handful is generous; the cap is here because the alternative is a dict
# that grows for the life of a connection whenever the runtime is busy.
MAX_DEFERRED_ENDINGS = 8

# Errors that end the SUBSCRIPTION, not just the turn. ``-32016`` is
# ``SubscriptionCapacityExceededError``: the emitter sends it and then removes
# the subscription, so it is the last frame this stream will ever carry. It
# deliberately carries no ``turn_id`` -- its shape is pinned by
# ``test_overflow_error_event_payload_shape`` -- which makes it invisible to turn
# correlation, and a prompt waiting for a correlated ending would then wait for
# something that can no longer be sent. Correlation exists to stop ANOTHER turn's
# ending from answering this prompt; when the stream itself is gone there is no
# other ending coming, so answering is the only truthful option left.
STREAM_TERMINAL_ERROR_CODES = frozenset({-32016})


@dataclass(frozen=True)
class Translated:
    """The result of reading one wire event.

    ``updates`` are ``SessionUpdate`` objects ready to be wrapped in a
    ``session/update`` notification. ``latch`` is a stop reason claimed by an
    event that does *not* end the turn (a blocked action), to be used when the
    terminating event arrives. ``stop`` is set only by a terminating event.
    """

    updates: tuple[dict[str, Any], ...] = ()
    latch: str | None = None
    stop: str | None = None


MAX_MEDIA_ITEMS = 32
"""How many files one media event may put on the wire.

One event per turn, so this is a page-weight bound rather than a correctness one.
Thirty-two covers what a ``deliver_files`` call realistically hands back; a turn
that produced more has a reporting problem, not a delivery one, and the reply
text says what it did.
"""


def _text_chunk(kind: str, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    update: dict[str, Any] = {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
    if meta:
        update["_meta"] = meta
    return update


def _meta_for(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The ``_meta`` an event's raven-specific fields belong in, or ``None``.

    ``target`` is the load-bearing one. A sub-agent's direct chat runs on its own
    lane but is emitted onto the *session's* subscription, so without this a
    delegated agent's reply is indistinguishable from the main agent's and gets
    rendered as it. Tagged rather than suppressed: hiding delegated work makes a
    turn look idle while a sub-agent talks for a minute.

    ``_meta`` and not a top-level key because the spec forbids custom fields on
    standard types, and declares ``_meta`` on nearly every one for exactly this.
    """
    target = payload.get("target")
    if not isinstance(target, dict):
        return None
    return {"raven.target": target}


def translate(event: Any, *, cwd: str | None = None) -> Translated:
    """Read one wire event. Pure: no state, no I/O, no event loop.

    Kept pure so the frames it produces can be validated against the official
    schema in a plain unit test, one case per event type, which is what makes the
    90% diff threshold reachable for this file at all.

    An event this has no branch for yields an empty result rather than raising.
    A translator that crashed on an unrecognised event would take the whole
    connection down over a wire event somebody added for the web client.
    """
    if not isinstance(event, dict):
        return Translated()
    kind = event.get("type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    meta = _meta_for(payload)

    if kind == "token.delta":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return Translated()
        return Translated(updates=(_text_chunk("agent_message_chunk", text, meta),))

    if kind == "thinking.delta":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return Translated()
        return Translated(updates=(_text_chunk("agent_thought_chunk", text, meta),))

    if kind == "tool.start":
        return Translated(updates=(_tool_call(payload, cwd, meta),))

    if kind == "tool.complete":
        return Translated(updates=(_tool_call_update(payload, meta),))

    if kind == "notice":
        return _notice(payload, meta)

    if kind == "message.complete":
        # The stop reason is decided by the caller, which knows whether anything
        # latched one earlier; ``end_turn`` here is the default that a plain
        # completion means. The usage rides out first, so the client has the
        # turn's cost before the turn is over rather than after.
        usage = _usage_update(payload.get("usage"))
        return Translated(updates=() if usage is None else (usage,), stop="end_turn")

    if kind == "media":
        return _media(payload, cwd)

    if kind == "error":
        return _error(payload, meta)

    # message.start carries only the turn id, which a client has no use for and
    # which rides ``_meta`` where it matters. episode.start is a TUI collapsing
    # boundary with no ACP counterpart. The dag.* trio would map to `plan`, but
    # ``PlanEntry.priority`` is required and raven has no source for it, so a
    # plan would have to be invented. cron.* belongs to a turn nobody in this
    # session asked for.
    return Translated()


def _media(payload: dict[str, Any], cwd: str | None) -> Translated:
    """Files the reply carried, as one ``agent_message_chunk`` per file.

    ``resource_link`` rather than ``image``/``audio`` even for a picture. Those
    two blocks carry base64 ``data``, which would mean reading the file -- and
    this function is pure, deliberately, so that every frame it builds can be
    validated against the official schema in a plain unit test. A link is also
    what a local client wants: it can open the file in an editor tab instead of
    rendering a copy of it.

    One chunk per file because ``content`` on a chunk is a single ``ContentBlock``,
    not a list. The order is the turn's own, which puts media ahead of the reply
    text.

    No ``_meta`` target, unlike every other content-bearing branch. The wire
    event has no ``target`` field to read: its payload is closed
    (``additionalProperties: false``) around ``items`` alone, and the emit site
    tags only the four events a sub-agent's direct chat can produce, which does
    not include this one. Reading a key the contract forbids would be a branch
    only an off-contract payload could reach.

    ``mimeType`` is forwarded as declared and not corrected here. Every emit site
    currently hardcodes ``application/octet-stream``, so it is nearly always the
    RFC's "unknown binary" rather than a real type -- but inventing one from the
    extension would be this translator claiming knowledge the wire event does not
    carry, and a *wrong* type is worse than an honest unknown: a client picks its
    viewer by it. Recorded in the compatibility matrix instead.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        return Translated()
    base = Path(cwd) if cwd else None
    updates: list[dict[str, Any]] = []
    for item in items:
        # The cap bounds what goes on the wire, so it is checked here rather than
        # by slicing the input: an entry this loop skips has cost the client
        # nothing, and slicing first would let a run of unusable entries push
        # real files past the limit and out of the reply.
        if len(updates) >= MAX_MEDIA_ITEMS:
            break
        if not isinstance(item, dict):
            continue
        raw = item.get("path")
        if not isinstance(raw, str) or not raw.strip():
            continue
        resolved = absolute_path(raw.strip(), base)
        if resolved is None:
            # A relative path with no session cwd to anchor it. Named in text
            # rather than dropped or sent as a relative ``file://`` URI: the URI
            # would resolve against the *client's* notion of the current
            # directory, so it either fails or opens a different file with the
            # same name, and silently dropping it leaves a reader wondering
            # where the file the agent said it produced went.
            updates.append(_text_chunk("agent_message_chunk", f"[attachment: {raw.strip()}]"))
            continue
        link: dict[str, Any] = {
            "type": "resource_link",
            "uri": _file_uri(resolved),
            # ``name`` is required and is what a client puts in a list. The
            # basename, not the whole path: the path is already in ``uri``, and a
            # deep path renders as one unreadable line.
            "name": PurePath(resolved).name or resolved,
        }
        mime = item.get("mime")
        if isinstance(mime, str) and mime:
            link["mimeType"] = mime
        updates.append({"sessionUpdate": "agent_message_chunk", "content": link})
    return Translated(updates=tuple(updates))


def _file_uri(path: str) -> str:
    """An absolute path as a ``file://`` URI.

    ``as_uri`` rather than a hand-built prefix because this repo runs on Windows
    too, where a path is ``C:\\work\\a.csv`` and the correct URI is
    ``file:///C:/work/a.csv`` -- concatenating a prefix would emit backslashes
    inside a URI and a client would resolve nothing. It also percent-encodes,
    which is what keeps the space in "My Documents" from terminating the URI.

    The fallback is unreachable through ``_media``, which only calls this once
    ``absolute_path`` has returned a non-``None`` string, and ``as_uri`` raises
    only on a relative path. It is kept and tested directly because the property
    the caller depends on is "this never raises": a raise here would leave a
    suspended ``session/prompt`` unanswered, which is the one failure this
    translator is built to avoid, and the next caller does not inherit the
    invariant from a comment.
    """
    try:
        return Path(path).as_uri()
    except ValueError:
        return "file://" + quote(path, safe="/")


def _usage_update(usage: Any) -> dict[str, Any] | None:
    """How full the context window is, and what the turn cost.

    The one place the rich accounting already on the internal wire maps cleanly
    onto ACP: ``used`` and ``size`` are the context numbers, and ``Cost`` wants
    ``{amount, currency}`` where raven has a dollar figure.

    Emitted only when the window numbers are both real. ``size`` of zero would
    have a client drawing a full bar or dividing by it, and a turn that reported
    no usage at all (a cached reply, a hook short-circuit) has nothing to say
    here -- an update of zeroes is not the same statement as no update.

    The currency is hardcoded because the figure is: ``estimated_cost_usd`` is
    dollars by name. A configurable currency here would relabel the same number.
    """
    if not isinstance(usage, dict):
        return None
    used = usage.get("context_used")
    size = usage.get("context_max")
    if not isinstance(used, int) or not isinstance(size, int) or size <= 0 or used < 0:
        return None
    update: dict[str, Any] = {"sessionUpdate": "usage_update", "used": used, "size": size}
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        update["cost"] = {"amount": float(cost), "currency": "USD"}
    return update


def _tool_call(payload: dict[str, Any], cwd: str | None, meta: dict[str, Any] | None) -> dict[str, Any]:
    """A ``tool_call`` update for the start of a call.

    ``status: "in_progress"`` and not ``pending``: pending means "not started --
    streaming input or awaiting approval", and by the time this event exists the
    call is running. It is also what a client can draw; a pending row that never
    changes reads as a hang.

    ``rawInput`` is deliberately absent. It would carry the tool's arguments
    verbatim, and for exec that is the entire command line -- which is a
    publishing surface rendered in an editor. The redaction table that makes it
    safe to send is not written yet; the title carries what a client needs to
    draw the row in the meantime.
    """
    name = payload.get("name")
    arguments = payload.get("arguments")
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": str(payload.get("tool_call_id") or ""),
        # Redacted here and not at the emit site: this is the last point before
        # the bytes leave for a client that persists its transcript, and the
        # title is built from the tool's arguments, which for exec is the whole
        # command line. ``redact`` is idempotent, so a caller that already
        # scrubbed loses nothing by this second pass.
        "title": redact(title_for(name if isinstance(name, str) else None, arguments, payload.get("display"))),
        "kind": tool_kind(name if isinstance(name, str) else None),
        "status": "in_progress",
    }
    found = locations(arguments, cwd)
    if found:
        update["locations"] = found
    extra = dict(meta or {})
    # ``blocking`` says the call has no deadline and may emit nothing for as long
    # as it runs. A client that clocks the stream needs it, and there is no
    # standard field for it, so it rides _meta.
    if payload.get("blocking"):
        extra["raven.blocking"] = True
    if extra:
        update["_meta"] = extra
    return update


def _tool_call_update(payload: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    """A ``tool_call_update`` for a finished call.

    ``status: "completed"`` unconditionally, which is a known inaccuracy and not
    an oversight: ``ToolEvent`` carries no success flag, and the preview it does
    carry comes from ``display_text or model_text`` -- so a tool that writes a
    friendly message on failure is indistinguishable here from one that
    succeeded. Guessing from the text would mislabel both directions. The fix is
    a status field at the emit site, which is a change to the spine's vocabulary,
    not to this mapping. Recorded in the compatibility matrix.
    """
    preview = payload.get("result_preview")
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": str(payload.get("tool_call_id") or ""),
        "status": "completed",
    }
    content: list[dict[str, Any]] = []
    if isinstance(preview, str) and preview:
        # Scanned before it is cut, and the order is the whole point: cutting
        # first can slice a credential so that it no longer matches the pattern
        # that would have caught it -- ``redact("token sk-ant-api")`` returns it
        # unchanged -- and then the head of it is published as ordinary text.
        # The scan cap in :mod:`raven.acp.redact` is four times this one, so a
        # preview of any length that reaches here is scanned whole or truncated
        # by that module with a notice of its own.
        scanned = redact(preview)
        text = scanned[:MAX_RESULT_PREVIEW]
        if payload.get("truncated") or len(scanned) > MAX_RESULT_PREVIEW:
            text += "\n[truncated]"
        content.append({"type": "content", "content": {"type": "text", "text": text}})
    if (change := _diff_block(payload.get("file_change"))) is not None:
        content.append(change)
    if content:
        update["content"] = content
    if meta:
        update["_meta"] = dict(meta)
    return update


def _diff_block(change: Any) -> dict[str, Any] | None:
    """A structured ``Diff`` for a client that draws the change itself.

    Built from the file's contents rather than from the unified diff string the
    same event carries: a unified diff cannot be turned back into the file, its
    context is limited, and an oversized rewrite is dropped from it entirely.

    ``oldText`` is included only when the file existed. The schema says outright
    that it is "the original content (None for new files)", so sending null for a
    file whose previous content is merely unavailable would tell the client the
    file was created -- and every line of a rewrite would render as an addition.
    ``path`` must be absolute per the spec, and it already is: the tools resolve
    before writing.
    """
    if not isinstance(change, dict):
        return None
    path = change.get("path")
    after = change.get("after")
    if not isinstance(path, str) or not path or not isinstance(after, str):
        return None
    diff: dict[str, Any] = {"path": path, "newText": after}
    before = change.get("before")
    if isinstance(before, str):
        diff["oldText"] = before
    return {"type": "diff", **diff}


def _event_turn_id(event: Any) -> str:
    """The turn an event belongs to, or ``""`` when the emitter did not say.

    An empty answer is not treated as a mismatch anywhere: an emitter that does
    not know the turn id predates this correlation, and refusing to settle on it
    would hang a prompt rather than protect one.
    """
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    turn_id = payload.get("turn_id")
    return turn_id if isinstance(turn_id, str) else ""


def _ends_the_stream(event: Any) -> bool:
    """Whether this event is the last one its subscription can carry."""

    if not isinstance(event, dict) or event.get("type") != "error":
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    return payload.get("code") in STREAM_TERMINAL_ERROR_CODES


def _notice(payload: dict[str, Any], meta: dict[str, Any] | None) -> Translated:
    """A runtime notice. Only ``action_blocked`` reaches the wire at all.

    It latches ``refusal`` rather than terminating: the runtime still ends the
    turn through its normal path, and claiming the stop reason here would race
    that. The detail is surfaced as message content because a refusal with no
    explanation is indistinguishable from an empty answer.
    """
    if payload.get("kind") != "action_blocked":
        return Translated()
    detail = payload.get("detail")
    # A refusal quotes what was refused, and what was refused is often a command
    # line. Same publishing surface as the title, same treatment.
    text = redact(detail) if isinstance(detail, str) and detail.strip() else "The runtime blocked this action."
    return Translated(updates=(_text_chunk("agent_message_chunk", text, meta),), latch="refusal")


def _error(payload: dict[str, Any], meta: dict[str, Any] | None) -> Translated:
    """A terminating error event, which is still not a JSON-RPC error.

    Two shapes. A cancel is the one signal ``turn.cancel`` emits and stops the
    turn as ``cancelled``. Anything else is a real failure, and the client is
    told what happened as message content before the turn ends -- because the
    alternative shapes are all worse: erroring the prompt makes some clients
    cancel the whole turn, and ending silently shows a person a turn that stopped
    for no stated reason.
    """
    if payload.get("reason") == _CANCELLED_REASON:
        return Translated(stop="cancelled")
    message = payload.get("message")
    detail = payload.get("detail")
    parts = [redact(str(part)) for part in (message, detail) if isinstance(part, str) and part.strip()]
    text = " ".join(parts) if parts else "The turn failed."
    code = payload.get("code")
    if isinstance(code, int):
        text = f"{text} (code {code})"
    return Translated(updates=(_text_chunk("agent_message_chunk", text, meta),), stop="end_turn")


class TurnAlreadyRunningError(RuntimeError):
    """A second ``session/prompt`` arrived while one was still in flight."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} already has a prompt in flight")
        self.session_id = session_id


@dataclass
class _Turn:
    """One in-flight ``session/prompt``."""

    future: asyncio.Future[str]
    latched: str | None = None
    # The turn ``turn.send`` accepted for this prompt. ``None`` until it answers,
    # which is why ``deferred`` exists: this session's stream also carries turns
    # the runtime submitted, and their endings must not answer this prompt.
    turn_id: str | None = None
    deferred: dict[str, str] = field(default_factory=dict)

    def settle(self, stop: str) -> bool:
        """Resolve the prompt once, and report whether this call was the one.

        The gate is the future's own state rather than a separate flag: a cancel
        that is followed by the sink's failure event, or two terminating events
        for one turn, must be a no-op rather than an ``InvalidStateError``
        raised inside the emitter's coalesce task, where nothing would report it.
        """
        if self.future.done():
            return False
        self.future.set_result(self.latched or stop)
        return True


@dataclass
class AcpSession:
    """One ACP session: its raven session key, its stream, and its turn."""

    session_id: str
    session_key: str
    cwd: str
    subscription_id: str | None = None
    turn: _Turn | None = None


class UpdateTranslator:
    """The outbound sink, plus the session and turn state it needs to route.

    ``emit`` writes one finished ACP frame. It is synchronous because the frame
    writer is: a write plus a flush, with no suspension point inside, which is
    what keeps event order on the wire identical to event order on the
    subscription -- every ``agent_message_chunk`` of a turn is on the wire before
    the ``session/prompt`` response that follows it.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        side_channel: Callable[[str, Any], bool] | None = None,
    ) -> None:
        self._emit = emit
        # Frames on this sink that are not subscription events, offered to a
        # handler before being dropped. ``clarify.request`` is the one that has
        # to be served: it blocks a tool call, so dropping it stalls a turn until
        # the broker's own timeout rather than failing it. Returning ``False``
        # puts the frame back on the dropped tally, so a surface that grows a new
        # notification still shows up there.
        self._side_channel = side_channel
        self._by_session_id: dict[str, AcpSession] = {}
        self._by_subscription: dict[str, AcpSession] = {}
        # Set once the connection is shutting down. A one-way latch, and the
        # reason it exists is a race a single sweep cannot close: a handler task
        # created before EOF may not have *begun* before EOF, so it would open its
        # turn after everything pending had been settled and then wait on a stream
        # that will never speak again.
        self._closing = False
        # Methods this sink saw and had no branch for, counted rather than logged
        # per occurrence: a chatty unknown notification would otherwise write a
        # log line per frame for the life of the process.
        self.dropped: dict[str, int] = {}

    # -- session registry -------------------------------------------------

    def add(self, session: AcpSession) -> None:
        self._by_session_id[session.session_id] = session
        if session.subscription_id:
            self._by_subscription[session.subscription_id] = session

    def bind_subscription(self, session_id: str, subscription_id: str) -> None:
        session = self._by_session_id.get(session_id)
        if session is None:
            return
        if session.subscription_id:
            self._by_subscription.pop(session.subscription_id, None)
        session.subscription_id = subscription_id
        self._by_subscription[subscription_id] = session

    def _mark_stream_dead(self, session: AcpSession) -> None:
        """Release a session's binding to a subscription the emitter has closed.

        The emitter removes an overflowed subscription from its own indexes, but
        that does not reach back here: ``AcpSession.subscription_id`` and the
        ``_by_subscription`` map still name a stream that can no longer deliver.
        The next prompt decides it must re-subscribe by this field being unset, so
        the binding is dropped rather than left pointing at a corpse.
        """
        if session.subscription_id:
            self._by_subscription.pop(session.subscription_id, None)
        session.subscription_id = None

    def get(self, session_id: str) -> AcpSession | None:
        return self._by_session_id.get(session_id)

    def sessions(self) -> tuple[AcpSession, ...]:
        return tuple(self._by_session_id.values())

    # -- turn lifecycle ---------------------------------------------------

    def begin_turn(self, session_id: str) -> asyncio.Future[str]:
        """Open a turn and return the future its stop reason arrives on.

        Refuses a second concurrent turn on one session rather than queueing it.
        ACP allows several sessions on one connection but a session's updates
        carry no request correlation, so two prompts in flight produce a single
        interleaved stream that cannot be split back apart -- raven's own client
        direction documents this as a certainty, and the mirror image holds here.

        Once :meth:`close` has run, the future comes back already resolved as
        ``cancelled``: the stream is finished, so a turn opened now would wait on
        nothing, and answering immediately is both the truth and what lets the
        handler return through its own code.
        """
        session = self._by_session_id[session_id]
        if session.turn is not None and not session.turn.future.done():
            raise TurnAlreadyRunningError(session_id)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        session.turn = _Turn(future=future)
        if self._closing:
            session.turn.settle("cancelled")
        return future

    def accept_turn(self, session_id: str, turn_id: str) -> None:
        """Record which turn ``turn.send`` accepted for the open prompt.

        Anything held from before this point is resolved here: an ending that
        belongs to this turn settles it now, and the rest are dropped, because
        they belonged to turns this prompt never asked for.
        """
        session = self._by_session_id.get(session_id)
        if session is None or session.turn is None:
            return
        turn = session.turn
        # Empty is stored as empty and not folded into ``None``: the two mean
        # different things and the difference is a hang. ``None`` is "not told
        # yet", which holds an ending. ``""`` is "told, and there is no id" --
        # a caller whose ``turn.send`` answered without one -- and that has to
        # settle on whatever ends the turn, because there is no key to correlate
        # with and waiting for one that never comes leaves the prompt unanswered
        # forever. Correlation is enforced only where a key exists.
        turn.turn_id = turn_id
        if not turn_id:
            logger.debug("acp: turn.send named no turn for {}; settlement cannot be correlated", session_id)
        held = turn.deferred.pop(turn_id, None) if turn_id else next(iter(turn.deferred.values()), None)
        turn.deferred.clear()
        if held is not None:
            turn.settle(held)

    def close(self) -> None:
        """Latch the connection shut and answer everything still waiting.

        ``cancelled`` because that is what the spec has for a turn torn down
        before it finished, and because the alternative -- cancelling the handler
        tasks -- leaves a client holding a request that is never answered.
        """
        self._closing = True
        for session in self._by_session_id.values():
            if session.turn is not None:
                session.turn.settle("cancelled")

    def end_turn(self, session_id: str) -> None:
        """Drop the turn slot. Idempotent: the caller runs it from a finally."""
        session = self._by_session_id.get(session_id)
        if session is not None:
            session.turn = None

    def settle_turn(self, session_id: str, stop: str) -> bool:
        """Resolve a turn from outside the event stream (a teardown, a cancel)."""
        session = self._by_session_id.get(session_id)
        if session is None or session.turn is None:
            return False
        return session.turn.settle(stop)

    # -- the sink ---------------------------------------------------------

    async def send_frame(self, frame: Any) -> None:
        """The ``send_frame`` given to ``build_rpc_stack``.

        The non-dict guard is first for a measured reason: the type alias in
        ``bootstrap.py`` says ``dict``, but the real contract is ``dict | bytes``
        -- ``browser.watch`` pushes an ``RVF1`` header plus a JPEG through this
        same sink, and the WebSocket transport branches on it. On stdio there is
        no such branch, so one frame of video would put binary on the protocol
        channel. The path is dormant under ACP (the dispatcher this stack builds
        is not the one serving these methods) but the guard costs three lines.
        """
        if not isinstance(frame, dict):
            self._drop("<non-dict frame>")
            return
        method = frame.get("method")
        if method != "event":
            name = method if isinstance(method, str) else "<no method>"
            if self._side_channel is not None and isinstance(method, str):
                try:
                    if self._side_channel(method, frame.get("params")):
                        return
                except Exception:
                    # The sink is shared with the streaming path; a handler bug
                    # must not take a turn's output down with it.
                    logger.exception("acp: handling {} failed", method)
            self._drop(name)
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            self._drop("event/<no params>")
            return
        session = self._by_subscription.get(str(params.get("subscription_id") or ""))
        if session is None:
            # A subscription this connection does not own, or one already
            # unbound. Not an error: the emitter also serves turns the runtime
            # submitted (cron), which have no ACP session.
            self._drop("event/<unbound subscription>")
            return
        await self._deliver(session, params.get("event"))

    async def _deliver(self, session: AcpSession, event: Any) -> None:
        result = translate(event, cwd=session.cwd)
        for update in result.updates:
            self._emit(protocol.notification("session/update", {"sessionId": session.session_id, "update": update}))
        if _ends_the_stream(event):
            # Answered rather than correlated, and answered as cancelled because
            # that is what the spec has for a turn torn down before it finished.
            # See ``STREAM_TERMINAL_ERROR_CODES``: the subscription is gone, so no
            # correlated ending can follow, and holding out for one hangs the
            # client's request for the life of the connection. The check runs
            # before the turn bookkeeping below because a runtime turn can
            # overflow while no ACP prompt is open, and the stream must still be
            # marked dead for the next prompt.
            turn = session.turn
            if turn is not None:
                turn.settle("cancelled")
            # The emitter closed the subscription out from under this session, so
            # the binding that is left points at a stream that can no longer
            # deliver. A session left bound to it would hang its next prompt: the
            # emitter has no subscriber left to push that prompt's events to.
            self._mark_stream_dead(session)
            return
        turn = session.turn
        if turn is None:
            return
        # Updates above go out for the whole session -- a client showing what the
        # runtime is doing in its workspace is information, not a bug. Turn
        # bookkeeping is different: it answers one request, so it takes a
        # POSITIVE match on the turn this prompt started. Reading a missing id as
        # "mine" is the same defect as not looking at the id at all: the runtime
        # shares this lane (see ``_owns_lane``), and the notice shape carries no
        # id at all, so "absent" cannot mean "this turn's".
        event_turn_id = _event_turn_id(event)
        if turn.turn_id == "":
            # Told there is no id. No correlation is possible, so this behaves the
            # way it did before correlation existed. Recorded as a distinct state
            # rather than silently sharing the "not told yet" branch, because that
            # one holds and holding here would never end.
            if result.latch and turn.latched is None:
                turn.latched = result.latch
            if result.stop:
                turn.settle(result.stop)
            return
        if turn.turn_id is None:
            # ``turn.send`` emits ``message.start`` before it returns, so events
            # can arrive before this prompt learns which turn is its own. An
            # ending that names a turn is held, because dropping it would hang a
            # prompt whose turn finished inside that window; one that names none
            # is neither held nor applied.
            # Held under its own id, which may be the empty one: an ending that
            # names no turn is still an ending, and whether it is this prompt's
            # cannot be decided until ``turn.send`` says whether there is an id
            # to compare at all. Dropping it here hung every prompt whose runtime
            # reports no turn id, because the legacy path below then had nothing
            # left to settle from.
            if result.stop and len(turn.deferred) < MAX_DEFERRED_ENDINGS:
                turn.deferred.setdefault(event_turn_id, result.stop)
            return
        if event_turn_id != turn.turn_id:
            self._drop(f"{event.get('type') if isinstance(event, dict) else 'event'}/<not this turn>")
            return
        if result.latch and turn.latched is None:
            turn.latched = result.latch
        if result.stop:
            turn.settle(result.stop)

    def _drop(self, what: str) -> None:
        self.dropped[what] = self.dropped.get(what, 0) + 1
        if self.dropped[what] == 1:
            # Once per distinct kind, at debug: the first occurrence is the
            # interesting one, and a per-frame log on a streaming channel is its
            # own outage.
            logger.debug("acp: dropped an outbound frame with no ACP mapping: {}", what)


__all__ = [
    "KNOWN_EVENT_TYPES",
    "MAX_DEFERRED_ENDINGS",
    "STREAM_TERMINAL_ERROR_CODES",
    "MAX_MEDIA_ITEMS",
    "MAX_RESULT_PREVIEW",
    "SIDE_CHANNEL_METHODS",
    "AcpSession",
    "Translated",
    "TurnAlreadyRunningError",
    "UpdateTranslator",
    "translate",
]
