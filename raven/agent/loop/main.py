"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

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
from raven.agent.loop.truncation import flag_truncation
from raven.agent.subagent import SubagentManager
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
from raven.agent.tools.spawn import SpawnTool
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.memory_engine.base import TokenBudget
from raven.memory_engine.consolidate.consolidator import MemoryConsolidator, MemoryStore
from raven.providers.base import (
    ErrorClassification,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    send_max_tokens,
)
from raven.providers.capabilities import image_placeholder_text, supports_image_tool_result, vision_verdict
from raven.providers.rates import effective_context_window, resolve_context_window
from raven.providers.reasoning import split_orphan_think
from raven.sandbox import SandboxConfig, SandboxExecutor, SandboxInitError, build_executor
from raven.session.manager import Session, SessionManager
from raven.spine.turn import Origin
from raven.tracing import semconv, trace
from raven.utils.helpers import estimate_prompt_tokens, is_image_part, is_inline_image

_ABORTED_ACTION_REPLY = (
    "The operation was not completed, and no alternative method will be attempted. "
    "Would you like me to continue with the remaining parts of the task that do not "
    "require this operation?"
)

# NOTE: ``raven.context_engine`` is intentionally imported lazily (inside
# ``__init__`` and ``_assemble_context_messages``) to break a runtime
# import cycle: ``raven.agent.__init__`` eagerly loads AgentLoop,
# while ``raven.context_engine.curator`` imports ``ContextBuilder`` from
# ``raven.agent.context`` — a module-level top-down ``from
# raven.context_engine import ...`` here re-enters a partially-initialized
# package and raises ImportError on ``TurnContext``.

if TYPE_CHECKING:
    from raven.agent.hook import CompositeHook
    from raven.agent.tools.base import Tool
    from raven.config.raven import (
        ContextConfig,
        MemoryConfig,
        RuntimeConfig,
        SkillForgeRouterConfig,
    )
    from raven.config.schema import ChannelsConfig, DeepResearchToolConfig, ExecToolConfig
    from raven.context_engine import ContextEngine
    from raven.memory_engine.backend import MemoryBackend
    from raven.proactive_engine.schedulers.cron.service import CronService
    from raven.routing.router import ModelRouter
    from raven.sandbox.debug_server import SandboxDebugServer
    from raven.skill_hub import SkillHubClient
    from raven.spine.runner import Drain, Emit, TurnOutcome
    from raven.spine.turn import TurnRequest
    from raven.token_wise.base import UsageSnapshot
    from raven.token_wise.registry import StrategyRegistry
    from raven.tui_rpc.question_broker import QuestionBroker


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

_ATTACHED_IMAGE_KEY = "_attached_image"


# Opening or closing reasoning tag, with or without a vendor namespace prefix
# (<think>, </thinking>, <mm:think>, ...). Used only to ask whether a stripped
# response is nothing but tag debris -- matching a spelling wrong costs a
# missed recovery, never a mangled answer.
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
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        sandbox_config: SandboxConfig | None = None,
        channels_config: ChannelsConfig | None = None,
        router: "ModelRouter | None" = None,
        strategies: "StrategyRegistry | None" = None,
        skill_forge_config: Any = None,
        response_modifier: Callable[[str, str], str] | None = None,
        on_user_inbound: Callable[["TurnRequest"], None] | None = None,
        decision_consumer: "Callable[[TurnRequest], Awaitable[Any]] | None" = None,
        hooks: "CompositeHook | None" = None,
        now_fn: Callable | None = None,
        context_config: "ContextConfig | None" = None,
        runtime_config: "RuntimeConfig | None" = None,
        interactive: bool = True,
        jina_api_key: str | None = None,
        max_concurrent_subagents: int = 4,
        max_subagent_spawns_per_hour: int = 30,
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
    ):
        from raven.agent.hook import (
            CompositeHook,
            DecisionConsumerAdapter,
            OnUserInboundAdapter,
            ResponseModifierAdapter,
        )
        from raven.config.schema import ExecToolConfig
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
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        # Resolved lazily on the first tool result that carries an image. Keyed
        # by model, not a single flag: the loop is a long-lived singleton and
        # takes a per-call model (strategies rewrite it, and the model chain
        # falls back), so one model's verdict must not answer for another's.
        self._image_tool_result_ok: dict[str, bool] = {}
        self._vision_ok: dict[str, bool] = {}
        self.max_iterations = max_iterations
        # Empty-response recovery budgets. None → enabled defaults.
        self._recovery_limits = empty_recovery if empty_recovery is not None else RecoveryLimits()
        # A caller that passed a positive value set the window explicitly;
        # None/0 means "figure it out", resolved once here against the model's
        # real window. ("Explicit", not "pinned" -- Provider Pin is a different
        # registered term, see CONTEXT.md.)
        self._context_window_explicit = bool(context_window_tokens)
        if context_window_tokens == 65536:
            # The retired schema default, which the old bootstrap wrote to
            # disk verbatim -- so on upgraded installs this exact value is
            # more often a fossil than a choice. The config is deliberately
            # not rewritten (a value the user can see in their own file stays
            # theirs); this line is what keeps that stance from failing
            # silently.
            logger.warning(
                "contextWindowTokens: 65536 is pinning the context window (the old default, "
                "written out by earlier versions); remove the line from config.json to size "
                "it from each model's real window"
            )
        # allow_fetch=False: construction must not block on a synchronous
        # network call for an OpenRouter model's window -- whatever is already
        # cached (in-process or on disk, any age) answers instead. See
        # rates._fetch_openrouter_models.
        self.context_window_tokens = context_window_tokens or effective_context_window(
            self.model, None, allow_fetch=False
        )
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        from raven.config.schema import DeepResearchToolConfig, MediaGenConfig

        self.media_config = media_config or MediaGenConfig()
        self.deep_research_config = deep_research_config or DeepResearchToolConfig()
        self.exec_config = exec_config or ExecToolConfig()
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

        self.context = ContextBuilder(
            workspace,
            skill_forge_config=skill_forge_config,
            llm_provider=provider,
            now_fn=now_fn,
        )
        self.sessions = session_manager or SessionManager(workspace)
        # Tool names to omit from the registry — applied after default-tool
        # registration and after MCP connect so it can blacklist either group.
        # Used by eval harnesses (e.g. BCP) that need a strict tool subset.
        self._disabled_tools = set(disabled_tools or [])
        self._tool_search_config = tool_search_config
        self.tools = ToolRegistry()

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
            model=self.model,
            context_window_tokens=self.context_window_tokens,
            get_tool_definitions=self.tools.get_definitions,
            now_fn=now_fn,
            # The factory uses these to assemble the unified engine's
            # SkillForgeRouter + EverOS recall lane.
            backend=backend,
            memory_config=memory_config,
            skill_forge_router_config=skill_forge_router_config,
            skill_forge_config=skill_forge_config,
            skill_hub_client=self._skill_hub_client,
        )

        # Runtime discipline (5th pillar). Bug2 uses ``runtime.checkpoint``;
        # gated by (policy, interactive) — see ``_checkpoint_active``. When
        # the gate is closed the loop is byte-identical to baseline.
        if runtime_config is None:
            from raven.config.raven import RuntimeConfig

            runtime_config = RuntimeConfig()
        self.runtime_config = runtime_config
        self.interactive = interactive
        self._checkpoint = None
        if self._checkpoint_active(runtime_config.checkpoint.policy, interactive):
            from raven.agent.loop.checkpoint import CheckpointService

            try:
                self._checkpoint = CheckpointService(
                    workspace,
                    shadow_dir=runtime_config.checkpoint.shadow_dir,
                )
            except ValueError as exc:
                # Bad shadow_dir (e.g. ``../escape`` or absolute path) →
                # CheckpointService refuses to construct. Don't crash the
                # whole agent over a config typo; log and disable the
                # safety net so the turn still runs.
                logger.warning("runtime.checkpoint disabled — {}", exc)
        # session_key -> {"checkpoint_id", "files"} stashed when a turn is
        # interrupted (max-iter); consumed by the next turn's recovery prompt.
        self._pending_recovery: dict[str, dict] = {}

        self._sandbox_config = sandbox_config
        self._owned_ids: set[str] = set()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            model=self.model,
            brave_api_key=brave_api_key,
            jina_api_key=jina_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            sandbox_config=sandbox_config,
            owned_ids=self._owned_ids,
            max_concurrent=max_concurrent_subagents,
            max_spawns_per_hour=max_subagent_spawns_per_hour,
        )

        # Executor: synchronous construction only; VM starts in _start_executor()
        self._executor: SandboxExecutor = build_executor(sandbox_config, workspace, self._owned_ids)
        self._executor_stack: AsyncExitStack | None = None
        self._executor_started: bool = False
        self._executor_start_lock = asyncio.Lock()
        self._debug_server: SandboxDebugServer | None = None

        self.router = router
        self.enable_personalization = False  # Set via configure_personalization()
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._processing_lock = asyncio.Lock()
        # Fired after every dispatched turn (success, error, or cancel).
        # Used by the proactive-engine WakeScheduler to re-fire wakes that
        # were parked while the agent was busy. Callbacks must be cheap and
        # must not raise.
        self.on_turn_complete: list[Callable[[], None]] = []
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            now_fn=now_fn,
        )

        self._consolidation_tasks: set[asyncio.Task] = set()

        # A switch that arrives mid-turn is parked here until no turn is
        # running, so a turn in flight finishes on the provider it started
        # with. A depth counter, not a flag: OriginPools gates USER and
        # system origins on independent semaphores with no global cap
        # (spine/scheduler.py), so a user turn and a cron turn overlap on
        # this loop under the TUI defaults.
        self._pending_provider: tuple[LLMProvider, str] | None = None
        self._turns_in_flight = 0

        # ``self.subagents``, ``self.context_engine`` and
        # ``self.memory_consolidator`` were each handed ``provider`` earlier in
        # this constructor and hold their own reference; ``_adopt_provider`` is
        # what keeps them from outliving a live model switch. Add the call
        # there when adding another holder.

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
        self._apply_disabled_tools()

        # LazyProvider defers the litellm import behind a background prewarm
        # thread (see providers.lazy); the window this constructor just
        # resolved above was answered with allow_import=False, so it can be
        # wrong until that import lands. Wiring the callback fixes it up in
        # place once the real provider is built -- a no-op for any other
        # provider, which has no ``on_built`` to set.
        if hasattr(provider, "on_built"):
            provider.on_built = self.refresh_context_window

    def _apply_disabled_tools(self) -> None:
        """Unregister tools whose names appear in ``tools.disabled_tools``.

        Run after :meth:`_register_default_tools` (here) and after MCP connect
        (see :meth:`_connect_mcp`) so the blacklist can cover either group.
        Silent on misses — eval configs commonly carry an over-broad list
        that's a no-op for tools that weren't registered in this build.
        """
        if not self._disabled_tools:
            return
        for name in list(self._disabled_tools):
            if self.tools.has(name):
                self.tools.unregister(name)

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Point the loop and everything it built at a new provider/model.

        ``config.set model`` builds a provider from the prospective config
        and hands it here. Assigning ``self.provider`` alone is not enough:
        the subagent manager, the context engine's LLM-backed segments and
        the consolidator each captured the provider handed to them in
        ``__init__``. Left behind, they keep calling the old endpoint for
        the rest of the process -- which is how switching away from a dead
        credential fixed the main loop while subagents and the skill
        rewriter/gate went on failing to authenticate.

        A switch that lands while any turn is running is parked rather than
        applied: the loop reads ``self.provider`` at call time (eight sites
        in this module, plus the context engine and consolidator
        underneath), so adopting mid-turn would relay one conversation
        across two vendors. How that surfaces depends on the path: the
        ``chat_with_retry`` sites turn a rejected request into
        ``finish_reason="error"`` content, so the turn reports a failure
        with no sign that its endpoint moved, while ``_llm_call_stream``
        (which a TUI turn takes) catches only ``TimeoutError`` and lets the
        rejection propagate. Neither is a diagnosis the user can act on.

        The park is the second line of defence, not the first: the RPC
        rejects a switch outright when the caller's own session has a turn
        in flight (``is_turn_active`` in ``tui_rpc.methods.config``). This
        covers what that guard cannot see -- a caller that passes no
        ``session_id``, and the proactive turns that run in their own lanes.
        Note the RPC still answers ``applied: True`` and the config file is
        already written, so a parked switch is applied on disk while the
        loop reports the old model until the last turn drains.

        Detached subagents are not covered by that park -- they outlive the
        turn that spawned them -- so ``SubagentManager`` snapshots instead.
        """
        if self._turns_in_flight:
            # The only trace of the window the docstring describes.
            logger.info("model switch to {} parked until {} running turn(s) drain", model, self._turns_in_flight)
            self._pending_provider = (provider, model)
            return
        self._adopt_provider(provider, model)

    def _adopt_provider(self, provider: LLMProvider, model: str) -> None:
        """Hand a provider to the loop and every subsystem holding the old one."""
        logger.info("adopting provider switch: model={}", model)
        self.provider = provider
        self.model = model
        # Cached per model id but computed from the provider, so a swap that
        # keeps the model id would keep serving the old transport's verdict.
        self._image_tool_result_ok.clear()
        self.subagents.set_provider(provider, model)
        self.context_engine.set_provider(provider, model)
        self.memory_consolidator.set_provider(provider, model)
        # Here rather than at the RPC call site: a parked switch adopts long
        # after that call returns, and the window must follow the pair that
        # was actually adopted, not the model the RPC saw.
        self.refresh_context_window()

    def _adopt_pending_provider(self) -> None:
        """Apply a parked switch. Callers must check that no turn is running."""
        pending = self._pending_provider
        if pending is None:
            return
        self._pending_provider = None
        self._adopt_provider(*pending)

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

    def refresh_context_window(self) -> None:
        """Re-resolve ``context_window_tokens`` against the current ``self.model``.

        A no-op once the window was set explicitly at construction -- an
        explicit value is a deliberate override, and a model switch afterwards
        must not quietly discard it. Otherwise the ladder is re-walked so a
        ``/model`` switch picks up the new model's real window instead of
        keeping the old one's.

        Also the callback ``LazyProvider.on_built`` fires from its prewarm
        thread, i.e. off the event loop -- safe because every write this
        method triggers, transitively through the consolidator and the
        context engine's builders, is a plain ``int`` attribute assignment,
        and the GIL makes each one atomic.
        """
        if self._context_window_explicit:
            return
        # allow_fetch=False: a /model switch runs inside the running event
        # loop, so this must not block it on a synchronous network call. See
        # rates._fetch_openrouter_models.
        self.context_window_tokens = effective_context_window(self.model, None, allow_fetch=False)
        # Cascade into the builders that sized themselves against the window
        # at construction (the Curator's trimmer) and the consolidator --
        # both would otherwise keep budgeting against the pre-switch model's
        # window for the rest of the session. The consolidator's window is a
        # plain attribute (no setter of its own), set directly here.
        self.context_engine.set_context_window(self.context_window_tokens)
        self.memory_consolidator.context_window_tokens = self.context_window_tokens

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, GrepTool, FindTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
                executor=self._executor,
                extra_deny_patterns=self.exec_config.extra_deny_patterns,
            )
        )
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
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
        # The QuestionBroker is a per-transport singleton, late-bound via
        # set_broker once the transport (TUI RPC server / gateway hub) exists.
        self.tools.register(AskUserTool())
        if self.cron_service:
            # Lazy import: CronTool lives under raven.proactive_engine.schedulers.cron.tool
            # which (a) imports raven.agent.tools.base, triggering raven.agent.__init__,
            # which (b) imports this very loop module. Importing at function scope breaks the
            # cycle since loop.py is fully loaded by the time _register_default_tools runs.
            from raven.proactive_engine.schedulers.cron.tool import CronTool

            self.tools.register(CronTool(self.cron_service))

        # Plugin-contributed tools (e.g. EverOS's ``understand_media``).
        # Registered last so a plugin can override a built-in by name if
        # it deliberately contributes the same name; ``_apply_disabled_tools``
        # still runs afterward and can strip any of them.
        for tool in self.plugin_tools:
            self.tools.register(tool)

        # Skill Hub retrieval tools. ``use_skill`` is source-agnostic — it
        # resolves local/everos skills on disk too — so it registers whenever
        # the skill registry is reachable. ``read_skill`` only fetches Hub
        # bodies (local/everos bodies already ride in context), so it
        # registers only when a Hub endpoint is configured.
        skill_registry = getattr(
            getattr(self.context, "skills", None),
            "registry",
            None,
        )
        if skill_registry is not None or self._skill_hub_client is not None:
            from raven.agent.tools.skill_hub import ReadSkillTool, UseSkillTool

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
            if self._skill_hub_client is not None:
                self.tools.register(
                    ReadSkillTool(
                        client=self._skill_hub_client,
                        registry=skill_registry,
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
            )
            self.tools.register(ToolSearchTool(self.tool_search_controller))
            self.tools.register(ToolCallTool(self.tool_search_controller))
            # ``first=True``: filter the tool list before CacheOptimizer marks
            # the final tool with ``cache_control`` (else the marked tool may be
            # filtered out and the breakpoint lost).
            self.strategies.register(
                ToolSearchStrategy(
                    self.tool_search_controller,
                    compaction_threshold=cfg.compaction_threshold,
                ),
                first=True,
            )

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
        reserved_output = send_max_tokens(getattr(self.provider, "generation", None), self.model)
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
        if self._checkpoint is None or outcome.status != "interrupted":
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
        try:
            await self.backend.store(session_key, messages_slice)
        except Exception:
            logger.exception(
                "backend.store failed for session {}; turn data preserved in session log, plugin-side indexing skipped",
                session_key,
            )

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

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        # Set flag synchronously before the first await — asyncio is single-threaded so no
        # context switch occurs here; a lock is not needed for this mutual-exclusion pattern.
        self._mcp_connecting = True
        try:
            await self._start_executor()  # ensure executor is live before MCP servers connect
            from raven.agent.tools.mcp import connect_mcp_servers

            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(
                self._mcp_servers,
                self.tools,
                self._mcp_stack,
                executor=self._executor,
            )
            # Re-apply blacklist: MCP servers may register tool names that
            # also appear in ``disabled_tools`` (e.g. ``mcp_<server>_search``).
            self._apply_disabled_tools()
            self._mcp_connected = True
            self._mcp_connecting = False
        except Exception:
            # Reset in-progress flag so a subsequent call can retry.
            self._mcp_connecting = False
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
            raise

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

    def _set_tool_context(
        self, channel: str, chat_id: str, message_id: str | None = None, session_key: str | None = None
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron", "deep_research"):
            if tool := self.tools.get(name):
                if not hasattr(tool, "set_context"):
                    continue
                if name == "message":
                    tool.set_context(channel, chat_id, message_id)
                elif name in ("spawn", "deep_research"):
                    tool.set_context(channel, chat_id, session_key or f"{channel}:{chat_id}")
                else:
                    tool.set_context(channel, chat_id)

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
        diverts to this helper instead of ``chat_with_retry``. Each non-empty
        content chunk fires the callback; tool_call fragments are merged
        positionally; the final response object is shape-compatible with what
        ``chat()`` would have returned.

        v0.1 first-cut tool-call merge: assumes one tool call per position,
        fragments arrive in order, ``id`` / ``function.name`` appear in the
        first fragment, ``function.arguments`` is the concatenation of
        per-fragment arguments strings. Multi-tool / out-of-order merging is
        a v0.2 ask.

        No retry on transient errors in v0.1 stream mode — adding retry to
        a partially-streamed call requires either restarting from scratch
        (wasteful) or resume-from-offset (provider-specific). Deferred.
        """
        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_call_slots: list[dict[str, Any]] = []
        final_usage: dict[str, Any] | None = None
        had_error = False
        error_content: str | None = None
        error_classification: ErrorClassification | None = None
        upstream_finish_reason: str | None = None

        # aclosing() guarantees the async generator (and its underlying stream)
        # is closed when a TimeoutError from the per-chunk idle cap unwinds the
        # loop, so a stalled stream terminates with a structured error instead
        # of hanging or leaking the connection. The stream path has no retry
        # (see docstring), so the error surfaces as the turn's response.
        try:
            async with aclosing(self.provider.chat_stream(messages=messages, tools=tools, model=model)) as stream:
                async for delta in stream:
                    if delta.finish_reason == "error":
                        # A non-streaming provider's chat() error, replayed
                        # through the fallback as its single terminal delta.
                        # Its content is the error text, not a token to render
                        # or accumulate -- surface it via error_classification
                        # instead of the normal success collation below.
                        had_error = True
                        error_content = delta.content
                        error_classification = delta.error_classification
                        if delta.usage is not None:
                            final_usage = delta.usage
                        continue
                    if delta.finish_reason:
                        upstream_finish_reason = delta.finish_reason
                    reasoning_delta = getattr(delta, "reasoning_content", None)
                    if reasoning_delta:
                        reasoning_buf.append(reasoning_delta)
                        if on_reasoning_delta is not None:
                            await on_reasoning_delta(reasoning_delta)
                    if delta.content:
                        content_buf.append(delta.content)
                        if on_token_delta is not None:
                            await on_token_delta(delta.content)
                    if delta.tool_call_delta:
                        _merge_tool_call_fragments(
                            tool_call_slots,
                            delta.tool_call_delta,
                        )
                    if delta.usage is not None:
                        final_usage = delta.usage
        except TimeoutError:
            return LLMResponse(
                content="".join(content_buf),
                finish_reason="error",
                error_classification=self.provider.classify_error(TimeoutError()),
            )

        if had_error:
            return LLMResponse(
                content=error_content,
                finish_reason="error",
                error_classification=error_classification,
                usage=final_usage or {},
            )

        tool_calls, args_parse_failed = _finalize_tool_calls(tool_call_slots)

        sent_max_tokens, truncated = flag_truncation(
            getattr(self.provider, "generation", None),
            model=model,
            finish_reason=upstream_finish_reason,
            usage=final_usage,
            tool_calls=tool_calls,
            args_parse_failed=args_parse_failed,
        )

        finish_reason = upstream_finish_reason or ("tool_calls" if tool_calls else "stop")

        content = "".join(content_buf)
        reasoning_content = "".join(reasoning_buf) or None
        # getattr because the loop accepts duck-typed providers (test stubs and
        # thin adapters implement just chat/chat_stream); absent means the
        # LLMProvider default, False.
        emits_unparsed = getattr(self.provider, "emits_unparsed_reasoning", None)
        if reasoning_content is None and emits_unparsed is not None and emits_unparsed():
            split_reasoning, content = split_orphan_think(content)
            reasoning_content = split_reasoning

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=final_usage or {},
            reasoning_content=reasoning_content,
            truncated=truncated,
            max_tokens=sent_max_tokens,
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
        model: str | None = None,
        fallback_models: list[str] | None = None,
        injected_skill_ids: list[str] | None = None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
        on_episode_start: Callable[[int], Awaitable[None]] | None = None,
        usage_sink: dict[str, Any] | None = None,
        drain: Drain | None = None,
    ) -> tuple[str | None, list[str], list[dict], TurnOutcome]:
        """Run the agent iteration loop.

        ``drain``, when wired, is called at the top of each iteration to pull
        any user messages injected mid-turn (BusyPolicy.INJECT) and merge them
        as user turns before the next LLM call.

        ``extraction_session_id`` is the session key passed to local
        skill extraction when a turn completes. When ``None``, extraction
        is skipped (no pipeline wired).
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        session_key = extraction_session_id or ""
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
                # Same decision as the streaming branch above, which reaches it
                # inside _llm_call_stream. Without this the CLI -- which wires
                # no token callback and so lands here -- reports a cut-off tool
                # call as a missing required field, which is the misdiagnosis
                # this whole path exists to remove.
                response.max_tokens, response.truncated = flag_truncation(
                    getattr(self.provider, "generation", None),
                    model=call_model,
                    finish_reason=response.finish_reason,
                    usage=response.usage,
                    tool_calls=response.tool_calls,
                )
            # TokenWise after-hook: strategies observe the response for
            # usage tracking, budget enforcement, etc. Errors are swallowed.
            usage_snapshot = self._build_usage_snapshot(response, call_model, session_key)
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
                if self._context_window_explicit:
                    context_max = self.context_window_tokens
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
                abort_action = False
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
                                # Tool-authored call label; None -> UI derives one.
                                "display": _tool.display_call(tool_call.arguments) if _tool else None,
                            },
                        )
                    if tool_call.name == "exec":
                        exec_tool = self.tools.get("exec")
                        if isinstance(exec_tool, ExecTool):
                            exec_tool.set_tool_call_id(tool_call.id)
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
                    preview = display_src[:200]
                    logger.info(
                        "Tool result: {} duration={}ms result={}",
                        tool_call.name,
                        duration_ms,
                        preview.replace("\n", " ")[:200],
                    )
                    if emit_tool_event:
                        await on_tool_event(
                            "complete",
                            {
                                "tool_call_id": tool_call.id,
                                "result_preview": preview,
                                "truncated": len(display_src) > 200,
                            },
                        )
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
                    if attach_blocks:
                        pending_images.extend(attach_blocks)
                    if getattr(result, "abort_action", False):
                        abort_action = True
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
                                (
                                    "Error: Tool call was not executed because a prior safety "
                                    "decision terminated this action."
                                ),
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

                if abort_action:
                    # A normal tool result starts another model iteration. That
                    # is specifically unsafe here: the next plan can translate
                    # the rejected operation into an equivalent interpreter,
                    # script, or tool call. Finish the turn in runtime code and
                    # expose only the non-destructive continuation question.
                    # Streaming callers need the explicit callback because no
                    # final model response exists to generate token deltas.
                    messages = self.context.add_assistant_message(messages, _ABORTED_ACTION_REPLY)
                    final_content = _ABORTED_ACTION_REPLY
                    if on_token_delta is not None:
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
                        + loop_break_nudge(loop_fail_key[0], loop_fail_streak)
                    )
                    loop_fail_streak = 0  # fire once per fresh streak
                # After the nudge above, which needs the last message to still be
                # the tool result it appends to. Also after the abort_action
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
        if self._checkpoint is not None:
            # Per-turn snapshot: one commit covering all of this turn's edits,
            # for both normal and interrupted exits (matches Claude Code/Cursor
            # granularity). Best-effort — commit_turn never raises.
            label = f"turn {session_key or 'anon'} [{status}]"
            cid, changed = await self._checkpoint.commit_turn(label)
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

    async def await_pending_extractions(
        self,
        flush_session_id: str | None = None,
        *,
        wait: bool = True,
    ) -> None:
        """No-op retained for CLI / batch-mode call-site compatibility.

        Previously this flushed the local skill-extraction buffer and
        blocked on its in-flight tasks. That embedded pipeline was
        removed — case-to-skill distillation now lives in the
        :class:`MemoryBackend` plugin (``backend.store`` /
        ``backend.feedback``), which the after-turn pipeline drives
        directly. Kept so ``raven agent`` callers don't need changing;
        the ``flush_session_id`` / ``wait`` knobs are inert.
        """
        del flush_session_id, wait
        return None

    async def close_mcp(self) -> None:
        """Close MCP connections and the sandbox executor."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None
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
                        session.record({"role": "user", "content": content, "timestamp": _ts})
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
                        session.record({"role": "user", "content": content, "timestamp": _ts})
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

        context_messages = self._context_messages_for_session(session)
        # SkillForge: Selector picks top-K. See note in the system-message
        # branch above — empty return falls back to the full directory.
        # Phase B-3: routed via ``_select_skills_for_turn`` so the new
        # ``default`` engine short-circuits selection here.
        selected_skills = await self._select_skills_for_turn(
            content,
            context_messages,
        )
        # ── Model routing (EcoClaw-style) ────────────────────────────────────
        # Ahead of assembly, not after it: assembly decides whether an
        # attachment is inlined as a picture or described in text, and that
        # question is about the model the request will actually reach. Routing
        # needs only ``content``, so asking first costs nothing and stops one
        # model's verdict from shaping a message another model receives.
        routed_model: str | None = None
        fallback_models: list[str] = []
        if self.router is not None:
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
            selected_skills=selected_skills or None,
            model=routed_model,
        )

        extraction_sid = None  # Phase B-1: embedded extraction removed; always None now.
        turn_start_idx = len(initial_messages) - 1
        final_content, _, all_msgs, outcome = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress,
            extraction_session_id=extraction_sid,
            model=routed_model,
            fallback_models=fallback_models,
            injected_skill_ids=self._collect_injected_skill_ids(selected_skills),
            on_token_delta=on_token_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_event=on_tool_event,
            on_episode_start=on_episode_start,
            usage_sink=usage_sink,
            drain=drain,
        )
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

        self._save_turn(session, all_msgs, turn_start_idx)
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

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if entry.get("_recovery_synthetic"):
                continue  # #1a synthetic recovery nudge — never persist scaffolding
            if entry.get(_ATTACHED_IMAGE_KEY):
                # Already filtered upstream; kept because this is the last gate
                # before a write that cannot be undone, unlike the code above it.
                continue
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
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
        """Turn boundary for a live ``/model`` switch; see ``_run_turn``.

        A parked switch is adopted here, before the turn reads
        ``self.provider`` for the first time, and the count kept for the
        duration is what parks the next one. Wrapping rather than
        snapshotting because the provider is read from ``self`` at eight
        sites in this module and by the context engine and consolidator
        underneath them -- one boundary covers all of them, a snapshot
        would have to be threaded through each.

        Both ends gate on zero, because turns overlap: a user turn and a
        proactive turn hold slots in separate pools. Adopting on the way in
        would otherwise land a switch parked for a turn that is still
        running, and clearing a flag on the way out would unpark it just as
        wrongly. Adopting again on the last exit is what keeps a park from
        outliving the turns it was waiting on.
        """
        if self._turns_in_flight == 0:
            self._adopt_pending_provider()
        self._turns_in_flight += 1
        try:
            return await self._run_turn(
                req,
                emit,
                drain,
                stream=stream,
                inline_tool_stream=inline_tool_stream,
                usage_sink=usage_sink,
                text_sink=text_sink,
            )
        finally:
            self._turns_in_flight -= 1
            if self._turns_in_flight == 0:
                self._adopt_pending_provider()

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
            await self._start_executor()
            await self._connect_mcp()
            out = await self._process_message(
                req,
                session_key=cid,
                on_progress=on_progress,
                on_token_delta=on_token if stream else None,
                on_reasoning_delta=on_reasoning if stream else None,
                on_tool_event=on_tool,
                on_episode_start=on_episode if stream else None,
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


def _merge_tool_call_fragments(
    slots: list[dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    """Merge a single chat_stream tool_call_delta into accumulator slots.

    Each slot follows the shape ``{id, function: {name, arguments_buf: [str]}}``.
    Per provider chunk semantics (OpenAI/LiteLLM): each tool call fragment
    carries an ``index`` field; ``id`` / ``function.name`` typically appear in
    the first fragment for that index, ``function.arguments`` is a JSON string
    streamed in pieces.

    Respects the ``index`` field so parallel multi-tool streams do not
    collapse into ``slots[0]``. Fragments without an ``index`` default to 0
    (single-tool case, backward-compatible).
    """
    incoming = delta.get("tool_calls") or []
    if not incoming:
        return
    for tc in incoming:
        idx = int(tc.get("index", 0) or 0)
        while len(slots) <= idx:
            slots.append({"id": None, "function": {"name": None, "arguments_buf": []}})
        slot = slots[idx]
        if tc.get("id") and not slot["id"]:
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name") and not slot["function"]["name"]:
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments_buf"].append(fn["arguments"])


def _finalize_tool_calls(slots: list[dict[str, Any]]) -> tuple[list[ToolCallRequest], bool]:
    """Convert accumulator slots into final ToolCallRequest list.

    Also reports whether any slot's arguments failed to parse. An incomplete
    JSON blob is the local, zero-dependency evidence that generation was cut
    short: it needs no cooperation from the backend, and it is available even
    when the backend reports a clean stop.
    """
    result: list[ToolCallRequest] = []
    parse_failed = False
    for slot in slots:
        name = slot["function"]["name"]
        if not name:
            continue
        args_text = "".join(slot["function"]["arguments_buf"])
        try:
            args = json.loads(args_text) if args_text else {}
        except json.JSONDecodeError:
            args = {"_raw_arguments": args_text}
            parse_failed = True
        result.append(
            ToolCallRequest(
                id=slot["id"] or "",
                name=name,
                arguments=args,
            )
        )
    return result, parse_failed
