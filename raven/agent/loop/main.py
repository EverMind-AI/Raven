"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from raven.agent import workdir
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
from raven.agent.loop.streaming import stream_llm_call
from raven.agent.subagent import SubagentManager
from raven.agent.subagent.direct_chat import DirectChatHandoff
from raven.agent.tools.ask_user import AskUserTool
from raven.agent.tools.base import SKIPPED_AFTER_BLOCKED_CALL, Continuation
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
from raven.agent.tools.spawn import SpawnTool
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.memory_engine.base import TokenBudget
from raven.memory_engine.consolidate.consolidator import MemoryConsolidator, MemoryStore
from raven.providers.base import (
    LLMProvider,
    LLMResponse,
    send_max_tokens,
)
from raven.providers.binding import ModelBinding, active_binding, use_binding
from raven.providers.capabilities import image_placeholder_text, supports_image_tool_result, vision_verdict
from raven.providers.rates import resolve_context_window
from raven.sandbox import SandboxConfig, SandboxExecutor, SandboxInitError, build_executor
from raven.session.manager import Session, SessionManager
from raven.spine.turn import Origin, session_of
from raven.tracing import semconv, trace
from raven.utils.helpers import estimate_prompt_tokens, is_image_part, is_inline_image

# How long a turn is willing to wait on plugin-side indexing before letting the
# write finish on its own. A budget, not a deadline: the task keeps running.
_STORE_TURN_BUDGET_S: float = 5.0
# Outstanding detached writes past which a turn waits for one to land, so a slow
# memory service cannot grow an unbounded queue behind a fast typist.
_STORE_MAX_INFLIGHT: int = 4
# Teardown's total budget for letting those writes finish.
_STORE_DRAIN_BUDGET_S: float = 15.0


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
    from raven.agent.tools._deliverables import DeliverableStore
    from raven.agent.tools.base import Tool
    from raven.agent.workdir import WorkdirResolver
    from raven.config.raven import (
        ContextConfig,
        MemoryConfig,
        RuntimeConfig,
        SkillForgeRouterConfig,
    )
    from raven.config.schema import (
        AskUserToolConfig,
        ChannelsConfig,
        DeepResearchToolConfig,
        ExecToolConfig,
        PlaybookConfig,
    )
    from raven.context_engine import ContextEngine
    from raven.mcp.manager import MCPConnectionManager
    from raven.mcp.report import ApplyReport
    from raven.memory_engine.backend import MemoryBackend
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.providers.pool import ProviderPool
    from raven.routing.router import ModelRouter
    from raven.rpc.question_broker import QuestionBroker
    from raven.sandbox.debug_server import SandboxDebugServer
    from raven.skill_hub import SkillHubClient
    from raven.spine.events import NoticeKind
    from raven.spine.runner import Drain, Emit, TurnOutcome
    from raven.spine.turn import TurnRequest
    from raven.token_wise.base import UsageSnapshot
    from raven.token_wise.registry import StrategyRegistry


@dataclass
class TurnOutcome:
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


_MCP_TURN_WAIT_S = 90.0
"""How long a turn waits for the first MCP sync before proceeding without it.

Mirrors the manager's own per-handshake budget (``_HANDSHAKE_TIMEOUT``), so a
cold stdio server downloading its package on first run still makes it into the
very turn a user sends after installing it. The sync no longer includes anyone
waiting on a person -- a connect that reaches the browser-authorization step is
marked ``auth_required`` and left behind by the sync itself -- and it connects
servers concurrently, so in practice this bound is the slowest single
handshake, not a sum. See ``AgentLoop._connect_mcp``.
"""


def _log_late_mcp_sync(task: "asyncio.Task") -> None:
    """Report an MCP sync that finished after its turn stopped waiting."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("MCP: the connect a turn stopped waiting for failed: {}", exc)


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


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the spine
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000
    # Max emergency context shrinks per turn before a context overflow is fatal.
    _MAX_COMPRESS_RETRIES = 2
    # Max image demotions per turn. One is enough: a refusal is deterministic for
    # the model, and the first retry also caches the verdict, so a second attempt
    # would mean the failure was never about images.
    _MAX_IMAGE_DEMOTE_RETRIES = 1
    # Most recent tool results kept intact when emergency-shrinking; older ones
    # are elided (their bodies are the bulk of mid-turn context growth).
    _SHRINK_KEEP_RECENT_TOOL_RESULTS = 3
    # Image-bearing messages kept intact when emergency-shrinking. Tighter than
    # the tool-result count because one image can cost 1568 tokens: the picture
    # the model is currently reasoning about is worth keeping, older ones are the
    # cheapest thing to give up.
    _SHRINK_KEEP_RECENT_IMAGES = 1
    # Reconnects allowed for a streamed call that failed before its first delta
    # (after one, the caller has output that a retry would duplicate).
    _MAX_STREAM_RECONNECTS = 1
    # Tool-failure-loop break: nudge after the same tool fails deterministically
    # this many times running; cap the nudges per turn so it can't itself loop.
    _LOOP_BREAK_THRESHOLD = 2
    _LOOP_BREAK_MAX = 2

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        ask_user_config: AskUserToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        sandbox_config: SandboxConfig | None = None,
        channels_config: ChannelsConfig | None = None,
        deliverables: "DeliverableStore | None" = None,
        router: "ModelRouter | None" = None,
        playbook_config: "PlaybookConfig | None" = None,
        strategies: "StrategyRegistry | None" = None,
        skill_forge_config: Any = None,
        response_modifier: Callable[[str, str], str] | None = None,
        on_user_inbound: Callable[["TurnRequest"], None] | None = None,
        decision_consumer: "Callable[[TurnRequest], Awaitable[Any]] | None" = None,
        hooks: "CompositeHook | None" = None,
        now_fn: Callable | None = None,
        context_config: "ContextConfig | None" = None,
        runtime_config: "RuntimeConfig | None" = None,
        provider_pool: "ProviderPool | None" = None,
        interactive: bool = True,
        jina_api_key: str | None = None,
        max_concurrent_subagents: int = 8,
        max_subagent_spawns_per_hour: int = 30,
        agents: list | None = None,
        media_config: Any = None,
        deep_research_config: Any = None,
        disabled_tools: list[str] | None = None,
        tool_search_config: Any = None,
        # AG-1: optional plugin-provided MemoryBackend. When supplied,
        # the after-turn pipeline gains a third peer step ``backend.store``
        # (alongside the existing ``maybe_consolidate`` and the implicit
        # ``append_history`` inside session save). ``None`` preserves
        # legacy behavior — no plugin-side memory indexing happens.
        backend: "MemoryBackend | None" = None,
        # Forwarded to ``build_context_engine`` so the factory can
        # assemble the unified engine's SkillForgeRouter (Local + Mass +
        # Everos) and EverOS recall lane. Both default to ``None``; with
        # no backend the engine degrades (recall → [], router Local-only).
        memory_config: "MemoryConfig | None" = None,
        skill_forge_router_config: "SkillForgeRouterConfig | None" = None,
        # Tools contributed by activated plugins (built by the CLI via
        # ``build_plugin_tools``). Registered alongside the built-in tools
        # in ``_register_default_tools``. ``None`` / empty = no plugin
        # tools, default behavior unchanged.
        plugin_tools: "list[Tool] | None" = None,
        empty_recovery: RecoveryLimits | None = None,
        workdir_resolver: "WorkdirResolver | None" = None,
    ):
        from raven.agent.hook import (
            CompositeHook,
            DecisionConsumerAdapter,
            OnUserInboundAdapter,
            ResponseModifierAdapter,
        )
        from raven.config.schema import AskUserToolConfig, ExecToolConfig
        from raven.token_wise.registry import StrategyRegistry

        # Optional transform applied to the final assistant content right
        # before outbound delivery. Signature: (session_key, content) -> content.
        # Used by Sentinel's NudgeInjector to piggyback on the agent's reply,
        # but designed as a generic hook (citations, warnings, etc.).
        # Skipped for SENTINEL-origin turns so Sentinel-initiated messages don't
        # trigger another layer of inject.
        self.response_modifier = response_modifier
        # Optional callback fired at the start of _process_message for
        # genuinely user-originated inbounds (not Sentinel-origin). Used by
        # Sentinel to detect engagement with a recent nudge (accept/dismiss).
        # Exception-safe — a raising callback is logged and swallowed.
        self.on_user_inbound = on_user_inbound
        # Optional async hook fired BEFORE slash-command parsing + normal
        # processing. Used by Sentinel's DecisionConsumer to short-circuit
        # the agent loop when the user replies to a discovery menu (Phase 4).
        # Returning a reply means "I handled this; don't process further".
        # Returning None means "fall through to normal flow".
        self.decision_consumer = decision_consumer
        self.channels_config = channels_config
        self._deliverables = deliverables
        self._workdir_resolver = workdir_resolver
        self.workspace = workspace
        # The model a turn runs on is per session, so it cannot live in two
        # attributes on a process-wide loop. ``_default_binding`` is what a
        # session starts on; ``provider``/``model`` below read whichever
        # binding the running turn entered.
        self._provider_pool = provider_pool
        # An explicit window rides on the binding, which is what answers for it
        # from here on -- every binding this loop makes carries it, so a session
        # switching models cannot shake off a number the user pinned. No special
        # case for 65536: the retired default the old bootstrap wrote to disk is
        # cleared where it lives by ``config.loader``, so what arrives here is a
        # real choice.
        self._configured_window = context_window_tokens or None
        self._default_binding = ModelBinding(provider, model or provider.get_default_model(), self._configured_window)
        self._session_bindings: dict[str, ModelBinding] = {}
        # Keys whose session record has been consulted for a stored model, hit
        # or miss. See ``_restore_once``.
        self._restore_attempted: set[str] = set()
        # Resolved lazily on the first tool result that carries an image. Keyed
        # by model, not a single flag: the loop is a long-lived singleton and
        # takes a per-call model (strategies rewrite it, and the model chain
        # falls back), so one model's verdict must not answer for another's.
        # Keyed by model but *computed from the provider*, so a rebuild that
        # keeps the model id would keep serving the old transport's verdict --
        # ``_forget_transport_verdicts`` is what the binding setters call.
        self._image_tool_result_ok: dict[str, bool] = {}
        self._vision_ok: dict[str, bool] = {}
        self.max_iterations = max_iterations
        # Empty-response recovery budgets. None → enabled defaults.
        self._recovery_limits = empty_recovery if empty_recovery is not None else RecoveryLimits()
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        from raven.config.schema import DeepResearchToolConfig, MediaGenConfig

        self.media_config = media_config or MediaGenConfig()
        self.deep_research_config = deep_research_config or DeepResearchToolConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.ask_user_config = ask_user_config or AskUserToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        # TokenWise strategies — empty registry acts as pure pass-through.
        self.strategies = strategies if strategies is not None else StrategyRegistry([])
        # Fake-clock injection point for benchmark/sim harnesses. Defaults
        # to wall clock so production paths (gateway, REPL) are unaffected.
        # Used both here (session entry timestamps) and threaded into
        # ContextBuilder so the LLM's "Current Time:" prompt stays in
        # sync with what we record on persisted messages.
        self._now_fn = now_fn or datetime.now

        # AG-1: optional plugin-provided MemoryBackend.
        # Bootstrap wires this from ``PluginRegistry.build_memory_backend``;
        # legacy callsites pass ``None`` and retain the existing post-turn
        # pipeline unchanged. See ``_dispatch_backend_store`` for the call
        # site that consumes it.
        self.backend: "MemoryBackend | None" = backend
        # Writes that outran their turn budget and are still running. Held so
        # teardown can drain them instead of dropping whatever was slowest.
        self._store_inflight: set[asyncio.Task] = set()
        # Writes refused because indexing never caught up. Reported at teardown:
        # a dropped write is a turn the user will not be able to recall.
        self._store_dropped = 0

        # Tools contributed by activated plugins; registered into the
        # ToolRegistry by ``_register_default_tools``.
        self.plugin_tools: "list[Tool]" = list(plugin_tools or [])

        # Phase A: per-turn stash for ``injected_skill_ids`` surfaced by
        # :class:`DefaultContextEngine.assemble`'s ``AssembledContext.metadata``.
        # Populated inside ``_assemble_context_messages`` so the after-turn
        # feedback dispatcher can read it without re-running selection.
        # ``None`` means "use the legacy ``_collect_injected_skill_ids``
        # path" — see that method for the branch.
        self._last_injected_skill_ids: list[str] | None = None
        # ``qualified_id -> registry source`` for the ids above. The id's own
        # prefix is the addressing namespace (``local`` for anything on disk),
        # so origin reporting needs this side map — see
        # ``SkillsSegmentBuilder.build``.
        self._last_injected_skill_sources: dict[str, str] = {}

        self.context = ContextBuilder(
            workspace,
            skill_forge_config=skill_forge_config,
            llm_provider=provider,
            now_fn=now_fn,
        )
        self.sessions = session_manager or SessionManager(workspace)
        # Off switches with no config file behind them: an eval harness that
        # needs a strict tool subset passes its list here directly
        # (benchmarks/appworld/agent_cli.py is the only such caller).
        #
        # Deliberately NOT where the config's own switches arrive. Those are read
        # live per request instead -- see `_withheld_tool_names`. A boot-time copy
        # of `tools.disabled_tools` unioned in here would make the switch one-way
        # forever: the page can take a name back out of the file, but nothing can
        # take it out of a set captured before the process started, so a tool that
        # was off at launch could never be turned back on.
        self._disabled_tools = set(disabled_tools or [])
        from raven.config.live import LiveConfig

        self._live_config = LiveConfig()
        # Entries already reported as naming a tool this switch does not own, so
        # the notice lands once rather than on every MCP connect.
        self._disabled_tools_reserved_warned: set[str] = set()
        self._tool_search_config = tool_search_config
        # Assigned for real only when the feature is on, but read on every
        # backgrounded DAG submission -- including default deploys, where an
        # unset attribute would raise instead of answering "no route".
        self.tool_search_controller = None
        self.tools = ToolRegistry()
        # Asked once per assembled tool array, so an off switch flipped now is
        # honoured by the next request rather than the next restart.
        self.tools.set_withheld_source(self._withheld_tool_names)

        # Context engine — the single ContextAssembler.
        # Constructed here (after self.tools) so the factory can capture
        # ``self.tools.get_definitions`` as a deferred callable; the actual
        # tool registry contents are filled by ``_register_default_tools``
        # later in this constructor.
        #
        # Deferred ``raven.context_engine`` import: see module-level note about
        # the import cycle with ``raven.agent.__init__``.
        if context_config is None:
            from raven.config.raven import ContextConfig

            context_config = ContextConfig()
        from raven.context_engine import build_context_engine

        self.context_config = context_config

        # Skill Hub client — built once and shared by the HubSkillSource
        # (catalog discovery) and the read_skill / use_skill tools (body /
        # bundle), so both lanes use one connection pool + identical config.
        # ``cache_dir`` points into the workspace skill tree so a use_skill'd
        # Hub skill is registry-discoverable on later turns. ``None`` when no
        # Hub endpoint is configured — read_skill is then not registered and
        # use_skill serves local/everos only.
        self._skill_hub_client = self._build_skill_hub_client(
            workspace,
            skill_forge_router_config,
        )
        # Install-policy knobs for the use_skill tool (registered later in
        # ``_register_builtin_tools``, which no longer sees these configs).
        self._skill_min_safety = float(
            getattr(getattr(skill_forge_router_config, "hub", None), "min_safety", 0.7),
        )
        self._skill_blocklist = list(getattr(skill_forge_config, "blocklist", None) or [])
        self._skill_auto_install = str(getattr(skill_forge_config, "auto_install", "auto") or "auto")

        self.context_engine: "ContextEngine" = build_context_engine(
            workspace=workspace,
            config=context_config,
            builder=self.context,
            provider=provider,
            model=self._default_binding.model,
            # The resolved window, not the constructor argument: unset (the
            # common case) it is None there and the real size comes from the
            # ladder in ``providers.rates``.
            context_window_tokens=self.context_window_tokens,
            get_tool_definitions=self.tools.get_definitions,
            # Read through a lambda, not bound here: ``self.subagents`` is built
            # further down this constructor, and the agent table it exposes is
            # rebuilt on a hot config apply, so anything captured now would be
            # either missing or stale by the time a turn asks for it.
            list_subagents=lambda: self.subagents.list_agents(),
            get_tool_notices=self._mcp_tool_notices,
            now_fn=now_fn,
            # The factory uses these to assemble the unified engine's
            # SkillForgeRouter + EverOS recall lane.
            backend=backend,
            memory_config=memory_config,
            skill_forge_router_config=skill_forge_router_config,
            skill_forge_config=skill_forge_config,
            skill_hub_client=self._skill_hub_client,
            provider_pool=provider_pool,
        )

        # Runtime discipline (5th pillar). Bug2 uses ``runtime.checkpoint``;
        # gated by (policy, interactive) — see ``_checkpoint_active``. When
        # the gate is closed the loop is byte-identical to baseline.
        if runtime_config is None:
            from raven.config.raven import RuntimeConfig

            runtime_config = RuntimeConfig()
        self.runtime_config = runtime_config
        self.interactive = interactive
        self._checkpoint_enabled = self._checkpoint_active(runtime_config.checkpoint.policy, interactive)
        # One entry per working directory this process has served, never
        # evicted: a service is cheap, but each one materialises a shadow repo
        # under that directory, so a long-lived gateway ends up holding one
        # repo per session rather than the single agent-home repo it used to.
        # Reclaim is by deleting the session directory; nothing here prunes.
        self._checkpoints: dict[Path, "CheckpointService | None"] = {}
        # session_key -> {"checkpoint_id", "files"} stashed when a turn is
        # interrupted (max-iter); consumed by the next turn's recovery prompt.
        self._pending_recovery: dict[str, dict] = {}

        self._sandbox_config = sandbox_config
        self._owned_ids: set[str] = set()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            model=self._default_binding.model,
            brave_api_key=brave_api_key,
            jina_api_key=jina_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            sandbox_config=sandbox_config,
            owned_ids=self._owned_ids,
            max_concurrent=max_concurrent_subagents,
            max_spawns_per_hour=max_subagent_spawns_per_hour,
            agents=agents,
            session_dir=self.sessions.session_dir,
        )
        self._direct_handoff = DirectChatHandoff()
        # Kept for hot-applying web config changes (P4) and for the operations
        # surfaces that report what config declared, as distinct from what the
        # agent table resolved (the table also holds the package built-in rows).
        self._agent_configs = agents or []
        self._dag_progress_sink = None
        # Late-bound sink that pushes per-turn SkillForge-injected skill ids to
        # the web UI's skill panel (host wires it to the web channel's emitter,
        # parallel to _dag_progress_sink). None in non-web contexts (CLI/IM).
        self._skills_sink = None

        # Executor: synchronous construction only; VM starts in _start_executor()
        # A sandboxed VM mounts one host root at /workspace, not one per
        # session, so it must be wide enough to cover every working directory
        # the resolver can hand out. Agent home is usually somewhere else
        # entirely -- under PER_CHANNEL the root is ``<agent home>/../tmp``, a
        # sibling, and an explicit ``-w`` cannot be an ancestor of agent home
        # because ``validate_override`` refuses one -- so it is mounted
        # separately at /agent-home to keep memory/skills/session state
        # reachable from inside the VM.
        #
        # The check is not dead: under LAUNCH_DIR the root is wherever the user
        # started raven, and launching from ``~`` puts agent home inside it. A
        # second mount of a directory already covered is what this avoids.
        mount_root = self._workdir_resolver.mount_root() if self._workdir_resolver else workspace
        home_volume = () if workdir.is_within(workspace, mount_root) else ((str(workspace), "/agent-home", "rw"),)
        self._executor: SandboxExecutor = build_executor(sandbox_config, mount_root, self._owned_ids, home_volume)
        self._executor_stack: AsyncExitStack | None = None
        self._executor_started: bool = False
        self._executor_start_lock = asyncio.Lock()
        self._debug_server: SandboxDebugServer | None = None

        self.router = router
        self._playbooks = None
        self._playbook_config = playbook_config
        self.enable_personalization = False  # Set via configure_personalization()
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_manager: MCPConnectionManager | None = None
        self._mcp_event_sink = None
        self._mcp_connected = False
        self._mcp_connecting = False
        # Consecutive backend.store failures; see _note_memory_failure.
        self._memory_fail_streak = 0
        self._processing_lock = asyncio.Lock()
        # Fired after every dispatched turn (success, error, or cancel).
        # Used by the proactive-engine WakeScheduler to re-fire wakes that
        # were parked while the agent was busy. Callbacks must be cheap and
        # must not raise.
        self.on_turn_complete: list[Callable[[], None]] = []
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self._default_binding.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            now_fn=now_fn,
        )

        self._consolidation_tasks: set[asyncio.Task] = set()

        # ``self.subagents``, ``self.context_engine`` and
        # ``self.memory_consolidator`` were each handed ``provider`` earlier in
        # this constructor. Inside a turn they read the turn's binding; the
        # reference they hold is only the fallback for work that runs outside
        # one, and ``set_default_binding`` is what keeps that fallback current.
        # Add the call there when adding another holder.

        # Phase B-3: the L4 facade (``DefaultMemoryEngine`` /
        # ``MemoryEngine`` ABC) has been retired. AgentLoop now holds
        # the underlying subsystems directly:
        #
        # - ``self.memory_consolidator`` (above) — markdown compaction
        #   policy. Owns the ``MemoryStore`` it built; reach it via
        #   ``self.memory_consolidator.store`` when needed.
        # - ``self.context.skills`` — :class:`LocalSkillCatalog` for the
        #   always-skills + ``# Skills`` render path. The SkillForgeRouter stack
        #   (assembled in ``context_engine.factory``) owns retrieval.

        # AgentHook lifecycle chain. The 3 legacy callback
        # parameters (``on_user_inbound`` / ``decision_consumer`` /
        # ``response_modifier``) get auto-wrapped into adapter hooks
        # and merged with any caller-supplied ``hooks`` composite.
        #
        # Ordering rationale:
        #   1. OnUserInboundAdapter first — pure observer, never
        #      short-circuits. Keeps FeedbackTracker engagement counting
        #      every legitimate inbound (matching legacy behavior).
        #   2. DecisionConsumerAdapter next — may short-circuit when the
        #      user replies to a Sentinel TaskDiscovery menu. Observers
        #      have already fired.
        #   3. Caller-supplied ``hooks`` after — typically empty today;
        #      eval_engine will populate it.
        #   4. ResponseModifierAdapter last — only meaningful in
        #      ``after_send`` phase, where it's the sole writer.
        self.hooks: "CompositeHook" = CompositeHook()
        if on_user_inbound is not None:
            self.hooks.append(OnUserInboundAdapter(on_user_inbound))
        if decision_consumer is not None:
            self.hooks.append(DecisionConsumerAdapter(decision_consumer))
        if hooks is not None:
            self.hooks.extend(hooks)
        if response_modifier is not None:
            self.hooks.append(ResponseModifierAdapter(response_modifier))

        self._register_default_tools()
        # After the registry is populated, and after ``_mcp_servers`` is set: the
        # runtime reads both to build the generator's inventory of what this
        # install can actually offer. Built here rather than beside the other
        # fields above because it is the only assembly in this constructor with
        # that dependency, and the two orderings are not compatible -- see
        # ``_build_playbook_runtime``.
        self._build_playbooks()
        self._report_reserved_disabled_tools()

    def _report_reserved_disabled_tools(self) -> None:
        """Tell the operator about an off switch the loop cannot honour.

        All this function does now. Withholding a tool is decided per request by
        :meth:`_withheld_tool_names`, which is what makes a switch flipped now
        take effect on the next turn -- this used to *unregister* the tool, and a
        preference expressed by destroying its subject is one that cannot be
        reversed: nothing remembered what to put back.

        The MCP meta-tools are the one exemption, and it is theirs by ownership
        rather than by policy: :meth:`_sync_mcp_meta_tools` registers them while
        some connected server serves resources or prompts and withdraws them when
        none does, so it owns those five names for the life of the loop. An entry
        naming one is a preference nothing can act on -- reported here once,
        ignored where the array is built.

        Run after :meth:`_register_default_tools` and after MCP connect, so an
        entry naming a tool from either group is resolvable by the time it is
        checked. Silent on misses: an eval config commonly carries an over-broad
        list that is a no-op in this build.

        Reads the same two sources as :meth:`_withheld_tool_names`, so the notice
        is about the entry the operator can actually see -- most of them are in
        the config file, which is no longer copied into ``_disabled_tools``.
        """
        from raven.config.live import disabled_tool_names
        from raven.mcp.prompts import PROMPT_TOOL_NAMES
        from raven.mcp.resources import RESOURCE_TOOL_NAMES

        entries = set(disabled_tool_names(self._live_config)) | self._disabled_tools
        if not entries:
            return
        reserved = RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES
        for entry in sorted(entries):
            if entry in self._disabled_tools_reserved_warned:
                continue
            if any(name in reserved for name in self.tools.resolve_configured(entry)):
                self._disabled_tools_reserved_warned.add(entry)
                logger.warning(
                    "tools.disabled_tools names '{}', which raven registers and withdraws on its "
                    "own as MCP servers serving resources or prompts come and go. The entry has "
                    "no effect; remove it to keep the config honest.",
                    entry,
                )

    def _withheld_tool_names(self) -> frozenset[str]:
        """Which tools are not on offer right now, read live.

        The registry asks this when it assembles a tool array. Reserved names are
        removed here rather than at the switch: ``_sync_mcp_meta_tools`` owns those
        five for the life of the loop (it registers them while some connected
        server serves resources or prompts and withdraws them when none does), so
        an entry naming one is a preference the loop cannot honour -- reported
        above, ignored here.

        Constructor-supplied names are unioned in because an eval harness passes
        them directly rather than through a config file; a file-less run would
        otherwise lose its blacklist entirely. Nothing copies the config's own
        list in there -- see the note in ``__init__`` on why that made the switch
        one-way.
        """
        from raven.config.live import disabled_tool_names
        from raven.mcp.prompts import PROMPT_TOOL_NAMES
        from raven.mcp.resources import RESOURCE_TOOL_NAMES

        configured = set(disabled_tool_names(self._live_config)) | set(self._disabled_tools)
        withheld: set[str] = set()
        for entry in configured:
            withheld.update(self.tools.resolve_configured(entry))
        return frozenset(withheld - (RESOURCE_TOOL_NAMES | PROMPT_TOOL_NAMES))

    @property
    def provider(self) -> LLMProvider:
        """The provider of the binding the running turn entered.

        A property, not an attribute: the model is per session now, so there
        is no single answer to cache on the loop. Outside a turn (startup, a
        one-shot CLI call) this is the configured default.
        """
        binding = active_binding()
        return binding.provider if binding is not None else self._default_binding.provider

    @property
    def model(self) -> str:
        """The model id of the binding the running turn entered."""
        binding = active_binding()
        return binding.model if binding is not None else self._default_binding.model

    @property
    def context_window_tokens(self) -> int:
        """How much the binding of the running turn can hold.

        A property for the same reason ``provider`` and ``model`` are: two
        sessions can be on models of different sizes at once, so a single int
        on the loop has no answer that is right for both. Outside a turn this
        is the configured default's window.
        """
        binding = active_binding() or self._default_binding
        return binding.context_window

    @property
    def provider_pool(self) -> "ProviderPool | None":
        """Where a model id becomes a model id plus the credential for it."""
        return self._provider_pool

    @property
    def default_binding(self) -> ModelBinding:
        """What a session with no switch of its own runs on."""
        return self._default_binding

    def binding_for_session(self, session_key: str) -> ModelBinding:
        """The binding this session runs on: its own switch, else the default.

        A new session has no entry, so it starts on the configured default
        rather than on whatever the last session switched to.

        A session whose choice is on disk but not yet in memory is restored here,
        on first ask. That is what makes the choice outlive a restart on *every*
        surface: the overrides live in this process, the session record is the
        only place they survive, and hanging the read off a TUI-only resume call
        meant a conversation on a channel came back on the default with its
        choice sitting unread in its own record.
        """
        binding = self._session_bindings.get(session_key)
        if binding is not None:
            return binding
        self._restore_once(session_key)
        return self._session_bindings.get(session_key, self._default_binding)

    def _restore_once(self, session_key: str) -> None:
        """Read this session's stored model, at most once per key per process.

        The negative answer is remembered too. Most sessions never switched, and
        without that this would re-read a record on every turn to learn the same
        nothing.
        """
        if session_key in self._restore_attempted:
            return
        self._restore_attempted.add(session_key)
        sessions = getattr(self, "sessions", None)
        if sessions is None or self._provider_pool is None:
            return
        try:
            record = sessions.peek(session_key)
        except Exception as exc:
            logger.debug("cannot read session {!r} to restore its model: {}", session_key, exc)
            return
        metadata = getattr(record, "metadata", None) or {}
        model = metadata.get("model")
        if model:
            self.restore_session_model(session_key, model, metadata.get("provider"))

    def session_model(self, session_key: str) -> str:
        """What to show this session's user, which is not the global default."""
        return self.binding_for_session(session_key).model

    def has_session_binding(self, session_key: str) -> bool:
        """Did this session switch, or is it just following the default?

        ``session_model`` cannot answer that -- it falls back to the default,
        so it never returns None. Callers that must distinguish "chose this"
        from "inherited this" ask here.

        Restores first, so a session that switched before a restart answers yes
        rather than being reported as having inherited the default.
        """
        if session_key not in self._session_bindings:
            self._restore_once(session_key)
        return session_key in self._session_bindings

    def restore_session_model(self, session_key: str, model: str, provider_name: str | None = None) -> None:
        """Put a session back on the model it was last switched to.

        Session overrides live in memory, so without this a restart moves every
        switched session back to the default and the user's choice lasts exactly
        as long as the process. A model that can no longer be built (a credential
        since removed) leaves the session on the default rather than failing the
        turn that asked.

        Normally reached through ``_restore_once``, which supplies the stored
        pair; kept public for a caller that has the pair already.
        """
        self._restore_attempted.add(session_key)
        pool = self._provider_pool
        if pool is None or not model:
            return
        try:
            self.set_session_binding(session_key, pool.bind(model, provider_name))
        except Exception as exc:
            # Broad on purpose. Building a provider imports a vendor module and
            # checks credentials, so the failures reachable here are open-ended
            # -- ``MissingCredentialsError`` is one that did not exist when this
            # guard was first written, and it escaped a tuple of three. A resume
            # that lands on the default is a worse session; a resume that raises
            # is no session at all.
            logger.warning("session {!r} cannot resume on {!r} ({}); using the default", session_key, model, exc)

    def set_session_binding(self, session_key: str, binding: ModelBinding) -> None:
        """Switch one session, leaving every other session where it was.

        Applied immediately and still safe mid-turn: a turn resolves its
        binding once at ``run_turn`` entry and holds it in a context var for
        its whole tree, including anything it detaches. So a switch during a
        turn cannot move that turn -- it lands on the next one -- and no
        parking is needed to arrange that.
        """
        self._session_bindings[session_key] = binding
        self._forget_transport_verdicts()

    def clear_session_binding(self, session_key: str) -> None:
        """Drop a session's override so it follows the default again.

        Deliberately does not mark the key as consulted. The one caller is
        ``session.delete``, which unlinks the record before this runs, so there
        is nothing left for a later ask to read back in -- and a session that
        somehow kept its record is better served by re-reading it than by a
        marking that claims we looked when we did not.
        """
        self._session_bindings.pop(session_key, None)

    def _forget_transport_verdicts(self) -> None:
        """Drop the capability verdicts a new provider may answer differently.

        Both caches key on a model id but are computed from the provider serving
        it, so a rebuild that keeps the id keeps the old endpoint's answer. The
        reachable case is an ``apiBase`` repointed at a box with different
        capabilities, or a re-authenticated provider: the credentials
        fingerprint changes, the pool builds a new provider, the model id does
        not move -- and images stay dropped from tool results for the life of
        the process, with nothing in the log to say why.

        Cleared wholesale rather than per binding: the loop now holds several
        providers at once, and the key does not say which one answered.
        """
        self._image_tool_result_ok.clear()
        self._vision_ok.clear()

    def set_default_binding(self, binding: ModelBinding) -> None:
        """Change what new sessions start on.

        Sessions that already switched keep their own binding; sessions that
        never did pick this up on their next turn. Subsystem fallbacks are
        re-pointed too, for the paths that run outside a turn and therefore
        have no binding to read.
        """
        self._default_binding = binding
        self._forget_transport_verdicts()
        self.subagents.set_provider(binding.provider, binding.model)
        self.context_engine.set_provider(binding.provider, binding.model)
        self.memory_consolidator.set_provider(binding.provider, binding.model)

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Change the default binding, from a pair a caller already built.

        No production caller today -- every switch path goes through the pool
        and lands on ``set_default_binding`` or ``set_session_binding``. Kept as
        the pair-free entry point for an embedder that has a provider in hand,
        which is why it carries ``_configured_window`` forward: a window the
        user pinned belongs to whatever they run, and building the binding
        without it here would drop it the day this grows a caller.
        """
        self.set_default_binding(ModelBinding(provider, model, self._configured_window))

    def configure_personalization(self, enable: bool) -> None:
        """Global switch for the 4-step personalization flow (PAHF-inspired).

        When enabled, each message goes through:
          Step 1 - classify:          classify() — does this request need a preference question?
          Step 2 - pre-action interaction: ask one question if needed, extract and store the answer
          Step 3 - execute:           normal agent loop (unchanged)
          Step 4 - post-action learn: post_learn() runs in background after every response

        Disabled by default. Enable via config: agents.defaults.enable_personalization: true
        """
        self.enable_personalization = enable
        logger.info("Personalization flow: {}", "enabled" if enable else "disabled")

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dirs = (self.workspace,) if self.restrict_to_workspace else ()
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, GrepTool, FindTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dirs=allowed_dirs))
        if self._deliverables is not None:
            from raven.agent.tools.deliver import DeliverFilesTool

            self.tools.register(
                DeliverFilesTool(self._deliverables, workspace=self.workspace, allowed_dirs=allowed_dirs)
            )
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                executor=self._executor,
                extra_deny_patterns=self.exec_config.extra_deny_patterns,
                extra_allowed_dirs=(self.workspace,),
            )
        )
        # web_search needs a Serper key it does not have by default, and offering
        # it anyway is worse than withholding it: the model reaches for it, the
        # call fails, and the error text -- naming a config file and an env var --
        # gets relayed to whoever is on the other end of the channel. Ask the tool
        # rather than the config, because it resolves the key at call time from
        # either source; gating on `brave_api_key` alone would withdraw the tool
        # from a deploy that only exports SERPER_API_KEY.
        web_search = WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy)
        if web_search.api_key:
            self.tools.register(web_search)
        # web_fetch is unconditional by contrast: it works without a key, and the
        # Jina one only upgrades the extraction.
        self.tools.register(WebFetchTool(api_key=self.jina_api_key, proxy=self.web_proxy))
        # Media tools (image/speech/video) are opt-in: a tool is registered only
        # when the user configured it (a model or apiKey under tools.media.<tool>),
        # which Config.effective_media_config() surfaces as a resolved key/model.
        # An OpenRouter key set for chat alone never enables them.
        media = self.media_config
        media_tools = (
            (ImageGenerateTool, media.image),
            (SpeechGenerateTool, media.speech),
            (VideoGenerateTool, media.video),
        )
        for cls, tool_cfg in media_tools:
            if tool_cfg.api_key or tool_cfg.model:
                self.tools.register(
                    cls(
                        tool_cfg,
                        workspace=self.workspace,
                        proxy=media.proxy,
                        output_subdir=media.output_subdir,
                    )
                )
        # Deep research (MiroThinker) is a paid, minute-scale HTTP engine, so it is
        # never a plain default tool. Two modes: ``real`` (key configured) is the
        # working tool + async manager; ``offer`` (no key) is a same-named stand-in
        # that, on a research query, asks the user deep-vs-regular and guides setup.
        self.deep_research_manager: DeepResearchManager | None = None
        # The async-delivery submit handle (gateway-wired, post-construction). Kept
        # on the loop so a manager built later by promotion inherits it too, rather
        # than only the startup manager -- see ``set_deep_research_submit``.
        self._deep_research_submit: Callable[[Any], Any] | None = None
        # The deep-vs-regular ask broker (transport-wired, post-construction). Kept
        # on the loop for the same reason: a tool built later by promotion must
        # inherit it, else it silently skips the ask -- see ``set_deep_research_broker``.
        self._deep_research_broker: QuestionBroker | None = None
        if deep_research_mode(self.deep_research_config) == "real":
            self._register_real_deep_research(self.deep_research_config)
        else:
            self.tools.register(DeepResearchOfferTool())
        self.tools.register(MessageTool())
        self.tools.register(SpawnTool(manager=self.subagents))
        # Sub-agent DAG orchestration (req4). Registered unconditionally now that
        # the agent table always holds the package's built-in rows: the tool used
        # to be gated on an enabled third-party entry existing, because without one
        # its roster was empty and a node had nothing to name. A graph over
        # research-raven and code-raven is a graph, so that gate would now be
        # withholding the tool from every default install.
        from raven.agent.subagent_dag.tool import SubAgentDagTool

        self.tools.register(
            SubAgentDagTool(
                workspace=self.subagents.workspace,
                registry=self.subagents.registry,
                guide_skill_id=self._dag_guide_skill_id(),
                session_dir=self.sessions.session_dir,
                is_paused=lambda: self.subagents.paused,
                state_for=self.subagents.instance_state,
                everos_for=self.subagents.everos_identity,
                gate=self.subagents.dispatch_gate,
                announce=self.subagents.announce_dag_result,
                adopt=self.subagents.adopt_background_run,
                charge=self.subagents.charge_dag_run,
                ask=self._confirm_graph,
                control_reachable=self.dag_control_reachable,
            )
        )
        # The graph tool's own acceptance text is the only advertisement these
        # two get: hidden from the schema so the per-turn tool list carries
        # nothing a conversation that never starts a DAG has any use for, they
        # stay reachable through the registry (and tool_call where it exists).
        from raven.agent.subagent_dag.control_tools import CancelDagTool, DagStatusTool

        self.tools.register(CancelDagTool(loop=self))
        self.tools.register(DagStatusTool(loop=self))
        self.tools.hide_from_schema("cancel_dag", "dag_status")
        # The QuestionBroker is a per-transport singleton, late-bound via
        # set_broker once the transport (TUI RPC server / gateway hub) exists.
        self.tools.register(AskUserTool(timeout_s=self.ask_user_config.timeout))
        # The plugin market, reachable from the conversation. Unconditional: the
        # catalog ships in the wheel and the connection manager is this loop's
        # own, so the only thing that ever made this impossible was the tool not
        # existing -- an agent asked to connect an integration could reach the
        # engine no other way than telling the user to go to the panel.
        from raven.agent.tools.plughub import PluginTool

        self.tools.register(PluginTool(loop=self))
        if self.cron_service:
            # Lazy import: CronTool lives under raven.proactive_engine.schedulers.cron.tool
            # which (a) imports raven.agent.tools.base, triggering raven.agent.__init__,
            # which (b) imports this very loop module. Importing at function scope breaks the
            # cycle since loop.py is fully loaded by the time _register_default_tools runs.
            from raven.proactive_engine.schedulers.cron.tool import CronTool

            self.tools.register(CronTool(self.cron_service))

        # Plugin-contributed tools (e.g. EverOS's ``understand_media``).
        # Registered last so a plugin can override a built-in by name if
        # it deliberately contributes the same name; ``_withheld_tool_names``
        # still runs afterward and can strip any of them.
        for tool in self.plugin_tools:
            self.tools.register(tool)

        # Skill retrieval tools (body -> scripts). Both are source-agnostic and
        # both serve local/everos straight from the registry, so both register
        # whenever the registry is reachable; a Hub endpoint only adds their
        # ``hub/`` branch. Gating ``read_skill`` on the client would take the
        # body-fetch route away from the skills that ship with Raven, whose
        # ``inject: description`` entry advertises exactly that route.
        skill_registry = getattr(
            getattr(self.context, "skills", None),
            "registry",
            None,
        )
        if skill_registry is not None or self._skill_hub_client is not None:
            from raven.agent.tools.skill_hub import FindSkillTool, ReadSkillTool, UseSkillTool

            self.tools.register(
                ReadSkillTool(
                    client=self._skill_hub_client,
                    registry=skill_registry,
                    min_safety=self._skill_min_safety,
                    blocklist=self._skill_blocklist,
                ),
            )
            # Pull-mode discovery: search on the model's own terms through the
            # same router the context engine retrieves with.
            self.tools.register(
                FindSkillTool(
                    lambda: getattr(self.context_engine, "skills_router", None),
                    hub_wired=self._skill_hub_client is not None,
                    min_safety=self._skill_min_safety,
                    blocklist=self._skill_blocklist,
                ),
            )
            self.tools.register(
                UseSkillTool(
                    client=self._skill_hub_client,
                    registry=skill_registry,
                    min_safety=self._skill_min_safety,
                    blocklist=self._skill_blocklist,
                    auto_install=self._skill_auto_install,
                    install_audit_path=(
                        self.workspace / "skills" / "hub" / "installs.jsonl"
                        if self._skill_hub_client is not None
                        else None
                    ),
                ),
            )

        # Progressive tool disclosure. Registered last so the catalog it
        # searches covers every built-in/plugin tool above; MCP tools join
        # later (registered in ``_connect_mcp``) and the strategy picks them up
        # since it re-reads the registry each turn.
        cfg = self._tool_search_config
        if cfg is not None and cfg.enabled:
            from raven.agent.tools.tool_search import (
                DEFAULT_ALWAYS_VISIBLE,
                ToolCallTool,
                ToolSearchController,
                ToolSearchStrategy,
                ToolSearchTool,
            )

            always = set(DEFAULT_ALWAYS_VISIBLE) | set(cfg.always_visible)
            self.tool_search_controller = ToolSearchController(
                self.tools,
                always_visible=always,
                search_result_limit=cfg.search_result_limit,
                compaction_threshold=cfg.compaction_threshold,
            )
            self.tools.register(ToolSearchTool(self.tool_search_controller))
            self.tools.register(ToolCallTool(self.tool_search_controller))
            # ``first=True``: filter the tool list before CacheOptimizer marks
            # the final tool with ``cache_control`` (else the marked tool may be
            # filtered out and the breakpoint lost).
            self.strategies.register(
                ToolSearchStrategy(self.tool_search_controller),
                first=True,
            )

    def _build_playbooks(self) -> None:
        """Build the playbook runtime and register its two entry tools.

        Runs after ``_register_default_tools`` because the runtime's generator
        needs a real inventory of what this install offers, and that is
        ``self._mcp_servers`` plus a *populated* tool registry. Registering the
        two playbook tools from inside ``_register_default_tools`` was what made
        the two orderings look compatible: the runtime had to exist before
        registration, yet could not be built until after it.

        It read ``self._mcp_servers`` several assignments before that field
        existed, so ``enabled: true`` raised ``AttributeError``, the guard below
        swallowed it, and the whole feature was off on every install that asked
        for it -- with one warning line as the only trace. No test caught it
        because none of them construct an ``AgentLoop`` with a playbook config;
        they assemble the runtime directly, which is the one path production
        never takes.

        A failure to build still leaves the feature off rather than breaking the
        loop: a library that cannot load is not a reason for the agent to refuse
        every turn.
        """
        cfg = self._playbook_config
        if cfg is None or not cfg.enabled:
            return
        try:
            self._playbooks = self._build_playbook_runtime(cfg)
        except Exception:
            logger.opt(exception=True).warning("Playbook runtime failed to build; feature disabled")
            return
        # The only entry into the library, and registered even when the library
        # is empty. Withholding it until something was in there made the first
        # creation of a session unreachable: `create_playbook` writes one, and
        # the tool that loads it did not exist until the next process. Its
        # description says so itself when there is nothing installed, which
        # costs a sentence.
        from raven.agent.tools.load_playbook import LoadPlaybookTool

        self.tools.register(LoadPlaybookTool(self._playbooks))
        # Creation registers whenever the feature is on -- an empty library is
        # exactly when capturing the first workflow matters.
        from raven.agent.tools.create_playbook import CreatePlaybookTool
        from raven.config.update import set_playbook_disabled

        self.tools.register(
            CreatePlaybookTool(
                self._playbook_generator,
                self._playbook_store,
                set_playbook_disabled,
                # So a playbook created mid conversation is loadable in the same
                # conversation: the library is read once at construction, and
                # nothing else would tell it that a file appeared.
                adopt=self._playbooks.adopt,
            )
        )

    def _build_playbook_runtime(self, cfg: "PlaybookConfig"):
        """Assemble the playbook funnel from pieces this loop already owns.

        The executor gets a private SubAgentDagTool instance (never
        registered, so no LLM-facing surface changes) wired to the same
        manager hooks as the registered one: one dispatch gate, one quota,
        one announce path.
        """
        from raven.agent.subagent_dag.tool import SubAgentDagTool
        from raven.playbook import PlaybookExecutor, PlaybookRuntime, PlaybookStore

        # The user layer of the two-layer library; the builtin layer is the
        # store's own default. ``subagents.workspace`` is agent home here, so
        # playbooks sit beside memory and skills rather than following the
        # per-turn working directory.
        user_layer = Path(cfg.dir) if cfg.dir else (self.subagents.workspace / "playbooks")
        dag_tool = SubAgentDagTool(
            workspace=self.subagents.workspace,
            registry=self.subagents.registry,
            guide_skill_id=None,
            session_dir=self.sessions.session_dir,
            is_paused=lambda: self.subagents.paused,
            # A playbook step may now name an `instance`, so it needs the same
            # message-list derivation a spawn and a registered DAG node get.
            # Missing here before because a playbook step could not carry a handle
            # at all -- the executor dropped the field on the way to dispatch.
            state_for=self.subagents.instance_state,
            gate=self.subagents.dispatch_gate,
            announce=self.subagents.announce_dag_result,
            adopt=self.subagents.adopt_background_run,
            charge=self.subagents.charge_dag_run,
            ask=self._confirm_graph,
        )
        from raven.playbook import PlaybookGenerator, RouterSizes, live_inventory

        executor = PlaybookExecutor(
            dag_tool=dag_tool,
            provider=self.provider,
            compose_model=cfg.model,
        )
        # The agent table, not a private pool: what the generator casts nodes
        # against has to be what the graph can then dispatch to.
        roster = self.subagents.registry.descriptions()
        executor.set_roster(roster)
        store = PlaybookStore(user_layer)
        # Kept for the create_playbook tool: creation shares the library and
        # generator with the funnel, so both entries write the same place.
        self._playbook_store = store
        self._playbook_generator = PlaybookGenerator(
            self.provider,
            None,
            roster,
            # A real inventory: with the empty one this used to pass,
            # ``check_assets`` judged every skill and mcp server a draft named to be
            # unknown, so a good draft came back annotated as missing everything.
            live_inventory(self._mcp_servers, self.tools.names()),
            model=cfg.model,
        )

        return PlaybookRuntime(
            store=store,
            executor=executor,
            # The file as it stands, and deliberately not ``cfg.disabled``
            # beside it: that is a snapshot of the same key, and a runtime
            # holding both can only ever add to the deny list -- ``disable``
            # would apply on the next call while ``enable`` waited for the next
            # process. ``_withheld_tool_names`` above avoids this the same way.
            disabled_source=self._disabled_playbook_names,
            # The live table, asked rather than copied, and the view its own
            # docstring reserves for validating: a row that is switched off is
            # still a resolvable reference, and ``apply_agents`` rebuilds this
            # registry in place, so a copy taken here would go stale against the
            # very dispatch it is meant to agree with.
            known_agents=self.subagents.registry.all_names,
            router=RouterSizes(top_k=cfg.router.top_k, over_fetch_factor=cfg.router.over_fetch_factor),
        )

    def _disabled_playbook_names(self) -> frozenset[str]:
        """The playbook deny list as it stands on disk, for the runtime to ask."""
        from raven.config.live import disabled_playbook_names

        return disabled_playbook_names(self._live_config)

    async def _confirm_graph(self, conversation_id: str, question: str) -> bool:
        """The graph-level ``confirm`` gate's route to a human.

        A method rather than a closure inside the playbook wiring, because both
        DAG tool instances need it and that wiring only runs when
        ``playbooks.enabled``. Built as a closure there first, it left the
        *registered* ``run_subagent_dag`` -- the one the model calls -- with no
        asker at all: a model-composed graph asking for approval got a log line,
        and was then told a human had approved something no human saw.

        Resolved per call rather than captured: this is handed to a tool built in
        ``_register_default_tools``, and the transport injects the ask broker
        later still.

        No broker (a channel with no question path, a test) returns True and the
        graph runs. Not every surface can put a question to a human, and letting
        the absence of one disable the feature outright is the worse failure;
        ``SubAgentDagTool._confirmed`` logs it when it happens.
        """
        tool = self.tools.get("ask_user")
        if not isinstance(tool, AskUserTool):
            return True
        answer = await tool.ask_direct(question, ["Run it", "Not now"], conversation_id)
        if answer is None:
            return True
        return answer.strip().lower() in {"run it", "run", "yes", "y", "ok", "go", "sure"}

    def _dag_guide_skill_id(self) -> str | None:
        """The orchestration guide's id for the DAG tool description, or None.

        The tool tells the agent to load this skill before its first call, so
        the pointer must be real: a deployment that removed the builtin skills
        would otherwise burn a turn on a read_skill that cannot resolve. When
        the registry itself is unreachable, keep the pointer — the shipped
        skill is there by default, and losing the instruction is the worse
        failure of the two.
        """
        from raven.agent.subagent_dag.tool import GUIDE_SKILL_ID

        registry = getattr(getattr(self.context, "skills", None), "registry", None)
        if registry is None:
            return GUIDE_SKILL_ID
        try:
            found = registry.get(GUIDE_SKILL_ID.split("/", 1)[1]) is not None
        except Exception:  # noqa: BLE001 - a registry hiccup must not unregister the guide
            return GUIDE_SKILL_ID
        return GUIDE_SKILL_ID if found else None

    @staticmethod
    def _build_skill_hub_client(
        workspace: Path,
        skill_forge_router_config: "SkillForgeRouterConfig | None",
    ) -> "SkillHubClient | None":
        """Construct the shared Skill Hub client, or ``None`` when no Hub is
        configured. Downloads land under ``<workspace>/skills/hub`` so a
        use_skill'd bundle is discoverable by the on-disk skill registry."""
        hub_cfg = getattr(skill_forge_router_config, "hub", None)
        if hub_cfg is None or not getattr(hub_cfg, "endpoint", None):
            return None
        from raven.skill_hub import SkillHubClient

        return SkillHubClient(
            hub_cfg.endpoint,
            api_key=hub_cfg.api_key,
            timeout_s=hub_cfg.timeout_s,
            source=hub_cfg.source,
            cache_dir=workspace / "skills" / "hub",
        )

    def _supports_image_tool_result(self, model: str | None = None) -> bool:
        """Cached per model: resolving the LiteLLM target parses the model string,
        and this is asked once per tool call that returns an image.

        A ``False`` learned from a refused request (see ``should_drop_tool_images``)
        is written into the same cache, so a static table that guessed wrong stops
        costing a wasted call after the first one.
        """
        key = model or self.model
        if key not in self._image_tool_result_ok:
            spec = None
            try:
                from raven.providers.registry import find_by_model

                spec = find_by_model(key)
            except Exception:
                pass
            self._image_tool_result_ok[key] = supports_image_tool_result(self.provider, key, spec)
            logger.debug(
                "image-in-tool-result support for {}: {}",
                key,
                self._image_tool_result_ok[key],
            )
        return self._image_tool_result_ok[key]

    # The tool that can read an attachment for a model that cannot see it.
    # Contributed by the EverOS plugin, so absent on a default install.
    _DESCRIBE_TOOL = "understand_media"

    def _describe_tool_name(self) -> str | None:
        """The description tool's name if it is registered, else ``None``.

        Checked rather than assumed: the note that replaces a picture points at
        this tool, and pointing at one the model was never given is an
        instruction it cannot follow.
        """
        return self._DESCRIBE_TOOL if self.tools.get(self._DESCRIBE_TOOL) else None

    def _route_result_images(
        self,
        model_text: str,
        blocks: list[dict[str, Any]] | None,
        model: str,
    ) -> tuple[str, list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        """Decide how a tool result's pictures reach ``model``.

        Returns the text the tool result carries, the blocks to put *in* it, and
        the blocks to attach to a following user message. Exactly one of the last
        two is ever populated.

        Three outcomes, and the wording differs because the model's next move
        differs. No vision at all: nothing follows, so the note must not promise
        an attachment, and it names the description tool when one is registered.
        Vision and a transport that carries images in a ``role="tool"`` message:
        the blocks ride along untouched. Vision but a transport that cannot (every
        OpenAI-style Chat Completions endpoint -- image is excluded from the tool
        role at the schema level): the result carries text and the picture follows
        in a user message, the shape OpenClaw uses.

        A method rather than a branch inside the loop so it can be tested at all:
        the loop reaches this point only through a live provider and a real tool
        call, and the wrong choice here is silent -- the model answers about a
        picture it never received.
        """
        if not blocks:
            return model_text, blocks, None
        if not self._supports_vision(model):
            return image_placeholder_text(blocks, blind=True, describe_tool=self._describe_tool_name()), None, None
        if self._supports_image_tool_result(model):
            return model_text, blocks, None
        attach = [b for b in blocks if b.get("type") == "image_url"]
        return image_placeholder_text(blocks), None, attach

    def _supports_vision(self, model: str | None = None) -> bool:
        """Cached per model: whether this model can see a picture at all.

        Asked once per turn and once per tool result that returns an image, and
        the lookup joins the model string against the gateway catalogue -- same
        reason the sibling probe above is cached.

        Only a real verdict is cached. ``vision_verdict`` returns ``None`` while
        the catalog has no answer -- a cold install, before the background warm
        lands -- and that is optimism rather than knowledge: caching it would
        freeze the guess for the life of this loop, which is the life of the
        process, and the warm would then fill a table nothing re-reads. An
        unknown model is re-asked each turn, which costs a dict lookup.

        The verdict is the routed primary's. A fallback further down the chain is
        sent the same message list, so a vision-capable primary with a blind
        fallback hands the blind endpoint an image block it will refuse; that
        refusal classifies as fatal and stops the chain rather than answering
        blind. Pre-existing in the sibling probe too, and it needs the fallback
        chain to be assembled per candidate to fix properly.
        """
        key = model or self.model
        cached = self._vision_ok.get(key)
        if cached is not None:
            return cached

        spec = None
        try:
            from raven.providers.registry import find_by_model

            spec = find_by_model(key)
        except Exception:
            pass
        verdict = vision_verdict(key, spec, self.provider)
        logger.debug("vision support for {}: {}", key, verdict)
        if verdict is None:
            return True
        self._vision_ok[key] = verdict
        return verdict

    # ── Context engine helpers ──────────────────────────────────────────

    def _context_messages_for_session(self, session: Session) -> list[dict[str, Any]]:
        """Return the candidate message view owned by the active context engine.

        Curator (``owns_compaction=True``) wants the full append-only log so
        it can decide what to archive itself; Legacy wants the post-consolidation
        slice to match the pre-Curator behavior exactly.
        """
        if self.context_engine.owns_compaction:
            return list(session.messages)
        return session.get_history(max_messages=0)

    def _make_token_budget(self, selected_skills: list[Any] | None = None) -> TokenBudget:
        """Compute a conservative per-turn prompt budget for the active engine."""
        # allow_fetch=False for the same reason construction passes it (see
        # __init__): this runs per turn on the loop's own thread, and it only
        # needs a number to reserve -- not the one a request will carry. The
        # fallback under-reserves at worst; the importing tier costs seconds.
        ceiling = send_max_tokens(
            getattr(self.provider, "generation", None),
            # The id a request will go out under, so the reservation matches the
            # ceiling that request will carry rather than the stored name's.
            getattr(self.provider, "wire_model_id", lambda m: m)(self.model),
            allow_fetch=False,
        )
        # The whole ceiling, not a share of it. Requests no longer name a
        # ceiling, so the one that applies is the model's own -- whatever the
        # vendor or LiteLLM's transformation fills in. Reserving less than that
        # hands out a prompt the reply cannot coexist with: measured on this
        # repo's default model, a share leaves the prompt 150000 of a 200000
        # window against a reply allowed 64000, and the sum is refused at
        # request time. `_emergency_shrink` only elides tool bodies, so a
        # history grown on conversation gets no retry from that refusal.
        #
        # A share would be right again only if the request carried one, which
        # is the trade the previous shape made and this one does not.
        reserved_output = min(ceiling, self.context_window_tokens)
        tool_tokens = estimate_prompt_tokens([], self.tools.get_definitions())
        system_prompt = self.context.build_system_prompt(selected_skills)
        system_tokens = estimate_prompt_tokens([{"role": "system", "content": system_prompt}])
        available_history = max(
            0,
            self.context_window_tokens - reserved_output - tool_tokens - system_tokens,
        )
        return TokenBudget(
            context_length=self.context_window_tokens,
            reserved_output=reserved_output,
            reserved_tools=tool_tokens,
            reserved_system=system_tokens,
            available_history=available_history,
        )

    def _uses_default_engine(self) -> bool:
        """Whether the active engine owns skill selection via SkillForgeRouter.

        Always ``True`` now — there is a single
        :class:`ContextAssembler` whose SkillsSegmentBuilder handles
        selection and populates ``injected_skill_ids`` in the assembled
        metadata. Kept as a method (rather than inlined) because several
        callsites still gate on it; it no longer branches on engine name.
        """
        return True

    async def _select_skills_for_turn(
        self,
        current_message: str,
        history: list[dict],
    ) -> list[Any] | None:
        """No host-side pre-selection — the engine's SkillForgeRouter owns it.

        The unified engine selects + renders skills internally and
        surfaces ``injected_skill_ids`` via ``AssembledContext.metadata``,
        which AgentLoop reads out of ``_last_injected_skill_ids`` after
        assemble. No SkillMeta list flows through this path.
        """
        return None

    async def _assemble_context_messages(
        self,
        *,
        session: Session,
        session_key: str,
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        surface: str | None = None,
        selected_skills: list[Any] | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ask the active context engine for the main-agent message window.

        ``model`` is the id the request will actually reach (the router's pick,
        when there is one). It decides whether an attachment is inlined as a
        picture, so defaulting it to ``self.model`` would let the configured
        model answer for a routed one.
        """
        from raven.context_engine import TurnContext  # deferred — see module note

        # Phase A / Phase C tidy: reset the metadata stash BEFORE calling
        # the engine. If ``engine.assemble`` raises partway, the next
        # caller falls back to the legacy ``_collect_injected_skill_ids``
        # path rather than accidentally consuming a previous turn's
        # injected ids. Only successful assemble repopulates the stash.
        self._last_injected_skill_ids = None
        self._last_injected_skill_sources = {}
        session_messages = self._context_messages_for_session(session)
        assembled = await self.context_engine.assemble(
            session_key,
            session_messages,
            self._make_token_budget(selected_skills),
            turn=TurnContext(
                current_message=current_message,
                media=media,
                can_see_images=self._supports_vision(model),
                describe_tool=self._describe_tool_name(),
                channel=channel,
                chat_id=chat_id,
                surface=surface,
                selected_skills=selected_skills,
            ),
        )
        # Stash the engine's injected_skill_ids so the after-turn
        # feedback dispatcher can read the source-qualified ids the
        # unified engine populates via SkillForgeRouter. If the key is absent
        # the stash stays None and _collect_injected_skill_ids falls back
        # to the SkillMeta-based path.
        meta_ids = assembled.metadata.get("injected_skill_ids") if assembled.metadata else None
        self._last_injected_skill_ids = list(meta_ids) if meta_ids else None
        meta_sources = assembled.metadata.get("injected_skill_sources") if assembled.metadata else None
        self._last_injected_skill_sources = dict(meta_sources) if meta_sources else {}
        messages = assembled.messages
        self._inject_recovery_block(session_key, messages)
        return messages

    @staticmethod
    def _checkpoint_active(policy: str, interactive: bool) -> bool:
        """Resolve ``runtime.checkpoint.policy`` against the call-site's
        ``interactive`` signal. ``"interactive"`` (the default) skips the
        snapshot for one-shot ``-m`` invocations — those have no "next turn"
        to inject recovery into, so paying the snapshot cost there is just
        deadweight. ``"always"`` opts in regardless; ``"never"`` opts out
        regardless."""
        if policy == "never":
            return False
        if policy == "always":
            return True
        return interactive  # policy == "interactive"

    def _turn_checkpoint(self) -> "CheckpointService | None":
        """The shadow-git service for the directory the running turn works in.

        Keyed on the bound directory rather than on a session key: ``run_turn``
        already resolved and validated it once for the whole turn, and
        re-resolving here would both repeat the sandbox-mount check and race a
        workdir override changed mid-turn (the web UI can rewrite it).

        Nothing bound means no turn is running, so there is nothing to
        snapshot and the answer is ``None``. Deriving a directory here instead
        would reintroduce both hazards the binding exists to avoid -- a
        mount-check refusal raised at end of turn, and an empty session key
        materializing the ``ws/_`` fallback directory.

        One service per directory: CheckpointService puts its git dir inside
        the directory it protects, so sharing one across working directories
        would cross-contaminate their edited-file sets.
        """
        target = workdir.current()
        if target is None or not self._checkpoint_enabled:
            return None
        if target not in self._checkpoints:
            from raven.agent.loop.checkpoint import CheckpointService

            try:
                self._checkpoints[target] = CheckpointService(
                    target,
                    shadow_dir=self.runtime_config.checkpoint.shadow_dir,
                )
            except ValueError as exc:
                logger.warning("runtime.checkpoint disabled for {} -- {}", target, exc)
                self._checkpoints[target] = None
        return self._checkpoints[target]

    def _stash_recovery(self, session_key: str, outcome: "TurnOutcome") -> None:
        """Remember an interrupted turn's snapshot so the next turn in this
        session gets a recovery prompt. No-op unless checkpoint is enabled
        and the turn was actually interrupted with something to recover.

        Status filter is intentional: only ``"interrupted"`` triggers a
        recovery prompt. ``"error"`` turns still get a per-turn shadow
        commit (useful for audit), but they don't usually have a partial-
        edits trajectory to resume (provider 400 etc.) and surfacing
        "Files modified last turn" for them would be misleading.
        """
        if self._turn_checkpoint() is None or outcome.status != "interrupted":
            return
        if outcome.edited_files or outcome.checkpoint_id:
            self._pending_recovery[session_key] = {
                "checkpoint_id": outcome.checkpoint_id,
                "files": outcome.edited_files,
            }

    def _inject_recovery_block(self, session_key: str, messages: list[dict]) -> None:
        """Prepend a recovery notice to the current user message when the
        previous turn for this session was interrupted. Consumed once on
        successful injection; if the current message's content has an
        unexpected shape (None / dict / etc.) the pending entry is kept so
        a later assembly with a normal content can still inject it."""
        recovery = self._pending_recovery.get(session_key)
        if not recovery or not messages:
            return
        last = messages[-1]
        if last.get("role") != "user":
            # Last message isn't the user turn — keep the recovery pending so
            # the next assembly (which does end with the user message) injects it.
            return
        content = last.get("content")
        files = recovery.get("files") or []
        cid = recovery.get("checkpoint_id")
        lines = ["[Recovery — the previous turn was interrupted before finishing]"]
        if files:
            lines.append("Files modified last turn: " + ", ".join(files))
        if cid:
            lines.append(f"Checkpoint: {cid}")
        lines.append("Verify the current state of these files before continuing.")
        block = "\n".join(lines)
        # Mutate first, pop second — atomic from the caller's perspective. If
        # we can't safely write to ``content`` (unknown shape) the recovery
        # stays pending instead of being silently dropped on the floor.
        if isinstance(content, str):
            last["content"] = f"{block}\n\n{content}"
        elif isinstance(content, list):
            last["content"] = [{"type": "text", "text": block}] + content
        else:
            return  # unexpected content shape → keep pending
        self._pending_recovery.pop(session_key, None)

    @trace.instrument("memory.feedback", extract=semconv.memory_feedback)
    async def _dispatch_backend_feedback(
        self,
        session_key: str,
        injected_skill_ids: list[str] | None,
        used_skill_ids: list[str] | None = None,
    ) -> None:
        """FB-1: forward source-qualified skill-usage signals to
        :meth:`MemoryBackend.feedback`.

        Skill IDs surface with a ``<source>/<native_id>`` prefix
        (``local/git-resolver`` / ``mass/abc`` / ``everos/xyz``).
        Only the ``everos/`` prefix is forwarded — static libraries
        (``local`` / ``mass``) have no feedback channel; the dispatcher
        is silent for them (no warning, just skipped). Unprefixed legacy
        ids (e.g. raw skill names emitted by the pre-SkillForgeRouter
        ``SkillService.select`` path) are also skipped — they predate
        the qualified-id convention and there's no safe routing target.

        No-ops when:
        - ``self.backend is None`` (no plugin wired)
        - No qualified-id matches the ``everos/`` prefix
        - The injected + used lists are both empty / None

        Exceptions from :meth:`backend.feedback` are caught + logged.
        The host MUST NOT abort the after-turn pipeline because a
        plugin's feedback handler raised — feedback is best-effort
        telemetry, not load-bearing state.
        """
        if self.backend is None:
            return
        injected_native = _filter_qualified_ids(injected_skill_ids, "everos")
        used_native = _filter_qualified_ids(used_skill_ids, "everos")
        if not injected_native and not used_native:
            return
        signals = {
            "kind": "skill_usage",
            "session_id": session_key,
            "injected": injected_native,
            "used": used_native,
        }
        try:
            await self.backend.feedback(signals)
        except Exception:
            logger.exception(
                "backend.feedback failed for session {}; signals dropped",
                session_key,
            )

    @trace.instrument("memory.store", extract=semconv.memory_store)
    async def _dispatch_backend_store(
        self,
        session_key: str,
        messages_slice: list[dict],
    ) -> None:
        """AG-1: forward a turn's messages to the plugin :class:`MemoryBackend`.

        Third peer step in the after-turn pipeline alongside
        ``context_engine.after_turn`` (engine-side bookkeeping) and
        ``memory.maybe_consolidate`` (raven-core compaction). When no
        backend was wired (``self.backend is None``), this is a no-op so
        legacy callsites that never registered a plugin behave
        identically to pre-AG-1.

        Exceptions raised by the backend are logged and swallowed —
        the AgentLoop's main pipeline must never abort because the
        plugin-side index failed; the turn is already saved to the
        session log and the host's MEMORY.md compaction will still run.
        """
        if self.backend is None:
            return
        if not messages_slice:
            return

        async def _store() -> None:
            try:
                await self.backend.store(session_key, messages_slice)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001 - the turn must survive a failed index
                logger.exception(
                    "backend.store failed for session {}; turn data preserved in session log, "
                    "plugin-side indexing skipped",
                    session_key,
                )
                self._note_memory_failure(str(e))
            else:
                self._note_memory_ok()

        task = asyncio.create_task(_store())
        self._store_inflight.add(task)
        task.add_done_callback(self._store_inflight.discard)
        # Deliberately not cancelled on timeout: the point is to stop *waiting*,
        # not to abandon the write. A turn that indexes quickly still does so
        # inline, which keeps ordering intact in the common case.
        await asyncio.wait({task}, timeout=_STORE_TURN_BUDGET_S)

        if len(self._store_inflight) > _STORE_MAX_INFLIGHT:
            # Backpressure rather than unbounded growth: a service slow enough
            # to accumulate this many outstanding writes is one whose queue
            # should stop growing, not one to keep feeding.
            #
            # Bounded by the turn's own budget, and that bound is the whole
            # point. Unbounded, this waited on the slowest outstanding write
            # instead -- and since flush_every_turns defaults to 1, every
            # interactive turn is a final flush carrying the six-minute
            # extraction budget, so reaching the cap stalled a turn for
            # minutes. That is the stall this method exists to remove.
            await asyncio.wait(set(self._store_inflight), timeout=_STORE_TURN_BUDGET_S)
            if len(self._store_inflight) > _STORE_MAX_INFLIGHT:
                # Still saturated. The queue is what gives, not the turn: this
                # write is dropped and said out loud at teardown, rather than
                # held open behind writes that are already over their time.
                task.cancel()
                self._store_inflight.discard(task)
                self._store_dropped += 1
                logger.warning(
                    "backend.store dropped for session {}: {} writes still in flight after {}s",
                    session_key,
                    len(self._store_inflight),
                    _STORE_TURN_BUDGET_S,
                )

    async def drain_backend_stores(self, timeout: float = _STORE_DRAIN_BUDGET_S) -> None:
        """Let detached writes finish before the process goes away.

        Writes that outran their turn budget are still in flight. Exiting on top
        of them loses exactly the turns that were slowest to index, which is a
        silent and biased kind of data loss.
        """
        pending = {t for t in self._store_inflight if not t.done()}
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        if self._store_dropped:
            logger.warning(
                "{} turn(s) were not indexed: the memory service never caught up",
                self._store_dropped,
            )

    _MEMORY_FAILURES_BEFORE_ALARM = 3
    """How many consecutive store failures make this a standing fault rather
    than a blip. One failed write is a network hiccup nobody needs told about;
    three in a row is a backend that is not coming back on its own."""

    def _note_memory_ok(self) -> None:
        """A successful store clears a standing fault, and says so once."""
        if self._memory_fail_streak >= self._MEMORY_FAILURES_BEFORE_ALARM:
            logger.info("backend.store recovered; long-term memory is being written again")
            self._emit_mcp_event("memory.health", {"ok": True, "error": None})
        self._memory_fail_streak = 0

    def _note_memory_failure(self, detail: str) -> None:
        """Escalate a run of failures from the log to the client.

        The swallow above is right -- a failed index must not cost the user
        their reply -- but on its own it made a permanently broken backend
        indistinguishable from a working one: writes and recalls can fail every
        turn for days with the only trace in a log nobody is reading.
        """
        self._memory_fail_streak += 1
        if self._memory_fail_streak == self._MEMORY_FAILURES_BEFORE_ALARM:
            logger.error(
                "backend.store has failed {} times in a row; long-term memory is not being written",
                self._memory_fail_streak,
            )
            self._emit_mcp_event("memory.health", {"ok": False, "error": detail[:400]})

    def _collect_injected_skill_ids(
        self,
        selected: list[Any] | None,
    ) -> list[str]:
        """Combine selector top-K + always-skills into a deduplicated id list.

        ``selected`` is the :class:`SkillMeta` list returned by the
        retrieval selector for this turn (or ``None`` when the selector
        is disabled / returned empty). always-skills are pulled from
        :class:`LocalSkillCatalog` since they are unconditionally rendered
        regardless of the selector's output.

        Returns ids canonicalized to ``{source}/{stable_key}`` form.
        ``stable_key`` is whatever the source uses for unambiguous
        addressing — the sqlite ``skills.id`` for ``everos`` (which
        allows duplicate names) and the directory / display name
        elsewhere. Different SkillMeta producers populate ``meta.id``
        inconsistently (file registry: bare key or ``{source}/{key}``;
        sqlite store: ``{source}/{key}``); this function normalizes them
        to a single shape so the after-turn ``backend.feedback`` signal
        can route them uniformly.
        """
        skills_svc = getattr(self.context, "skills", None)
        if skills_svc is None:
            return []

        seen: set[str] = set()
        ids: list[str] = []

        def _add(meta: Any) -> None:
            src = getattr(meta, "source", None)
            mid = getattr(meta, "id", None)
            if not src or not mid:
                return
            canonical = mid if "/" in mid else f"{src}/{mid}"
            if canonical not in seen:
                seen.add(canonical)
                ids.append(canonical)

        def _add_raw_id(qid: str) -> None:
            if qid and qid not in seen:
                seen.add(qid)
                ids.append(qid)

        # Prefer the AssembledContext metadata the unified engine
        # populated from its SkillForgeRouter. Those ids are already
        # source-qualified (``local/x`` / ``mass/y`` / ``everos/z``) so
        # they bypass the SkillMeta-canonicalization path. Always-skills
        # get folded in afterwards because they live outside
        # SkillForgeRouter's selection.
        if self._last_injected_skill_ids is not None:
            for qid in self._last_injected_skill_ids:
                _add_raw_id(qid)
        else:
            for meta in selected or []:
                _add(meta)
        try:
            always = skills_svc.get_always_skills()
        except Exception:
            always = []
        for meta in always:
            _add(meta)
        return ids

    async def _start_executor(self) -> None:
        """Idempotent: start the sandbox executor once before first use."""
        async with self._executor_start_lock:
            if self._executor_started:
                return
            stack = AsyncExitStack()
            try:
                await stack.__aenter__()
                await stack.enter_async_context(self._executor)
            except Exception:
                await stack.aclose()
                raise
            self._executor_stack = stack
            self._executor_started = True

    async def _start_debug_server(self) -> None:
        """Start the sandbox debug socket server if debug mode is enabled."""
        cfg = self._sandbox_config
        if cfg is None or not cfg.debug.enabled:
            return
        if cfg.backend == "none":
            logger.warning(
                "sandbox.debug.enabled=true is ignored because backend='none' (no boxlite runtime is active)"
            )
            return
        try:
            from raven.config.paths import get_data_dir
            from raven.sandbox.debug_server import SandboxDebugServer

            socket_path = SandboxDebugServer.resolve_socket_path(cfg.debug.socket, get_data_dir())
            server = SandboxDebugServer(
                socket_path=socket_path,
                owned_ids=self._owned_ids,
                max_message_bytes=cfg.debug.max_message_bytes,
            )
            await server.start()
            self._debug_server = server
        except Exception as exc:
            # The user explicitly opted in to debug mode; failing silently here
            # leaves them puzzled when `raven sandbox` later says "socket not
            # found". Log loud so the reason is visible.
            logger.error("Failed to start sandbox debug server: %s", exc)

    async def close_executor(self) -> None:
        """Tear down the sandbox executor."""
        if self._debug_server is not None:
            try:
                await self._debug_server.stop()
            except Exception as exc:
                logger.warning("Error stopping sandbox debug server: %s", exc)
            self._debug_server = None
        if self._executor_stack:
            try:
                await self._executor_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass
            self._executor_stack = None
        self._executor_started = False
        if self._skill_hub_client is not None:
            try:
                await self._skill_hub_client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing Skill Hub client: %s", exc)
            self._skill_hub_client = None

    async def _mcp_executor(self):
        """The sandbox executor an MCP connect should run under, started."""
        await self._start_executor()
        return self._executor

    def set_mcp_event_sink(self, sink) -> None:
        """Late-bind where per-server MCP events go (same shape as the DAG sink).

        ``sink`` is an async callable ``(method, params)``. Without it the manager
        still works, but every state change is silent -- and a client waiting on
        ``mcp.status`` / ``oauth.pending`` has no way to learn that an OAuth
        connect finished, which is most of what makes a browser round-trip
        completable at all.
        """
        self._mcp_event_sink = sink

    def _emit_mcp_event(self, method: str, params: dict) -> None:
        """Fire-and-forget bridge: the manager's callbacks are sync, the sink is not."""
        sink = self._mcp_event_sink
        if sink is None:
            return
        try:
            asyncio.get_running_loop().create_task(sink(method, params))
        except RuntimeError:
            pass  # no loop (a sync CLI path): nothing is listening anyway

    @property
    def mcp_manager(self) -> "MCPConnectionManager":
        """The per-server connection lifecycle, created on first use.

        Lazy rather than built in ``__init__`` because a loop with no MCP servers
        configured should not carry one, and because the registry it writes into
        is assembled after ``__init__`` in some entry points.
        """
        if self._mcp_manager is None:
            from raven.mcp.manager import MCPConnectionManager

            self._mcp_manager = MCPConnectionManager(
                self.tools,
                # MCP servers can register a name that is also blacklisted (e.g.
                # ``mcp_<server>_search``), and the manager records what survived,
                # so the blacklist has to be re-applied on every connect.
                post_connect=self._after_mcp_connect,
                on_state_change=lambda snap: self._emit_mcp_event("mcp.status", snap),
                on_oauth_event=lambda event, payload: self._emit_mcp_event(event, payload),
            )
        return self._mcp_manager

    def _after_mcp_connect(self) -> None:
        """Re-apply the blacklist, then re-gate the MCP meta-tools.

        Runs on every connect, not only the first: an MCP server can register a
        name that is also in ``disabled_tools``, and a server that just arrived
        may be the first one to serve resources or prompts.
        """
        self._report_reserved_disabled_tools()
        self._sync_mcp_meta_tools()

    def _sync_mcp_meta_tools(self) -> None:
        """Register the resource / prompt meta-tools iff some server serves them.

        Five schemas that no deploy without MCP should pay for, and that a deploy
        whose servers offer only tools should not pay for either -- most servers
        offer only tools, and advertising ``read_mcp_resource`` to them spends
        context on calls that can only fail.

        Gating moves the tool array when a server connects or disconnects, which
        costs the prompt-cache prefix. That is not a new cost: the server's own
        tools appear and disappear at exactly those moments, so the array was
        already moving. What it buys is that the array does not carry these five
        the rest of the time.

        Called after every connect and after every config apply, because both can
        change the answer -- and idempotent, so calling it when nothing moved
        registers and unregisters nothing.

        Idempotence is the reason these five names are not the operator's to
        switch off. The predicate is ``all(...)`` over the set, so one name
        missing reads as "the set is not installed" and puts the whole set back;
        anything else that removes a single member turns every connect into an
        unregister-and-re-register of all of them. ``_withheld_tool_names``
        therefore skips them by name, which makes this method their sole owner:
        they exist exactly while a connected server serves the primitive.
        """
        manager = self._mcp_manager
        if manager is None:
            return
        from raven.mcp.prompts import PROMPT_TOOL_NAMES, prompt_tools
        from raven.mcp.resources import RESOURCE_TOOL_NAMES, resource_tools

        for primitive, names, build in (
            ("resources", RESOURCE_TOOL_NAMES, lambda: resource_tools(manager, workspace=self.workspace)),
            ("prompts", PROMPT_TOOL_NAMES, lambda: prompt_tools(manager)),
        ):
            wanted = bool(manager.servers_offering(primitive))
            present = all(self.tools.has(n) for n in names)
            if wanted and not present:
                for tool in build():
                    self.tools.register(tool)
                logger.info("MCP: {} meta-tools registered", primitive)
            elif not wanted and any(self.tools.has(n) for n in names):
                for n in names:
                    self.tools.unregister(n)
                logger.info("MCP: {} meta-tools withdrawn -- no server offers them", primitive)

    def _mcp_tool_notices(self) -> list[str]:
        """Host facts about MCP tools the definitions cannot carry.

        One line per enabled server sitting in ``auth_required``: its tools are
        not in the definitions at all, so without this the model reads an
        unauthorized plugin as a capability that does not exist and says so.
        Rendered into the runtime-context block, not the system prompt -- the
        set changes turn to turn and must never be cached with the prefix.
        """
        # getattr: the engine factory takes this callable during __init__,
        # before the manager attribute is assigned further down.
        mgr = getattr(self, "_mcp_manager", None)
        if mgr is None:
            return []
        # Stated as fact, not as a directive: this block's own header says
        # "metadata only, not instructions", and the model is told to treat it
        # that way -- an imperative here would be either ignored or a fence
        # violation. The fact alone is enough to stop it reporting a missing
        # capability.
        return [
            f"MCP plugin '{snap['name']}': installed, awaiting authorization. Its tools are "
            f"absent from this turn's definitions until it is authorized -- by the `plugin` "
            f"tool's authorize action, or by the user in the plugin panel."
            for snap in mgr.status()
            if snap["state"] == "auth_required" and snap.get("enabled", True)
        ]

    async def _connect_mcp(self, *, wait: float | None = None) -> None:
        """Connect to configured MCP servers (one-time, lazy).

        ``wait`` bounds how long the CALLER blocks, not how long the connect
        gets: past it the sync keeps running and its tools land in the registry
        for the next turn. The manager already refuses to wait on a person --
        a connect that reaches the browser-authorization step is marked
        ``auth_required`` and the sync moves on without it -- so this bound
        only covers real handshakes, and it matches their own budget. It
        exists because one wedged sync once held a message for 10m35s, and a
        turn must never inherit a wait like that whatever the cause.

        Left unbounded for a caller that has nothing else to do (``run()``),
        which also keeps SandboxInitError reaching its handler there.
        """
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        # Set flag synchronously before the first await — asyncio is single-threaded so no
        # context switch occurs here; a lock is not needed for this mutual-exclusion pattern.
        self._mcp_connecting = True

        async def _sync() -> None:
            try:
                # Sets ``_mcp_connected`` itself, so every path that brings MCP
                # up agrees on the flag rather than only this one.
                await self.apply_mcp_config(self._mcp_servers)
            finally:
                self._mcp_connecting = False

        if wait is None:
            await _sync()
            return

        # Shielded, so the timeout below leaves the connect running rather than
        # cancelling it mid-handshake -- a cancelled sync leaves its remaining
        # servers marked `connecting`, which no later sync retries.
        task = asyncio.ensure_future(_sync())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except TimeoutError:
            logger.info(
                "MCP: still connecting after {:.0f}s; starting the turn without the servers "
                "that have not finished (they join the next one)",
                wait,
            )
            # Nobody awaits the task now, so it has to report for itself.
            task.add_done_callback(_log_late_mcp_sync)

    def mcp_config_changed(self, cfg_servers: dict) -> bool:
        """Whether :meth:`apply_mcp_config` would do anything, without doing it.

        The gate in front of every caller that can fire on a timer -- the reload
        RPC, a config-file watch. Answering it stays in memory, so an unchanged
        config never reaches a transport.
        """
        return self.mcp_manager.config_changed(cfg_servers)

    async def apply_mcp_config(self, cfg_servers: dict) -> "ApplyReport":
        """Reconcile live MCP connections with ``cfg_servers``.

        The entry point for everything that changes the server set while the loop
        runs -- a market install, an uninstall, an enable/disable, a config edit.
        Each server owns its own transport, so one can be attached or detached
        without restarting raven, and a disabled server is disconnected here
        rather than left running with its tools registered.

        Reconciling, not restarting: servers whose config did not change are not
        touched, so this is safe to call while turns are running.
        """
        self._mcp_servers = cfg_servers
        # The blacklist is re-applied by the manager's post_connect hook, on every
        # connect rather than only the first -- an MCP server can register a name
        # that is also in disabled_tools.
        report = await self.mcp_manager.apply_config(cfg_servers, executor_provider=self._mcp_executor)
        # After the apply, not only after a connect: a *detach* can take the last
        # server that served resources with it, and no connect fires for that.
        self._sync_mcp_meta_tools()
        # Whatever brought MCP up, it is up. Left unset, the one-shot lazy
        # connect would still run later and re-walk every server this apply
        # already attached, and the surfaces that read this flag as "MCP is
        # live" would report a working server as disconnected.
        self._mcp_connected = True
        return report

    def _register_real_deep_research(self, cfg: DeepResearchToolConfig) -> None:
        """Build the working deep_research tool (+ async manager) and register it.
        Shared by initial registration and mid-session promotion."""
        self.deep_research_manager = DeepResearchManager(cfg, workspace=self.workspace, proxy=self.web_proxy)
        # Inherit the gateway's async-delivery handle if it was wired before this
        # manager existed (i.e. a promotion after startup), so a channel keeps the
        # async path instead of falling back to a blocking synchronous run.
        if self._deep_research_submit is not None:
            self.deep_research_manager.set_submit(self._deep_research_submit)
        tool = DeepResearchTool(cfg, workspace=self.workspace, proxy=self.web_proxy, manager=self.deep_research_manager)
        # Inherit the deep-vs-regular ask broker too, else the promoted tool would
        # silently skip the ask and run the paid engine unprompted.
        if self._deep_research_broker is not None:
            tool.set_broker(self._deep_research_broker)
        self.tools.register(tool)

    def set_deep_research_submit(self, submit: Callable[[Any], Any]) -> None:
        """Wire the async-delivery submit handle (gateway only). Stored on the loop
        and applied to the current manager, so a later promotion inherits it too."""
        self._deep_research_submit = submit
        if self.deep_research_manager is not None:
            self.deep_research_manager.set_submit(submit)

    def set_deep_research_broker(self, broker: QuestionBroker) -> None:
        """Wire the deep-vs-regular ask broker (TUI/gateway). Stored on the loop and
        applied to the currently-registered deep_research tool, so a tool built
        later by promotion inherits it too (mirrors ``set_deep_research_submit``)."""
        self._deep_research_broker = broker
        if (tool := self.tools.get("deep_research")) is not None and hasattr(tool, "set_broker"):
            tool.set_broker(broker)

    def _maybe_promote_deep_research(self) -> None:
        """Swap the offer stand-in for the working tool once a key appears on disk,
        so a mid-session ``raven deep-research enable`` is picked up on the next
        turn without a restart. Called from ``run_turn`` before the per-turn tool
        wiring, so the promoted tool gets this turn's stream callback and routing.

        Re-reads config (the in-memory copy is fixed at startup); a corrupt config
        must not fail the turn, so a read error just skips promotion. The promoted
        manager inherits the gateway's async-delivery handle via
        ``set_deep_research_submit``, so a channel keeps the async path."""
        if not isinstance(self.tools.get("deep_research"), DeepResearchOfferTool):
            return
        from raven.config.loader import ConfigReadError
        from raven.config.schema import DeepResearchToolConfig
        from raven.config.update_tools import get_deep_research

        try:
            cfg = DeepResearchToolConfig(**get_deep_research(redact=False))
        except ConfigReadError as exc:
            logger.warning("deep_research: skipping promotion, config unreadable: {}", exc)
            return
        if not DeepResearchTool.is_configured(cfg):
            return
        self.deep_research_config = cfg
        self._register_real_deep_research(cfg)
        logger.info("deep_research: promoted offer stand-in to the working tool (key configured mid-session)")

    def session_workdir(self, session_key: str) -> Path:
        """The directory this session's turn works in, ready to be used.

        The mount check runs before the directory is created, so a refusal
        leaves nothing behind on disk.
        """
        resolved = self.peek_session_workdir(session_key)
        self.check_workdir_mounted(resolved, session_key)
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    @property
    def deliverables(self) -> "DeliverableStore | None":
        """The registry `deliver_files` writes into, for callers that only read
        it -- the RPC surface that answers what a conversation handed over."""
        return self._deliverables

    def peek_session_workdir(self, session_key: str) -> Path:
        """Where this session would work, with no side effect and no refusal.

        The read-only form, for a caller that only reports the path. Without a
        resolver the loop keeps its pre-split behaviour: every session shares
        ``self.workspace``.
        """
        if self._workdir_resolver is None:
            return self.workspace
        return self._workdir_resolver.resolve(session_key, create=False)

    def check_workdir_mounted(self, path: Path, session_key: str | None = None) -> None:
        """Refuse a working directory a sandboxed run could not see.

        A VM's volumes are fixed when the box is created, so a directory
        outside the mount cannot be served by adding one later.
        """
        if self._workdir_resolver is None or not self._executor.is_sandboxed:
            return
        root = self._workdir_resolver.mount_root()
        if workdir.is_within(path, root):
            return
        subject = f"session {session_key} is pinned to {path}" if session_key else str(path)
        raise ValueError(
            f"{subject}, which is outside the sandbox mount {root}; "
            "clear the override or restart with a wider workspace root"
        )

    def _set_tool_context(
        self, channel: str, chat_id: str, message_id: str | None = None, session_key: str | None = None
    ) -> None:
        """Update context for all tools that need routing info."""
        # Before the per-tool contexts because the schema is assembled from this
        # same turn: a channel-bound tool is withheld by the registry, not by
        # its own refusal (see ToolRegistry.set_channel).
        self.tools.set_channel(channel)
        for name in (
            "message",
            "spawn",
            "cron",
            "deep_research",
            "run_subagent_dag",
            "deliver_files",
            "dag_status",
            "cancel_dag",
        ):
            if tool := self.tools.get(name):
                if not hasattr(tool, "set_context"):
                    continue
                if name == "message":
                    tool.set_context(channel, chat_id, message_id)
                elif name in (
                    "spawn",
                    "deep_research",
                    "run_subagent_dag",
                    "deliver_files",
                    "dag_status",
                    "cancel_dag",
                ):
                    tool.set_context(channel, chat_id, session_key or f"{channel}:{chat_id}")
                else:
                    tool.set_context(channel, chat_id)
        # Not in the name list above: the playbook executor's DAG tool is a
        # private unregistered instance, reachable only through the runtime.
        # Recorded here -- the one place every origin passes -- because a
        # CRON/SENTINEL turn can call load_playbook too, and its announce must go
        # to that turn's own address rather than the last human conversation's.
        if self._playbooks is not None:
            self._playbooks.set_context(
                channel=channel, chat_id=chat_id, session_key=session_key or f"{channel}:{chat_id}"
            )

    def dag_tools(self) -> list[Any]:
        """Every live graph tool: the registered one, plus the playbook engine's.

        Two instances exist by design -- one on the model's tool table, one
        private to the playbook executor, because a ``mode: dag`` playbook is
        dispatched by the engine rather than by the model. They share the agent
        table, the dispatch gate, the quota and the announce path, and everything
        a *consumer* asks about a run has to be shared the same way: whether it
        is live, cancelling it, where its progress goes. Reaching only for the
        registered instance answers "not happening" to all three for a run a
        playbook started -- no progress events reach the page, so the graph never
        appears in the conversation at all; the cancel button reports False; and
        the instance rows read the handle as finished.

        Which of the two dispatched a run is not a distinction any consumer
        should be able to observe, so the list is what they are given.
        """
        tools = []
        if (registered := self.tools.get("run_subagent_dag")) is not None:
            tools.append(registered)
        if self._playbooks is not None and (private := self._playbooks.dag_tool) is not None:
            tools.append(private)
        return tools

    def active_dag_run_ids(self) -> set[str]:
        """Run ids in flight across every graph tool instance."""
        live: set[str] = set()
        for tool in self.dag_tools():
            try:
                live.update(tool.active_run_ids())
            except Exception:  # noqa: BLE001 - liveness is advisory, never fatal
                continue
        return live

    def cancel_dag_run(self, run_id: str) -> bool:
        """Stop one in-flight run, whichever instance owns it."""
        return any(tool.request_cancel(run_id) for tool in self.dag_tools())

    def dag_control_reachable(self) -> bool:
        """Whether the schema-hidden dag control tools have a call path.

        The graph tool advertises ``dag_status`` / ``cancel_dag`` in its
        acceptance text, and this is the gate that keeps the advertisement
        honest: it answers the same question ToolSearchStrategy answers when it
        assembles the per-turn tool list, through the controller's single
        predicate rather than a second copy of the fold condition.
        """
        if self.tool_search_controller is None:
            return False
        return self.tool_search_controller.tool_call_available()

    def set_dag_progress_sink(self, sink) -> None:
        """Late-bind the graph tools' progress sink (host wires it to the web
        channel's emitter so a run's events reach the web UI).

        Every instance, not just the registered one -- see :meth:`dag_tools`.
        """
        self._dag_progress_sink = sink
        for tool in self.dag_tools():
            if hasattr(tool, "set_progress_sink"):
                tool.set_progress_sink(sink)

    def set_skills_sink(self, sink) -> None:
        """Late-bind the sink that reports SkillForge-injected skills to the web
        UI (host wires it to the web channel's emitter). ``sink`` is an async
        callable ``(conversation, name, payload)`` matching the DAG sink."""
        self._skills_sink = sink

    async def _emit_injected_skills(self, session_key: str) -> None:
        """Push this turn's SkillForge-injected skills to the web UI's skill
        panel as a ``skills_injected`` custom event.

        The id's prefix is the *addressing* namespace, which is ``local`` for
        every on-disk skill, so origin comes from
        ``_last_injected_skill_sources`` (the registry source the engine
        reported) and falls back to the prefix only when that is absent. No-op
        when there is no sink (CLI/IM) or nothing was injected this turn."""
        if self._skills_sink is None:
            return
        ids = self._last_injected_skill_ids or []
        if not ids:
            return
        skills = []
        for qid in ids:
            prefix, _, name = str(qid).partition("/")
            skills.append(
                {
                    "id": str(qid),
                    "source": self._last_injected_skill_sources.get(str(qid)) or prefix,
                    "name": name or str(qid),
                    "kind": "injected",
                }
            )
        await self._emit_skills(session_key, skills)

    async def _emit_skills(self, session_key: str, skills: list[dict[str, Any]]) -> None:
        """Send one ``skills_injected`` payload, swallowing any failure."""
        try:
            await self._skills_sink(session_key, "skills_injected", {"skills": skills})
        except Exception:  # never break a turn on a telemetry/UI push
            logger.exception("skills_injected emit failed")

    async def _report_skill_read(self, session_key: str, tool_name: str, args: dict[str, Any]) -> None:
        """Report a skill the model loaded itself to the web UI's skill panel.

        ``read_skill`` / ``use_skill`` are how a skill advertised by description
        alone actually gets loaded -- notably the builtin orchestration guide,
        whose id the DAG tool names in its own description. Those never pass
        through SkillForge injection, so without this the panel shows nothing
        for a turn that demonstrably used a skill.

        The model passes the addressing id (``local/<name>``); the registry is
        what knows the real source. An unresolvable id is still reported, with
        the addressed namespace as source, rather than dropped -- the call
        happened."""
        if self._skills_sink is None:
            return
        qid = str(args.get("skill_id") or "").strip()
        if not qid:
            return
        namespace, native = qid.partition("/")[0], qid.partition("/")[2]
        registry = getattr(getattr(self.context, "skills", None), "registry", None)
        try:
            # The same split and resolver ``read_skill`` itself uses, so the id
            # grammar -- including "a bare id addresses the Hub" -- and "local
            # spans every on-disk source" stay defined in exactly one place.
            from raven.agent.tools.skill_hub import _lookup_on_disk, _split_qualified_id

            namespace, native = _split_qualified_id(qid)
            meta = _lookup_on_disk(registry, namespace, native)
            source = str(meta.source) if meta is not None and getattr(meta, "source", None) else namespace
        except Exception:  # noqa: BLE001 - a registry hiccup must not lose the report
            logger.debug("skill source lookup failed for %s", qid)
            source = namespace
        name = native or qid
        await self._emit_skills(
            session_key,
            [{"id": qid, "source": source, "name": name, "kind": tool_name}],
        )

    def apply_agents(self, configs: list) -> None:
        """Hot-apply new agent config to the live runtime (P4), with no restart.

        One call, one table: ``spawn`` and the DAG tool read the same
        ``AgentRegistry``, so applying to the manager is applying to both. This
        used to refresh two independently-built maps through two setters that each
        skipped a bad entry on its own, which made "the manager has hermes, the DAG
        tool does not" a reachable state.

        No registration branch either -- the DAG tool is registered at startup
        whatever config holds, because the built-in rows are always on the table.
        """
        self._agent_configs = list(configs)
        self.subagents.apply_agents(configs)

    # The pre-``agents`` spelling, still called by the RPC config handlers and the
    # web config surface. Kept as a name only: both apply the whole list.
    apply_third_party_subagents = apply_agents

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content.

        Paired blocks are removed. What is left is then checked for being
        nothing but tag debris: when a backend inlines its reasoning and the
        turn is cut off inside it, content arrives as a lone closing tag with
        no opener to pair against, so the substitution above finds nothing and
        an eleven-character string reads as a real answer. Recovery is skipped
        and the tag is what the user sees.

        The check is on residue, not on vendor spellings -- it does not matter
        which prefix a backend picked. Text that merely mentions a tag keeps
        its other words and is returned untouched.
        """
        if not text:
            return None
        cleaned = strip_think_blocks(text)
        if is_only_think_debris(cleaned):
            return None
        return cleaned or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _build_usage_snapshot(response, model: str, session_key: str) -> "UsageSnapshot":
        """Build a UsageSnapshot from an LLMResponse for TokenWise after-hooks.

        Normalizes input_tokens to *fresh* (non-cached) prompt tokens. The
        ``prompt_tokens`` field has two conventions in the wild:
          - Anthropic native: fresh-only (cache_read/write are separate counts)
          - OpenRouter/LiteLLM: total (already includes cache_read + cache_write)
        We detect by inequality and subtract when needed so downstream code
        (pricing, telemetry) sees a single consistent semantics.
        """
        from raven.token_wise.base import UsageSnapshot
        from raven.token_wise.pricing import estimate_cost_usd

        usage = response.usage or {}
        prompt_t = int(usage.get("prompt_tokens", 0) or 0)
        out_toks = int(usage.get("completion_tokens", 0) or 0)
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)

        # Normalize to fresh-only.
        if prompt_t >= cache_read + cache_write and (cache_read + cache_write) > 0:
            fresh = prompt_t - cache_read - cache_write
        else:
            fresh = prompt_t

        # Left as None for a plan-billed provider: the field is optional all the
        # way to the status bar, which renders it only when it is a number.
        cost = estimate_cost_usd(model, fresh, out_toks, cache_read, cache_write)
        return UsageSnapshot(
            model=model,
            input_tokens=fresh,
            output_tokens=out_toks,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_cost_usd=cost,
            session_key=session_key or None,
        )

    @trace.instrument("llm.call", extract=semconv.llm_call_stream)
    async def _llm_call_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Stream LLM response via ``provider.chat_stream`` + accumulate to LLMResponse.

        Per design.md §D3: when a turn caller wires ``on_token_delta``, AgentLoop
        diverts here instead of to ``chat_with_retry``. The work itself lives in
        ``raven.agent.loop.streaming`` — a sub-agent backend answering a direct
        chat drives the same call and must not drift from it. This method stays
        as the loop's own entry point (its span, its reconnect budget).

        Generation parameters are deliberately not passed on: the main loop has
        always let ``chat_stream``'s own signature defaults stand here. See
        ``generation_kwargs`` for the callers that cannot.
        """
        return await stream_llm_call(
            self.provider,
            messages=messages,
            tools=tools,
            model=model,
            on_token_delta=on_token_delta,
            on_reasoning_delta=on_reasoning_delta,
            max_reconnects=self._MAX_STREAM_RECONNECTS,
        )

    @classmethod
    def _emergency_shrink(cls, messages: list[dict]) -> tuple[list[dict], int]:
        """Elide the bodies of older tool-result messages to fit a tighter window.

        Mid-turn context overflow is almost always accumulated tool output, so
        replacing the content of all but the most recent few ``role="tool"``
        messages with a short placeholder frees the most tokens while keeping
        system / user / assistant reasoning intact. Deterministic, no extra LLM
        call. Returns ``(new_messages, num_elided)``; ``num_elided == 0`` means
        there was nothing worth eliding (caller should not bother retrying).
        """
        messages, elided = cls._elide_older_images(messages)

        placeholder = "[earlier tool output elided to fit the context window]"
        tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_idxs) <= cls._SHRINK_KEEP_RECENT_TOOL_RESULTS:
            return messages, elided
        elide = set(tool_idxs[: -cls._SHRINK_KEEP_RECENT_TOOL_RESULTS])
        shrunk: list[dict] = []
        for i, m in enumerate(messages):
            if i in elide and m.get("content") and m.get("content") != placeholder:
                clean = dict(m)
                clean["content"] = placeholder
                shrunk.append(clean)
                elided += 1
            else:
                shrunk.append(m)
        return shrunk, elided

    @staticmethod
    def _demote_tool_images(messages: list[dict]) -> tuple[list[dict], int]:
        """Move images out of tool results into a following user message.

        The recovery for an endpoint that refuses a picture in ``role="tool"``.
        Produces exactly the message list a ``False`` capability verdict would
        have built in the first place, so the retry lands on the already-tested
        placeholder path rather than inventing a third shape.

        A run of consecutive tool messages is one batch answering one assistant
        message, so the pictures pulled out of it are attached once after the last
        of them -- putting one between two tool results leaves a tool_call
        unanswered where the API checks the sequence (measured, see the batching
        comment in the tool loop).

        Returns ``(new_messages, num_demoted)``; ``0`` means no tool result
        carried an image, so the refusal was about something else and the caller
        should not retry.
        """
        out: list[dict] = []
        pending: list[dict] = []
        demoted = 0

        def flush() -> None:
            if pending:
                out.append({"role": "user", "content": list(pending), _ATTACHED_IMAGE_KEY: True})
                pending.clear()

        for m in messages:
            content = m.get("content")
            if m.get("role") != "tool":
                flush()
                out.append(m)
                continue
            if not isinstance(content, list):
                out.append(m)
                continue
            images = [p for p in content if is_image_part(p)]
            if not images:
                out.append(m)
                continue
            clean = dict(m)
            clean["content"] = image_placeholder_text(content)
            out.append(clean)
            pending.extend(images)
            demoted += len(images)
        flush()
        return out, demoted

    @classmethod
    def _elide_older_images(cls, messages: list[dict]) -> tuple[list[dict], int]:
        """Drop inline images from all but the most recent image-bearing message.

        Run before the tool-text pass because an image is by far the densest
        thing in the window -- one costs up to 1568 tokens, which is more than
        most tool outputs -- so dropping a stale picture buys more room than
        eliding several text results, and costs less of what the model still
        needs.

        Not restricted to ``role="tool"``: when the endpoint cannot carry an
        image in a tool result the picture is attached to a following ``user``
        message instead, and that message would otherwise be untouchable here.
        """
        bearing = [
            i
            for i, m in enumerate(messages)
            if isinstance(m.get("content"), list) and any(is_inline_image(p) for p in m["content"])
        ]
        if len(bearing) <= cls._SHRINK_KEEP_RECENT_IMAGES:
            return messages, 0

        target = set(bearing[: -cls._SHRINK_KEEP_RECENT_IMAGES] if cls._SHRINK_KEEP_RECENT_IMAGES else bearing)
        out: list[dict] = []
        elided = 0
        for i, m in enumerate(messages):
            if i not in target:
                out.append(m)
                continue
            clean = dict(m)
            # New list: the caller's messages may still be referenced elsewhere.
            clean["content"] = [
                {"type": "text", "text": "[image elided to fit the context window]"} if is_inline_image(p) else p
                for p in m["content"]
            ]
            out.append(clean)
            elided += 1
        return out, elided

    async def _synthesize_final_on_exhaustion(
        self,
        messages: list[dict],
        model: str | None,
        fallback_models: list[str] | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """One tools-disabled LLM call to wrap up after the iteration budget runs out.

        Instead of returning a canned apology, ask the model to summarize what
        it accomplished and deliver its best partial answer. Tools are withheld
        (``tools=None``) so it cannot start another tool call — or an
        ``ask_user`` — at the cliff edge. Falls back to a static message if the
        call errors or comes back empty, so the turn is never left silent.

        When the turn caller wired streaming callbacks, this synthesized reply
        must stream too — otherwise it never reaches a streaming outlet: the
        run_turn boundary only emits a closing ``Text`` when nothing streamed,
        so a non-streamed wrap-up after an already-streamed turn gets dropped.
        """
        synth_messages = messages + [{"role": "user", "content": _MAX_ITER_SYNTHESIS_PROMPT}]
        try:
            if on_token_delta is not None or on_reasoning_delta is not None:
                response = await self._llm_call_stream(
                    messages=synth_messages,
                    tools=None,
                    model=model,
                    on_token_delta=on_token_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=synth_messages,
                    tools=None,
                    model=model,
                    fallback_models=fallback_models,
                )
            text = self._strip_think(response.content)
            if response.finish_reason != "error" and text:
                return text
            logger.warning(
                "Max-iter synthesis returned no usable content (finish_reason={})",
                response.finish_reason,
            )
        except Exception as exc:
            logger.warning("Max-iter synthesis call failed: {}", exc)
        fallback = _MAX_ITER_STATIC_FALLBACK.format(n=self.max_iterations)
        # The streamed-success path already delivered its text through
        # ``on_token_delta``; this fallback did not. Push it through the stream
        # too, or the run_turn boundary — which suppresses the closing ``Text``
        # once anything has streamed — would drop it on a streaming outlet.
        if on_token_delta is not None:
            await on_token_delta(fallback)
        return fallback

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        extraction_session_id: str | None = None,
        session_key: str | None = None,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        injected_skill_ids: list[str] | None = None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
        on_episode_start: Callable[[int], Awaitable[None]] | None = None,
        on_notice: Callable[[NoticeKind, str], Awaitable[None]] | None = None,
        usage_sink: dict[str, Any] | None = None,
        drain: Drain | None = None,
    ) -> tuple[str | None, list[str], list[dict], TurnOutcome]:
        """Run the agent iteration loop.

        ``drain``, when wired, is called at the top of each iteration to pull
        any user messages injected mid-turn (BusyPolicy.INJECT) and merge them
        as user turns before the next LLM call.

        ``extraction_session_id`` is the session key passed to local
        skill extraction when a turn completes. When ``None``, extraction
        is skipped (no pipeline wired) -- which is the only case today, so
        ``extraction_key`` below is always empty. It is not the turn's
        session key and must not be used as one.

        ``session_key`` is the turn's real session key. It labels the
        checkpoint commit only; usage attribution deliberately still goes
        through ``extraction_key``, since widening it would start filling
        per-session cost buckets that have always been empty.
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        extraction_key = extraction_session_id or ""
        effective_model = model or self.model

        # Bug2 / decision B — track whether the turn was a normal exit or a
        # max-iter interruption. ``status`` is the only piece read downstream
        # (used to label the shadow-git commit and stamp the ``TurnOutcome``).
        status = "completed"

        # Context-overflow recovery: bound the number of emergency shrinks so a
        # turn that overflows even after eliding can't loop forever.
        compress_retries = 0
        # Image-demotion recovery: bound per turn, same reason.
        image_demote_retries = 0
        # Tool-failure-loop break (#1b): track consecutive hard failures of the
        # same tool *with the same kind of error* across iterations; nudge once
        # per fresh streak, bounded/turn.
        loop_fail_key: tuple[str, str] | None = None
        loop_fail_streak = 0
        loop_nudges = 0
        # Empty-response recovery state, local to the turn — the AgentLoop is a
        # long-lived singleton shared across sessions, so per-instance counters
        # would leak across turns; resetting here gives clean per-turn budgets.
        prev_had_tool_calls = False
        post_tool_nudges = 0
        prefill_retries = 0
        empty_retries = 0

        while iteration < self.max_iterations:
            iteration += 1
            logger.info(
                "Iteration {}/{} model={}",
                iteration,
                self.max_iterations,
                effective_model,
            )

            # Mark the episode boundary (one per model call) so an outlet can
            # group this call's reasoning + text + tools into a single step.
            if on_episode_start is not None:
                await on_episode_start(iteration - 1)

            # Merge any INJECT-ed user messages (BusyPolicy.INJECT) before this
            # iteration's LLM call. Media-carrying injects keep their file
            # paths in the text so nothing is silently dropped.
            if drain is not None:
                for inj in drain():
                    inj_text = inj.text or ""
                    inj_paths = [m.path for m in inj.media]
                    if inj_paths:
                        prefix = inj_text + "\n" if inj_text else ""
                        inj_text = f"{prefix}[injected message; attached files: {', '.join(inj_paths)}]"
                    if inj_text:
                        messages.append({"role": "user", "content": inj_text})
                        logger.info("inject: merged a mid-turn user message")

            tool_defs = self.tools.get_definitions()

            # When CacheOptimizer runs, it owns the breakpoints, so the sending
            # provider's own automatic marking must be off. Applied here, to the
            # provider the running turn's binding resolved, rather than at the
            # construction site: a session switched mid-conversation runs on a
            # pool-built provider the construction site never saw, which would
            # otherwise keep auto-marking on top of the strategy's budget.
            if self.strategies.get("cache_optimizer") is not None:
                call_provider = self.provider
                if hasattr(call_provider, "disable_auto_cache_control"):
                    call_provider.disable_auto_cache_control = True

            # TokenWise before-hook: strategies may rewrite messages, tools,
            # or model (e.g. CacheOptimizer marks cache_control blocks).
            call_messages, call_tools, call_model = await self.strategies.before_llm_call(
                messages,
                tool_defs,
                effective_model,
            )
            if on_token_delta is not None or on_reasoning_delta is not None:
                response = await self._llm_call_stream(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    on_token_delta=on_token_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    fallback_models=fallback_models,
                )
            # TokenWise after-hook: strategies observe the response for
            # usage tracking, budget enforcement, etc. Errors are swallowed.
            usage_snapshot = self._build_usage_snapshot(response, call_model, extraction_key)
            await self.strategies.after_llm_call(
                {
                    "content": response.content,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                },
                usage_snapshot,
            )
            # tui-chat L2-A wire: stream caller (turn.* handler) may want the
            # final-iteration usage to populate `message.complete.payload.usage`
            # per CAP-CHAT-1 wire shape. Use the wire-contract UsageSnapshot
            # fields (prompt_tokens / completion_tokens / total_tokens) — not
            # the agent-internal snapshot with model / cache / cost fields.
            if usage_sink is not None and response.usage:
                prompt_tokens = int(response.usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(response.usage.get("completion_tokens", 0) or 0)
                # An explicitly configured window always wins over the live
                # table -- that is what setting it means. Otherwise the live
                # window from the model's provider table (e.g. OpenRouter,
                # when LiteLLM lags) answers instead; unknown to that table
                # too, 0 tells the UI to show its empty state rather than a
                # number that isn't this model's.
                if self._configured_window:
                    context_max = self._configured_window
                else:
                    # Off the event loop: allow_fetch=True here can hit the
                    # network for up to 10s on an OpenRouter model with both
                    # caches expired. See rates._fetch_openrouter_models.
                    context_max = await asyncio.to_thread(resolve_context_window, call_model) or 0
                context_used = prompt_tokens + completion_tokens
                usage_sink.clear()
                usage_sink["prompt_tokens"] = prompt_tokens
                usage_sink["completion_tokens"] = completion_tokens
                usage_sink["total_tokens"] = int(response.usage.get("total_tokens", 0) or 0)
                usage_sink["cost_usd"] = usage_snapshot.estimated_cost_usd
                usage_sink["context_max"] = context_max
                usage_sink["context_used"] = context_used
                usage_sink["context_percent"] = round(100 * context_used / context_max) if context_max else 0

            # Context-window overflow recovery: the structured classifier flags
            # should_compress (a smaller window won't help, but eliding the bulk
            # of accumulated tool output will). Shrink in place and retry this
            # iteration instead of surfacing it as a fatal error. Bounded.
            cls_ = response.error_classification
            if (
                response.finish_reason == "error"
                and cls_ is not None
                and cls_.should_compress
                and compress_retries < self._MAX_COMPRESS_RETRIES
            ):
                shrunk, elided = self._emergency_shrink(messages)
                if elided > 0:
                    messages = shrunk
                    compress_retries += 1
                    iteration -= 1  # the overflowed call did no work; don't bill it
                    logger.warning(
                        "Context overflow; elided {} old tool result(s), retrying ({}/{})",
                        elided,
                        compress_retries,
                        self._MAX_COMPRESS_RETRIES,
                    )
                    continue

            # Image-in-tool-result refused: this endpoint takes a picture only in
            # a user message. Rebuild onto the placeholder path -- the shape a
            # False capability verdict would have produced -- and retry this
            # iteration. Also cache the verdict so the rest of the process stops
            # paying for the attempt: the static table in `capabilities` guessed
            # wrong, and this is how it self-corrects.
            if (
                response.finish_reason == "error"
                and cls_ is not None
                and cls_.should_drop_tool_images
                and image_demote_retries < self._MAX_IMAGE_DEMOTE_RETRIES
            ):
                demoted_messages, demoted = self._demote_tool_images(messages)
                if demoted > 0:
                    messages = demoted_messages
                    self._image_tool_result_ok[call_model or effective_model] = False
                    image_demote_retries += 1
                    iteration -= 1  # the refused call did no work; don't bill it
                    logger.warning(
                        "Endpoint refused {} image(s) in a tool result; moved them "
                        "to a user message and retrying ({}/{})",
                        demoted,
                        image_demote_retries,
                        self._MAX_IMAGE_DEMOTE_RETRIES,
                    )
                    continue

            if response.has_tool_calls:
                # Blocking a call is handled where the call is: the
                # branch below refuses it and cancels the siblings the
                # model wrote beside it. The only answer that has to
                # leave that branch is the one about the turn, and this
                # is it. Every refusal asks for ABORT_TURN today; the
                # point of naming it separately is that the one which
                # should not can say so without touching this loop.
                continuation = Continuation.CONTINUE
                abort_reason = ""
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                # Images this transport cannot carry inside a tool result.
                # Collected across the whole batch and attached once *after* the
                # last tool result: a user message sitting between two tool
                # results leaves an assistant tool_call unanswered at the point
                # the API validates the sequence. Measured on gpt-4o, 2026-07-31,
                # two tool calls with the picture from the first:
                #   tool(c1), user, tool(c2) -> 400 "An assistant message with
                #     'tool_calls' must be followed by tool messages responding
                #     to each 'tool_call_id'"
                #   tool(c1), tool(c2), user -> 200, and the model named the
                #     image's colour
                # Anthropic accepts both, so this only bites on Chat Completions
                # -- which is the only transport that takes this path at all.
                pending_images: list[dict[str, Any]] = []
                tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                _stamp_reasoning_ms(messages, response)

                for tool_call_index, tool_call in enumerate(response.tool_calls):
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    # Skip the message tool: turn.py emits its tool.complete; a
                    # second emit here would double it.
                    emit_tool_event = on_tool_event is not None and tool_call.name != "message"
                    if emit_tool_event:
                        _tool = self.tools.get(tool_call.name)
                        await on_tool_event(
                            "start",
                            {
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                                "blocking": self.tools.is_blocking(tool_call.name, tool_call.arguments),
                                # Tool-authored call label; None -> UI derives one.
                                "display": _display_label(_tool, tool_call.arguments),
                            },
                        )
                    # A tool whose output also reaches the UI on a side channel
                    # (exec's inline diff, run_subagent_dag's progress events)
                    # needs this call's id to correlate with the row the UI drew.
                    if (setter := getattr(self.tools.get(tool_call.name), "set_tool_call_id", None)) is not None:
                        setter(tool_call.id)
                    tool_t0 = time.monotonic()
                    result = await self.tools.execute(tool_call.name, tool_call.arguments, run_meta=tool_call.run_meta)
                    duration_ms = int((time.monotonic() - tool_t0) * 1000)
                    # The registry already unwrapped any ToolResult: `result` is
                    # the model-facing text, with the optional display string
                    # riding along on it (ToolOutput). The model always gets the
                    # model text; the UI preview prefers the display string.
                    model_text = str(result)
                    display_src = getattr(result, "display_text", None) or model_text
                    # The log stays one line; the UI event keeps newlines so a
                    # tool that reports several items (e.g. ask_user's
                    # question -> answer pairs) renders one row each.
                    #
                    # 200 was a row's worth, and the row is not where this ends
                    # up: the detail card shows the same string, where 200 chars
                    # cut an ordinary error message mid-sentence and left the
                    # reader to guess the rest. The cap is what a card can show
                    # without becoming a file viewer, not what a row can.
                    preview = display_src[:_TOOL_PREVIEW_MAX_CHARS]
                    logger.info(
                        "Tool result: {} duration={}ms result={}",
                        tool_call.name,
                        duration_ms,
                        preview.replace("\n", " ")[:200],
                    )
                    tool_metadata = self.tools.take_metadata(tool_call.name, tool_call.arguments)
                    if emit_tool_event:
                        await on_tool_event(
                            "complete",
                            {
                                "tool_call_id": tool_call.id,
                                "result_preview": preview,
                                "truncated": len(display_src) > _TOOL_PREVIEW_MAX_CHARS,
                                "metadata": tool_metadata,
                                # The tool's own verdict, with the registry's
                                # failure text kept as the backstop for a result
                                # that carried none. See ToolEvent.ok.
                                "ok": bool(getattr(result, "ok", True)) and not model_text.startswith("Error"),
                                # The one hop the diff has to make by hand: the
                                # registry attaches it to the result, and only
                                # this event reaches a UI.
                                "diff": getattr(result, "diff", None),
                                # Alongside it, for a surface that renders the
                                # change itself rather than a unified diff of it.
                                "file_change": _file_change_payload(getattr(result, "file_change", None)),
                            },
                        )
                    # A skill the model loaded itself never passes through
                    # SkillForge injection, so report it here or the skill panel
                    # misses the whole class (builtin guides in particular).
                    if tool_call.name in ("read_skill", "use_skill") and not model_text.startswith("Error"):
                        await self._report_skill_read(session_key or "", tool_call.name, tool_call.arguments)
                    model_text, blocks, attach_blocks = self._route_result_images(
                        model_text, getattr(result, "blocks", None), call_model or effective_model
                    )
                    if blocks:
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, model_text, blocks
                        )
                    else:
                        # Keep the long-standing 4-arg call for text results so no
                        # existing caller or test double sees a signature change.
                        messages = self.context.add_tool_result(messages, tool_call.id, tool_call.name, model_text)
                    if messages:
                        # Dispatch to result, on the entry that answers the call.
                        # The live tool event was its only carrier, so a restored
                        # transcript had to either invent a number or say nothing
                        # about a call that took two minutes.
                        messages[-1][_TOOL_DURATION_MS_KEY] = duration_ms
                        if tool_metadata:
                            messages[-1][_TOOL_METADATA_KEY] = tool_metadata
                    if (tool_diff := getattr(result, "diff", None)) and messages:
                        # Underscore-keyed while the turn is live so no provider
                        # payload grows a field mid-turn; `_save_turn` renames it
                        # to `diff` on the stored entry. Without this the diff
                        # exists only on the live tool event, and a reloaded page
                        # can never number a change it no longer has.
                        messages[-1]["_diff"] = tool_diff
                    if attach_blocks:
                        pending_images.extend(attach_blocks)
                    if getattr(result, "blocks_call", False):
                        continuation = getattr(result, "continuation", Continuation.ABORT_TURN)
                        # The blocking tool's own words, kept for the reader: the
                        # canned reply below says an operation stopped but never
                        # which one, so without this the user is told a thing
                        # happened and given no way to find out what.
                        abort_reason = _first_line(model_text)
                        # A single assistant message may contain several parallel
                        # tool calls (for example ``rm`` followed by a Python
                        # fallback). Once policy terminates the action, none of
                        # the siblings may execute. We must nevertheless append
                        # one result for every advertised call id: OpenAI-style
                        # providers reject conversation history containing an
                        # assistant tool call without its matching tool result.
                        for skipped_call in response.tool_calls[tool_call_index + 1 :]:
                            messages = self.context.add_tool_result(
                                messages,
                                skipped_call.id,
                                skipped_call.name,
                                SKIPPED_AFTER_BLOCKED_CALL,
                            )
                        break
                    # #1b Track consecutive same-tool deterministic failures
                    # (transient errors excluded — a retry would clear those).
                    if is_hard_tool_failure(model_text):
                        failure_key = (tool_call.name, failure_class(model_text))
                        if failure_key == loop_fail_key:
                            loop_fail_streak += 1
                        else:
                            loop_fail_key, loop_fail_streak = failure_key, 1
                    else:
                        loop_fail_key, loop_fail_streak = None, 0

                if continuation is Continuation.ABORT_TURN:
                    # A normal tool result starts another model iteration. That
                    # is specifically unsafe here: the next plan can translate
                    # the rejected operation into an equivalent interpreter,
                    # script, or tool call. Finish the turn in runtime code and
                    # expose only the non-destructive continuation question.
                    # The model must read this, so it goes into the history as an
                    # assistant message -- but it goes to the CLIENT as a notice.
                    # Pushed down the token stream instead, it arrived as the
                    # model's own prose: glued to whatever the model had just
                    # narrated (nothing separates two segments in one buffer),
                    # dressed in the answer's copy and branch actions, and always
                    # in English no matter what language the turn was in.
                    from raven.spine.events import NoticeKind as _NoticeKind

                    messages = self.context.add_assistant_message(messages, _ABORTED_ACTION_REPLY)
                    if messages:
                        messages[-1][_NOTICE_KEY] = {
                            "kind": _NoticeKind.ACTION_BLOCKED.value,
                            **({"detail": abort_reason} if abort_reason else {}),
                        }
                    final_content = _ABORTED_ACTION_REPLY
                    if on_notice is not None:
                        await on_notice(_NoticeKind.ACTION_BLOCKED, abort_reason)
                    elif on_token_delta is not None:
                        # A channel with no notice outlet still has to say
                        # something, and silence is the worse failure.
                        await on_token_delta(_ABORTED_ACTION_REPLY)
                    break

                # #1b Failure-loop break: the same tool failed deterministically
                # `threshold` times running → append a change-approach nudge to
                # the last tool result so the model stops repeating a dead call.
                if (
                    loop_fail_streak >= self._LOOP_BREAK_THRESHOLD
                    and loop_nudges < self._LOOP_BREAK_MAX
                    and messages
                    and messages[-1].get("role") == "tool"
                ):
                    loop_nudges += 1
                    messages[-1]["content"] = (
                        str(messages[-1].get("content", ""))
                        + "\n\n"
                        + loop_break_nudge(
                            loop_fail_key[0],
                            loop_fail_streak,
                            loop_fail_key[1],
                            suggest_find_skill=self.tools.offers_by_name("find_skill"),
                        )
                    )
                    loop_fail_streak = 0  # fire once per fresh streak
                # After the nudge above, which needs the last message to still be
                # the tool result it appends to. Also after the blocked-call
                # branch, which ends the turn in runtime code -- there is no
                # further model call to show a picture to, so an aborted action
                # deliberately drops it rather than leaving it dangling.
                if pending_images:
                    messages.append({"role": "user", "content": pending_images, _ATTACHED_IMAGE_KEY: True})
                prev_had_tool_calls = True
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    status = "error"
                    break

                # Empty-response recovery: an empty assistant turn would
                # otherwise break out here and surface a "no response to give"
                # dud. Try to recover before giving up. Synthetic scaffolding is
                # marked ``_recovery_synthetic`` and stripped before persistence
                # / extraction so it can't poison future context.
                action = classify_empty_response(
                    response,
                    clean,
                    prev_had_tool_calls=prev_had_tool_calls,
                    nudges_done=post_tool_nudges,
                    prefill_retries=prefill_retries,
                    empty_retries=empty_retries,
                    limits=self._recovery_limits,
                )
                if action is RecoveryAction.PREFILL:
                    prefill_retries += 1
                    logger.warning(
                        "empty-recovery: thinking-only prefill {}/{}",
                        prefill_retries,
                        self._recovery_limits.thinking_prefill_max_retries,
                    )
                    # Re-feed the model its own reasoning (not stripped) so it
                    # continues into the body. Marked synthetic → dropped before
                    # persistence/extraction; the reasoning fields are stripped
                    # from the wire request by the provider's key allowlist.
                    messages = self.context.add_assistant_message(
                        messages,
                        response.content,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    )
                    messages[-1]["_recovery_synthetic"] = True
                    prev_had_tool_calls = False
                    continue
                if action is RecoveryAction.NUDGE:
                    post_tool_nudges += 1
                    logger.warning("empty-recovery: post-tool empty nudge")
                    # The (empty) assistant must sit between the tool result and
                    # the nudge — a bare tool→user sequence is a 400 on most APIs.
                    messages = self.context.add_assistant_message(messages, "(empty)")
                    messages[-1]["_recovery_synthetic"] = True
                    messages.append({"role": "user", "content": POST_TOOL_NUDGE, "_recovery_synthetic": True})
                    prev_had_tool_calls = False
                    continue
                if action is RecoveryAction.RETRY:
                    empty_retries += 1
                    logger.warning(
                        "empty-recovery: plain empty retry {}/{}",
                        empty_retries,
                        self._recovery_limits.empty_content_max_retries,
                    )
                    prev_had_tool_calls = False
                    continue

                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                _stamp_reasoning_ms(messages, response)
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached; synthesizing final answer", self.max_iterations)
            # Exhaustion is two orthogonal facts, not an either/or:
            #   1. The turn did NOT complete — tag it ``interrupted`` so the
            #      shadow-git checkpoint commit is labelled and the next turn's
            #      recovery prompt can surface the sha + edited files to resume.
            #   2. The user still deserves a useful reply NOW — so, checkpoint
            #      or not, synthesize a best-effort wrap-up (one tools-disabled
            #      call summarising what was done and what's left) instead of a
            #      canned apology. Synthesis falls back to a static message
            #      internally if the call fails, so the turn is never silent.
            status = "interrupted"
            final_content = await self._synthesize_final_on_exhaustion(
                messages,
                effective_model,
                fallback_models,
                on_token_delta=on_token_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
            # Persist the wrap-up into history like any normal final reply.
            # Persistence downstream reads only the returned ``messages`` list,
            # so without this the synthesized answer reaches the user via the
            # stream yet never enters the conversation — the next turn (notably
            # an interrupted-turn resume) could not see what was summarized. The
            # synthesis prompt itself stays local to the helper, so only the
            # reply lands here.
            if final_content:
                messages = self.context.add_assistant_message(messages, final_content)

        # Drop transient empty-recovery scaffolding before persistence /
        # extraction / return — refactor/Raven's empty-recovery marks
        # synthetic nudge/prefill messages with ``_recovery_synthetic``; strip
        # them so they never persist. (Embedded ``_trigger_local_extraction``
        # was retired on feature/integrate-everos; the after-turn pipeline —
        # ``context_engine.after_turn`` + ``backend.store`` in
        # ``_process_message`` — owns extraction now.)
        # Attached-image messages are dropped here for the same reason and at the
        # same point: the returned list feeds persistence, ``after_turn``
        # extraction and ``backend.store`` alike, so filtering once upstream of
        # all three is the only place that covers them.
        _transient = ("_recovery_synthetic", _ATTACHED_IMAGE_KEY)
        if any(any(m.get(k) for k in _transient) for m in messages):
            messages = [m for m in messages if not any(m.get(k) for k in _transient)]

        # Phase B-1 (feature/integrate-everos): embedded extraction (the
        # ``_trigger_local_extraction`` / ``SkillService.on_execution`` path)
        # was retired here in favor of the after-turn pipeline owned by the
        # caller — ``context_engine.after_turn`` + ``backend.store`` +
        # ``backend.feedback`` run from ``_process_message``. We surface
        # ``outcome.status`` so that pipeline can gate on completion later.

        outcome = TurnOutcome(status=status)
        checkpoint = self._turn_checkpoint()
        if checkpoint is not None:
            # Per-turn snapshot: one commit covering all of this turn's edits,
            # for both normal and interrupted exits (matches Claude Code/Cursor
            # granularity). Best-effort — commit_turn never raises.
            # The label is read in ``git log`` inside the shadow repo by
            # someone recovering a file. The repo already implies the
            # directory, so the session key is what disambiguates -- several
            # sessions share one shadow repo whenever they resolve to the same
            # directory (LAUNCH_DIR, or an explicit workdir override).
            label = f"turn {session_key} [{status}]" if session_key else f"turn [{status}]"
            cid, changed = await checkpoint.commit_turn(label)
            outcome.checkpoint_id = cid
            if status == "interrupted":
                outcome.edited_files = changed

        return final_content, tools_used, messages, outcome

    async def run(self) -> None:
        """Bring the agent runtime up and stay alive.

        Turns arrive through the spine (``run_turn``); this coroutine no longer
        drains an inbound bus. It starts the executor / debug server / MCP, then
        idles on ``self._running`` so the gateway can gather it as a long-lived
        task and tear it down via ``stop()`` on shutdown.
        """
        self._running = True
        try:
            await self._start_executor()
            await self._start_debug_server()
            await self._connect_mcp()
        except SandboxInitError as exc:
            logger.error("Sandbox failed to start: {}", exc)
            await self.close_executor()
            self._running = False
            return
        except Exception:
            await self.close_executor()
            raise
        logger.info("Agent loop started")

        while self._running:
            await asyncio.sleep(1.0)

    @property
    def is_processing(self) -> bool:
        """True while a turn is being dispatched under the global lock."""
        return self._processing_lock.locked()

    def _notify_turn_complete(self) -> None:
        for callback in self.on_turn_complete:
            try:
                callback()
            except Exception:
                logger.exception("on_turn_complete callback failed")

    async def close_mcp(self) -> None:
        """Close MCP connections and the sandbox executor."""
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_manager = None
        self._mcp_connected = False  # reset so _connect_mcp() can reconnect after close
        self._mcp_connecting = False  # reset so a concurrent caller isn't permanently blocked
        await self.close_executor()  # always runs, even when no MCP servers are configured

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    @trace.instrument("session.turn", seed=semconv.turn_seed, on_open=semconv.turn_open, extract=semconv.turn)
    async def _process_message(
        self,
        req: TurnRequest,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
        on_episode_start: Callable[[int], Awaitable[None]] | None = None,
        on_notice: Callable[[NoticeKind, str], Awaitable[None]] | None = None,
        usage_sink: dict[str, Any] | None = None,
        origin: Origin | None = None,
        drain: Drain | None = None,
    ) -> tuple[str | None, list[str]] | None:
        """Process a single turn request and return its reply.

        Returns ``(reply_content, media_paths)`` for a turn that produced an
        outbound reply, or ``None`` for a silent turn (the message tool already
        sent, or a hook short-circuit chose to return None). ``origin`` is the
        spine TurnRequest's origin.
        """
        from raven.agent.hook import AgentHookContext

        # Captured before any pre-turn work (hooks, personalization, context
        # assembly): this is when the user's message arrived, and it becomes the
        # stored user entry's timestamp. Everything saved by _save_turn is
        # stamped at turn END, so without this the whole turn shares one clock
        # read and a restored transcript cannot say how long the turn took.
        turn_received_at = self._now_fn().isoformat()

        channel = req.source.channel
        sender_id = req.source.sender_id
        chat_id = req.source.chat_id
        content = req.text
        metadata = dict(req.source.extras)
        media_paths = [m.path for m in req.media]
        msg_session_key = req.conversation or f"{channel}:{chat_id}"

        # AgentHook ``before_user_inbound`` chain.
        #
        # Replaces the legacy inline ``on_user_inbound`` + ``decision_consumer``
        # try/except blocks. The chain runs once and:
        #   - lets observer hooks (FeedbackTracker, on_user_inbound legacy
        #     adapter) record engagement;
        #   - lets short-circuit hooks (DecisionConsumer adapter for
        #     Sentinel /pick replies) halt processing and return their
        #     (content, media) reply directly.
        #
        # Skip the user-inbound hooks for Sentinel / subagent turns (by origin).
        skip_user_inbound = origin in _SKIP_USER_INBOUND_ORIGINS
        if len(self.hooks) > 0 and not skip_user_inbound:
            _hook_ctx = AgentHookContext(
                session_key=msg_session_key,
                turn_request=req,
            )
            _decision = await self.hooks.before_user_inbound(_hook_ctx)
            if _decision.short_circuit_result is not None:
                return _decision.short_circuit_result

        preview = content[:80] + "..." if len(content) > 80 else content
        logger.info("Processing message from {}:{}: {}", channel, sender_id, preview)

        # NOTE: the Sentinel ``decision_consumer`` short-circuit lives in the
        # unified AgentHook ``before_user_inbound`` chain at the
        # top of this method. Reaching this point means no hook claimed
        # the message and we proceed to normal slash-command / agent-loop
        # processing.

        key = session_key or msg_session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = content.strip().lower()
        if cmd == "/new":
            try:
                if not await self.memory_consolidator.archive_unconsolidated(session):
                    return (
                        "Memory archival failed, session not cleared. Please try again.",
                        [],
                    )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return (
                    "Memory archival failed, session not cleared. Please try again.",
                    [],
                )

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return ("New session started.", [])
        if cmd == "/help":
            lines = [
                "🐦‍⬛ Raven commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/help — Show available commands",
            ]
            return ("\n".join(lines), [])
        if not self.context_engine.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Personalization flow (global switch: self.enable_personalization) ──
        # Skip for a subagent result re-injection: its content is a system-generated
        # announce, not user input — personalizing it would pollute the profile or
        # fire a clarification on the announce. Only SUBAGENT skips here (not the
        # wider after-send / user-inbound sets): a Sentinel notice and cron/heartbeat
        # reach this flow today and keep it.
        if self.enable_personalization and origin is not Origin.SUBAGENT:
            from datetime import datetime as _dt

            from raven.agent.personalizer import Personalizer

            _personalizer = Personalizer(MemoryStore(self.workspace), self.provider, self.model)

            # ── Step 2 completion: user is answering a pending clarification ──
            if session.pending_clarification:
                _pending = session.pending_clarification

                # Determine whether the user is answering the previous question
                # or starting a fresh request. A fresh request typically contains
                # action verbs and is unrelated to the original; re-classify to
                # decide: if clarification is still needed, treat it as new.
                _recent = session.get_history(max_messages=4)
                _recheck = await _personalizer.classify(content, history=_recent)
                _is_new_request = _recheck.get("needs_clarification", False)

                if _is_new_request:
                    # User started a new request; discard the old pending state and re-classify.
                    session.pending_clarification = None
                    self.sessions.save(session)
                    logger.info("Personalization: new request detected, discarding old pending_clarification")

                    _question = await _personalizer.generate_question(
                        content,
                        _recheck.get("domain", ""),
                    )
                    if _question:
                        _ts = _dt.now().isoformat()
                        self._record_inbound(session, content, origin, _ts)
                        session.record({"role": "assistant", "content": _question, "timestamp": _ts})
                        session.pending_clarification = {
                            "original_message": content,
                            "question": _question,
                            "domain": _recheck.get("domain", ""),
                        }
                        self.sessions.save(session)
                        logger.info(
                            "Personalization: asked clarification for new request, session {}",
                            session.key,
                        )
                        return (_question, [])
                    # Clear pending state and proceed normally when question generation fails.
                    session.pending_clarification = None

                else:
                    # User is answering the previous question; extract preference and resume the original task.
                    session.pending_clarification = None

                    # Extract preference into MEMORY.md in the background without blocking the response.
                    async def _extract():
                        await _personalizer.extract_and_store_preference(
                            original_message=_pending["original_message"],
                            question=_pending["question"],
                            answer=content,
                        )

                    _t = asyncio.create_task(_extract())
                    self._consolidation_tasks.add(_t)
                    _t.add_done_callback(self._consolidation_tasks.discard)
                    # Continue normally: LLM understands the task via conversation history.

            else:
                # ── Step 1: classify the request — decide whether clarification is needed ──
                _recent = session.get_history(max_messages=4)
                _classification = await _personalizer.classify(content, history=_recent)

                if _classification.get("needs_clarification"):
                    # ── Step 2: pre-action interaction — generate and return a clarifying question ──
                    _question = await _personalizer.generate_question(
                        content,
                        _classification.get("domain", ""),
                    )

                    if _question:
                        # Write the original request and the clarifying question into history to keep the conversation coherent.
                        _ts = _dt.now().isoformat()
                        self._record_inbound(session, content, origin, _ts)
                        session.record({"role": "assistant", "content": _question, "timestamp": _ts})

                        # Save the pending state so the next message can resume it.
                        session.pending_clarification = {
                            "original_message": content,
                            "question": _question,
                            "domain": _classification.get("domain", ""),
                        }
                        self.sessions.save(session)

                        logger.info("Personalization: asked clarification for session {}", session.key)
                        return (_question, [])
                    # generate_question failed: skip silently and proceed
        # ── End personalization flow ─────────────────────────────────────────

        self._set_tool_context(channel, chat_id, metadata.get("message_id"), session_key=key)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()
        # ask_user keys by the true conversation_id (== the lane / gate key),
        # which is topic-aware (req.conversation), not just channel:chat_id.
        if (ask_tool := self.tools.get("ask_user")) and isinstance(ask_tool, AskUserTool):
            ask_tool.set_context(key)

        # No playbook interception. A playbook is one of the things the model can
        # reach for this turn (`load_playbook`), not something that decides ahead
        # of it: the funnel that used to sit here judged one message with no
        # history and, on a hit, replaced the whole turn -- so the party with the
        # least context made the most expensive call. All that remains per turn is
        # telling the tool what the turn is about, so its listing can be ranked.
        if self._playbooks is not None:
            self._playbooks.set_context(channel=channel, chat_id=chat_id, session_key=key)
            if (pb_tool := self.tools.get("load_playbook")) is not None and hasattr(pb_tool, "set_turn_message"):
                pb_tool.set_turn_message(content)

        context_messages = self._context_messages_for_session(session)
        # SkillForge: Selector picks top-K. See note in the system-message
        # branch above — empty return falls back to the full directory.
        # Phase B-3: routed via ``_select_skills_for_turn`` so the new
        # ``default`` engine short-circuits selection here.
        selected_skills = await self._select_skills_for_turn(
            content,
            context_messages,
        )
        # ── Model selection: the session's own pick, else the router ─────────
        # Ahead of assembly, not after it: assembly decides whether an
        # attachment is inlined as a picture or described in text, and that
        # question is about the model the request will actually reach. Routing
        # needs only ``content``, so asking first costs nothing and stops one
        # model's verdict from shaping a message another model receives.
        # A per-session model is an explicit user choice, so it outranks the
        # router's heuristic and suppresses its fallback chain.
        routed_model: str | None = session.metadata.get("model")
        fallback_models: list[str] = []
        if routed_model is not None:
            logger.info("Session model: {} → {}", self.model, routed_model)
        elif self.router is not None:
            routed_model, fallback_models = await self.router.select_model_chain(content)
            if routed_model and routed_model != self.model:
                logger.info("Router: {} → {}", self.model, routed_model)
            if fallback_models:
                logger.info("Router fallback chain: {}", fallback_models)

        initial_messages = await self._assemble_context_messages(
            session=session,
            session_key=key,
            current_message=content,
            media=media_paths if media_paths else None,
            channel=channel,
            chat_id=chat_id,
            surface=req.source.surface,
            selected_skills=selected_skills or None,
            model=routed_model,
        )
        if (origin_mark := _runtime_origin(req.origin)) and initial_messages:
            # Stamped on the envelope the assembler just built, which is the entry
            # ``_save_turn`` persists as this turn's user message. Marked here
            # rather than inside the assembler because the origin is a fact about
            # the request, and the assembler is handed a turn, not a request.
            last = initial_messages[-1]
            if last.get("role") == "user":
                last[_ORIGIN_KEY] = origin_mark
        # Surface the skills SkillForge injected this turn to the web UI's skill
        # panel (populated into _last_injected_skill_ids by the assemble above).
        await self._emit_injected_skills(key)

        extraction_sid = None  # Phase B-1: embedded extraction removed; always None now.
        turn_start_idx = len(initial_messages) - 1
        # The assembled list ends with THIS turn's user message, which is the
        # entry the mark belongs on.
        if req.delegated and initial_messages:
            initial_messages[-1][_DELEGATED_KEY] = dict(req.delegated)
        # The stream buffers exist so a turn that dies mid-answer still has the
        # text that was already on the reader's screen: the loop only appends an
        # assistant message once the provider call returns, so a cancel in the
        # middle of one would otherwise lose exactly what streamed.
        streamed: dict[str, str] = {"text": "", "thought": ""}

        async def _tap_token(delta: str) -> None:
            streamed["text"] += delta
            if on_token_delta is not None:
                await on_token_delta(delta)

        async def _tap_reasoning(delta: str) -> None:
            streamed["thought"] += delta
            if on_reasoning_delta is not None:
                await on_reasoning_delta(delta)

        async def _tap_episode(index: int) -> None:
            # A new episode is a new stream: without the reset, a buffer that
            # spans two assistant messages matches neither and would be saved
            # as a duplicate of text the loop already committed.
            streamed["text"] = ""
            streamed["thought"] = ""
            if on_episode_start is not None:
                await on_episode_start(index)

        try:
            final_content, _, all_msgs, outcome = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress,
                extraction_session_id=extraction_sid,
                session_key=key,
                model=routed_model,
                fallback_models=fallback_models,
                injected_skill_ids=self._collect_injected_skill_ids(selected_skills),
                on_token_delta=_tap_token if on_token_delta is not None else None,
                on_reasoning_delta=_tap_reasoning if on_reasoning_delta is not None else None,
                on_tool_event=on_tool_event,
                on_episode_start=_tap_episode,
                on_notice=on_notice,
                usage_sink=usage_sink,
                drain=drain,
            )
        except asyncio.CancelledError:
            # A stop is not a failure, but it is also not amnesia: what already
            # streamed is work the reader saw, so it lands in the session with a
            # note saying a person ended the turn. Then the cancel proceeds.
            self._save_broken_turn(
                session, initial_messages, turn_start_idx, turn_received_at, streamed, status="cancelled"
            )
            raise
        except Exception as exc:
            self._save_broken_turn(
                session,
                initial_messages,
                turn_start_idx,
                turn_received_at,
                streamed,
                status="failed",
                reason=str(exc),
            )
            raise
        self._stash_recovery(key, outcome)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # AgentHook ``after_send`` chain — typically a Sentinel
        # NudgeInjector / response_modifier modifying the outbound text. Skip it
        # for system-originated turns (Sentinel / subagent) so their reply
        # doesn't get a nudge layered on. A menu pick is USER (its reply IS the
        # user's intent — runs after_send); the Sentinel supersede notice is
        # SENTINEL and a subagent result is SUBAGENT (both skip).
        skip_after_send = origin in _SKIP_AFTER_SEND_ORIGINS
        if len(self.hooks) > 0 and not skip_after_send:
            from raven.agent.hook import AgentHookContext

            _send_ctx = AgentHookContext(
                session_key=key,
                outbound_content=final_content,
            )
            _send_decision = await self.hooks.after_send(_send_ctx)
            if _send_decision.modified_content is not None:
                final_content = _send_decision.modified_content

        self._save_turn(session, all_msgs, turn_start_idx, received_at=turn_received_at)
        self.sessions.save(session)
        await self.context_engine.after_turn(
            key,
            {
                "final_content": final_content,
                "messages": all_msgs[turn_start_idx:],
            },
        )
        # AG-1: plugin-side indexing (third peer step in after-turn pipeline).
        await self._dispatch_backend_store(
            key,
            all_msgs[turn_start_idx:],
        )
        # FB-1: forward source-qualified skill-usage feedback. Only
        # ``everos/`` prefix is forwarded to the plugin; static-library
        # sources (``local`` / ``mass``) have no feedback channel.
        await self._dispatch_backend_feedback(
            key,
            self._collect_injected_skill_ids(selected_skills),
        )
        if not self.context_engine.owns_compaction:
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # ── Step 4: post-action learning (background, non-blocking) ─────────────
        # Skip for a subagent result re-injection (see the pre-turn flow above):
        # its content is a system-generated announce, not user input to learn from.
        if self.enable_personalization and origin is not Origin.SUBAGENT:
            from raven.agent.personalizer import Personalizer

            _p4 = Personalizer(MemoryStore(self.workspace), self.provider, self.model)

            async def _post_learn():
                await _p4.post_learn(content, final_content)

            _t4 = asyncio.create_task(_post_learn())
            self._consolidation_tasks.add(_t4)
            _t4.add_done_callback(self._consolidation_tasks.discard)
        # ── End Step 4 ──────────────────────────────────────────────────────

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt.sent_in_turn:
            # Defensive fingerprint. The silent return None
            # previously left no trace when the agent replied via message
            # tool, making stochastic dud-turn bugs invisible to grep. Log
            # the would-be response so future investigations have a trail
            # parallel to "Response to ..." below.
            if final_content:
                preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
                logger.info(
                    "MessageTool sent in turn for {}:{}: {}",
                    channel,
                    sender_id,
                    preview,
                )
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", channel, sender_id, preview)
        return (final_content, [])

    def _save_broken_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        received_at: str | None,
        streamed: dict[str, str],
        *,
        status: str,
        reason: str | None = None,
    ) -> None:
        """Persist what a cancelled or failed turn got as far as producing.

        The tail of the turn plus two kinds of repair, then one closing marker:

        - an assistant message whose tool calls never got results gains a
          synthetic ``[interrupted]`` result per open call, because a stored
          history with an unanswered tool call is one strict providers reject
          on the next turn;
        - the text that streamed after the last committed message is saved as
          its own assistant message -- it was on the reader's screen, and the
          loop only commits a message once the provider call returns;
        - the marker entry carries ``turn_ended`` so a client can say WHY the
          transcript stops there, and readable text so the model sees the same.

        Never raises: this runs on the way out of a dying turn, and a rescue
        that throws replaces one loss with another.
        """
        try:
            tail: list[dict] = [dict(m) for m in messages[skip:]]
            open_calls: dict[str, str] = {}
            for m in tail:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        cid = str(getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else "") or "")
                        if cid:
                            name = getattr(getattr(tc, "function", None), "name", None) or (
                                (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
                            )
                            open_calls[cid] = str(name or "tool")
                elif m.get("role") == "tool":
                    open_calls.pop(str(m.get("tool_call_id") or ""), None)
            for cid, name in open_calls.items():
                tail.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": name,
                        "content": "[interrupted] this call never returned",
                    }
                )
            text = (streamed.get("text") or "").strip()
            if text and not any(
                m.get("role") == "assistant" and str(m.get("content") or "").strip() == text for m in tail
            ):
                partial: dict[str, Any] = {"role": "assistant", "content": streamed["text"]}
                if (thought := (streamed.get("thought") or "").strip()) and not any(
                    str(m.get("reasoning_content") or "").strip() == thought for m in tail
                ):
                    partial["reasoning_content"] = streamed["thought"]
                tail.append(partial)
            word = "cancelled by the user" if status == "cancelled" else f"failed: {reason or 'unknown error'}"
            marker: dict[str, Any] = {
                "role": "assistant",
                "content": f"(turn {word})",
                "turn_ended": {"status": status, **({"reason": reason} if reason else {})},
            }
            tail.append(marker)
            self._save_turn(session, tail, 0, received_at=received_at)
            self.sessions.save(session)
        except Exception:  # noqa: BLE001 - see docstring
            logger.opt(exception=True).warning("could not persist the broken turn for {}", session.key)

    def _record_inbound(self, session: Session, content: str, origin: "Origin | None", timestamp: str) -> None:
        """Persist the inbound entry of a turn that answers before ``_save_turn``.

        The personalization flow can reply with a clarifying question and return,
        so the envelope the assembler would have marked is never built and the
        turn's user entry reaches disk from here instead. Both paths that do it
        go through this one, because two copies of the same write is how one of
        them came to be marked and the other not.

        Writes ``origin`` rather than ``_origin``: the underscore exists so a
        mark stays out of the live provider payload until ``_save_turn`` renames
        it, and nothing here is going to a provider.
        """
        entry: dict[str, Any] = {"role": "user", "content": content, "timestamp": timestamp}
        if mark := _runtime_origin(origin):
            entry["origin"] = mark
        session.record(entry)

    def _save_turn(self, session: Session, messages: list[dict], skip: int, *, received_at: str | None = None) -> None:
        """Save new-turn messages into session, truncating large tool results.

        ``received_at`` is the wall clock at which the turn's inbound message
        arrived. This save runs after the turn completes, so stamping every
        entry "now" would give the user message and the final answer the same
        timestamp -- and a restored transcript reads the gap between those two
        as the turn's duration.
        """
        first_user_pending = received_at is not None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if first_user_pending and role == "user":
                entry.setdefault("timestamp", received_at)
                first_user_pending = False
            if entry.get("_recovery_synthetic"):
                continue  # #1a synthetic recovery nudge — never persist scaffolding
            if entry.get(_ATTACHED_IMAGE_KEY):
                # Already filtered upstream; kept because this is the last gate
                # before a write that cannot be undone, unlike the code above it.
                continue
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if turn_origin := entry.pop(_ORIGIN_KEY, None):
                entry["origin"] = turn_origin
            if delegated := entry.pop(_DELEGATED_KEY, None):
                # Same rename as the origin below, for the same reason: without
                # it a reload draws a delegated result as a question the user
                # asked, fence and all. The origin says WHO opened the turn;
                # the delegated identity says WHICH run came back, so the two
                # coexist.
                entry["delegated"] = delegated
            if notice := entry.pop(_NOTICE_KEY, None):
                # Same rename as the diff below, for the same reason. Without
                # it a reload draws this runtime prose as the model's answer,
                # so the live view and the restored one disagree about who
                # spoke.
                entry["notice"] = notice
            if tool_diff := entry.pop("_diff", None):
                # Renamed for storage: the private spelling kept it out of the
                # live provider payload, the plain one is what session.resume
                # maps onto the wire so a reloaded page can renumber the change.
                entry["diff"] = tool_diff
            if tool_metadata := entry.pop(_TOOL_METADATA_KEY, None):
                entry["metadata"] = tool_metadata
            for private_key, stored_key in (
                (_REASONING_MS_KEY, "reasoning_ms"),
                (_TOOL_DURATION_MS_KEY, "duration_ms"),
            ):
                # Renamed for the same reason as the diff above. A duration is a
                # property of the part it measures, so it is written down with
                # it: a clock started when a page loads can only ever guess.
                if (took := entry.pop(private_key, None)) is not None:
                    entry[stored_key] = took
            if role == "tool" and isinstance(content, list):
                # A multimodal tool result. Images must never reach the JSONL:
                # a single one adds megabytes that are then replayed on every
                # resume and re-fed to the model, and unlike a code bug that is
                # not revertible once written. The char cap below cannot catch
                # it either — it guards `str` content only.
                content = _strip_inline_images(content)
                entry["content"] = content
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = [
                        c
                        for c in _strip_inline_images(content)
                        if not (
                            isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        )
                    ]
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", self._now_fn().isoformat())
            session.record(entry)
        session.updated_at = self._now_fn()

    async def run_turn(
        self,
        req: TurnRequest,
        emit: Emit,
        drain: Drain,
        *,
        stream: bool = True,
        inline_tool_stream: bool = False,
        usage_sink: dict[str, Any] | None = None,
        text_sink: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        """Bind the turn to its session's model; see ``_run_turn`` for the turn.

        This is where a session's model becomes the one thing everything under
        the turn reads: the loop's own ``provider``/``model``, the context
        engine's LLM-backed segments, the skill gate and rewriter, the
        consolidator, and anything the turn detaches (a subagent inherits the
        context it was created in).

        Resolving once here is also what makes a mid-turn switch harmless
        without any parking. The binding is captured before the first read and
        held for the tree, so a switch that lands while this turn runs is
        simply not visible to it -- it takes effect on the session's next
        turn. Turns from other sessions run under their own binding
        concurrently, which is the point.
        """
        session_key = req.conversation or f"{req.source.channel}:{req.source.chat_id}"
        with use_binding(self.binding_for_session(session_key)):
            return await self._run_turn(
                req,
                emit,
                drain,
                stream=stream,
                inline_tool_stream=inline_tool_stream,
                usage_sink=usage_sink,
                text_sink=text_sink,
            )

    async def _run_turn(
        self,
        req: TurnRequest,
        emit: Emit,
        drain: Drain,
        *,
        stream: bool = True,
        inline_tool_stream: bool = False,
        usage_sink: dict[str, Any] | None = None,
        text_sink: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        """Spine-native turn entry: consume a TurnRequest, fan the agent's output
        onto the single ``emit``, return a TurnOutcome. Collapses the legacy
        output paths (a str return + the five callbacks) onto one boundary.

        Named ``run_turn`` rather than ``run``: ``run`` is the runtime keep-alive
        (executor / debug server / MCP up, then idle). A spine runner calls the
        public ``run_turn`` to satisfy the TurnRunner protocol.

        ``stream`` is the canon Q2-D assembly switch: a streaming outlet (TUI)
        wires it True so the reply goes out as StreamDelta and dissolves (b2 — no
        trailing Text); a non-streaming outlet (REPL) wires it False so the reply
        is one Text. It gates both LLM callbacks (the loop streams when either is
        wired) and the message-tool routing, so the whole reply travels one way.

        Exceptions propagate so the lane turns them into TurnFailed — run_turn
        does not catch sandbox-init to return an error string (the legacy direct
        path did; the spine surfaces it as a TurnFailed event instead).

        ``usage_sink`` lets a caller observe the turn's full token accounting
        (cost / context, richer than the three-field TurnOutcome.usage): pass a
        dict and it is filled. The TUI passes one to attach the rich usage to
        message.complete; the REPL omits it and uses TurnOutcome.usage.

        ``text_sink`` is its sibling for the reply text: pass a dict and the
        final reply lands in text_sink["text"] (the reply still goes out via
        emit — this is an observation copy, not a second delivery). cron passes
        one so its system event can tell the heartbeat what the run produced.
        Both sinks are transitional, to retire together when taps lands (a
        read-only observer of the turn's output).

        ``drain`` pulls user messages injected mid-turn (BusyPolicy.INJECT); it
        is threaded into the agent loop and consumed at the top of each iteration.
        """
        from raven.agent.subagent.direct_chat import DirectChatError
        from raven.proactive_engine.schedulers.cron.tool import CronTool
        from raven.spine.events import (
            EpisodeStart,
            MediaOut,
            Notice,
            NoticeKind,
            Reasoning,
            StreamDelta,
            Text,
            ToolEvent,
            ToolPhase,
            Usage,
        )
        from raven.spine.message import Media
        from raven.spine.runner import TurnOutcome

        cid = req.conversation or f"{req.source.channel}:{req.source.chat_id}"

        # Direct sub-agent turn (direct_target -- see TurnRequest). Deliberately
        # asymmetric with the deliver_text branch below: that one persists to the
        # session before emitting, this one persists nothing. The whole purpose
        # of a direct chat is that the main agent's transcript does not carry it;
        # the turn's evidence is its record directory, and what the main agent
        # eventually learns is the handoff block, not these messages.
        if req.direct_target is not None:
            agent, handle = req.direct_target
            # The lane is this instance's, so that a direct chat runs concurrently
            # with the main agent's turn and with every other instance's. Records,
            # the instance registry and the handoff are the *session's* though --
            # keyed by the lane they would scatter one instance's history into a
            # directory of its own and land the handoff on a conversation nobody
            # reads. See ``raven.spine.turn.session_of``.
            session_key = session_of(cid)
            streamed_direct = False

            async def on_direct_delta(text: str) -> None:
                # Text only, never Reasoning: the wire tags an instance on four
                # event types and thinking.delta is not one of them, so a direct
                # chat's reasoning would be rendered into the main transcript.
                nonlocal streamed_direct
                if not text:
                    return
                streamed_direct = True
                await emit(StreamDelta(delta=text))

            try:
                reply, meta = await self.subagents.chat(
                    session_key=session_key,
                    agent=agent,
                    handle=handle,
                    text=req.text,
                    # The session's working directory, the same one this turn's
                    # own tools would get and the same one `spawn` captures. The
                    # binding is not set here -- `workdir.bind` wraps the main
                    # turn body further down, which this branch returns before
                    # reaching -- so it is resolved rather than read. Omitting it
                    # fell back to agent home (`~/.raven/workspace`), which is
                    # raven's memory and skills rather than the work: a direct
                    # chat about the checkout the user is sitting in ran `git
                    # status` against raven's own home and answered about that.
                    workspace=self.session_workdir(session_key),
                    # A non-streaming outlet (the REPL) gets no deltas to
                    # assemble, exactly as it gets none from a normal turn.
                    on_delta=on_direct_delta if stream else None,
                )
            except DirectChatError as exc:
                self._direct_handoff.record(session_key, exc.meta)
                raise
            self._direct_handoff.record(session_key, meta)
            # Same rule as the main path below (canon Q2-D b2): what streamed is
            # already on screen, so a closing Text would render the reply twice.
            # An instance whose transport cannot stream never sets the flag and
            # is delivered whole, which is what every direct chat did before.
            if not streamed_direct:
                await emit(Text(content=reply))
            return TurnOutcome(
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                explicit_reply=True,
            )

        # Verbatim delivery (deliver_text — see TurnRequest). Persist before
        # emit, mirroring the normal turn's save-then-reply order, so a save
        # failure never leaves the user a delivered message no turn recorded.
        # The after-turn work a normal turn does in _process_message is reduced
        # to what a no-model delivery needs: backend.store indexes the report;
        # after_turn is a no-op without a turn_id; consolidation is the curator's
        # job on its next assemble.
        if req.deliver_text is not None:
            session = self.sessions.get_or_create(cid)
            msg = {"role": "assistant", "content": req.deliver_text}
            self._save_turn(session, [msg], 0)
            self.sessions.save(session)
            await self._dispatch_backend_store(cid, [msg])
            await emit(Text(content=req.deliver_text))
            return TurnOutcome(
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                explicit_reply=True,
            )

        # A direct chat is invisible to the main agent by design, so this is
        # where it finds out one happened: pointers to what was asked and
        # answered, never the text. Take-and-clear, so a segment is reported
        # once. Nothing pending returns immediately -- this runs on every turn.
        handoff = self._direct_handoff.take(cid)
        if handoff is not None:
            req = replace(req, text=f"{handoff}\n\n{req.text}")

        streamed = False

        async def on_token(text: str) -> None:
            nonlocal streamed
            if not text:
                return
            streamed = True
            await emit(StreamDelta(delta=text))

        async def on_reasoning(text: str) -> None:
            if text:
                await emit(Reasoning(content=text))

        async def on_episode(index: int) -> None:
            await emit(EpisodeStart(index=index))

        async def on_tool(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                await emit(
                    ToolEvent(
                        phase=ToolPhase.START,
                        tool_call_id=info["tool_call_id"],
                        name=info["name"],
                        arguments=info["arguments"],
                        blocking=bool(info.get("blocking")),
                        display=info.get("display"),
                    )
                )
            else:
                await emit(
                    ToolEvent(
                        phase=ToolPhase.COMPLETE,
                        tool_call_id=info["tool_call_id"],
                        result_preview=info["result_preview"],
                        truncated=info["truncated"],
                        ok=bool(info.get("ok", True)),
                        metadata=info.get("metadata"),
                        diff=info.get("diff"),
                        file_change=info.get("file_change"),
                    )
                )

        async def on_progress(text: str, tool_hint: bool = False) -> None:
            # Keep the progress/tool-hint distinction so an outlet can gate each on
            # its own config flag (send_progress vs send_tool_hints), as the bus
            # path did — tool-hint text rides NoticeKind.TOOL_HINT, progress rides
            # PROGRESS. Outlets that don't render either eat both kinds anyway.
            if text:
                await emit(
                    Notice(
                        kind=NoticeKind.TOOL_HINT if tool_hint else NoticeKind.PROGRESS,
                        detail=text,
                    )
                )

        async def on_notice(kind: NoticeKind, detail: str) -> None:
            await emit(Notice(kind=kind, detail=detail or None))

        async def _emit_media(paths: list[str]) -> None:
            await emit(
                MediaOut(media=tuple(Media(path=p, mime="application/octet-stream", kind="file") for p in paths))
            )

        # Route the message tool's reply through the token stream so a
        # tool-driven reply streams like the main response; _process_message
        # then returns None, so the boundary below emits nothing for it. The
        # callback is turn-local (a ContextVar in MessageTool), so a concurrent
        # turn cannot clobber this turn's routing — no save/restore needed.
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):

            async def _route_to_stream(content: str, media: list[str]) -> None:
                # A message-tool reply can attach media; emit it independently so
                # it is not dropped (_process_message returns None for a tool reply
                # so the boundary below never sees it). The content follows the same
                # stream switch as the main reply: StreamDelta when streaming, one
                # Text otherwise — else a non-streaming outlet would eat the delta.
                if media:
                    await _emit_media(media)
                if text_sink is not None and content:
                    text_sink["text"] = content
                if stream:
                    await on_token(content)
                elif content:
                    await emit(Text(content=content))

            message_tool.set_send_callback(_route_to_stream)

        # Pick up a mid-session `deep-research enable` before the wiring below, so
        # a promoted working tool gets THIS turn's stream callback and (in
        # _process_message) routing context -- not just the next turn's.
        self._maybe_promote_deep_research()

        # deep_research (streaming surfaces only): stream its progress live and
        # deliver its finished answer inline, so the tool returns a compact
        # receipt and the model relays instead of re-emitting/rewriting. Progress
        # rides Reasoning (TUI thinking.delta / CLI progress line); the answer
        # follows the same stream switch as the main reply.
        if inline_tool_stream:
            dr_tool = self.tools.get("deep_research")
            if dr_tool is not None and hasattr(dr_tool, "set_stream_callback"):

                async def _route_deep_research(kind: str, text: str) -> None:
                    if not text:
                        return
                    if kind == "progress":
                        await emit(Reasoning(content=text))
                    elif stream:
                        await on_token(text)
                    else:
                        await emit(Text(content=text))

                dr_tool.set_stream_callback(_route_deep_research)

        # A CRON turn must not let the agent schedule new cron jobs mid-run. The
        # CronTool guards via a ContextVar; set it here, in the lane task that runs
        # the turn, so it propagates to the tool — the cron callback sets it in a
        # different task that never reaches this one.
        cron_tool = self.tools.get("cron")
        cron_token = None
        if req.origin is Origin.CRON and isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)

        if usage_sink is None:
            usage_sink = {}
        try:
            # Resolve inside the try so a bad persisted override (deleted
            # directory, permission change) still releases the cron token
            # below via the outer finally, instead of stranding it.
            try:
                turn_workdir = self.session_workdir(cid)
            except Exception as exc:
                raise RuntimeError(
                    "Session working directory is invalid; clear this session's working "
                    "directory override from the web UI to recover."
                ) from exc
            # Bind the session's working directory around the whole turn body (not
            # just the _set_tool_context call inside _process_message) so every
            # path-aware tool sees it, including on the exception and cancellation
            # paths below -- workdir.bind's finally always resets the ContextVar.
            with workdir.bind(turn_workdir):
                try:
                    await self._start_executor()
                    await self._connect_mcp(wait=_MCP_TURN_WAIT_S)
                    out = await self._process_message(
                        req,
                        session_key=cid,
                        on_progress=on_progress,
                        on_token_delta=on_token if stream else None,
                        on_reasoning_delta=on_reasoning if stream else None,
                        on_tool_event=on_tool,
                        on_episode_start=on_episode if stream else None,
                        on_notice=on_notice,
                        usage_sink=usage_sink,
                        origin=req.origin,
                        drain=drain,
                    )
                except Exception:
                    await self.close_executor()
                    raise
        finally:
            if cron_token is not None and isinstance(cron_tool, CronTool):
                cron_tool.reset_cron_context(cron_token)

        # Single return->emit boundary (N-UNIFORM). MediaOut is independent of the
        # stream and precedes Text (G-MEDIA-2(a): the current order is media-first).
        if out is not None:
            reply_content, reply_media = out
            if reply_media:
                await _emit_media(reply_media)
            if not streamed and reply_content:
                await emit(Text(content=reply_content))
            if text_sink is not None and reply_content:
                text_sink["text"] = reply_content

        usage = Usage(
            prompt_tokens=int(usage_sink.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_sink.get("completion_tokens", 0) or 0),
            total_tokens=int(usage_sink.get("total_tokens", 0) or 0),
        )
        # A message-tool reply returns None from _process_message but did reply,
        # so it counts as an explicit reply too.
        replied_via_tool = isinstance(message_tool, MessageTool) and message_tool.sent_in_turn
        return TurnOutcome(usage=usage, explicit_reply=out is not None or replied_via_tool)
