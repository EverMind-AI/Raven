"""Third-party ACP agent backend: one ``session/prompt`` on a pooled connection.

The counterpart of :mod:`raven.agent.subagent.backends.cli_agent`. Where that one
spawns a process per task and parses whatever the CLI prints, this one keeps a
connection (owned by :mod:`raven.acp_client.pool`) and delivers the task as a
request on it.

Two consequences worth stating, because they are what the transport buys:

- The agent's intermediate work arrives as ``session/update`` notifications while
  the task runs, instead of being reconstructed from stdout afterwards. It is
  recorded on this backend's own tracing span and in the run's own activity
  record (:mod:`raven.agent.subagent.activity`) rather than returned, so neither
  ``spawn`` nor ``run_subagent_dag`` has to change to carry it.
- Those notifications are the run as a reader wants it, not everything that
  crossed the wire. The whole exchange -- the agent's own requests and what raven
  answered, raven's outbound frames, notifications no session was listening for,
  stderr -- is written by :mod:`raven.acp_client.journal` for the life of the
  connection, and each call records the byte range it occupied there.
- Resuming is the agent's own ``session/load``, not a second command template, so
  whether an agent *can* resume is read from its handshake instead of inferred
  from a config field a human filled in.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.acp_client.acp_dialects import AcpDialect, ToolCall, content_texts, dialect_for
from raven.acp_client.ask_user import AskUserResponder, clarify_responder
from raven.acp_client.capabilities import (
    CapabilitySnapshot,
    read_session_current_model,
    relearn_session_modes,
    steer_offered,
)
from raven.acp_client.elicitor import Elicitor
from raven.acp_client.permissions import PERMISSION_METHOD
from raven.acp_client.pool import get_pool
from raven.acp_client.protocol import (
    SESSION_MCP_CAPABILITY,
    STEER_METHOD,
    AcpBusyError,
    AcpError,
    AcpRemoteError,
    AcpTimeoutError,
)
from raven.agent.subagent import activity
from raven.agent.subagent.attachments import attachment_blocks
from raven.agent.subagent.backends import turn_rows
from raven.agent.subagent.backends.base import bounded_delta, clamp_output
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.backends.observability import (
    external_agent_span,
    record_events,
    record_frames,
    record_outcome,
    record_session,
)
from raven.agent.subagent.instances import InstanceRegistry, get_registry
from raven.agent.subagent.mcp_grant import McpDispatchError, McpGrant, McpSource, acp_target, resolve_grant
from raven.agent.tools.removals import RemovalWatch
from raven.agent.tools.snapshot import take as take_snapshot
from raven.contracts.subagent_backend import SubagentActionAbortedError
from raven.contracts.tool import FileChange
from raven.mcp.endpoint import McpEndpoints, bridge_command
from raven.spine.message import Media

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

# Text-bearing update kinds, and the ones whose text is the answer rather than
# commentary. Only `usage_update` was observed on a live server; the rest follow
# the ACP contract and are handled defensively -- an unrecognised shape is
# recorded raw and skipped, never guessed at.
_ANSWER_UPDATES = ("agent_message_chunk",)
_THOUGHT_UPDATES = ("agent_thought_chunk",)
# What the person said mid-turn. Only a steer produces one during a live turn:
# every other user message reached the agent as the prompt and is already on
# the caller's side, so this frame is the one record of a steer landing.
_USER_UPDATES = ("user_message_chunk",)

# A tool call ends whatever message was being written: an agent that says what
# it is about to do, runs a command and then reports back has sent two messages,
# and the protocol marks the boundary only by what came between them. Thoughts
# and usage updates are deliberately not here -- neither ends a message, and
# breaking on one would split a single reply mid-sentence.
_BREAKING_UPDATES = ("tool_call", "tool_call_update")
_MESSAGE_BREAK = "\n\n"

# One synthetic id for the turn's plan. The frame carries no call id of its
# own, and a plan frame re-sends the whole list on every change -- five times
# for one plan in codex's capture, the only adapter measured sending one -- so
# a row per frame would be five near-identical rows. The branch this feeds is
# dialect-independent: any adapter's plan frame lands here.
_PLAN_CALL_ID = "acp-plan"


def _uploads_root() -> Path | None:
    """The host's agent home, where the page deposits uploads; None when no config loads."""
    try:
        from raven.config import load_config

        return Path(load_config().workspace_path)
    except Exception:
        return None


class AcpEmptyTurnError(RuntimeError):
    """The agent finished a turn without producing anything usable.

    Its own name because the cause is almost never visible in the protocol:
    measured on ``hermes acp``, a provider ``HTTP 401`` still returned
    ``stopReason: "end_turn"`` and reported the failure only on stderr. So a turn
    with no content is treated as a failure and the stderr tail carried with it.

    Also raised for a turn that DID say something and still answered nothing:
    one that ends on a failed tool call, having narrated only its plan to make
    the call. Same outcome for the caller -- nothing usable came back -- and
    without this it was reported as a completed run whose result was the plan.
    """


_RESULT_TEXT_CAP = 2000


@asynccontextmanager
async def _replay_dropped(router: Any, session_id: str) -> AsyncIterator[None]:
    """Hold ``session_id`` with a sink that throws its frames away.

    Attached rather than merely un-routed, because the router's fallback for an
    unclaimed session is the resident sink and that is exactly what must not see
    these. `detach` is identity-checked, so if the body has already handed the
    session to a real sink this leaves it alone.
    """
    if router is None:
        yield
        return

    async def _drop(method: str, params: dict[str, Any]) -> None:
        logger.debug("acp agent: dropped replayed {} for session {!r}", method, session_id)

    router.attach(session_id, _drop)
    try:
        yield
    finally:
        router.detach(session_id, _drop)


#: Workspace listings one turn holds for calls that have not settled. Each is a
#: whole directory, and a call that never reports an end would otherwise keep
#: its own copy for as long as the turn lasts. At the ceiling the oldest listing
#: goes rather than the newest being refused: a call still open after sixty-four
#: others is the one least likely to ever report an end.
_MAX_OPEN_LISTINGS = 64

#: Call kinds a listing would learn nothing from. The pair of walks costs tens of
#: milliseconds of the connection's read loop -- which every session sharing it
#: waits behind -- and none of these can leave a file behind. A frame with no
#: kind is listed: codebuddy announces a call before it knows the kind.
_KINDS_THAT_WRITE_NOTHING = frozenset({"read", "search", "think", "fetch"})


def _diff_blocks(content: Any) -> list[dict[str, Any]]:
    """The ``diff`` entries of a tool call's content, whole enough to record.

    A block without a path or without the text it left is not a change anyone
    can draw, so it is dropped rather than recorded with a guess in its place.
    """
    if not isinstance(content, list):
        return []
    return [
        item
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "diff"
        and isinstance(item.get("path"), str)
        and item["path"]
        and isinstance(item.get("newText"), str)
    ]


def _block_kind(block: dict[str, Any]) -> str | None:
    """What an adapter says its block did, where it says so at all."""
    meta = block.get("_meta")
    kind = meta.get("kind") if isinstance(meta, dict) else None
    return kind if isinstance(kind, str) else None


class _TurnCollector:
    """Accumulates one session's notifications while a prompt is in flight.

    Beside the flat tallies, an ordered event list survives the turn: thoughts,
    tool calls and their results, in the order the agent reported them. That is
    the run's own transcript, and :meth:`messages` renders it in the provider
    shape the main transcript is stored in -- so a delegated run can be read
    back with the same renderer as the conversation that spawned it.

    ``on_delta`` forwards the answer chunks as they arrive, for a caller
    rendering the turn live. It is called from the connection's read loop, which
    already treats a raising notification handler as non-fatal (see
    ``AcpClient._dispatch``) -- so a client that went away mid-turn costs this
    turn its live rendering, not the connection or the answer, which keeps
    accumulating here either way.

    One turn can hold several messages, and the chunks carry no boundary of
    their own: an agent that says what it is about to do, runs a command and
    then reports back sends two, and joined chunk-to-chunk they read as one
    paragraph whose halves do not follow from each other. ``_BREAKING_UPDATES``
    is where the boundary is recovered, and the break is forwarded to
    ``on_delta`` as well so a live view and the stored reply agree.
    """

    def __init__(
        self,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        dialect: AcpDialect | None = None,
        prompt: str | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._on_delta = on_delta
        # The prompt this turn was opened with. A ``user_message_chunk`` that
        # repeats it is an agent echoing the question back (the spec uses the
        # frame for replays), not a steer, and must not become a user row.
        self._prompt = (prompt or "").strip()
        # The spec by default, so a collector built without one (every test that
        # only cares about answer text) still reads tool calls correctly.
        self._dialect = dialect or AcpDialect()
        self._tool_ran = False
        # The run this collector belongs to, captured here rather than read at
        # publish time. `__call__` is awaited from the connection's read loop,
        # a task created when the *connection* was opened, so the ContextVar it
        # carries predates this run -- and a pooled connection shared by two
        # runs would put this one's steps on the other one's live record.
        self._run = activity.current()
        # Where this turn's files are, for the account of what it wrote. The
        # agent reports a diff block for a file its own tools touched; a file one
        # of its commands produced is only ever visible as a directory that
        # changed around the call, which is what the per-call listings are for.
        self._workspace = workspace
        self._blocks: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._listings: dict[str, Any] = {}
        self._settled_calls: set[str] = set()
        self._created: set[str] = set()
        self._removals = RemovalWatch()
        self.answer: list[str] = []
        self.thoughts: list[str] = []
        self.kinds: list[str] = []
        self.usage: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.answer_at: str | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        # Any frame from the agent is the run moving, whatever it carries. The
        # transcript republish further down stamps the run too, but only for
        # the kinds it renders; a `usage_update` or a permission request would
        # otherwise leave the stall watcher reading a run that never moved.
        activity.note_alive(self._run)
        if method == PERMISSION_METHOD:
            # Subject only, never the name: this frame reports `kind: "execute"`
            # even for a call the session update badged `read`, and reading it
            # for a name would erase the distinction codex drew.
            command = self._dialect.permission_command(params)
            tool = params.get("toolCall")
            call_id = str(tool.get("toolCallId") or "") if isinstance(tool, dict) else ""
            if command and call_id:
                self._backfill_permission_command(call_id, command)
            # The earliest frame that names the call. An agent that asks first
            # and announces the call as it runs it -- raven's own does -- leaves
            # the announce too late for a listing to be a baseline: the file the
            # call writes is already there by then.
            if call_id and call_id not in self._settled_calls:
                await self._open_listing(call_id, tool.get("kind") if isinstance(tool, dict) else None)
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        kind = kind if isinstance(kind, str) else "unknown"
        self.kinds.append(kind)
        if kind in _ANSWER_UPDATES:
            if not self.answer:
                self.answer_at = self._now()
            texts = content_texts(update.get("content"))
            said = "".join(texts)
            if texts and self._tool_ran and self.answer:
                texts.insert(0, _MESSAGE_BREAK)
            if texts:
                self._tool_ran = False
            self.answer.extend(texts)
            if said:
                # Merged with the previous burst only when nothing came between:
                # a `call` event in between is exactly what makes this a new
                # burst, and the boundary is what puts each one on the step it
                # was said before instead of at the end with the rest.
                if self.events and self.events[-1].get("t") == "say":
                    self.events[-1]["text"] += said
                else:
                    self.events.append({"t": "say", "text": said, "at": self._now()})
            if self._on_delta is not None:
                for text in texts:
                    await self._on_delta(text)
        elif kind in _USER_UPDATES:
            said = "".join(content_texts(update.get("content")))
            if said and said.strip() != self._prompt:
                # A steer ends the message being written the way a tool call
                # does: what the agent says next answers the new words.
                self._tool_ran = bool(self.answer)
                self.events.append({"t": "user", "text": said, "at": self._now()})
        elif kind in _THOUGHT_UPDATES:
            chunks = content_texts(update.get("content"))
            self.thoughts.extend(chunks)
            for c in chunks:
                if self.events and self.events[-1].get("t") == "thought":
                    self.events[-1]["text"] += c
                else:
                    self.events.append({"t": "thought", "text": c, "at": self._now()})
        elif kind in _BREAKING_UPDATES:
            # Both tool kinds break the message, and each also contributes its
            # own row to the transcript: the call names what was run, the update
            # carries how it ended. One branch, because the boundary is the same
            # fact for both -- an answer chunk after either starts a new message.
            self._tool_ran = True
            if kind == "tool_call":
                call = self._dialect.call(update)
                if call.id and any(e.get("t") == "call" and e.get("id") == call.id for e in self.events):
                    # A second opening frame for a call already announced. The spec
                    # revises with `tool_call_update`, but codebuddy re-sends
                    # `tool_call` once it knows the kind, and appending again
                    # rendered one read as two rows with only one result to pair.
                    # Treated as the revision it is, which also lets the finer
                    # `kind` on this frame name the call.
                    self._revise_call(update)
                else:
                    self.events.append(
                        {
                            "t": "call",
                            "id": call.id or f"call-{len(self.calls) + 1}",
                            "call": call,
                            "at": self._now(),
                        }
                    )
            else:
                self._revise_call(update)
                status = update.get("status")
                if status in ("completed", "failed"):
                    outcome = self._dialect.result(update)
                    self.events.append(
                        {
                            "t": "result",
                            "id": str(update.get("toolCallId") or ""),
                            "ok": outcome.ok,
                            "text": outcome.text[:_RESULT_TEXT_CAP],
                            "at": self._now(),
                        }
                    )
                    self._backfill_subject(str(update.get("toolCallId") or ""), update)
            await self._note_files(update)
        elif kind == "plan":
            self._plan(update)
        elif kind == "usage_update":
            self.usage = {k: v for k, v in update.items() if k != "sessionUpdate"}
        # Republished on every update rather than once at the end: the live
        # index is how a panel watches the run, and a transcript that only
        # exists after the answer is not a live view of anything.
        if kind in (*_ANSWER_UPDATES, *_THOUGHT_UPDATES, *_USER_UPDATES, "tool_call", "tool_call_update", "plan"):
            activity.set_transcript(self._run, self.messages(in_flight=True))
        # The count beside it, on the same beat: `tasks.list` draws a running
        # node's tool count off the live account, and a tally published only
        # at the end of the turn left the chip blank while the transcript
        # already listed the calls.
        if kind in (*_BREAKING_UPDATES, "plan"):
            activity.set_tool_calls(self._run, self.tool_calls, self.failed_calls)

    async def _note_files(self, update: dict[str, Any]) -> None:
        """Record what one tool call did to the files, once it has settled.

        The account is built from two sources, because neither sees the other's
        changes: the diff blocks the agent attaches to the call, which carry the
        text and so the line counts, and a listing of the workspace taken when
        the call opened against one taken when it completed, which is the only
        sign of a file a command of the agent's wrote. A failed call keeps only
        the second: its blocks are claims about a change that may not have
        landed, while what the listing finds on disk is there whatever the exit
        status said. A call id settles once, however many frames repeat it.

        Written into the run captured at construction, not the ambient one: this
        runs on the connection's read loop, whose ContextVar predates the run.
        """
        call_id = str(update.get("toolCallId") or "")
        if not call_id or call_id in self._settled_calls:
            return
        status = update.get("status")
        settling = status in ("completed", "failed")
        # No listing for a call first seen already over: taken now it would be
        # the state after the call, which is no baseline at all -- it would say
        # every file the call created had been there all along.
        if not settling:
            await self._open_listing(call_id, update.get("kind"))
        blocks_here: dict[str, list[dict[str, Any]]] = {}
        for block in _diff_blocks(update.get("content")):
            blocks_here.setdefault(self._absolute(block["path"]), []).append(block)
        for path, group in blocks_here.items():
            # Keyed by the resolved path, so one file revised twice in a call is
            # one change and the newest frame's blocks for it are the ones that
            # stand. Kept as a group: an adapter sends one block per hunk of a
            # single edit, and only the whole frame describes what it did.
            self._blocks.setdefault(call_id, {})[path] = group
        if not settling:
            return
        self._settled_calls.add(call_id)
        before = self._listings.pop(call_id, None)
        blocks = self._blocks.pop(call_id, {})
        if status == "failed":
            # The blocks describe what the call meant to do, which a failed call
            # may not have done; the disk says what it did do. A command that
            # wrote a file and then exited non-zero left that file behind.
            blocks = {}
        accounted: list[str] = []
        # Settled before this call's own blocks are noted, so a file it just
        # wrote is not stat'ed to say it exists -- the order the in-process lane
        # keeps around its own tool results.
        for removal in self._removals.settle():
            accounted.append(removal.path)
            activity.record_file_change(
                self._run,
                activity.workspace_relative(removal.path, self._workspace),
                "delete",
                0,
                len((removal.before or "").splitlines()),
                None,
            )
        for path, group in blocks.items():
            accounted.append(path)
            self._record_blocks(path, group, before)
        if before is not None:
            activity.record_snapshot_changes(
                before,
                await self._listing(),
                self._workspace,
                already=accounted,
                run=self._run,
                seen_created=self._created,
            )

    async def _open_listing(self, call_id: str, kind: Any) -> None:
        """Take the call's baseline listing, once, unless its kind writes nothing."""
        if call_id in self._listings or kind in _KINDS_THAT_WRITE_NOTHING:
            return
        while len(self._listings) >= _MAX_OPEN_LISTINGS:
            self._listings.pop(next(iter(self._listings)))
        self._listings[call_id] = await self._listing()

    def _record_blocks(self, path: str, blocks: list[dict[str, Any]], before: Any) -> None:
        """Record one path's blocks from one call as the single change they are.

        Summed, because an adapter sends one block per hunk: keeping the last
        one would report a two-hunk edit as whichever hunk arrived last.
        """
        add = delete = 0
        edited = False
        for block in blocks:
            # A block that carries no text for what was there says nothing about
            # the file having existed, whether the field is absent, null, or a
            # shape that is not text.
            old_text = block.get("oldText")
            old_text = old_text if isinstance(old_text, str) else None
            edited = edited or old_text is not None
            block_add, block_delete = activity.count_line_changes(old_text, block["newText"])
            add += block_add
            delete += block_delete
        newest = blocks[-1]["newText"]
        try:
            size: int | None = os.stat(path).st_size
        except OSError:
            size = None
        relative = activity.workspace_relative(path, self._workspace)
        if not newest and (size is None or _block_kind(blocks[-1]) == "delete"):
            # codex reports a removal as a block holding the old content and no
            # new one. Recorded as the deletion it is: an edit down to zero bytes
            # would read as a file that is there and empty.
            activity.record_file_change(self._run, relative, "delete", 0, delete, None)
            return
        if edited:
            op = "edit"
        elif self._dialect.missing_old_text_is_creation:
            # The spec's reading, and what codex and raven's own agent send: no
            # `oldText` is a file that was not there.
            op = "add"
        elif before is not None and os.path.realpath(path) in before:
            # claude-agent-acp announces every `Write` without `oldText`,
            # existing file or not, so the listing is the only thing that tells
            # a creation from a rewrite -- and a rewrite recorded as a creation
            # would cancel itself away against a later removal of a file the
            # user had.
            op = "write"
        else:
            op = "add"
        activity.record_file_change(
            self._run, relative, op, add, delete, size if size is not None else len(newest.encode("utf-8"))
        )
        # Only when the block is the whole file: a hunk remembered as the file's
        # text would count the hunk's lines as a later removal's.
        if len(blocks) == 1 and (size is None or size == len(newest.encode("utf-8"))):
            self._removals.note_write(FileChange(path=path, after=newest))

    async def _listing(self) -> Any:
        """A listing of the turn's workspace, taken off the read loop.

        The walk is tens of milliseconds on a working tree, and every frame the
        connection carries -- for every session sharing it -- waits behind it.
        """
        if self._workspace is None:
            return None
        return await asyncio.to_thread(take_snapshot, self._workspace)

    def _absolute(self, path: str) -> str:
        """A diff block's path as the filesystem knows it.

        The spec calls for an absolute path and adapters have been seen to send
        one relative to the session's working directory, which is the only thing
        it could be relative to.
        """
        if os.path.isabs(path) or self._workspace is None:
            return path
        return str(Path(self._workspace) / path)

    def _revise_call(self, update: dict[str, Any]) -> None:
        """Re-read a call's arguments from a later frame.

        The opening ``tool_call`` announces the call before the arguments are
        known: measured on claude-agent-acp and opencode, it carries
        ``rawInput: {}`` and the real input follows on a ``tool_call_update``.
        Until that lands, the only thing a reader has is the title -- which is
        why the fallback to it is resolved at render time (``ToolCall.subject``)
        rather than stored here.

        The newest input wins, because that is what a ``tool_call_update``
        means: the fields it carries replace the call's current ones, so an
        adapter that revises an argument has revised what the tool actually ran
        with. The whole call is re-parsed rather than only its input, since the
        same frame also carries the ``kind`` and ``_meta`` a dialect names the
        tool from -- but only when it does: ``names_call`` is what decides, and
        a frame that answers False leaves the name alone.
        """
        if not self._dialect.revises_call(update):
            return
        call_id = str(update.get("toolCallId") or "")
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                revised = self._dialect.call(update)
                previous: ToolCall = event["call"]
                event["call"] = ToolCall(
                    id=previous.id,
                    # Guarded like the fields below it: a frame that carries no
                    # kind cannot name the call, and reading one anyway is what
                    # renamed codex's searches to the fallback.
                    name=revised.name if self._dialect.names_call(update) else previous.name,
                    argument=revised.argument or previous.argument,
                    # An update need not repeat the title, and losing it would
                    # leave a no-argument call with nothing to show at all.
                    title=revised.title or previous.title,
                    raw_input=revised.raw_input or previous.raw_input,
                )
                return

    def _backfill_subject(self, call_id: str, update: dict[str, Any]) -> None:
        """Set a call's subject from its own result.

        The frame that opened the call did not have it: codex sends
        ``apply_patch`` as the command and names the files it patched only in
        the output. Never overwrites a subject already known -- a later frame
        revising an argument is ``_revise_call``'s business, not this.

        Passes the call's own name, established when it opened, into
        ``subject_from_result`` as the tool the completed frame belongs to: that
        frame typically carries none of ``tool_name``'s own discriminators, so
        without it a dialect could not tell an ``apply_patch`` completion from
        any other tool's own -- including one whose output merely looks like an
        apply_patch envelope.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.argument:
                    return
                subject = self._dialect.subject_from_result(update, name=previous.name)
                if not subject:
                    return
                # The new subject must win a `path` collision -- an
                # `imageGeneration` call's own `rawInput` can already carry one
                # -- so it seeds the dict and `previous.raw_input` is copied in
                # behind it, the same order `CodexDialect.call` merges with.
                raw_input = {"path": subject}
                for key, value in previous.raw_input.items():
                    if key == "path":
                        continue
                    # `apply_patch`'s own `command` is the tool's name, not an
                    # argument -- a reader that picks a subject by scanning
                    # known key names rather than by position (as
                    # `ui-tui`'s `callSubject` does) would otherwise take it
                    # over the subject just recovered here. A real recovered
                    # command never equals the call's own name, so this never
                    # drops one.
                    if key == "command" and value == previous.name:
                        continue
                    raw_input[key] = value
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=subject,
                    title=previous.title,
                    raw_input=raw_input,
                )
                return

    def _backfill_permission_command(self, call_id: str, command: str) -> None:
        """Set a call's subject to the command a permission request recovered.

        Guarded on ``raw_input`` already carrying a ``command`` key rather than
        on ``argument`` being non-empty: a ``commandExecution.read`` opens with
        its ``locations`` path already filling ``argument``, and that
        placeholder is exactly what this must replace. A real
        ``commandExecution`` -- and ``apply_patch``, whose ``rawInput.command``
        is its own name -- already carry their command there, and overwriting
        either would either restate the same value or, for ``apply_patch``,
        block the file-derived subject its own result still owes it.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.raw_input.get("command"):
                    return
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=command,
                    title=previous.title,
                    raw_input={"command": command, **previous.raw_input},
                )
                return

    def _plan(self, update: dict[str, Any]) -> None:
        """Open the turn's plan row, or move the one already open.

        Only the opening frame breaks the message. A revision that broke it too
        would split the narration around a plan that changes four times.
        """
        subject, checklist = self._dialect.plan_rows(update)
        if not checklist:
            return
        call = ToolCall(
            id=_PLAN_CALL_ID,
            name=self._dialect.plan_tool_name,
            argument=subject,
            title="",
            raw_input={"argument": subject} if subject else {},
        )
        for event in self.events:
            if event.get("t") == "call" and event.get("id") == _PLAN_CALL_ID:
                event["call"] = call
                break
        else:
            self._tool_ran = True
            self.events.append({"t": "call", "id": _PLAN_CALL_ID, "call": call, "at": self._now()})
        for event in self.events:
            if event.get("t") == "result" and event.get("id") == _PLAN_CALL_ID:
                event["text"] = checklist
                return
        self.events.append({"t": "result", "id": _PLAN_CALL_ID, "ok": True, "text": checklist, "at": self._now()})

    @property
    def calls(self) -> list[ToolCall]:
        return [ev["call"] for ev in self.events if ev["t"] == "call"]

    @property
    def tool_calls(self) -> list[str]:
        """One label per call, read at the end rather than when each was announced.

        The opening ``tool_call`` frame does not yet carry the arguments, so a
        label built there names the call by its title and can never be corrected
        -- which put "read_file read_file src/a.py" on the span, the title
        pasted under a verb derived from it.
        """
        return [call.label for call in self.calls]

    @property
    def failed_calls(self) -> list[str]:
        """The label of every call whose result reported failure, in order.

        Paired back to the call by id rather than by position: a run interleaves
        calls and results freely, and an agent that opens two before either
        answers would otherwise attribute each outcome to the wrong one. A
        result with no call behind it is dropped -- there is nothing to name.
        """
        labels = {ev["id"]: ev["call"].label for ev in self.events if ev["t"] == "call"}
        return [
            labels[ev["id"]]
            for ev in self.events
            if ev["t"] == "result" and not ev.get("ok") and ev.get("id") in labels
        ]

    @property
    def text(self) -> str:
        return "".join(self.answer).strip()

    @property
    def closing_text(self) -> str:
        """What the agent said after its last tool call -- the reply proper.

        Measured on codex-acp, a turn narrates as it goes: a plan, then a
        progress note before each of three calls, then the report. ``text``
        joins all of it, which is right for the caller that receives the run's
        answer, and wrong for a transcript -- the three progress notes belong on
        the steps they preceded, and repeating them inside the final message is
        what made a direct chat read as one blob of restated plan.

        Empty when the turn ended on a tool call and said nothing after it, which
        is a real outcome and not the same as "not reported".

        A steer is a boundary too: what was said before the person's words is
        already on its own row above them, and repeating it here printed the
        first half of the reply twice.
        """
        said: list[str] = []
        for ev in reversed(self.events):
            if ev["t"] in ("call", "user"):
                break
            if ev["t"] == "say":
                said.append(ev["text"])
        return "".join(reversed(said)).strip()

    @property
    def failed_call_without_answer(self) -> tuple[str, str] | None:
        """The tool call this turn ended on, when it failed and nothing followed.

        A turn can end on a tool call and say nothing after it -- that is what
        ``closing_text`` returns empty for, and when the call SUCCEEDED it is a
        real outcome. When it failed, the run has no answer at all: what
        ``text`` holds is whatever the agent narrated on its way to the call,
        which is a plan, not a result.

        Returns the call's label and its error text, because those are the
        actionable part and nothing else on the path carries them: measured on a
        real dispatch, the whole of what a caller could act on was
        ``update_task_state`` and ``operations should be array``, and both were
        thrown away.

        Walking back rather than reading the last event, and stopping at the
        first thing that settles the question: anything said, or a person's
        words, means the turn did not end on the call. A ``call`` with no
        ``result`` behind it means nobody told us how it went, which is not the
        same as knowing it failed.
        """
        for ev in reversed(self.events):
            if ev["t"] == "say" and str(ev.get("text") or "").strip():
                return None
            if ev["t"] == "user":
                return None
            if ev["t"] == "call":
                return None
            if ev["t"] == "result":
                if ev.get("ok"):
                    return None
                call = next(
                    (e["call"] for e in reversed(self.events) if e["t"] == "call" and e.get("id") == ev.get("id")),
                    None,
                )
                # `name`, not `label`: the label pairs the verb with its target
                # for a flat list, and a call whose only subject is its own
                # title reads as that title twice ("update_task_state
                # update_task_state", which is what `meta.tool_calls` shows).
                named = getattr(call, "name", None) or str(ev.get("id") or "a tool call")
                return str(named), str(ev.get("text") or "")
        return None

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for kind in self.kinds:
            tally[kind] = tally.get(kind, 0) + 1
        return tally

    def messages(self, *, in_flight: bool = False) -> list[dict[str, Any]]:
        """The ordered events as provider-shaped messages.

        The shape itself lives in
        :mod:`raven.agent.subagent.backends.turn_rows`, shared with the OpenAI
        Step Dialect: one implementation is what keeps the two transports'
        conversations readable by one renderer. This maps ACP's events onto that
        vocabulary and nothing else.
        """
        events: list[dict[str, Any]] = []
        for ev in self.events:
            kind = ev["t"]
            if kind == "say":
                events.append(turn_rows.say(ev["text"]))
            elif kind == "thought":
                events.append(turn_rows.thought(ev["text"], at=ev.get("at")))
            elif kind == "user":
                events.append(turn_rows.user(ev["text"], at=ev.get("at")))
            elif kind == "call":
                acp_call: ToolCall = ev["call"]
                events.append(
                    turn_rows.call(
                        id=ev["id"],
                        name=acp_call.name,
                        arguments_json=acp_call.arguments_json(),
                        at=ev.get("at"),
                    )
                )
            elif kind == "result":
                events.append(turn_rows.result(id=ev["id"], text=ev["text"], ok=ev["ok"], at=ev.get("at")))
        return turn_rows.rows(
            events,
            in_flight_answer=self.closing_text if in_flight else None,
            in_flight_answer_at=self.answer_at if in_flight else None,
        )


async def _bridged_auth(name: str, cfg: Any, *, scope: str | None = None) -> Any:
    """The credential the host uses for ``name``, for the endpoint's own upstream.

    The endpoint dials the real server once per downstream connection, so it
    needs what the host's connection manager attaches to its own -- without it
    an OAuth server answers the bridge with 401 and the sub-agent sees a server
    that never finished connecting, while the same server is connected and
    listed on the host.

    Nothing about this crosses the socket: the token is used on the host's side
    of the relay, and the sub-agent still receives only JSON-RPC frames.

    ``can_park=False`` because this connect is a dispatch: a stored token is
    attached and works, and one that needs a browser degrades this server now
    rather than holding the node for the flow timeout.
    """
    if getattr(cfg, "auth", "none") != "oauth":
        return None
    from raven.mcp.oauth import provider_for

    try:
        return await provider_for(name, cfg, can_park=False, scope=scope)
    except Exception as exc:  # noqa: BLE001 - an unusable credential costs this server, not the turn
        logger.warning("acp: no credential for bridged MCP server {!r}: {}", name, exc)
        return None


def _prompt_deadline(configured: int | None) -> int | None:
    """How long this prompt may run: the tighter of the config and the brief.

    Every shipped worker configures ``None`` here, and deliberately -- a long
    job is a long job, and a fixed ceiling would cut the ones this roster exists
    to run. A brief, unlike a config, knows what *this* dispatch is, so it may
    name a deadline the config could not have known to set. It may only tighten.
    """
    try:
        from raven.agent.subagent.charter import narrowed_timeout

        return narrowed_timeout(configured)
    except Exception:  # noqa: BLE001 - a brief that cannot be read sets no deadline
        return configured


def _prompt_meta(skey: Any) -> dict[str, Any]:
    """The ``_meta`` one prompt carries.

    The usage context always; a charter only when this dispatch brought one, so
    a turn with no playbook in play puts exactly the bytes on the wire it put
    before charters existed. The receiving side drops a key it does not know,
    which is what lets a newer host talk to an older worker.
    """
    from raven.agent.subagent.delegate import outbound_charter
    from raven.token_wise import usage_context

    meta: dict[str, Any] = {"raven.usage": usage_context.delegation(skey)}
    charter = outbound_charter()
    if charter:
        meta["raven.playbook"] = dict(charter)
    return meta


class AcpAgentBackend:
    """Runs a task as one ``session/prompt`` against a pooled ACP connection."""

    streams = True
    kind = "acp"

    def __init__(
        self,
        *,
        name: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        ready_timeout_ms: int = 30000,
        timeout: int | None = None,
        max_output_chars: int = 30000,
        snapshot: CapabilitySnapshot | None = None,
        registry: InstanceRegistry | None = None,
        mcps: list[str] | None = None,
        allow_mcp_secrets: bool = False,
        session_mcp: bool = True,
        pool: Any = None,
    ) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env = env or {}
        self.ready_timeout_ms = ready_timeout_ms
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._snapshot = snapshot
        # What each live session's model was before this host touched it, and
        # what this host last set it to. Both keyed by session id, and both exist
        # only so that clearing an override can be honoured: the agent takes a
        # value, never an "unset", so the only way back to its own choice is to
        # send that choice again -- and nothing else remembers what it was.
        self._model_baseline: dict[str, str] = {}
        self._model_pushed: dict[str, str] = {}
        # Values this agent has refused, so a refusal re-attempted on every
        # route into a session -- and on every fresh session a spawn opens --
        # is said once per value rather than once per turn.
        self._model_refused: set[str] = set()
        self._registry = registry or get_registry()
        # Which pool this backend's turns are served from. Almost always the
        # process-wide one, and held unresolved until it is used so that
        # ``close_pool`` still means what it says. A caller passes its own when
        # its turns must not disturb the roster's: the pool keys a connection on
        # its launch arguments, ``cwd`` among them, and a caller whose workspace
        # is a throwaway directory therefore never matches a held connection --
        # so on the shared pool it would retire this agent's live connections
        # before opening its own. ``ping_agent`` is that caller.
        self._pool = pool
        self.mcps = mcps
        self.allow_mcp_secrets = allow_mcp_secrets
        self.session_mcp = session_mcp
        self.mcp_source: McpSource | None = None
        # Set by whichever manager dispatches through this backend; see
        # ``bind_session_dir``. ``None`` until then, which is why the recorder is
        # built lazily rather than in this constructor.
        self._session_dir_for: Any = None
        self._event_sink: Any = None
        self._caps_listener: Any = None
        self._unprompted_announce: Any = None

    @property
    def _follows_parent(self) -> bool:
        """Whether this agent is raven's own, and so runs on the parent's providers.

        The handshake's answer, the same one the roster reads for ``own`` and
        the listing for its model rule: a product built on ``raven acp`` names
        itself ``raven`` there whatever its row is called here.
        """
        return getattr(self._snapshot, "agent_name", "") == "raven"

    @property
    def pool(self) -> Any:
        """The connection pool this backend's turns are served from.

        Resolved on each read rather than in the constructor: ``close_pool``
        replaces the process-wide pool with a fresh one, and a backend that had
        captured the old object would go on acquiring from a closed pool for the
        rest of its life.
        """
        return self._pool if self._pool is not None else get_pool()

    def _resident_sinks(self) -> tuple[Any, Any]:
        """The two callables a resident recorder routes through, bound to this backend.

        Read through self at call time, not captured: the sinks are re-bound on
        every dispatch, so a capture would freeze whichever manager happened to
        dispatch first. `self` is still this backend, though, and a backend lives
        one generation while the pooled connection lives the process -- which is
        why an existing recorder is re-pointed (``_repoint_resident``) rather
        than kept as it was built.
        """
        emit = lambda session_key, event: (  # noqa: E731 - the same shape twice, bind and rebind
            self._event_sink(session_key, event) if self._event_sink is not None else None
        )
        announce = lambda session_key, handle, text: (  # noqa: E731
            self._unprompted_announce(session_key, self.name, handle, text)
            if self._unprompted_announce is not None
            else None
        )
        return emit, announce

    @staticmethod
    def _repoint_resident(connection: Any, emit: Any, announce: Any) -> bool:
        """Re-point the connection's resident recorder, if it has one. True when it did."""
        existing = getattr(connection, "_raven_unprompted", None)
        if existing is None:
            return False
        rebind = getattr(existing, "rebind", None)
        if callable(rebind):
            rebind(emit=emit, announce=announce)
        return True

    def repoint_pooled_resident(self) -> None:
        """Re-point the live pooled connection's recorder at this backend now.

        Called by the manager at the SWAP boundary (`set_submit`, when the new
        generation is given its scheduler) -- never from the binders, which run
        during BUILD while generation N must keep serving untouched, and never
        from the next dispatch, which may come long after an existing agent has
        woken on its own. The pool is process-lifetime; this is the one place
        the surviving transport is rewired, and only once the generation that
        takes it can actually serve a wake. A candidate that fails to assemble
        never reaches here, so N stays connected.
        """
        try:
            connections = self.pool.live(self.name)
        except Exception:  # noqa: BLE001 - a swap must never fail on a transport that cannot be listed
            return
        if not connections:
            return
        emit, announce = self._resident_sinks()
        # One connection per parent binding under the agent's name; every one
        # of them may hold a recorder, and every one belongs to this generation now.
        for connection in connections:
            self._repoint_resident(connection, emit, announce)

    def _ensure_unprompted_recorder(self, connection: Any) -> None:
        """Give this connection a resident sink, once.

        Idempotent by an attribute on the connection rather than on this backend:
        connections are pooled per agent and outlive any one backend instance, so
        a flag here would stop re-attaching after a reconnect that cleared it.

        Best-effort: a recorder that cannot be built costs the record of an
        unprompted turn, which is what the situation was before it existed, and
        must not cost the turn now being sent.
        """
        emit, announce = self._resident_sinks()
        if self._repoint_resident(connection, emit, announce):
            return
        try:
            from raven.acp_client.unprompted import UnpromptedRecorder

            recorder = UnpromptedRecorder(
                self.name, self._registry, self._session_dir_for, emit=emit, announce=announce
            )
            connection.router.set_resident(recorder)
            connection._raven_unprompted = recorder  # noqa: SLF001 - a marker on a pooled object, and the handle to rebind
        except Exception as exc:  # noqa: BLE001 - never at the cost of the turn being sent
            logger.warning("acp agent {!r}: no unprompted-turn recorder: {}", self.name, exc)

    def bind_event_sink(self, sink: Any) -> None:
        """Take the dispatching manager's event emitter.

        Same shape and same reasoning as ``bind_session_dir``: backends are
        shared across managers, both managers' sinks reach the same subscription
        emitter, and last-writer-wins is therefore correct.
        """
        self._event_sink = sink

    def bind_unprompted_announcer(self, announce: Any) -> None:
        """Take the dispatching manager's wake-the-conversation call.

        Same last-writer-wins shape as the binders around it: every manager's
        announce routes by the session key the call itself carries, so which
        manager is held changes nothing about where a wake lands. The pooled
        connection's resident recorder is not touched here: that happens at the
        SWAP boundary (`repoint_pooled_resident`), once the manager can serve.
        """
        self._unprompted_announce = announce

    def bind_caps_listener(self, listener: Any) -> None:
        """Take the dispatching manager's "re-derive the agent table" call.

        Same last-writer-wins shape as the two binders around it, and safe for a
        stronger reason: every manager wants the same rebuild, so which one is
        held makes no difference to what happens.
        """
        self._caps_listener = listener

    def _note_session_model(self, session_id: str | None, result: Any) -> None:
        """Remember what a session was on before this host moved it.

        Recorded once per session and never overwritten: on a resumed route the
        reported value already carries whatever this host pushed earlier, so a
        second reading would adopt that as the baseline and clearing would then
        restore the override it was meant to undo.
        """
        if not session_id or session_id in self._model_baseline:
            return
        current = read_session_current_model(result)
        if current:
            self._model_baseline[session_id] = current

    def _relearn_modes(self, result: Any) -> None:
        """Take the modes a session response advertises over the cached ones.

        Best-effort at every step. The menu is a description the NEXT dispatch
        reads, never something this turn depends on, so neither the write nor
        the table rebuild may cost the turn that happened to carry the evidence.
        """
        try:
            updated = relearn_session_modes(self._snapshot, result)
        except Exception as exc:  # noqa: BLE001 - a cache refresh must not sink a turn
            logger.warning("acp agent {!r}: refreshing modes from the session failed: {}", self.name, exc)
            return
        if updated is None:
            return
        self._snapshot = updated
        if callable(self._caps_listener):
            try:
                self._caps_listener()
            except Exception:
                logger.exception("acp agent {!r}: rebuilding the agent table failed", self.name)

    def bind_session_dir(self, resolver: Any) -> None:
        """Take the dispatching manager's session-directory rule.

        Backends are shared -- the registry hands one instance to every manager --
        so this is last-writer-wins, and correct for the same reason it is safe:
        the resolver answers where a *session key* keeps its records, and two
        managers looking at one session key mean the same directory. What differs
        between them is which sessions they have, not where a session lives.
        """
        self._session_dir_for = resolver

    def set_mcp_source(self, source: McpSource | None) -> None:
        """Late-bind the host MCP definition source."""
        self.mcp_source = source

    def _adapter_path(self) -> str | None:
        """The PATH the adapter process will see, which is where raven has to be findable.

        Costly exactly once per process: ``login_shell_env`` runs the user's
        login shell with a 15-second budget and memoizes the result, so the
        first caller pays for a slow profile and every later one reads the memo.
        Which caller that is matters -- see :meth:`resolve_mcp_grant_async`.
        """
        return {**login_shell_env(), **self.env}.get("PATH")

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
        """Resolve this dispatch against this entry's export policy.

        The adapter's PATH is captured only when this dispatch actually asked
        for servers. It used to be captured first, to build the target, and that
        put :meth:`_adapter_path` on the path of every ordinary dispatch --
        including the ones requesting no MCP at all, whose grant is empty
        whatever the PATH turns out to be.

        Synchronous, because the preflight callers are. A caller with a loop to
        await on should take :meth:`resolve_mcp_grant_async` instead, which keeps
        the capture off it.
        """
        effective = self.mcps if mcps is None else mcps
        stdio_path = self._adapter_path() if effective else None
        target = acp_target(allow_secrets=self.allow_mcp_secrets, stdio_path=stdio_path)
        return resolve_grant(effective, self.mcp_source, target)

    async def resolve_mcp_grant_async(self, mcps: list[str] | None = None) -> McpGrant:
        """:meth:`resolve_mcp_grant` with the PATH capture off the event loop.

        The capture shells out and can block for real seconds on a slow profile
        (nvm/conda init); ``to_thread`` keeps that off the loop, the same way the
        cli transport and the ACP client spawn do it. It is warmed rather than
        read here because the memo is what the synchronous resolution below then
        hits, so the thread hop costs one dispatch per process and nothing after.
        """
        effective = self.mcps if mcps is None else mcps
        if effective:
            await asyncio.to_thread(login_shell_env)
        return self.resolve_mcp_grant(mcps)

    @property
    def _session_mcp_refused(self) -> bool:
        """Whether this dispatch's ``mcpServers`` must be withheld.

        Two unrelated peers earn it, and the two reasons are worth keeping apart.

        **The agent is configured as not isolating** (``sessionMcp: false``).
        Nothing in ``initialize`` reports isolation -- the three shipped adapters
        report the same ``mcpCapabilities`` and only one of them fails to isolate
        -- so this is a declaration on the agent row and not something to probe;
        see ``ThirdPartyAcpSubagentConfig.session_mcp``. It matters because raven
        pools one connection per agent name: on a peer that does not isolate, two
        concurrent sub-agents see each other's servers, so a node deliberately not
        granted one reaches it through the sibling that was.

        **The peer is a raven build that would refuse the field**, answering a
        non-empty ``mcpServers`` with ``-32602`` instead of connecting it. Such a
        build reports the same ``mcpCapabilities`` object as one that honours the
        field, so the only signal is the absence of the ``_meta`` promise
        (``SESSION_MCP_CAPABILITY``) -- read from the snapshot the way
        ``is_stateful`` and ``can_steer`` are, because the decision has to be made
        before any endpoint is opened.

        Absence of the promise is *not* read as a refusal on its own, and this is
        the load-bearing half: stdio servers are the ACP baseline, so
        claude-agent-acp, codex-acp and opencode all accept a bridge stanza while
        declaring nothing, and per-session lifetime for them is the host socket's
        lifetime rather than anything they agreed to. Requiring the promise from
        them would turn their MCP off. So the peer must also identify as a raven,
        by the same ``agentInfo.name`` substring test ``dialect_for`` uses --
        measured: ``raven``, ``raven-ppt`` and ``raven-x-research`` against
        ``@agentclientprotocol/claude-agent-acp``, ``@agentclientprotocol/codex-acp``
        and ``OpenCode``.

        An unmeasured agent has no snapshot and is therefore never refused by the
        second branch, which leaves the pre-existing ``-32602`` as its failure and
        never silences a peer that would have worked.

        That second branch is transitional by intent: every raven fork is meant to
        serve per-session MCP, so it exists for the window in which a fork has not
        taken the code yet, not as a standing policy about forks. The first is not
        transitional -- it is the peer's own property.
        """
        from raven.agent.subagent.backends import session_mcp_delivered

        return not session_mcp_delivered(session_mcp=self.session_mcp, snapshot=self._snapshot)

    def _mcp_note(self, grant: McpGrant) -> str:
        """Every secret-free reason this dispatch's MCP is degraded.

        The withholding decided here is not a ``McpGrant`` reason, so it is
        joined onto ``note_text`` rather than routed through it -- a grant whose
        servers all resolved cleanly still owes the user this sentence, because a
        sub-agent that silently received no servers looks exactly like one nobody
        configured.
        """
        notes = []
        if self._session_mcp_refused and grant.granted:
            names = ", ".join(repr(server.name) for server in grant.granted)
            if not self.session_mcp:
                notes.append(
                    f"MCP servers {names} were withheld because acp agent {self.name!r} is configured as not "
                    f"keeping one session's servers to that session (sessionMcp: false); raven pools one "
                    f"connection per agent, so delivering them would offer this dispatch's servers to every "
                    f"concurrent sub-agent of that agent. Set sessionMcp: true once it isolates"
                )
            else:
                notes.append(
                    f"MCP servers {names} were withheld because acp agent {self.name!r} is a raven build that "
                    f"does not declare per-session MCP support ({SESSION_MCP_CAPABILITY}); sending them would "
                    f"fail the session instead of degrading it. Upgrade that agent's raven to deliver them"
                )
        if note := grant.note_text():
            notes.append(note)
        return "; ".join(notes)

    @contextmanager
    def _annotate_mcp_failure(self, grant: McpGrant) -> Iterator[None]:
        """``mcp_grant.annotate_mcp_failure`` over :meth:`_mcp_note`.

        Same shape and same reason, one note wider: the withholding this backend
        decides is not a ``McpGrant`` reason, so the generic helper cannot see it,
        and a dispatch that failed *because* its tools never arrived would
        otherwise carry no word of why.
        """
        try:
            yield
        except SubagentActionAbortedError:
            raise
        except Exception as exc:
            if note := self._mcp_note(grant):
                raise McpDispatchError(f"{exc}\n\n[raven] {note}.") from exc
            raise

    @asynccontextmanager
    async def _mcp_endpoints(self, node_id: str, grant: McpGrant) -> AsyncIterator[list[dict[str, Any]]]:
        """The bridge stanzas for one dispatch, live for exactly as long as it runs.

        ``node_id`` is this dispatch's identity -- a DAG node's id, a spawn's
        task id -- because reaping is per node and not per run: a graph of a
        dozen nodes would otherwise hold every node's sockets, and the upstream
        server process behind each of them, open until the whole graph finished.

        Empty when raven is not on the adapter's PATH, and empty when the peer
        would refuse the field (:attr:`_session_mcp_refused`). Nothing is withheld
        silently in either case: ``resolve_grant`` records the first as
        ``command_not_found``, :meth:`_mcp_note` names the second, and the
        dispatch closes with both.

        A grant with nothing to bridge leaves without asking for the adapter's
        PATH at all, and the ask itself is a thread hop: :meth:`_adapter_path`
        can run the login shell, and this runs inside the dispatch's own turn.
        """
        # Both of these come off the host's MCP source: the executor so a bridged
        # stdio server is spawned inside the sandbox the host configured rather
        # than on the host, and the deny set so a tool the host switched off is
        # unreachable through the relay too. Without them the endpoint has the
        # mechanism and no policy.
        provider = getattr(self.mcp_source, "executor_provider", None)
        endpoints = McpEndpoints(
            executor_provider=provider() if callable(provider) else None,
            disabled_tools=frozenset(grant.disabled_tools),
        )
        try:
            if not grant.granted:
                yield []
                return
            argv = bridge_command(path=await asyncio.to_thread(self._adapter_path))
            if argv is None or self._session_mcp_refused:
                if self._session_mcp_refused:
                    # Two independent reasons reach here and they call for
                    # different actions, so they must not share a sentence: one
                    # is answered by upgrading that agent's raven, the other by
                    # the peer learning to isolate. ``_mcp_note`` already tells
                    # them apart on the reply; this is the operator's copy.
                    logger.warning(
                        "acp agent {!r}: withholding {} MCP server(s); {}",
                        self.name,
                        len(grant.granted),
                        "configured as not keeping a session's servers to that session (sessionMcp: false)"
                        if not self.session_mcp
                        else f"a raven build with no {SESSION_MCP_CAPABILITY} declaration",
                    )
                yield []
                return
            paths = {
                server.name: str(
                    await endpoints.open(
                        node_id,
                        server.name,
                        server.config,
                        http_auth=await _bridged_auth(server.name, server.config, scope=server.scope),
                    )
                )
                for server in grant.granted
            }
            yield grant.with_endpoints(paths).for_acp(argv)
        finally:
            # Through close(), never by closing the listeners here: it also
            # cancels the in-flight relays, and without that wait_closed blocks
            # for as long as the sub-agent keeps its bridge open -- which is
            # exactly this moment.
            await endpoints.close(node_id)

    @property
    def is_stateful(self) -> bool:
        """Whether reusing a handle continues this agent's session.

        Read from the handshake snapshot, never declared: a cli entry derives this
        from ``resume_command`` because that is the mechanism that would have to
        deliver it, and for acp the equivalent mechanism is the agent's own
        ``session/load`` -- so the capability read here is ``loadSession``, the one
        that method is gated on, and not ``sessionCapabilities.resume``.

        Measured 2026-09-07: github-copilot, codebuddy and pi-acp all report
        ``resume: false`` alongside ``loadSession: true`` and all three resume
        correctly when ``session/load`` is called. Read from ``resume``, they were
        stateless -- which never reached the load call, and cost them their strip
        row and their place among the spawn schema's ``instance`` targets.
        """
        return bool(self._snapshot and self._snapshot.can_load)

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        mcps: list[str] | None = None,
        mcp_grant: McpGrant | None = None,
        mode: str | None = None,
        # Not `model` above, which is the PARENT's and rides out as
        # RAVEN_PARENT_MODEL: this is what this agent should answer with.
        session_model: str | None = None,
        authored_task: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        media: Sequence[Media] = (),
    ) -> str:
        # The parent binding is forwarded when the pooled ACP worker launches.
        # Without this, a long-lived worker keeps the provider/model captured
        # during its first startup even after the WebUI session switches.
        skey = session_key or "default"
        handle = instance or task_id
        # Two directories, deliberately not one. `launch_cwd` is where the server
        # process starts and is part of the pool's launch key, so it has to stay
        # constant across workspaces or every new one relaunches the server and
        # drops the sessions the old one was serving -- which is why an entry may
        # pin it. `session_cwd` is the working directory of the *turn*: the agent
        # binds it as the session's `workdir` and its tools resolve paths against
        # it, so it must be the caller's workspace whatever the entry pinned.
        # Conflating them sent a pinned entry's own folder to `session/new`, and
        # the agent then edited that tree instead of the caller's.
        launch_cwd = self.cwd or str(workspace)
        session_cwd = str(workspace)
        started = time.monotonic()
        grant = mcp_grant if mcp_grant is not None else await self.resolve_mcp_grant_async(mcps)
        async with self._mcp_endpoints(task_id, grant) as mcp_servers:
            with (
                external_agent_span(agent=self.name, transport="acp", task_id=task_id, instance=handle) as span,
                self._annotate_mcp_failure(grant),
            ):
                # The entry's own budget, not the pool's default: `readyTimeoutMs` is
                # documented as how long the `initialize` handshake may take, and
                # after the handshake moved into the connection it was the one thing
                # that stopped honouring it -- so an operator who raised it for a
                # slow adapter had `verify` pass at 150s and every dispatch fail at
                # the module constant.
                budget = max(1.0, self.ready_timeout_ms / 1000)
                # Handed to the pool apart from the entry's own env: a binding is
                # not a config edit. Folded into `env` it changed the launch key, and
                # the pool answered a second binding by closing the first binding's
                # connection -- mid-turn, if one was running. Kept separate, each
                # binding gets a connection of its own and the two coexist.

                binding: dict[str, str] = {}
                if model:
                    binding["RAVEN_PARENT_MODEL"] = model
                parent_provider = str(getattr(provider, "provider_name", "") or "")
                if parent_provider:
                    binding["RAVEN_PARENT_PROVIDER"] = parent_provider
                parent_protocol = str(getattr(provider, "api_protocol", "") or "")
                if parent_protocol:
                    binding["RAVEN_PARENT_PROTOCOL"] = parent_protocol
                # One of raven's own with no pin of its own follows the parent.
                # The binding above puts a fresh worker on the parent's model at
                # launch; this says the same to the session on every route in,
                # because a resumed session keeps whatever it was last put on --
                # a pin since cleared, kept in the agent's own session record
                # across a host restart that forgot it ever pushed one -- and the
                # page then reads "follows the main Raven" over a session that
                # answers on something else. The value is the child's own
                # spelling, `<slug>/<id>`; a parent whose provider names no slug
                # gives it nothing to route by, so nothing is pushed and the
                # launch binding stands. A third party is left alone: the
                # parent's model is not one of its choices.
                if not session_model and model and parent_provider and self._follows_parent:
                    session_model = model if model.startswith(f"{parent_provider}/") else f"{parent_provider}/{model}"
                connection = await self.pool.acquire(
                    name=self.name,
                    command=self.command,
                    cwd=launch_cwd,
                    env=dict(self.env),
                    binding=binding or None,
                    ready_timeout_s=budget,
                )
                client = connection.client
                # Marked after acquire, so a call's range covers its own traffic and
                # not the handshake of a connection it merely inherited. The pool
                # records where that handshake ends, and it is carried alongside so a
                # reader of one call can still find it.
                journal = client.journal
                frames_start = journal.offset if journal is not None else None

                session_id, resumed = await self._open_session(
                    client,
                    cwd=session_cwd,
                    skey=skey,
                    handle=handle,
                    budget=budget,
                    mcp_servers=mcp_servers,
                    router=connection.router,
                    mode=mode,
                    session_model=session_model,
                )
                if journal is not None:
                    # Inside the marked range on purpose, so the per-call copy of the
                    # frames carries the line that says whose call they are.
                    journal.bind(
                        {
                            "session": session_id,
                            "agent": self.name,
                            "instance": handle,
                            "task_id": task_id,
                            "session_key": skey,
                            "resumed": resumed,
                        }
                    )
                record_session(span, session_id=session_id, resumed=resumed)

                # Snapshotted before the prompt so the error below names what this
                # turn ran into rather than what the connection has ever refused.
                refused_before = client.refusal_count
                sink = bounded_delta(on_delta, self.max_output_chars)
                # From the live handshake, not the stored snapshot: this is the
                # process actually answering, and a snapshot can be stale.
                # `session_cwd` rather than `workspace`: it is the directory the
                # agent's own tools resolve against, so it is where a file this
                # turn writes actually lands.
                collector = _TurnCollector(
                    sink, dialect_for(connection.initialize), prompt=task, workspace=Path(session_cwd)
                )
                # Built here, in the turn's context, for the reason `_TurnCollector`
                # documents: the read loop's ContextVars predate this run, and the
                # asker is bound per turn.
                elicitor = Elicitor(self.name, handle, dialect_for(connection.initialize))
                # Built here for the same reason and bound to this connection's
                # client, because the answer goes back as a request rather than as
                # this frame's return value.
                responder = AskUserResponder(self.name, handle, clarify_responder(client))
                # Held across the whole turn, not just the send: see
                # `_Connection.session_lock` for why two prompts cannot share one
                # session id.
                async with connection.session_lock(session_id):
                    connection.router.reserve(session_id)
                    await connection.router.take_over(session_id, collector)
                    # Once per connection, and here because this is where the pieces
                    # it needs are in hand. What it records is what the agent does
                    # when no run of raven's is attached -- an on-call wake, above
                    # all -- which before this was dropped at the router with a
                    # debug line. After the session's own take_over, so a turn that
                    # is starting owns the session before the resident sink exists.
                    self._ensure_unprompted_recorder(connection)
                    connection.elicitors.attach(session_id, elicitor)
                    connection.responders.attach(session_id, responder)
                    if self.can_steer or steer_offered(connection.initialize):
                        activity.offer_steer(collector._run, self._steerer(client, session_id))
                    try:
                        result = await client.request(
                            "session/prompt",
                            {
                                "sessionId": session_id,
                                "_meta": _prompt_meta(skey),
                                # The attachments ride as resource links beside the
                                # text, the block an editor sends for an @-mentioned
                                # file; see raven.agent.subagent.attachments.
                                "prompt": [
                                    {"type": "text", "text": task},
                                    *attachment_blocks(media, root=_uploads_root()),
                                ],
                            },
                            timeout=_prompt_deadline(self.timeout),
                            cancel_session=session_id,
                        )
                    except asyncio.CancelledError:
                        # An UNSETTLED cancel means the turn outlived its cancel
                        # budget, so it is still running on the agent while this lock
                        # is about to be released -- prompting the same session again
                        # would collide with it. Dropping the binding is the same
                        # recovery `_open_session` makes when a resume fails: the
                        # instance keeps its handle and the next dispatch opens a
                        # fresh session under it.
                        # Evaluated in this order so take_unsettled_cancel -- a
                        # consuming read -- always clears the flag; skipping it for a
                        # stateless agent would leak it.
                        if client.take_unsettled_cancel(session_id) and self.is_stateful:
                            await self._registry.unbind(skey, self.name, handle)
                        elif not resumed and self.is_stateful:
                            # A cancel that SETTLED leaves a live session holding the
                            # partial exchange, and the deferred commit below is
                            # unreachable from here -- so an instance whose *first*
                            # turn was interrupted was never bound, and the next
                            # dispatch opened a fresh session. That reads as the
                            # sub-agent having forgotten the conversation the user
                            # just interrupted, which is the one moment they are
                            # certain it happened.
                            await self._registry.commit(skey, self.name, handle, session_id, kind="acp")
                        # The frames of a turn that was cut short are the ones worth
                        # having, and this path returns no result to hang them off:
                        # published here or the record of a timed-out call points at
                        # nothing, while the journal holds the whole exchange.
                        cancelled = self._frames(journal, connection, session_id, frames_start)
                        activity.note_frames(cancelled)
                        record_frames(span, cancelled)
                        raise
                    finally:
                        activity.offer_steer(collector._run, None)
                        connection.router.detach(session_id, collector)
                        connection.elicitors.detach(session_id, elicitor)
                        connection.responders.detach(session_id, responder)
                        # Detaching stops only the *next* request from routing here.
                        # One already put to the user waits on a task of the
                        # connection's, which no part of this teardown reaches --
                        # the pooled connection stays open, so `AcpClient.close`
                        # never cancels it either. Left standing it holds this
                        # conversation's form lock and keeps a sheet up that answers
                        # into a run that is gone.
                        elicitor.cancel()
                        # Same reason, and the same window: a question already put to
                        # the user answers into a run that is gone, and holds this
                        # conversation's lock against the next one while it waits.
                        responder.cancel()

                stop_reason = (result or {}).get("stopReason") if isinstance(result, dict) else None
                # The agent's own metadata on the response, kept for the run
                # record as sent; the host reads none of it.
                activity.note_response_meta(result.get("_meta") if isinstance(result, dict) else None)
                # The ceiling, from the protocol's own field rather than from the
                # agent's metadata table: every conforming agent reports it here,
                # and the stop reason is a value this design already acts on --
                # where `_meta` is the agent's record the host decides nothing
                # from, which is what the Response Meta term's `_Avoid_` names.
                if stop_reason == "max_tokens":
                    activity.note_output_limit()
                text = collector.text
                frames = self._frames(journal, connection, session_id, frames_start)
                self._record(span, collector, stop_reason=stop_reason, started=started, frames=frames)

                if not text:
                    tail = client.stderr_tail(1500)
                    span.error(f"empty turn (stopReason={stop_reason})")
                    # An agent that asked raven for something it does not serve --
                    # `fs/read_text_file` and its siblings, permission requests
                    # being answered now -- was told the method does not exist, and
                    # adapters answer that by ending the turn with nothing. The
                    # stderr tail says nothing about it, so name it here or it is
                    # unknowable.
                    refused = client.refusals_since(refused_before, session_id=session_id)
                    # Collapsed: one turn with three tool calls refuses the same
                    # method three times, and naming it three times says no more.
                    asked = f"; refused agent requests: {', '.join(sorted(set(refused)))}" if refused else ""
                    raise AcpEmptyTurnError(
                        f"acp agent {self.name!r} ended its turn with no content "
                        f"(stopReason={stop_reason!r}){asked}; stderr tail: {tail or '<empty>'}"
                    )

                if failed := collector.failed_call_without_answer:
                    label, detail = failed
                    span.error(f"no answer after a failed {label}")
                    # Not `if not text`: this turn said something. What it said
                    # is the plan it had for the call that then failed, and
                    # handing that back made a run that did nothing read as a
                    # completed one whose result was a promise to begin.
                    raise AcpEmptyTurnError(
                        f"acp agent {self.name!r} ended its turn on a failed {label} and answered "
                        f"nothing after it (stopReason={stop_reason!r}); the call reported: "
                        f"{detail.strip() or '<no detail>'}"
                    )

                if not resumed and self.is_stateful:
                    # Deferred commit, as the cli transport does it: binding a handle
                    # before the first turn succeeded would resume a session that
                    # never produced anything. A settled cancel binds it above
                    # instead -- that session did produce something, and is the one
                    # the next turn has to continue.
                    await self._registry.commit(skey, self.name, handle, session_id, kind="acp")

                reply = await self._finished(text, stop_reason=stop_reason, span=span, sink=on_delta)
                if note := self._mcp_note(grant):
                    notice = f"\n\n[raven] {note}."
                    activity.append_closing(notice)
                    if on_delta is not None:
                        await on_delta(notice)
                    reply += notice
                return reply

    @property
    def can_steer(self) -> bool:
        """Whether this agent takes text mid-turn (``_raven/session/steer``).

        Read from the handshake snapshot like ``is_stateful``: the agent declares
        the extension in ``agentCapabilities._meta``, and calling an undeclared
        extension gets a method-not-found in the middle of somebody's turn. A
        snapshot measured before the agent learned the extension says no; the
        turn itself also asks the live handshake (``steer_offered``), so a stale
        snapshot delays nothing.
        """
        return bool(self._snapshot and self._snapshot.can_steer)

    def _steerer(self, client: Any, session_id: str) -> Callable[[str], Awaitable[str]]:
        """The steer hook for one prompt: merge text into that session's turn.

        Answers the agent's own status -- ``injected`` or ``no_turn`` -- and
        ``unsupported`` if the agent refuses the method after all. Never raises
        for a refusal: the caller is a person typing, and the honest answer is
        a status they can act on.
        """

        async def steer(text: str) -> str:
            try:
                answer = await client.request(STEER_METHOD, {"sessionId": session_id, "text": text}, timeout=30)
            except AcpRemoteError as exc:
                logger.warning("acp agent {!r} refused a steer: {}", self.name, exc)
                return "unsupported"
            status = answer.get("status") if isinstance(answer, dict) else None
            return status if status in ("injected", "no_turn") else "no_turn"

        return steer

    async def _finished(
        self,
        text: str,
        *,
        stop_reason: Any,
        span: Any,
        sink: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """The reply, marked when the agent stopped before it had said everything.

        Only ``end_turn`` means the agent finished. Every other stop reason
        leaves a reply that reads complete and is not, and the reader -- the main
        agent as much as a person in a direct chat -- has no other way to tell:
        the text simply ends, mid-thought, and the run is reported as a success.
        Measured, and the reason this exists: a cancelled codex turn returned its
        opening sentence and nothing else, with the span still green.

        Appended rather than raised. A partial answer is worth having, and the
        turn's record already holds it -- discarding it to signal the truncation
        would trade one kind of silence for another.

        The notice is budgeted *before* the reply is clamped, so the one line
        saying the answer is incomplete cannot be the part that gets cut.

        Pushed through ``sink`` as well as returned, because for a caller that
        streamed there is no other way for it to arrive: the return value is
        deliberately not delivered a second time (``AgentLoop.run_turn``), so a
        notice that only rode on it reached the record and never the screen --
        which is the one reader it exists for.

        The *unbounded* callback, not the reply's ``bounded_delta``: that budget
        exists to cap what the agent says, and a reply which saturates it would
        otherwise swallow the one line explaining that it was cut off -- exactly
        the case the notice is for. This line is raven's own and fixed-length.
        """
        if stop_reason in ("end_turn", None):
            return await clamp_output(text, self.max_output_chars, agent=self.name, sink=sink)
        span.error(f"turn ended early (stopReason={stop_reason})")
        logger.warning("acp agent {!r}: turn ended with stopReason={!r}; reply is partial", self.name, stop_reason)
        notice = f"\n\n[raven] {self.name} stopped before finishing (stopReason={stop_reason}); reply is partial."
        activity.append_closing(notice)
        if sink is not None:
            await sink(notice)
        # Both lines can be owed at once -- a turn that was cut short can still
        # have said more than the cap allows -- and `clamp_output` budgets this
        # one first for the same reason it was budgeted first here.
        return await clamp_output(text, self.max_output_chars, agent=self.name, reserved=notice, sink=sink)

    async def _open_session(
        self,
        client: Any,
        *,
        cwd: str,
        skey: str,
        handle: str,
        budget: float,
        mcp_servers: list[dict[str, Any]],
        router: Any = None,
        mode: str | None = None,
        session_model: str | None = None,
    ) -> tuple[str, bool]:
        """The session to prompt, and whether it continues an earlier one."""

        async def request_or_busy(method: str, params: dict[str, Any]) -> Any:
            # Busy and broken demand opposite recoveries, and upstream can only
            # act on the message it gets: measured 2026-09-02, session opens
            # timing out behind one long-running turn were judged transport
            # failures, and the re-dispatch loop ran seven adjudication rounds
            # against an agent working correctly the whole time.
            try:
                return await client.request(method, params, timeout=budget)
            except AcpTimeoutError:
                if getattr(client, "prompting", False):
                    raise AcpBusyError(
                        f"acp agent {self.name!r}: {method} waited {budget:.0f}s behind a turn "
                        f"already running on this agent's one connection. The agent is busy, not "
                        f"broken: nothing failed and the transport needs no fixing. Wait for the "
                        f"running turn to end and submit this work again -- re-dispatching now "
                        f"only queues another wait behind the same turn."
                    ) from None
                # Idle the whole budget and silent: nobody is home on this
                # connection, and a retry against it would wait out the same
                # budget against the same silence -- measured 2026-09-02 on the
                # fork, three session opens in a row died on one wedged
                # process because nothing ever gave up on it. Dropping it here
                # is what turns the judge's retry into a fresh launch.
                await self.pool.drop(self.name)
                raise AcpTimeoutError(
                    f"acp agent {self.name!r}: {method} timed out after {budget:.0f}s with no turn "
                    f"in flight -- the agent answered nothing the whole wait. Its connection was "
                    f"dropped, so a retry launches a fresh agent process instead of waiting out "
                    f"the same silence again."
                ) from None

        if self.is_stateful:
            known = await self._registry.lookup(skey, self.name, handle, kind="acp")
            if known is not None and (self._snapshot and self._snapshot.can_load):
                try:
                    # `session/load` is not a getter: the agent answers it by
                    # replaying the whole transcript as `session/update` frames
                    # (see the fork's `raven/acp/replay.py`). No run is attached
                    # to this session yet, so those frames would fall through to
                    # the connection's resident sink -- the unprompted recorder --
                    # and every resumed turn would log its own history as work
                    # the agent did on its own account, and stream it to the pane
                    # a second time. A sink that drops them holds the session for
                    # the length of the call; the turn's collector takes it over
                    # immediately after.
                    async with _replay_dropped(router, known):
                        loaded = await request_or_busy(
                            "session/load",
                            {"sessionId": known, "cwd": cwd, "mcpServers": mcp_servers},
                        )
                    self._relearn_modes(loaded)
                    self._note_session_model(known, loaded)
                    await self._set_mode(client, known, mode, budget=budget)
                    await self._set_model(client, known, session_model, budget=budget)
                    return known, True
                except AcpRemoteError as exc:
                    # The agent's own store may have pruned this id, and the
                    # `session/load` param shape is not measured against a live
                    # server. Either way a fresh session is a working outcome, so
                    # drop the stale binding rather than fail the task -- the
                    # instance itself stays on the strip, see `unbind`.
                    logger.warning(
                        "acp agent {!r}: resuming {}/{!r} failed ({}); starting a fresh session",
                        self.name,
                        handle,
                        known,
                        exc.message,
                    )
                    await self._registry.unbind(skey, self.name, handle)
                except AcpError:
                    # A transport-level failure is not evidence the session was
                    # pruned, so the binding is kept and the caller is told.
                    raise

        result = await request_or_busy("session/new", {"cwd": cwd, "mcpServers": mcp_servers})
        self._relearn_modes(result)
        session_id = (result or {}).get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AcpEmptyTurnError(f"acp agent {self.name!r}: session/new returned no sessionId")
        self._note_session_model(session_id, result)
        await self._set_mode(client, session_id, mode, budget=budget)
        await self._set_model(client, session_id, session_model, budget=budget)
        return session_id, False

    async def _set_model(self, client: Any, session_id: str, model: str | None, *, budget: float) -> None:
        """Put this session on ``model``, or back on its own when that is cleared.

        On every route into a session, for the reason ``_set_mode`` gives below
        and for the same mechanism: the agent binds the choice to the session id
        it holds in memory, so it survives an engine eviction but not a restart
        of the agent process, and the pool relaunches that process whenever the
        launch key changes. Re-asserting each time is what repairs that without
        anyone noticing.

        The channel is ``session/set_config_option`` with ``configId: "model"``.
        ``session/set_model`` is not in the stable schema, and an agent waiting
        for it would never be asked to switch.

        A cleared override is a request too, and that is the half worth spelling
        out. The option takes a value and has no "unset", so dropping the host's
        record restores nothing: the session keeps answering on whatever it was
        last told, while every surface reports the agent's own. The way back is
        to send the session's own baseline again -- the value it reported before
        this host first moved it -- and only when this host did move it, so an
        untouched session still costs no frame at all.

        Never fatal, like the mode beside it. An agent that offers no such option
        answers invalid-params and one that will not take the value answers with
        its own code; either way the task still runs on the agent's own model,
        and failing the run would be a worse outcome than running it on a model
        the caller did not pick. Said once per value: the push is re-asserted
        on every route in and on every session a spawn opens, so a permanent
        refusal would otherwise be a warning per turn for as long as the row
        keeps the pick.
        """
        pushed = self._model_pushed.get(session_id)
        if model:
            target = model
        elif pushed is None:
            # Nothing asked for and nothing of this host's to undo.
            return
        else:
            target = self._model_baseline.get(session_id, "")
            if not target:
                # Moved, and the agent never said what it was on beforehand. Said
                # out loud rather than left as a silent no-op, because the reader
                # has just asked for something this cannot deliver.
                logger.warning(
                    "acp agent {!r}: session {} was put on {!r} and reported no earlier model; "
                    "cannot restore the agent's own",
                    self.name,
                    session_id,
                    pushed,
                )
                self._model_pushed.pop(session_id, None)
                return
        try:
            await client.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": target},
                timeout=budget,
            )
        except AcpRemoteError as exc:
            self._say_model_refused(
                target,
                "acp agent {!r}: session {} would not take model {!r} ({}); running on its default",
                self.name,
                session_id,
                target,
                exc.message,
            )
            return
        except AcpError as exc:
            self._say_model_refused(
                target,
                "acp agent {!r}: could not set model {!r} on session {} ({}); running on its default",
                self.name,
                target,
                session_id,
                exc,
            )
            return
        self._model_refused.discard(target)
        # Reached only when the agent took it, which both refusal arms above
        # return before. That is what keeps this record true in either
        # direction: a refused switch must not leave this host believing it
        # moved a session it did not, and a refused restore must not erase the
        # note that it DID -- without which every later turn with no override
        # returns early and the session is never brought back at all.
        if model:
            self._model_pushed[session_id] = target
        else:
            self._model_pushed.pop(session_id, None)

    def _say_model_refused(self, value: str, message: str, *args: Any) -> None:
        first = value not in self._model_refused
        self._model_refused.add(value)
        logger.log("WARNING" if first else "DEBUG", message, *args)

    async def _set_mode(self, client: Any, session_id: str, mode: str | None, *, budget: float) -> None:
        """Put this session in ``mode`` before the prompt, if one was asked for.

        On every route into a session, not only on creation. The agent holds the
        mode in memory keyed by session id, so it survives an engine eviction but
        NOT a restart of the agent process -- and the pool relaunches that process
        whenever the launch key changes. A resumed session would then silently be
        back on the agent's default, which is the failure this exists to prevent:
        the caller asked for a budget and would be billed a different one with
        nothing anywhere saying so.

        Never fatal. An agent with no modes answers method-not-found and one that
        does not know this id answers invalid-params; in both cases the task can
        still run on the agent's default, and failing it would be a worse outcome
        than running it slightly cheaper or dearer than asked.
        """
        if not mode:
            return
        try:
            await client.request("session/set_mode", {"sessionId": session_id, "modeId": mode}, timeout=budget)
        except AcpRemoteError as exc:
            logger.warning(
                "acp agent {!r}: session {} would not take mode {!r} ({}); running on its default",
                self.name,
                session_id,
                mode,
                exc.message,
            )
        except AcpError as exc:
            logger.warning(
                "acp agent {!r}: setting mode {!r} failed ({}); running on its default",
                self.name,
                mode,
                exc,
            )

    @staticmethod
    def _frames(journal: Any, connection: Any, session_id: str, start: int | None) -> dict[str, Any]:
        """Where this call's frames sit in the connection's journal.

        Empty when no journal is being kept, so a disabled journal leaves the
        record saying nothing about frames rather than pointing at a file that
        does not exist.
        """
        if journal is None or start is None:
            return {}
        return {
            "path": str(journal.path),
            "session_id": session_id,
            "start": start,
            "end": journal.offset,
            "handshake_end": getattr(connection, "handshake_bytes", 0),
        }

    def _record(
        self,
        span: Any,
        collector: _TurnCollector,
        *,
        stop_reason: Any,
        started: float,
        frames: dict[str, Any],
    ) -> None:
        counts = collector.counts()
        record_events(span, transport="acp", kinds=counts)
        thought_chars = sum(len(t) for t in collector.thoughts)
        record_outcome(
            span,
            answer_chars=len(collector.text),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            stop_reason=stop_reason,
            update_counts=counts,
            tool_calls=collector.tool_calls,
            thought_chars=thought_chars,
            usage=collector.usage or None,
        )
        # The same facts to the run's own record, so a reader sees what the agent
        # did without opening a trace viewer. Once, not per notification: an ACP
        # agent's `usage_update` is cumulative for the turn. The calls were
        # already published as they landed; this is the settled list, with the
        # labels every later frame corrected, written over it rather than added.
        activity.set_tool_calls(activity.current(), collector.tool_calls, collector.failed_calls)
        activity.note_usage(collector.usage)
        activity.note_steps(counts)
        activity.note_thoughts(thought_chars)
        activity.note_transcript(collector.messages())
        # After the transcript, and only here: the live republish inside the
        # collector carries the streaming tail as a message, and this is the
        # settled split -- narration on the steps, the reply on its own.
        activity.note_closing(collector.closing_text)
        activity.note_frames(frames)
        record_frames(span, frames)


__all__ = ["AcpAgentBackend", "AcpEmptyTurnError"]
