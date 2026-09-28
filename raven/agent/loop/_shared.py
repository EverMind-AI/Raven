"""Module-level names of the agent loop, shared by main and its mixins.

``main`` and the mixins import what they use from here.
"""
# ruff: noqa: F401 -- every name here is imported by main or by a mixin;
# unused-here is the point.

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection
from uuid import uuid4

from loguru import logger

from raven.acp_client import resolver as autofill_resolver
from raven.acp_client.asker import current_autofill
from raven.agent import workdir
from raven.agent.context import ContextBuilder
from raven.agent.loop.failure_streak import (
    failure_class,
    is_hard_tool_failure,
    loop_break_nudge,
)
from raven.agent.loop.no_progress import NoProgressAction, NoProgressGuard
from raven.agent.loop.recovery import (
    OUTPUT_LIMIT_NUDGE,
    POST_TOOL_NUDGE,
    RecoveryAction,
    RecoveryLimits,
    classify_empty_response,
    is_only_think_debris,
    strip_think_blocks,
)
from raven.agent.subagent import SubagentManager
from raven.agent.subagent.direct_chat import DirectChatHandoff
from raven.agent.subagent.spawn_tool import SpawnTool
from raven.agent.tools.ask_user import AskUserTool
from raven.agent.tools.connection_add import ConnectionAddTool
from raven.agent.tools.file_search import FindTool, GrepTool
from raven.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.media_gen import (
    ImageGenerateTool,
    SpeechGenerateTool,
    VideoGenerateTool,
)
from raven.agent.tools.message import MessageTool
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.shell import ExecTool
from raven.agent.tools.web import (
    SEARCH_PROVIDERS,
    ImageSearchTool,
    WebFetchTool,
    WebSearchTool,
    image_search_vendor,
    resolve_vendor_key,
)
from raven.contracts.assembled import TokenBudget
from raven.contracts.llm_provider import LLMProvider, LLMResponse
from raven.contracts.tool import SKIPPED_AFTER_BLOCKED_CALL, Continuation, FileRemoval, ToolOutput
from raven.memory_engine import MemoryConsolidator, MemoryStore, StorePipeline
from raven.observability import semconv
from raven.providers.base import send_max_tokens
from raven.providers.binding import ModelBinding, active_binding, use_binding
from raven.providers.capabilities import image_placeholder_text, supports_image_tool_result, vision_verdict
from raven.providers.rates import resolve_context_window, resolve_max_output_tokens
from raven.providers.streaming import stream_llm_call
from raven.sandbox import SandboxConfig, SandboxExecutor, SandboxInitError, build_executor
from raven.session.manager import Session, SessionManager
from raven.spine.turn import Origin, session_of
from raven.tracing import trace
from raven.utils.images import is_image_part, is_inline_image
from raven.utils.tokens import estimate_prompt_tokens

# Teardown's budget for letting outstanding writes finish. See
# ``drain_backend_stores``: the pipeline cuts retries short first, so this only
# ever covers a request already on the wire, not a worker asleep in backoff.
# Deliberately shorter than the write it waits on rather than sized to it: an
# EverOS turn store is a final flush, and a flush is an extraction -- measured
# at 16.98s end to end. Sizing this to cover that would park every CLI exit for
# as long and buy nothing, because the service finishes the request whether or
# not this process is still listening (one extraction landed 32s after the
# drain gave up) and nothing needs to recall the turn that just ended. What a
# ceiling this short costs is certainty, and that is paid where the outcome is
# reported: a request still running here is reported as unsettled -- neither
# lost nor written, because from here it is not knowable which.
_STORE_DRAIN_BUDGET_S: float = 2.0
# Runtime prose, not the model's: written here and shown to the model so it
# stops, and carried to the client as a notice rather than as an answer. It
# reads as the assistant speaking, which is exactly why it must never be
# rendered in the assistant's voice -- see the ``_notice`` key below.
_ABORTED_ACTION_REPLY = (
    "The operation was not completed, and no alternative method will be attempted. "
    "Would you like me to continue with the remaining parts of the task that do not "
    "require this operation?"
)

# Marks a stored assistant message the runtime wrote. ``_save_turn`` renames it
# to ``notice`` for storage, the same way ``_diff`` becomes ``diff``: the
# underscore keeps it out of the provider payload while the turn is live.
_NOTICE_KEY = "_notice"

# Marks the user entry of a turn the runtime opened on the agent's behalf, with
# the origin that opened it. Same underscore-then-rename convention as
# ``_notice``: ``_save_turn`` writes it as ``origin``, and the underscore keeps
# it out of the live provider payload.
#
# The entry's *text* is runtime prose in these turns -- a sub-agent's announce
# carries an untrusted fence, an instance handle and an instruction not to repeat
# either of them to the user; a cron reminder carries "when you reply, mention
# when the reminder was originally set". A reader with no way to tell it apart
# from a person typing draws all of it as the user's own words on the next
# reload, which is how those internals reached a screen.
_ORIGIN_KEY = "_origin"


def _runtime_origin(origin: "Origin | None") -> str | None:
    """The mark for a turn the runtime opened, or ``None`` for a person's.

    Every origin but ``USER`` earns one, not just ``SUBAGENT``: a cron
    reminder's text carries "when you reply, mention when the reminder was
    originally set", which is as much an instruction to the model as an
    announce's untrusted fence is, and a reader that draws it as typed words
    puts it on screen the same way.
    """
    return None if origin is None or origin is Origin.USER else str(origin)


# How long a turn's parts took, on the entry each one belongs to: the thinking
# span on the assistant message that carries the thought, the execution span on
# the tool result. Same underscore-then-rename convention as ``_diff`` -- these
# are for a reader, and a message the provider sees must not grow a field
# mid-turn. ``_save_turn`` writes them as ``reasoning_ms`` / ``duration_ms``.
_REASONING_MS_KEY = "_reasoning_ms"
_TOOL_DURATION_MS_KEY = "_duration_ms"
_TOOL_METADATA_KEY = "_metadata"


def _stamp_reasoning_ms(messages: list[dict[str, Any]], response: Any) -> None:
    """Put a model call's thinking span on the assistant entry it produced.

    Absent when the call was not streamed: a single-shot ``chat()`` has one
    arrival time for the whole response and cannot separate thought from answer.
    """
    reasoning_ms = getattr(response, "reasoning_ms", None)
    if reasoning_ms is not None and messages:
        messages[-1][_REASONING_MS_KEY] = int(reasoning_ms)


def _first_line(text: str) -> str:
    """The one line of a tool error worth putting in front of a person."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# NOTE: ``raven.context_engine`` is intentionally imported lazily (inside
# ``__init__`` and ``_assemble_context_messages``) to break a runtime
# import cycle: ``raven.agent.__init__`` eagerly loads AgentLoop,
# while ``raven.context_engine.curator`` imports ``ContextBuilder`` from
# ``raven.agent.context`` — a module-level top-down ``from
# raven.context_engine import ...`` here re-enters a partially-initialized
# package and raises ImportError on ``TurnContext``.

if TYPE_CHECKING:
    from raven.agent.hook import CompositeHook
    from raven.agent.loop.checkpoint import CheckpointService
    from raven.agent.tools.deliverables import DeliverableStore
    from raven.agent.workdir import WorkdirResolver
    from raven.config.raven import (
        ContextConfig,
        MemoryConfig,
        RuntimeConfig,
        SkillForgeRouterConfig,
        SubagentDagConfig,
        SubagentQuestionsConfig,
    )
    from raven.config.schema import (
        AskUserToolConfig,
        ChannelsConfig,
        ExecToolConfig,
        PlaybookConfig,
    )
    from raven.context_engine import ContextEngine
    from raven.contracts.memory import MemoryBackend
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.contracts.tool import Tool
    from raven.mcp.manager import MCPConnectionManager
    from raven.mcp.report import ApplyReport
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.providers.pool import ProviderPool
    from raven.routing.router import ModelRouter
    from raven.sandbox.debug_server import SandboxDebugServer
    from raven.skill_hub import SkillHubClient
    from raven.spine.events import NoticeKind
    from raven.spine.runner import Drain, Emit
    from raven.spine.turn import TurnRequest
    from raven.token_wise.registry import StrategyRegistry


@dataclass
class LoopOutcome:
    """Result of one ``_run_agent_loop`` turn beyond its text reply.

    ``status`` distinguishes a normal completion from a max-iteration
    interruption or an LLM error — so the caller never mistakes "ran out of
    budget" for "done". ``checkpoint_id`` and
    ``edited_files`` carry the shadow-git snapshot info used to build the
    next turn's recovery prompt.

    ``error`` is the loop's own account of why this turn has no answer, in the
    words a reader is shown: a model call it gave up on, or an empty-response
    recovery that spent every budget without a word coming back. None when the
    turn produced an answer or a hook salvaged one, and also when the turn
    returned no text at all with the recovery switched off, which the caller
    fails on by itself. The caller fails the turn on it unless one of the
    turn's tools has already put an answer in front of the reader.
    """

    status: str = "completed"  # "completed" | "interrupted" | "error"
    error: str | None = None
    checkpoint_id: str | None = None
    edited_files: list[str] = field(default_factory=list)


def _filter_qualified_ids(
    ids: list[str] | None,
    source_prefix: str,
) -> list[str]:
    """Extract native ids from a list of qualified ids
    matching ``<source_prefix>/<native>``.

    Returns the bare native portion for each match (i.e. strips the
    ``"<source>/"`` prefix) so the receiving backend doesn't have to
    re-parse. Non-matching / unprefixed / malformed entries silently
    drop. ``None`` and empty inputs return ``[]``.
    """
    if not ids:
        return []
    needle = f"{source_prefix}/"
    out: list[str] = []
    for qid in ids:
        if not isinstance(qid, str):
            continue
        if qid.startswith(needle):
            native = qid[len(needle) :]
            if native:
                out.append(native)
    return out


# Asks the model for a best-effort wrap-up after the iteration budget is spent.
# Tools are withheld on this call, so the prompt must not invite another tool
# use or a question — there is no further turn to answer it.
_MAX_ITER_SYNTHESIS_PROMPT = (
    "You've used up the tool-calling budget for this turn, so no tools are "
    "available now. Using only what you've already gathered, give your best "
    "final answer: summarize what you accomplished, deliver any partial "
    "results, and briefly note what's left undone. Do not ask questions — "
    "there is no further turn to answer them. Reply in the same language as "
    "the user's request (this instruction is in English, but it is not the "
    "conversation language)."
)

# Returned only if the synthesis call itself fails — never leave the turn silent.
_MAX_ITER_STATIC_FALLBACK = (
    "I reached the maximum number of tool call iterations ({n}) without "
    "completing the task. You can try breaking the task into smaller steps."
)

_WALL_CLOCK_STATIC_FALLBACK = "I reached the time limit for this turn before completing the task."

# The same wrap-up, for a turn stopped because one call was repeating to no
# effect. Its own wording because the max-iter prompt opens by telling the model
# it used up its budget, which here would be false: the budget is not what ran
# out, and a model told the wrong reason writes the wrong summary.
_STALLED_SYNTHESIS_PROMPT = (
    "One tool call was repeating with an identical result and has been stopped, "
    "so no tools are available now. Using only what you've already gathered, "
    "give your best final answer: summarize what you accomplished, deliver any "
    "partial results, and briefly note what's left undone. Do not ask questions "
    "— there is no further turn to answer them. Reply in the same language as "
    "the user's request (this instruction is in English, but it is not the "
    "conversation language)."
)

_STALLED_STATIC_FALLBACK = (
    "I stopped because `{tool}` kept returning the same result and the task was "
    "not moving forward. Tell me what to change and I'll retry."
)

# Origins whose turns skip the user-inbound hooks (engagement / decision): a turn
# from one of these is not genuine user input. cron/heartbeat are deliberately
# NOT here: they use real channels and fire the hooks today (run_turn keeps that;
# whether they should is a separate question, not this change). Named for what it
# does, not "proactive" — cron and heartbeat are proactive yet absent, and
# subagent is reactive yet present.
_SKIP_USER_INBOUND_ORIGINS = frozenset({Origin.SENTINEL, Origin.SUBAGENT})

# Origins whose reply skips the ``after_send`` chain (Sentinel NudgeInjector /
# response_modifier): their output is system-originated and must not get a nudge
# layered on. A separate set from _SKIP_USER_INBOUND_ORIGINS on purpose, even
# though the members coincide today — the two gates have different meanings, so
# a future change to one set must not silently move the other (e.g. adding
# cron/heartbeat to the user-inbound set for engagement reasons must not start
# dropping their after_send). SENTINEL = the supersede notice (a system notice);
# SUBAGENT = the result re-injection (skipped so the announce gets no nudge).
_SKIP_AFTER_SEND_ORIGINS = frozenset({Origin.SENTINEL, Origin.SUBAGENT})


def _appended_by_hook(before: str | None, after: str) -> str:
    """What an ``after_send`` hook added to the end of the reply, or ``""``.

    Only an appended tail can be sent after the fact: the reply's own text has
    already gone out as deltas, so a hook that rewrote it wholesale leaves nothing
    a streaming client can be given without repeating what it has.
    """
    head = before or ""
    if not after.startswith(head):
        return ""
    return after[len(head) :]


#: A message a hook injected on rollback: the harness re-prompting itself (a
#: reviewer rejection, a commit nudge). It persists into history because the
#: model was shown it, but no real user said it; the underscore key is dropped
#: on the way to the provider, so what it changes is only how readers of the
#: transcript classify the line.
_HOOK_INJECTED_KEY = "_hook_injected"
#: A user message the runtime merged into a turn that was already running
#: (``BusyPolicy.INJECT``). It is a real user message and persists as one; the
#: mark says only that it arrived mid-turn, and the underscore key is dropped on
#: the way to the provider like the others here. A turn that may be re-run is
#: what needs the mark: the rerun starts again from the question and throws the
#: failed attempt's work away, and without this it cannot tell a correction the
#: reader typed -- which is the question now, and which the queue has already
#: given up -- from the research it is entitled to discard.
_MID_TURN_USER_KEY = "_mid_turn_user"

#: Introduces the mid-turn arrivals on the way to the provider. Without it the
#: model reads a correction the reader typed while it worked as a fresh question
#: and answers it instead of steering, because nothing in the payload says the
#: two arrived out of band.
_MID_TURN_HEADER = "[Mid-turn messages — sent by the user while this turn was already running]"


def merge_mid_turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One user message per adjacent run of mid-turn arrivals, under the header.

    Applied at the call seam, on the payload only: history keeps one entry per
    message, so a reader and a re-run still see what was sent when. The mark
    rides the merged message because the re-run seed reads it to find the
    mid-turn messages again.

    Only the private spelling counts. A replayed transcript carries the plain
    ``mid_turn`` of an entry already saved, and labelling that again would put
    the header on a question the model answered turns ago.
    """
    if not any(m.get(_MID_TURN_USER_KEY) for m in messages):
        return messages
    out: list[dict[str, Any]] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        body = "\n\n".join(run)
        out.append({"role": "user", "content": f"{_MID_TURN_HEADER}\n\n{body}", _MID_TURN_USER_KEY: True})
        run.clear()

    for m in messages:
        if m.get(_MID_TURN_USER_KEY):
            # The merged message keeps the mark, so a second pass over the same
            # list must not stack a second header onto its own output.
            run.append(str(m.get("content") or "").removeprefix(f"{_MID_TURN_HEADER}\n\n"))
            continue
        flush()
        out.append(m)
    flush()
    return out


@dataclass(frozen=True)
class SessionPolicy:
    """What one session's turns run under beyond the loop-wide defaults.

    ``max_iterations`` caps the ReAct loop for this session (``None`` inherits
    the loop's); ``reasoning_effort`` is the effort every call this session
    makes asks for (``None`` leaves the provider's configured one); ``mode``
    and ``mode_overlay`` are the session's operating profile as the transport
    named it -- the loop does not interpret the overlay, it hands it to the
    hook chain as ``ctx.metadata`` so a product's own hooks read their own
    knobs.
    """

    max_iterations: int | None = None
    mode: str = ""
    mode_overlay: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None


def append_hook_note(messages: list[dict[str, Any]] | None, note: str) -> bool:
    """Land a hook's ``append_note`` on the last transcript message.

    A blank line separates it from the body, and a block-shaped body gains a
    text block, so the note reads as part of what the model was already about
    to read. False when there is nothing to land on or the body's shape is one
    the loop does not know -- the note is dropped rather than guessed into
    place, and the caller logs that.
    """
    if not messages or not note:
        return False
    last = messages[-1]
    body = last.get("content")
    if isinstance(body, str):
        last["content"] = f"{body}\n\n{note}" if body else note
        return True
    if isinstance(body, list):
        last["content"] = body + [{"type": "text", "text": note}]
        return True
    if body is None:
        last["content"] = note
        return True
    return False


def turn_question(messages: list[dict[str, Any]] | None) -> str:
    """This turn's question: the text of the last user message at loop entry.

    Read once, at entry -- later the history has grown and the last user
    message may be an injection rather than what the user asked.
    """
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        text = text.strip()
        # The context builder prepends a runtime-context block (time, channel)
        # to the inbound message; the question is what follows the blank line.
        if text.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
            text = text.partition("\n\n")[2].strip()
        if text:
            return text
    return ""


# Marks the stored user entry of a turn that is a delegated result coming back,
# not something a person sent. Renamed to ``delegated`` at the save gate for the
# same reason ``_notice`` is: the private spelling keeps it out of the provider
# payload, the plain one is what session.resume puts on the wire. Without it a
# reload has no way to tell this entry from a question and draws it as one --
# prompt-injection fence and all -- while a client watching live draws the
# delivery row. Unlike ``_attached_image`` this entry IS persisted: the model
# reads the result on its next turn. Only the reader must not read it as prose.
_DELEGATED_KEY = "_delegated"


def _strip_inline_images(content: list[Any]) -> list[Any]:
    """Replace inline base64 images with a text placeholder, for persistence.

    Images live for exactly the turn that produced them. Keeping the bytes would
    bloat the session JSONL by megabytes per picture, and every later turn would
    replay them to the model — paying for an image nobody asked about again.

    A *new* list is returned: the input is the live message the model is still
    working from this turn, and `_save_turn` only shallow-copies the entry, so
    mutating in place would pull the picture out from under the current request.
    """
    out: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            out.append(part)
            continue
        if is_inline_image(part):
            out.append({"type": "text", "text": "[image]"})
        else:
            out.append(part)
    return out


def _display_label(tool: Any, arguments: dict[str, Any]) -> str | None:
    """A tool's own label for its transcript row, or None if it cannot give one.

    ``display_call`` is handed the model's raw arguments: the registry's cast
    and validation run later, on the execute path, so this sees whatever the
    model emitted, including shapes the schema forbids. Its entire job is to
    label a row, so a failure here has to cost the label and nothing else.

    Without the guard the exception leaves the tool-event emit and ends the
    turn, and the user gets no reply at all -- an array argument arriving as a
    JSON string did exactly that, raising ``AttributeError`` on a character of
    it. Guarding one tool leaves the trap set for the next one written; the
    call site is where it closes for all of them.
    """
    if tool is None:
        return None
    try:
        return tool.display_call(arguments)
    except Exception as exc:  # noqa: BLE001
        logger.warning("display_call failed for {}: {}", getattr(tool, "name", "?"), exc)
        return None


# A whole file both ways is the largest thing a tool event carries, and it is
# carried so a client can draw the change itself. Past this the pair is dropped
# rather than truncated, for the reason ``_unified`` drops an oversized diff: half
# a file reads as a smaller change than the one that happened.
_FILE_CHANGE_MAX_CHARS = 512 * 1024


def _file_change_payload(change: Any) -> dict[str, Any] | None:
    """One write as a plain mapping, or ``None`` when there is nothing to send.

    Flattened here rather than passed as the dataclass: ``spine.events`` is
    deliberately free of the tools package, and a mapping is also what goes on
    the wire two hops later.

    ``before`` is preserved as ``None`` when the file did not exist, because a
    client renders a creation differently from a rewrite -- so this cannot use a
    "falsy means absent" shortcut, an empty file having the same emptiness.
    """
    if change is None:
        return None
    after = getattr(change, "after", None)
    path = getattr(change, "path", None)
    if not isinstance(after, str) or not isinstance(path, str) or not path:
        return None
    before = getattr(change, "before", None)
    if len(after) + len(before or "") > _FILE_CHANGE_MAX_CHARS:
        return None
    payload: dict[str, Any] = {"path": path, "after": after}
    if before is not None:
        payload["before"] = before
    return payload


def _file_removed_payload(removals: Any) -> list[dict[str, Any]] | None:
    """The files a call made vanish, as plain mappings, or ``None`` for none.

    Flattened here for the reason ``_file_change_payload`` is, and ``None`` rather
    than an empty list so the emit site can leave the key off a payload entirely:
    a call that removed nothing is every call, and the wire shape it already had
    must not change under it.

    ``before`` is dropped past the budget instead of truncated -- half a removed
    file reads as a smaller deletion than the one that happened -- and the removal
    is still reported without it. The budget is the event's and not each file's:
    one command can unlink as many files as it names, and a per-file ceiling would
    let a single payload carry all of them at full size.
    """
    out: list[dict[str, Any]] = []
    budget = _FILE_CHANGE_MAX_CHARS
    for removal in removals or ():
        path = getattr(removal, "path", None)
        if not isinstance(path, str) or not path:
            continue
        entry: dict[str, Any] = {"path": path}
        before = getattr(removal, "before", None)
        if isinstance(before, str) and len(before) <= budget:
            entry["before"] = before
            budget -= len(before)
        out.append(entry)
    return out or None


#: A file the listing found is counted in lines only when it is text this size
#: or under. Past it the count is unknown rather than wrong: reading a gigabyte
#: to number it would cost the turn more than the row it draws is worth.
_FILE_WRITTEN_TEXT_MAX_BYTES = 256 * 1024


def _file_written_payload(
    created: Collection[str],
    modified: Collection[str],
    after: dict[str, tuple[int, int]] | None,
    *,
    already: Collection[str] = (),
) -> list[dict[str, Any]] | None:
    """The files a command left behind, as plain mappings, or ``None`` for none.

    The other half of ``_file_change_payload``: a file tool reports what it
    wrote, a command reports its output and nothing else, so this is read off
    two listings of the working directory instead of off a result. Sizes and a
    line count rather than contents -- one command can write a hundred files,
    and what a row draws is that they were written and how big they are.

    ``lines`` belongs to a created file alone, and ``None`` there means unknown:
    too large to read, or not text. A rewritten file has no count at all, since
    the listing never held the old content and a number against nothing would
    read as a change nobody measured.

    ``already`` are the paths this same call accounted for by name. The listing
    sees those too, and reporting one again would draw a single write twice.
    """
    accounted = {os.path.realpath(path) for path in already if isinstance(path, str) and path}
    out: list[dict[str, Any]] = []
    for path in created:
        if os.path.realpath(path) in accounted:
            continue
        size = (after or {}).get(path, (0, 0))[0]
        out.append({"path": path, "created": True, "size": size, "lines": _text_line_count(path, size)})
    for path in modified:
        if os.path.realpath(path) in accounted:
            continue
        out.append({"path": path, "created": False, "size": (after or {}).get(path, (0, 0))[0], "lines": None})
    return out or None


def _text_line_count(path: str, size: int) -> int | None:
    """Lines in a file the listing found, or ``None`` when it cannot be counted."""
    if size > _FILE_WRITTEN_TEXT_MAX_BYTES:
        return None
    try:
        return len(Path(path).read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return None


def _listing_removals(deleted: Collection[str], *, already: Collection[str] = ()) -> list[FileRemoval]:
    """Files a listing says went, for the deletions no tool reported itself.

    Without a body: the file was gone before anything read it, and the turn only
    knows it was there when the command started. ``already`` are the removals
    the call reported by name, which the listing sees as well.
    """
    accounted = {os.path.realpath(path) for path in already if isinstance(path, str) and path}
    return [FileRemoval(path=path) for path in deleted if os.path.realpath(path) not in accounted]


def monotonic() -> float:
    """The turn's elapsed-time clock, as one name the loop calls.

    A seam, and the reason is that the alternative is worse. The budgets this feeds
    are checked against elapsed time, and a test that wants to see a deadline fire
    cannot wait for one; replacing ``time.monotonic`` itself would replace the clock
    asyncio schedules on, so the substitute has to be this narrow. The datetime
    equivalent already goes through ``_now_fn`` for the same reason.
    """
    return time.monotonic()


_TOOL_PREVIEW_MAX_CHARS = 4_000
"""How much of a tool's output rides the ``tool.complete`` event to a client.

Nothing is lost from the model's side by this number -- it always receives the
whole result, and this is only what a reader is shown. It bounds one event, and
one event lands per tool call in a live turn and again on a session replay, so
it is a page-weight budget rather than a correctness one. Four thousand covers
an error with its traceback, a directory listing, and a short file, which is
most of what a reader opens a card to read."""


TURN_BUDGETS_KEY = "turn_budgets"
"""Where a product leaves the bounds it wants a turn run under, on the turn's metadata.

The loop serves every agent and must not know any of them, so bounds arrive as data a
hook writes rather than as config the loop reads: a hook that writes nothing leaves the
turn bounded exactly as it was before this key existed, which is what keeps every other
agent byte-identical. The value is a plain dict, and :func:`turn_budgets` is the only
reader -- a malformed one leaves the turn unbounded rather than raising inside the loop.
"""


TURN_ASK_KIND_KEY = "turn_ask_kind"
"""Where a product leaves the labeller for its own harness-injected asks.

The same seam as ``TURN_BUDGETS_KEY`` and for the same reason, carrying a callable
rather than a dict because what it holds is knowledge of wording: only the product that
writes an ask can name it. The loop pairs the name with the structural marker on the
injected message, so the boolean "the harness asked and the model never answered" holds
whether or not this key is set, and only the sub-label depends on it.

Unlike ``observers`` this entry stays in the process -- it is never filed onto a message
or sent to a client -- so a callable here crosses no serialization boundary.
"""


TURN_SYNTHESIS_KEY = "turn_synthesis"


@dataclass(frozen=True)
class TurnSynthesisPolicy:
    """Product guidance and one bounded repair for an interrupted turn's reply."""

    guidance: str
    repair_prompt: Callable[[str], str | None] | None = None
    format_fallback: Callable[[str], str] | None = None


def turn_synthesis(metadata: dict[str, Any] | None) -> TurnSynthesisPolicy | None:
    value = (metadata or {}).get(TURN_SYNTHESIS_KEY)
    return value if isinstance(value, TurnSynthesisPolicy) else None


@dataclass(frozen=True)
class TurnBudgets:
    """The bounds one turn runs under, beyond the iteration cap.

    ``wall_clock_seconds`` is checked between iterations, never mid-generation:
    cancelling a call in flight throws away a finished generation and leaves no
    answer, and a deadline landing between a tool result and the model reading it
    produces a trajectory nothing can interpret. The cost is an overrun of at most
    one iteration, and that is the intended trade rather than an oversight.

    ``dead_end_retries`` is how many times a turn that produced no answer may be
    run again from the original question. Re-run, never salvaged: squeezing an
    answer out of a failed attempt's leftovers was measured to convert a
    detectable zero into a confident wrong answer, and it empties the very
    trigger this budget reads.

    ``dead_end_reasons`` narrows which dead ends are worth re-running, matched as
    prefixes of what ``dead_reasons`` returns. Empty means all of them.
    """

    wall_clock_seconds: float | None = None
    dead_end_retries: int = 0
    dead_end_reasons: tuple[str, ...] = ()


def turn_budgets(metadata: dict[str, Any] | None) -> TurnBudgets:
    """Read the turn's budgets off its hook metadata; defaults mean unbounded.

    Tolerant by construction. This reads a dict a plugin wrote, so a malformed
    value must leave the turn bounded the way it was rather than raise inside the
    loop: an instrument that can end the turn it measures is worse than no
    instrument, and a budget is not even an instrument.
    """
    raw = (metadata or {}).get(TURN_BUDGETS_KEY)
    if not isinstance(raw, dict):
        return TurnBudgets()
    wall = raw.get("wall_clock_seconds")
    retries = raw.get("dead_end_retries")
    reasons = raw.get("dead_end_reasons")
    # ``bool`` is an ``int``, so a switch left in a number's place would otherwise
    # read as one retry or a one-second deadline -- a misconfiguration that ends
    # turns rather than one that is ignored.
    numeric = (int, float)
    return TurnBudgets(
        wall_clock_seconds=(
            float(wall) if isinstance(wall, numeric) and not isinstance(wall, bool) and wall > 0 else None
        ),
        dead_end_retries=(
            int(retries) if isinstance(retries, int) and not isinstance(retries, bool) and retries > 0 else 0
        ),
        dead_end_reasons=tuple(str(r) for r in reasons) if isinstance(reasons, (list, tuple)) else (),
    )


def turn_ask_kind(metadata: dict[str, Any] | None) -> Callable[[object], "str | None"] | None:
    """Read the product's ask labeller off the turn's hook metadata, or ``None``.

    Tolerant for the reason :func:`turn_budgets` is: a non-callable left under the key
    must leave the turn labelled the way it was rather than raise inside the loop, since
    what this names is a measurement and an instrument may not end the turn it measures.
    """
    value = (metadata or {}).get(TURN_ASK_KIND_KEY)
    return value if callable(value) else None
