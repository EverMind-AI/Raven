"""Module-level names of the agent loop, shared by main and its mixins.

``main`` and the mixins import what they use from here.
"""
# ruff: noqa: F401 -- every name here is imported by main or by a mixin;
# unused-here is the point.

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

from loguru import logger

from raven.agent import workdir
from raven.agent.acp_client import resolver as autofill_resolver
from raven.agent.acp_client.asker import current_autofill
from raven.agent.context import ContextBuilder
from raven.agent.loop.failure_streak import (
    failure_class,
    is_hard_tool_failure,
    loop_break_nudge,
)
from raven.agent.loop.recovery import (
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
from raven.agent.tools.deep_research import (
    DeepResearchManager,
    DeepResearchOfferTool,
    DeepResearchTool,
    deep_research_mode,
)
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
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.contracts.assembled import TokenBudget
from raven.contracts.llm_provider import LLMProvider, LLMResponse
from raven.contracts.tool import SKIPPED_AFTER_BLOCKED_CALL, Continuation, ToolOutput
from raven.memory_engine import MemoryConsolidator, MemoryStore, StorePipeline
from raven.observability import semconv
from raven.providers.base import send_max_tokens
from raven.providers.binding import ModelBinding, active_binding, use_binding
from raven.providers.capabilities import image_placeholder_text, supports_image_tool_result, vision_verdict
from raven.providers.rates import resolve_context_window
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
        DeepResearchToolConfig,
        ExecToolConfig,
        PlaybookConfig,
    )
    from raven.context_engine import ContextEngine
    from raven.contracts.asking import QuestionResponder
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
    budget" for "done" (Bug2 / decision B). ``checkpoint_id`` and
    ``edited_files`` carry the shadow-git snapshot info used to build the
    next turn's recovery prompt.
    """

    status: str = "completed"  # "completed" | "interrupted" | "error"
    checkpoint_id: str | None = None
    edited_files: list[str] = field(default_factory=list)


def _filter_qualified_ids(
    ids: list[str] | None,
    source_prefix: str,
) -> list[str]:
    """FB-1 helper: extract native ids from a list of qualified ids
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

# Marks the synthetic user message that carries images a transport cannot put in
# a tool result. Not persisted: the tool result above it already names the file
# path, so the only thing this message would add to the transcript is a user turn
# saying "[image]" that the user never sent -- misleading on resume and in
# session export. Deliberately a different key from ``_recovery_synthetic``:
# that one marks empty-response recovery scaffolding, and collapsing the two
# would make either meaning impossible to reason about separately.
_ATTACHED_IMAGE_KEY = "_attached_image"
#: A message a hook injected on rollback: the harness re-prompting itself (a
#: reviewer rejection, a commit nudge). It persists into history because the
#: model was shown it, but no real user said it; the underscore key is dropped
#: on the way to the provider, so what it changes is only how readers of the
#: transcript classify the line.
_HOOK_INJECTED_KEY = "_hook_injected"


@dataclass(frozen=True)
class SessionPolicy:
    """What one session's turns run under beyond the loop-wide defaults.

    ``max_iterations`` caps the ReAct loop for this session (``None`` inherits
    the loop's); ``mode`` and ``mode_overlay`` are the session's operating
    profile as the transport named it -- the loop does not interpret the
    overlay, it hands it to the hook chain as ``ctx.metadata`` so a product's
    own hooks read their own knobs.
    """

    max_iterations: int | None = None
    mode: str = ""
    mode_overlay: dict[str, Any] = field(default_factory=dict)


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


_TOOL_PREVIEW_MAX_CHARS = 4_000
"""How much of a tool's output rides the ``tool.complete`` event to a client.

Nothing is lost from the model's side by this number -- it always receives the
whole result, and this is only what a reader is shown. It bounds one event, and
one event lands per tool call in a live turn and again on a session replay, so
it is a page-weight budget rather than a correctness one. Four thousand covers
an error with its traceback, a directory listing, and a short file, which is
most of what a reader opens a card to read."""
