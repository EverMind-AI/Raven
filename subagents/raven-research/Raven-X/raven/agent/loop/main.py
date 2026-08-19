"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from raven.agent.context import TOOL_OUTPUT_ELIDED, ContextBuilder
from raven.agent.flow.answer_text import visible_answer
from raven.agent.flow.turn_task import turn_question
from raven.agent.loop.journal import TurnJournal
from raven.agent.loop.recovery import (
    POST_TOOL_NUDGE,
    RecoveryAction,
    RecoveryLimits,
    classify_empty_response,
)
from raven.agent.subagent import SubagentManager
from raven.agent.tools.ask_user import AskUserTool
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
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.sandbox import SandboxConfig, SandboxExecutor, SandboxInitError, build_executor
from raven.security.benchmark_containment import BenchmarkContainment
from raven.session.manager import Session, SessionManager
from raven.spine.turn import Origin
from raven.token_wise.pricing import resolve_context_window
from raven.tracing import semconv, trace
from raven.utils.helpers import estimate_prompt_tokens

# NOTE: ``raven.context_engine`` is intentionally imported lazily (inside
# ``__init__`` and ``_assemble_context_messages``) to break a runtime
# import cycle: ``raven.agent.__init__`` eagerly loads AgentLoop,
# while ``raven.context_engine.curator`` imports ``ContextBuilder`` from
# ``raven.agent.context`` — a module-level top-down ``from
# raven.context_engine import ...`` here re-enters a partially-initialized
# package and raises ImportError on ``TurnContext``.

if TYPE_CHECKING:
    from raven.agent.hook import AgentHook, CompositeHook
    from raven.agent.tools.base import Tool
    from raven.config.raven import (
        ContextConfig,
        DRFlowConfig,
        MemoryConfig,
        RuntimeConfig,
        SkillForgeRouterConfig,
    )
    from raven.config.schema import ChannelsConfig, ExecToolConfig
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


@dataclass
class TurnOutcome:
    """Result of one ``_run_agent_loop`` turn beyond its text reply.

    ``status`` distinguishes a normal completion from a max-iteration
    interruption or an LLM error — so the caller never mistakes "ran out of
    budget" for "done" (Bug2 / decision B). ``checkpoint_id`` and
    ``edited_files`` carry the shadow-git snapshot info used to build the
    next turn's recovery prompt. ``synthesized_on_exhaustion`` is True only
    when the reply came from the exhaustion wrap-up call — a best-effort
    summary, not a task completion; the persisted assistant message carries
    the same marker so trajectories keep the distinction.
    """

    status: str = "completed"  # "completed" | "interrupted" | "error"
    checkpoint_id: str | None = None
    edited_files: list[str] = field(default_factory=list)
    synthesized_on_exhaustion: bool = False


def _flow_version() -> str:
    """Identity of the harness build stamped on persisted trajectory lines.

    Currently the installed package version; a workflow identity segment
    (e.g. a DR flow name) can prefix it later without changing consumers.
    """
    from raven import __version__

    return f"raven-{__version__}"


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

# dr@2.9: the same wrap-up, for a turn that died on the COMPLETION budget rather
# than the tool-calling budget. It needs its own text because the sentence above
# ("You've used up the tool-calling budget") is a false statement here: measured on
# eval_web_dr27_20260807, 22/60 HLE anchor items ended with finish_reason='length'
# on turn 1 with n_search=0 - no tool was ever called, let alone budgeted out.
# Telling the model it exhausted a budget it never touched invites it to explain
# a constraint that does not exist instead of answering.
#
# Why this is worth a call at all: sampling those items showed the model had
# ALREADY reached the answer and said so (one wrote "I think 76 is the answer",
# and 76 was gold) before talking itself out of it until the budget ran out.
# 48% of truncated items contain a conclusion sentence, and 10/10 sampled ones
# then contradict it. So the instruction below is deliberately narrow: commit to
# what you already concluded. It must NOT invite fresh reasoning - that is the
# behaviour that ran out of budget in the first place.
_TRUNCATED_SYNTHESIS_PROMPT = (
    "Your previous message hit the generation length limit and was cut off "
    "mid-writing, so it never delivered a final answer. No tools are available "
    "now. Do not start a new analysis and do not re-derive anything: read back "
    "what you already wrote above, take the conclusion you had already reached, "
    "and state it directly as your final answer. If you had narrowed it to a "
    "few candidates, give the one you favoured and say it is your best "
    "judgement. Answer in one short paragraph. Do not ask questions — there is "
    "no further turn to answer them. Reply in the same language as the user's "
    "request (this instruction is in English, but it is not the conversation "
    "language)."
)

def _is_truncated_answerless(
    final_content: str | None,
    finish_reason: str | None,
    *,
    salvaged: bool,
) -> bool:
    """Did this turn end because the COMPLETION budget cut it off mid-answer?

    Module-level and not a method so it can be exercised directly: a test that
    re-implements a predicate asserts its own copy, and keeps passing after the
    original changes.

    ``closing_tag_required=True`` unconditionally. ``_think_closing_tag_required``
    is False whenever the flow is off, so keying on it would judge the anchor with
    a permissive bar and the treated arm with the strict one - repairing the
    treated arm while leaving the anchor broken, i.e. manufacturing an
    arm-correlated defect in the course of fixing one.

    ``salvaged`` is read from the gate's commit flag, never re-derived: a
    committed salvage structurally never carries a closing tag (see
    ``ForcedFinalizeGate._mark_committed``), so recomputing the test on one always
    says "answerless". That is how the terminal seam once ran 22 salvage calls
    over 16 items.
    """
    return (
        final_content is not None
        and finish_reason == "length"
        and not salvaged
        and not visible_answer(final_content, closing_tag_required=True)
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

# Names this process's product-side ledger file, one per turn. Paired with the pid so two
# processes sharing an instance data dir cannot collide, and counted rather than
# timestamped so two turns opened inside the same clock tick still get separate files.
_TURN_SEQ = itertools.count()

# Failure markers a plain retry would likely clear — these must NOT count toward
# the tool-failure-loop streak (nudging on a 429 that self-heals is just noise).
_TRANSIENT_FAILURE_MARKERS = (
    "429",
    "rate limit",
    "timed out",
    "timeout",
    "no healthy upstream",
    "502",
    "503",
)
# Successful-but-empty results: the tool ran fine and just found nothing. A
# repeated empty search is legitimate exploration, not a stuck dead call, so it
# must NOT count toward the failure streak.
_EMPTY_SUCCESS_MARKERS = ("no matches found", "no files found")


def _text_of(content: object) -> str:
    """Flatten a message's content to the text a classifier can read.

    Multimodal turns arrive as a list of parts; the image parts carry data URIs
    that are both useless to a text classifier and large enough to dominate its
    input, so only ``text`` parts survive.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c["text"] for c in content
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
        )
    return ""


def _is_hard_tool_failure(result: object) -> bool:
    """True for a deterministic tool failure (recurs on an identical retry).

    False for success or a transient/retryable error. Used to decide whether a
    repeated identical tool call is a stuck loop worth breaking.
    """
    s = str(result)
    low = s.lower()
    if any(m in low for m in _TRANSIENT_FAILURE_MARKERS):
        return False
    if s.strip().rstrip(".").lower() in _EMPTY_SUCCESS_MARKERS:
        return False
    m = re.search(r"Exit code:\s*(-?\d+)", s)
    if m:
        return m.group(1) != "0"
    # Real not-found failures (file / dir / path / old_text) all start with
    # "Error:" or carry a non-zero exit code, so those are already covered; a
    # bare "not found" scan would only risk flagging successful output that
    # merely mentions the phrase.
    return s.lstrip().startswith("Error") or "error:" in low[:80]


def _loop_break_nudge(tool: str, n: int, *, dr_mode: bool = False) -> str:
    """Injected when the same tool fails deterministically N times running.

    Two texts on purpose. The product wording names remedies a DR arm cannot
    reach - other tools, shell commands, file paths - and carried the same
    restart idiom the spin detector reads, so DR mode gets a version whose only
    remedies are the two tools it actually has. The product wording is left
    untouched so the flow-off anchor stays byte-comparable with earlier batches:
    an anchor's job is to be a stable instrument, not to be well written.
    """
    if dr_mode:
        return (
            f"[loop] `{tool}` has returned the same kind of error {n} times. Stop calling it. "
            "If it is the retrieval service, work from the results you already have and name "
            "in your answer which part stayed unverified. Do not call it again with the same "
            "arguments."
        )
    return (
        f"[loop] `{tool}` has failed {n} times in a row with the same kind of error. "
        "Stop repeating it. If it is an external dependency (network/API/search), "
        "complete what you can offline from local data and report what stayed blocked. "
        "If it is a file or path error, re-examine the EXACT path before any retry — "
        "do not call it again unchanged. Otherwise change approach: a different tool, "
        "command, or strategy."
    )


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
    # Tool results are capped at ingestion, with headroom for the untrusted
    # fence and appended in-history notes (budget line, nudges), so the
    # persist-time cap above never fires and cannot chop them off — the
    # persisted trajectory must equal what the model saw (train-serve
    # homomorphism). Uncapped ingestion also kills turns outright: a single
    # ``web_fetch`` defaults to 50k chars, and two of those in one iteration
    # push a 64k-window request past the limit with nothing left to elide.
    _DR_RESULT_HEADROOM = 1_000
    # Max emergency context shrinks per turn before a context overflow is fatal.
    _MAX_COMPRESS_RETRIES = 2
    # Context-overflow recovery, first move: the completion reserve is part of
    # the request, so an overflow can be lossless-fixed by asking for fewer
    # output tokens instead of eliding evidence. Bounded and monotone (each
    # attempt halves), with a floor below which a clamp is pointless (the model
    # could not fit reasoning + answer) and a margin covering the gap between
    # our token estimate and the server's count.
    _MAX_CLAMP_RETRIES = 2
    _MIN_CLAMPED_COMPLETION_TOKENS = 2_048
    _COMPLETION_CLAMP_MARGIN_TOKENS = 1_500
    # Most recent tool results kept intact when emergency-shrinking; older ones
    # are elided (their bodies are the bulk of mid-turn context growth).
    _SHRINK_KEEP_RECENT_TOOL_RESULTS = 3
    # Tool-failure-loop break: nudge after the same tool fails deterministically
    # this many times running; cap the nudges per turn so it can't itself loop.
    _LOOP_BREAK_THRESHOLD = 2
    _LOOP_BREAK_MAX = 2
    # Hook-requested rollbacks (pop the iteration, re-sample) per turn — a
    # global backstop over any per-observer bound; past it, rollback decisions
    # degrade to pass-through so a misbehaving observer can't spin the loop.
    # Sized above the worst-case sum of per-observer budgets (loopscan 2 +
    # dup-query 2 + spin-breaker 1 + force-finalize 1) so the cap only stops
    # a misbehaving observer, never a within-budget terminal gate.
    _MAX_HOOK_ROLLBACKS = 6
    # Generation parameters a rollback may override on the re-sample call.
    # Anything else is dropped: these kwargs feed provider calls directly,
    # and an unknown key from a hook must not TypeError the whole turn.
    _ROLLBACK_OVERRIDE_KEYS = frozenset({"temperature", "max_tokens", "reasoning_effort"})

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        web_corpus_endpoint: str | None = None,
        web_benchmark_containment: bool = False,
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
        # In-loop degeneration/budget guards, on by default. ``None`` wires
        # the default set (loopscan, budget, duplicate-query); pass an
        # explicit list to replace it, ``[]`` to run bare.
        loop_observers: "list[AgentHook] | None" = None,
        # Deep-research flow config. When enabled, the loop runs in DR mode:
        # DR observers + prompt section + reshaped web tools + versioned
        # flow stamp. ``None`` / disabled keeps the chat flow byte-identical.
        dr_flow: "DRFlowConfig | None" = None,
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
        self.max_iterations = max_iterations
        # Empty-response recovery budgets. None → enabled defaults.
        self._recovery_limits = empty_recovery if empty_recovery is not None else RecoveryLimits()
        # dr@3.2: resolve the window ONCE, here, rather than at each consumer.
        #
        # ``_window_for`` already existed and had folded four call sites together, but
        # three consumers were still reading the raw configured number: the context
        # engine (which is what builds ``HistoryTrimmer``, i.e. the trim point that the
        # whole elision mechanism turns on), the memory consolidator, and the flow-off
        # ``BudgetObserver``. On the served 35B all of them agree by accident - its model
        # id resolves to nothing, so every branch falls back to the same configured value
        # - which is exactly why the divergence has zero symptom on every published batch
        # and would appear only when someone runs this on a backend the resolver knows.
        #
        # ⚠️ The last one is arm-correlated: ``BudgetObserver`` is installed only on the
        # flow-OFF branch, so the anchor and the DR arm would be told two different window
        # sizes. Resolving at each site would fix today's three and leave the fourth
        # consumer - the one nobody has written yet - wrong again; this project has paid
        # four times for the "N copies of one number" shape. So the attribute itself now
        # holds the resolved value, and ``_window_for(model)`` remains only for the
        # per-call model override.
        self.context_window_tokens = (
            resolve_context_window(self.model) or context_window_tokens
        )

        from raven.agent.flow import build_dr_flow

        if dr_flow is not None:
            self._assert_evidence_survives_shrink(dr_flow)
        self._dr_flow = (
            build_dr_flow(
                dr_flow,
                provider,
                max_iterations=self.max_iterations,
                # dr@3.0: the resolved window, not the configured one, so the two
                # observers that divide by it quote the model the turn actually
                # runs on. Both consumers are flow-only, so the anchor - which
                # builds no assembly at all - cannot move. ``_make_token_budget``
                # deliberately still uses the configured value: it sets
                # ``available_history`` on BOTH arms, so changing it is an
                # anchor-moving change and belongs to its own labelled round.
                context_window_tokens=self._window_for(),
            )
            if dr_flow is not None
            else None
        )
        if self._dr_flow is not None and self._dr_flow.max_iterations:
            self.max_iterations = self._dr_flow.max_iterations
        # Off unless the flow is on, so chat behavior stays byte-identical.
        self._think_closing_tag_required = (
            self._dr_flow is not None and self._dr_flow.think_closing_tag_required
        )
        # Trajectory stamp: the flow identity prefixes the harness build so
        # data produced by different flow versions stays distinguishable.
        self._flow_version = (
            f"{self._dr_flow.version}/{_flow_version()}" if self._dr_flow is not None else _flow_version()
        )
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        self.web_corpus_endpoint = web_corpus_endpoint
        # One instance shared by web_search and web_fetch: an arm reports one
        # firing rate, not two that have to be added up correctly downstream.
        self.benchmark_containment = BenchmarkContainment(enabled=web_benchmark_containment)
        from raven.config.schema import MediaGenConfig

        self.media_config = media_config or MediaGenConfig()
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

        # dr@3.0 product surface: the last turn's conversation-gate verdict, kept
        # for the observer payload. ``None`` means the gate did not run, which is
        # a third state and not the same as "it ran and said no" - the two are
        # separated here for the same reason ``sat_action`` distinguishes "did not
        # fire" from "was not installed".
        self._turn_mode: Any = None

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

        self.context_engine: "ContextEngine" = build_context_engine(
            workspace=workspace,
            config=context_config,
            builder=self.context,
            provider=provider,
            model=self.model,
            # dr@3.2: the RESOLVED window (see __init__). This is what builds
            # HistoryTrimmer, i.e. the trim point the whole elision mechanism
            # turns on -- it must not read a configured number the backend
            # disagrees with.
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
            extra_segment_builders=(
                [self._dr_flow.segment_builder]
                if self._dr_flow is not None and self._dr_flow.segment_builder is not None
                else None
            ),
            drop_segments=(self._dr_flow.drop_segments if self._dr_flow is not None else None),
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
            web_corpus_endpoint=web_corpus_endpoint,
            benchmark_containment=self.benchmark_containment,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            sandbox_config=sandbox_config,
            owned_ids=self._owned_ids,
            max_concurrent=max_concurrent_subagents,
            max_spawns_per_hour=max_subagent_spawns_per_hour,
            # dr@2.3: the sub-agent surface must be a subset of ours. Pass the BOUND
            # METHOD, not its result - self.tools is empty on this line and is populated
            # afterwards, so a snapshot would fence against the empty set and be ignored
            # (the same shape that makes undeclared_tool fire on nothing).
            parent_tool_names=self.tools.names,
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
            context_window_tokens=self.context_window_tokens,   # dr@3.2: resolved
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            now_fn=now_fn,
        )

        self._consolidation_tasks: set[asyncio.Task] = set()

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
        #   0. Loop observers first — in-loop degeneration/budget guards,
        #      on by default like the tool-failure loop break. A doomed
        #      response is rolled back before any later hook spends work
        #      (or an LLM call) judging it.
        #   1. OnUserInboundAdapter — pure observer, never
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
        if loop_observers is None:
            from raven.agent.hook.observers import (
                BudgetObserver,
                DuplicateQueryObserver,
                LoopscanObserver,
            )

            loop_observers = [
                LoopscanObserver(),
                BudgetObserver(
                    max_iterations=self.max_iterations,
                    context_window_tokens=self.context_window_tokens,
                ),
                DuplicateQueryObserver(),
            ]
        self.hooks.extend(loop_observers)
        if self._dr_flow is not None:
            self.hooks.extend(self._dr_flow.observers)
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
        self._apply_dr_tools_allowlist()
        self._assert_corpus_containment()

    _CORPUS_ESCAPE_TOOLS = ("exec", "spawn")

    def _assert_corpus_containment(self) -> None:
        """Refuse to run pinned-corpus retrieval alongside a tool that escapes it.

        ``web_corpus_endpoint`` routes web_search/web_fetch to a fixed corpus so a
        measurement reads the same retrieval every time. A shell or a sub-agent
        reaches the open web around that route, and nothing in the config looks
        wrong when it does: the escape shows up only in the trajectory, after the
        batch is already spent. So the check runs against the live registry -- the
        surface that both filters actually left behind, including tools a plugin
        injected -- rather than against the config that was asked for.

        Local-file tools are out of scope: they cannot produce a page the corpus
        does not have.

        dr@2.3: benchmark containment is the same contract and used to skip this
        check entirely. That axis has no corpus endpoint, so the guard returned on
        its first line -- on the one axis where the fence is easiest to walk around,
        because ``curl`` reaches the dataset file the web tools were just taught to
        refuse. Either fence being active is enough reason to run.
        """
        fence = (
            "web corpus endpoint" if self.web_corpus_endpoint
            else "benchmark containment" if self.benchmark_containment.enabled
            else None
        )
        if fence is None:
            return
        escapes = [n for n in self._CORPUS_ESCAPE_TOOLS if self.tools.has(n)]
        escapes += [n for n in self.tools.names() if n.startswith("mcp_")]
        if escapes:
            raise ValueError(
                f"{fence} is active but {sorted(escapes)} stayed registered; "
                "these reach content the fence does not cover, which voids the "
                "containment the run depends on. Add them to tools.disabledTools "
                "(a drFlow allowlist alone does not cover tools registered later)."
            )

    def _apply_dr_tools_allowlist(self) -> None:
        """DR mode runs a narrow tool surface: every extra tool is context
        noise, a mis-call surface and a distribution drift risk. Run after
        default registration and after MCP connect, like the denylist."""
        if self._dr_flow is None or not self._dr_flow.tools_allowlist:
            return
        allow = set(self._dr_flow.tools_allowlist)
        for name in self.tools.names():
            if name not in allow:
                self.tools.unregister(name)

    @classmethod
    def _assert_evidence_survives_shrink(cls, dr_flow: "DRFlowConfig") -> None:
        """Warn when a node asks for more evidence items than a shrink keeps.

        ``_emergency_shrink`` keeps only the most recent
        ``_SHRINK_KEEP_RECENT_TOOL_RESULTS`` tool bodies verbatim, so a node
        configured to read more than that gets placeholders for the difference on
        any turn that overflowed. Both evidence-reading nodes now skip elided
        bodies and reach further back, which means the shortfall is silent —
        hence this check, so the two numbers cannot drift apart unnoticed.
        """
        if not getattr(dr_flow, "enabled", False):
            return
        keep = cls._SHRINK_KEEP_RECENT_TOOL_RESULTS
        for node in ("force_finalize", "verify"):
            wanted = getattr(getattr(dr_flow, node, None), "evidence_items", None)
            if wanted and wanted > keep:
                logger.warning(
                    "drFlow.{}.evidence_items={} exceeds the {} tool results kept verbatim by an "
                    "emergency shrink; the evidence pack skips elided bodies and may therefore be "
                    "shorter than requested on overflowing turns",
                    node,
                    wanted,
                    keep,
                )

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
            )
        )
        search_kwargs = self._dr_flow.web_search_kwargs if self._dr_flow is not None else {}
        fetch_kwargs = self._dr_flow.web_fetch_kwargs if self._dr_flow is not None else {}
        self.tools.register(
            WebSearchTool(
                api_key=self.brave_api_key,
                proxy=self.web_proxy,
                corpus_endpoint=self.web_corpus_endpoint,
                containment=self.benchmark_containment,
                **search_kwargs,
            )
        )
        self.tools.register(
            WebFetchTool(
                api_key=self.jina_api_key,
                proxy=self.web_proxy,
                corpus_endpoint=self.web_corpus_endpoint,
                containment=self.benchmark_containment,
                **fetch_kwargs,
            )
        )
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
                UseSkillTool(client=self._skill_hub_client, registry=skill_registry),
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

    def _window_for(self, model: str | None = None) -> int:
        """This call's real context window, falling back to the configured one.

        One expression, four callers. It was three inline near-copies of
        ``resolve_context_window(model) or self.context_window_tokens`` plus a
        fourth consumer - the DR flow's budget note and spin breaker - that used
        the configured value alone. That fourth path is why this is a method
        rather than a repeated line: the divergence had no symptom on the served
        student, whose model id resolves to nothing, so both branches fell back to
        the same 65,536 and agreed by accident. On any model the resolver knows,
        the note told the model it was at 110% of a window it was nowhere near,
        while not one tool result had been elided.
        """
        return resolve_context_window(model or self.model) or self.context_window_tokens

    async def _decide_turn_mode(self, session: Session, content: Any) -> None:
        """Set this turn's research flag. Turn one by config, later turns by gate.

        A no-op unless the multi-turn product surface is enabled, and the flag it
        sets defaults to True, so with the feature off nothing downstream can tell
        this method exists. ``session.messages`` is still empty on the first turn
        of a session - the inbound message is recorded by ``_save_turn`` after the
        loop - which is what makes "is there a conversation yet" answerable here
        without a counter that could drift.
        """
        from raven.agent.flow.conversation import TurnMode, set_research_turn

        self._turn_mode = None
        if not getattr(self._dr_flow, "conversation_enabled", False):
            return
        prior = [m for m in session.messages if m.get("role") in ("user", "assistant")]
        gate = getattr(self._dr_flow, "conversation_gate", None)
        if not prior:
            # The first message is the one the user opened the product to ask, and
            # there is no conversation for a classifier to read. It follows the
            # config, which is also what makes every bench trajectory - always a
            # single turn - unreachable from the gate.
            mode = TurnMode(True, "first_turn")
        elif gate is None:
            mode = TurnMode(True, "config_always")
        else:
            text = content if isinstance(content, str) else _text_of(content)
            mode = await gate.decide(text, prior)
        set_research_turn(mode.research)
        self._turn_mode = mode
        if not mode.research:
            logger.info("conversation-gate: answering from context ({}) - {}", mode.source, mode.why)

    def _inject_research_memo(self, session: Session, messages: list[dict]) -> None:
        """Prepend this conversation's evidence record to the current user message.

        Injected into the message list rather than rendered as a prompt segment on
        purpose: a segment would sit in the cached prefix and change on every turn,
        re-billing the whole conversation at uncached rates. History changes every
        turn anyway, so this costs the cache nothing.

        Written outside the persisted range by ``_save_turn``'s stripper rather
        than by position, because the recovery block already prepends to this same
        message and the two must compose. Persisting it would accumulate: turn
        three would carry turn two's memo as history plus a freshly rendered one
        describing the same pages.
        """
        from raven.agent.flow.conversation import ResearchMemo, set_prior_sources

        # Cleared first, unconditionally: this runs on every turn and the value it
        # sets widens a grounding check's denominator, so inheriting the previous
        # turn's list would absolve a citation to a page nobody opened.
        set_prior_sources(())
        if not getattr(self._dr_flow, "research_memo", False) or not messages:
            return
        memo = ResearchMemo.from_metadata(session.metadata.get("dr_research_memo"))
        # Recorded before the render, and independently of whether the block ends up
        # injected: "did this conversation open that page" is a fact about the
        # conversation, not about whether we happened to show the list this turn.
        # ``opened``, not ``sources``: the rendered list is capped for context and the
        # check needs completeness. Sharing the cap meant a page opened in turn one and
        # cited in turn five was named a fabricated citation once the render cap had
        # evicted it - 18 pages opened against a 12-source cap on the run that showed it.
        set_prior_sources(tuple(memo.opened))
        block = memo.render(max_chars=getattr(self._dr_flow, "memo_max_chars", 2000))
        if not block:
            return
        last = messages[-1]
        if last.get("role") != "user":
            return
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = f"{block}\n\n{content}"
        elif isinstance(content, list):
            last["content"] = [{"type": "text", "text": block}] + content

    def _update_research_memo(self, session: Session) -> None:
        """Fold this turn's ledger rows into the conversation's memo.

        Reads the rows the agent loop stashed before it closed the ledger. A turn
        that opened nothing leaves an empty list and the merge is a no-op except
        for the turn counter, which is deliberate: "three turns, two sources" and
        "two turns, two sources" are different states and the memo should be able
        to say which one it is.
        """
        if not getattr(self._dr_flow, "research_memo", False):
            return
        from raven.agent.flow.conversation import ResearchMemo, take_turn_rows

        rows = take_turn_rows()
        memo = ResearchMemo.from_metadata(session.metadata.get("dr_research_memo"))
        memo.merge_ledger(
            rows,
            max_sources=getattr(self._dr_flow, "memo_max_sources", 12),
            max_queries=getattr(self._dr_flow, "memo_max_queries", 12),
            max_opened=getattr(self._dr_flow, "memo_max_opened", 200),
        )
        if memo.empty:
            return
        session.metadata["dr_research_memo"] = memo.to_metadata()

    def _research_turn(self) -> bool:
        """Whether the turn running in this context should run the DR machine.

        Always True when the multi-turn product surface is off, and always True on
        turn one, because the ContextVar behind it defaults True and only the
        conversation gate ever sets it. Callers therefore do not branch on the
        feature being enabled - a seam guarded by this reads identically on every
        measured arm, which is what keeps the anchor and the bench arms out of it.
        """
        from raven.agent.flow.conversation import is_research_turn

        return is_research_turn()

    def _make_token_budget(self, selected_skills: list[Any] | None = None) -> TokenBudget:
        """Compute a conservative per-turn prompt budget for the active engine."""
        reserved_output = int(getattr(getattr(self.provider, "generation", None), "max_tokens", 4096) or 4096)
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
    ) -> list[dict[str, Any]]:
        """Ask the active context engine for the main-agent message window."""
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
        # After the recovery block, so that when both fire the memo is the outer
        # one and ``_save_turn``'s prefix-anchored stripper still finds it.
        self._inject_research_memo(session, messages)
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
            self._apply_dr_tools_allowlist()
            self._assert_corpus_containment()
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

    def _set_tool_context(
        self, channel: str, chat_id: str, message_id: str | None = None, session_key: str | None = None
    ) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if not hasattr(tool, "set_context"):
                    continue
                if name == "message":
                    tool.set_context(channel, chat_id, message_id)
                elif name == "spawn":
                    tool.set_context(channel, chat_id, session_key or f"{channel}:{chat_id}")
                else:
                    tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

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

        cost = estimate_cost_usd(model, fresh, out_toks, cache_read, cache_write) or 0.0
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
        generation_overrides: dict[str, Any] | None = None,
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

        async for delta in self.provider.chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            **(generation_overrides or {}),
        ):
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

        tool_calls = _finalize_tool_calls(tool_call_slots)
        finish_reason = "tool_calls" if tool_calls else "stop"

        return LLMResponse(
            content="".join(content_buf),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=final_usage or {},
            reasoning_content="".join(reasoning_buf) or None,
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
        placeholder = TOOL_OUTPUT_ELIDED
        tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_idxs) <= cls._SHRINK_KEEP_RECENT_TOOL_RESULTS:
            return messages, 0
        elide = set(tool_idxs[: -cls._SHRINK_KEEP_RECENT_TOOL_RESULTS])
        shrunk: list[dict] = []
        elided = 0
        for i, m in enumerate(messages):
            if i in elide and m.get("content") and m.get("content") != placeholder:
                clean = dict(m)
                clean["content"] = placeholder
                shrunk.append(clean)
                elided += 1
            else:
                shrunk.append(m)
        return shrunk, elided

    def _fit_request(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None,
        current_cap: int | None,
    ) -> tuple[list[dict], int | None, int]:
        """Shrink the next request until it fits the window. No LLM calls.

        Returns ``(messages, completion_cap, n_elided)``. Lossless first: lower
        the completion reserve. Only when even the minimum reserve does not fit
        does it elide old tool bodies, because that is the evidence the answer
        has to come from. Never raises the reserve back — history only grows.
        """
        cap = current_cap or int(getattr(getattr(self.provider, "generation", None), "max_tokens", 0) or 0)
        if not cap:
            return messages, current_cap, 0
        window = self._window_for(model)
        used = estimate_prompt_tokens(list(messages), list(tools or []))
        budget = window - self._COMPLETION_CLAMP_MARGIN_TOKENS
        if used + cap <= budget:
            return messages, current_cap, 0

        elided_total = 0
        while used + self._MIN_CLAMPED_COMPLETION_TOKENS > budget:
            shrunk, elided = self._emergency_shrink(messages)
            if elided == 0:
                break
            messages = shrunk
            elided_total += elided
            used = estimate_prompt_tokens(list(messages), list(tools or []))
        if elided_total:
            logger.warning(
                "pre-call fit: elided {} old tool result(s) to stay inside the context window", elided_total
            )
        room = budget - used
        if room < self._MIN_CLAMPED_COMPLETION_TOKENS or room >= cap:
            return messages, current_cap, elided_total
        logger.info("pre-call fit: completion reserve {} -> {}", cap, room)
        return messages, int(room), elided_total

    def _completion_clamp(self, current_cap: int | None) -> int | None:
        """Smaller completion reserve to retry an overflow-rejected call with, or ``None``.

        Takes only the cap. It used to take ``(messages, tools, model, attempt)`` as
        well, and every one of them fed the estimator recomputation that made it dead
        code - four parameters whose only job was to make the function look like it
        was measuring something. ``attempt`` is not needed either: the caller assigns
        the returned cap back to ``completion_cap``, so a second call already sees the
        shrunk value and the factor compounds on its own.

        Called only after the provider rejected the request for size
        (``error_classification.should_compress``). **That rejection is the
        predicate**, and it is one ``_fit_request`` cannot have: it means the
        server's accounting disagreed with ours on a request we had already fitted.

        So this does NOT recompute ``room`` from ``estimate_prompt_tokens``. It used
        to, and that made it dead code: ``_fit_request`` runs pre-call on the same
        messages with the same estimator and the same ``room >= cap`` test, so the
        reactive test agreed with it by construction and returned ``None`` on all
        three paths. A backstop whose predicate is derivable from the thing it backs
        up is a copy of it. See ``DRFlowReactiveClampConfig`` for the walk-through
        and the measurement.

        ``None`` means a clamp cannot help: the feature is off, the reserve in force
        is unknown (clamping blind could raise the request instead of lowering it),
        or shrinking would leave no room for a usable answer.
        """
        if not getattr(self._dr_flow, "reactive_clamp", False):
            # Off by default, and the anchor builds no assembly at all, so this is
            # structurally unreachable on every arm. An explicit early return rather
            # than a condition woven into the arithmetic below, so that "this never
            # fires" is readable at a glance instead of being an emergent property of
            # three comparisons - which is precisely how the previous version hid.
            return None
        cap = current_cap or int(getattr(getattr(self.provider, "generation", None), "max_tokens", 0) or 0)
        if not cap:
            return None
        factor = float(getattr(self._dr_flow, "reactive_clamp_factor", 0.5) or 0.5)
        target = int(cap * factor)
        # Bounded below by a reserve that can still hold an answer. Committed answers
        # measure 121-389 tokens, so the 2,048 floor is generous on purpose; going
        # under it trades an overflow for an empty answer, which scores the same and
        # is harder to detect.
        if target < self._MIN_CLAMPED_COMPLETION_TOKENS or target >= cap:
            return None
        return target

    async def _synthesize_final_on_exhaustion(
        self,
        messages: list[dict],
        model: str | None,
        fallback_models: list[str] | None,
        on_token_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        *,
        truncated: bool = False,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, bool]:
        """One tools-disabled LLM call to wrap up after the iteration budget runs out.

        Instead of returning a canned apology, ask the model to summarize what
        it accomplished and deliver its best partial answer. Tools are withheld
        (``tools=None``) so it cannot start another tool call — or an
        ``ask_user`` — at the cliff edge. Falls back to a static message if the
        call errors or comes back empty, so the turn is never left silent.

        Returns ``(text, synthesized)``: ``synthesized`` is True only for a
        model-written wrap-up. Infra dying at the cliff yields the honest
        static message with ``False`` — never a fabricated answer — so
        callers and trajectories can tell the two apart.

        When the turn caller wired streaming callbacks, this synthesized reply
        must stream too — otherwise it never reaches a streaming outlet: the
        run_turn boundary only emits a closing ``Text`` when nothing streamed,
        so a non-streamed wrap-up after an already-streamed turn gets dropped.
        """
        # dr@2.9: ``truncated`` means the turn died on the completion budget, not the
        # tool-calling budget - a different failure needing a different instruction.
        prompt = _TRUNCATED_SYNTHESIS_PROMPT if truncated else _MAX_ITER_SYNTHESIS_PROMPT
        # ...and this call is the one MOST likely to be squeezed: it is appended to a
        # history that just proved too long to finish writing in. Measured on the same
        # batch, items carrying >40 tool results had a p10 of 8,496 characters of room.
        # So free the room the same way the loop already does mid-turn, with the same
        # length-blind elision, instead of inventing a second shrink policy. Cheap and
        # already tested; a no-op when there is nothing old to elide.
        # dr@3.0: ``max_tokens``/``reasoning_effort`` are passed ONLY by the truncation
        # branch. The exhaustion branch fires on both arms, so giving it a budget of its
        # own would move the anchor - the exact mistake dr@2.9 made one branch over. When
        # they are None the call is byte-identical to dr@2.9's, which is what keeps the
        # published exhaustion behaviour intact.
        extra: dict[str, Any] = {}
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort
        # Reached through the class, not ``self``: it is a @classmethod with no override
        # anywhere in the tree, and this helper is unit-tested bound to a duck-typed
        # stand-in. Widening what the helper demands of ``self`` would make that
        # isolation test assert the shape of its caller instead of the behaviour
        # under test. The two in-loop call sites keep using ``self`` - there ``self``
        # is always a real AgentLoop.
        shrunk, elided = AgentLoop._emergency_shrink(messages)
        if elided:
            logger.info("wrap-up: elided {} older tool result(s) to make room", elided)
        synth_messages = shrunk + [{"role": "user", "content": prompt}]
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
                    **extra,
                )
            text = self._strip_think(response.content)
            if response.finish_reason != "error" and text:
                return text, True
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
        return fallback, False

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
        usage_sink: dict[str, Any] | None = None,
        drain: Drain | None = None,
        journal: TurnJournal | None = None,
    ) -> tuple[str | None, list[str], list[dict], TurnOutcome]:
        """Run the agent iteration loop.

        ``drain``, when wired, is called at the top of each iteration to pull
        any user messages injected mid-turn (BusyPolicy.INJECT) and merge them
        as user turns before the next LLM call.

        ``extraction_session_id`` is the session key passed to local
        skill extraction when a turn completes. When ``None``, extraction
        is skipped (no pipeline wired).
        """
        from raven.agent.hook import AgentHookContext

        # dr@2.9: the research trail is computed from the client-side ledger, and until
        # now the only writer of RAVEN_WEB_LEDGER in the tree was the batch launcher - so
        # ``process_appendix``, which defaults on because the default is the product, was
        # the one knob that could never take effect off a bench. Opened here rather than
        # in __init__ because the appendix reads the whole file and this instance serves
        # every turn of a session. Gated on the assembly, so the flow-off anchor cannot
        # reach it, and no-ops when a launcher already named a file.
        # dr@3.0: ``research_memo`` joins the appendix as a second reader of the
        # same file. Both are computed from the ledger rather than asked of the
        # model for the same reason, and neither can open it on an arm whose
        # assembly does not exist.
        if getattr(self._dr_flow, "process_appendix", False) or getattr(self._dr_flow, "research_memo", False):
            from raven.agent.ledger import open_product_ledger

            open_product_ledger(f"{os.getpid()}-{next(_TURN_SEQ)}")

        messages = initial_messages
        # Index of this turn's inbound user message, i.e. where this turn's own
        # messages begin. Same convention ``_save_turn`` and ``TurnJournal`` already
        # use, and it exists for the same reason: everything before it is history.
        #
        # dr@3.0. ``turn_invariants`` is a per-TURN checker and was being handed the
        # whole assembled list, which on a single-turn bench run is the same list and
        # in a conversation is not. Measured on the first eight-turn run: one fetch
        # error in turn one made all eight turns report ``tool_error_result``,
        # including the four that called no tool at all. A check that fires on every
        # turn is not a check - it trains everyone to ignore its red light, and the
        # tool-surface health gate downstream reads exactly this field.
        turn_base = max(0, len(initial_messages) - 1)
        iteration = 0
        llm_calls = 0
        final_content = None
        # dr@2.9: only the NORMAL loop exit sets this. A hook short-circuit (salvage)
        # deliberately leaves it None, so a salvaged answer can never be mistaken for a
        # truncated one - the guard below keys on it and would otherwise re-run a lossy
        # stage on text the gate had just produced.
        final_finish_reason: str | None = None
        tools_used: list[str] = []
        session_key = extraction_session_id or ""
        effective_model = model or self.model

        # One context for the whole turn (not per iteration): hooks keep
        # cross-iteration state in ``ctx.metadata``, which must not live on
        # the AgentLoop singleton or it would leak across sessions.
        # ``turn_question`` is captured here and never recomputed: a rejected
        # draft appends its own user-role revision prompt, so by the time a gate
        # asks "what was the task" the list can no longer answer it.
        hook_ctx = (
            AgentHookContext(
                session_key=session_key,
                turn_question=turn_question(initial_messages),
            )
            if len(self.hooks) > 0
            else None
        )
        hook_rollbacks = 0
        iter_msg_base = 0
        pending_gen_overrides: dict[str, Any] | None = None

        def _hook_rollback(decision) -> bool:
            """Honor a hook's rollback: pop everything this iteration appended
            and re-sample without consuming an iteration. Message helpers
            mutate-and-return the same list, so truncating at the iteration
            watermark removes exactly this iteration's products."""
            nonlocal iteration, hook_rollbacks, pending_gen_overrides
            if not decision.rollback:
                return False
            if hook_rollbacks >= self._MAX_HOOK_ROLLBACKS:
                logger.warning(
                    "Hook rollback cap ({}) reached; proceeding without rollback",
                    self._MAX_HOOK_ROLLBACKS,
                )
                return False
            requested = decision.rollback_overrides or {}
            overrides = {k: v for k, v in requested.items() if k in self._ROLLBACK_OVERRIDE_KEYS}
            if len(overrides) != len(requested):
                logger.warning(
                    "Hook rollback overrides dropped (not in allowlist): {}",
                    sorted(set(requested) - set(overrides)),
                )
            hook_rollbacks += 1
            del messages[iter_msg_base:]
            if journal is not None:
                journal.rewind(
                    iter_msg_base,
                    injected=len(decision.rollback_inject or ()),
                    notes=list(decision.notes),
                )
            if decision.rollback_inject:
                for m in decision.rollback_inject:
                    entry = dict(m)
                    entry.setdefault("timestamp", self._now_fn().isoformat())
                    messages.append(entry)
            iteration -= 1
            pending_gen_overrides = overrides or None
            logger.info(
                "Hook rollback {}/{}: popped iteration messages, re-sampling (overrides={})",
                hook_rollbacks,
                self._MAX_HOOK_ROLLBACKS,
                sorted(overrides) or None,
            )
            return True

        # Bug2 / decision B — track whether the turn was a normal exit or a
        # max-iter interruption. ``status`` is the only piece read downstream
        # (used to label the shadow-git commit and stamp the ``TurnOutcome``).
        status = "completed"

        # Context-overflow recovery: bound the number of emergency shrinks so a
        # turn that overflows even after eliding can't loop forever.
        compress_retries = 0
        # Completion-reserve clamp (overflow recovery, first move). Once set,
        # ``completion_cap`` applies to every remaining call of the turn: the
        # history only grows, so a reserve that had to shrink cannot re-expand.
        clamp_retries = 0
        completion_cap: int | None = None
        overflows = 0
        # Pre-call fit (the proactive half of the same mechanism).
        prefits = 0
        preelisions = 0
        # Tool-failure-loop break (#1b): track consecutive same-tool hard
        # failures across iterations; nudge once per fresh streak, bounded/turn.
        loop_fail_tool: str | None = None
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
                        messages.append(
                            {"role": "user", "content": inj_text, "timestamp": self._now_fn().isoformat()}
                        )
                        logger.info("inject: merged a mid-turn user message")

            tool_defs = self.tools.get_definitions()

            if hook_ctx is not None:
                hook_ctx.iteration = iteration
                hook_ctx.messages = messages
                hook_ctx.tools = tool_defs
                hook_ctx.response = None
                decision = await self.hooks.before_iteration(hook_ctx)
                if decision.short_circuit_result is not None:
                    final_content = str(decision.short_circuit_result)
                    messages = self.context.add_assistant_message(messages, final_content)
                    break
                # A hook may withhold a tool for this iteration only. Taken from the
                # decision rather than from ``hook_ctx.tools``: the two happen to be
                # the same object today, so an in-place edit would also be observed,
                # and relying on that would make the rule stop firing silently the
                # day ``get_definitions`` returns a cached list. The registry itself
                # is never touched, so the next iteration starts from the full set
                # unless the hook decides again.
                if decision.modified_tools is not None:
                    tool_defs = decision.modified_tools

            # Fit the request before sending it. The recovery below only runs
            # after a 400 round-trip and each recovery buys exactly one
            # iteration, so a turn that keeps fetching walks straight through
            # the bounded budget: measured over 880 turns, a quarter hit at
            # least one overflow and *every* fatal one had spent all four
            # recoveries — none took an uncovered path. Checking the fit here
            # removes both the wasted round-trips and the exhaustion; the
            # reactive path stays as the net for estimate error.
            messages, fitted_cap, pre_elided = self._fit_request(
                messages, tool_defs, effective_model, completion_cap
            )
            preelisions += pre_elided
            if fitted_cap != completion_cap:
                completion_cap = fitted_cap
                prefits += 1

            # Watermark for hook rollback: everything appended past this index
            # belongs to the current iteration. Taken after drain-injects so a
            # rollback can never pop a user message.
            iter_msg_base = len(messages)
            gen_overrides = dict(pending_gen_overrides or {})
            pending_gen_overrides = None
            if completion_cap is not None:
                gen_overrides.setdefault("max_tokens", completion_cap)

            # TokenWise before-hook: strategies may rewrite messages, tools,
            # or model (e.g. CacheOptimizer marks cache_control blocks).
            call_messages, call_tools, call_model = await self.strategies.before_llm_call(
                messages,
                tool_defs,
                effective_model,
            )
            # Journal the previous iteration's products before this call: a
            # batch-timeout kill mid-call then leaves everything up to here on
            # disk, and the marker line pins down exactly which call stalled.
            if journal is not None:
                journal.checkpoint(messages)
                journal.event("llm_call_start", iteration=iteration, model=call_model)
            # Counted separately from ``iteration`` on purpose: a hook rollback
            # re-samples without consuming an iteration, so a gate that sends a
            # draft back for revision buys the arm an extra generation that the
            # iteration count hides. Comparing arms on iterations alone treats
            # that as free.
            llm_calls += 1
            if on_token_delta is not None or on_reasoning_delta is not None:
                response = await self._llm_call_stream(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    on_token_delta=on_token_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    generation_overrides=gen_overrides or None,
                )
            else:
                response = await self.provider.chat_with_retry(
                    messages=call_messages,
                    tools=call_tools,
                    model=call_model,
                    fallback_models=fallback_models,
                    **gen_overrides,
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
                # Real window from the model's provider table when LiteLLM lags
                # (e.g. OpenRouter); otherwise the configured default.
                context_max = self._window_for(call_model)
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
            # should_compress (a smaller window won't help, but shrinking the
            # request will). Two moves, cheapest first — clamp the completion
            # reserve, then elide accumulated tool output — retrying this
            # iteration instead of surfacing it as a fatal error. Both bounded.
            # Order matters: eliding throws away the evidence the answer is
            # supposed to come from, so it is the fallback, not the first move.
            cls_ = response.error_classification
            if response.finish_reason == "error" and cls_ is not None and cls_.should_compress:
                overflows += 1
                clamp = (
                    self._completion_clamp(completion_cap)
                    if clamp_retries < self._MAX_CLAMP_RETRIES
                    else None
                )
                if clamp is not None:
                    completion_cap = clamp
                    clamp_retries += 1
                    iteration -= 1  # the overflowed call did no work; don't bill it
                    logger.warning(
                        "Context overflow; clamped completion reserve to {} tokens, retrying ({}/{})",
                        clamp,
                        clamp_retries,
                        self._MAX_CLAMP_RETRIES,
                    )
                    continue
                if compress_retries < self._MAX_COMPRESS_RETRIES:
                    shrunk, elided = self._emergency_shrink(messages)
                    if elided > 0:
                        messages = shrunk
                        compress_retries += 1
                        iteration -= 1
                        logger.warning(
                            "Context overflow; elided {} old tool result(s), retrying ({}/{})",
                            elided,
                            compress_retries,
                            self._MAX_COMPRESS_RETRIES,
                        )
                        continue

            if response.has_tool_calls:
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.before_execute_tools(hook_ctx)
                    if _hook_rollback(decision):
                        continue
                    if decision.short_circuit_result is not None:
                        # Drop the tool-call response entirely: persisting an
                        # assistant message whose tool_calls never executed
                        # leaves dangling calls that strict providers reject.
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    finish_reason=getattr(response, "finish_reason", None),
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    # Skip the message tool: turn.py emits its tool.complete; a
                    # second emit here would double it.
                    emit_tool_event = on_tool_event is not None and tool_call.name != "message"
                    if emit_tool_event:
                        await on_tool_event(
                            "start",
                            {
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        )
                    tool_t0 = time.monotonic()
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    duration_ms = int((time.monotonic() - tool_t0) * 1000)
                    result_str = str(result)
                    preview = result_str.replace("\n", " ")[:200]
                    logger.info(
                        "Tool result: {} duration={}ms result={}",
                        tool_call.name,
                        duration_ms,
                        preview,
                    )
                    if emit_tool_event:
                        await on_tool_event(
                            "complete",
                            {
                                "tool_call_id": tool_call.id,
                                "result_preview": preview,
                                "truncated": len(result_str) > 200,
                            },
                        )
                    tool_payload = result
                    ingest_cap = self._TOOL_RESULT_MAX_CHARS - self._DR_RESULT_HEADROOM
                    if len(result_str) > ingest_cap:
                        tool_payload = result_str[:ingest_cap] + "\n... (truncated)"
                    messages = self.context.add_tool_result(messages, tool_call.id, tool_call.name, tool_payload)
                    # #1b Track consecutive same-tool deterministic failures
                    # (transient errors excluded — a retry would clear those).
                    if _is_hard_tool_failure(result):
                        if tool_call.name == loop_fail_tool:
                            loop_fail_streak += 1
                        else:
                            loop_fail_tool, loop_fail_streak = tool_call.name, 1
                    else:
                        loop_fail_tool, loop_fail_streak = None, 0

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
                        + _loop_break_nudge(loop_fail_tool, loop_fail_streak, dr_mode=self._dr_flow is not None)
                    )
                    loop_fail_streak = 0  # fire once per fresh streak

                # Dispatch before prev_had_tool_calls is set: a rollback means
                # this iteration never happened, so the empty-response
                # classifier must see the pre-iteration state on the re-sample.
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.after_iteration(hook_ctx)
                    if _hook_rollback(decision):
                        continue
                    if decision.short_circuit_result is not None:
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break
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

                # Fires before the final answer is persisted so a
                # short-circuit can replace it without leaving the replaced
                # text in history (the seam a future verify-gate needs), and
                # a rollback can discard-and-re-sample it (the loopscan seam).
                if hook_ctx is not None:
                    hook_ctx.messages = messages
                    hook_ctx.response = response
                    decision = await self.hooks.after_iteration(hook_ctx)
                    if _hook_rollback(decision):
                        continue
                    if decision.short_circuit_result is not None:
                        final_content = str(decision.short_circuit_result)
                        messages = self.context.add_assistant_message(messages, final_content)
                        break

                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    finish_reason=getattr(response, "finish_reason", None),
                )
                final_content = clean
                final_finish_reason = getattr(response, "finish_reason", None)
                break

        synthesized_on_exhaustion = False
        # dr@2.9: an iteration-seam salvage has already committed an answer by now, and
        # a committed salvage structurally never carries a closing tag (see
        # ForcedFinalizeGate._mark_committed). Recomputing "has an answer" on it always
        # says no - which is exactly how the terminal seam once ran 22 salvage calls
        # over 16 items. Consult the commit flag, never re-derive it.
        _salvaged_already = bool(
            (hook_ctx.metadata.get("force_finalize") or {}).get("salvage_committed")
            if hook_ctx is not None
            else False
        )
        # Two ways a turn ends with nothing to show, and they are NOT the same failure:
        #   - budget_exhausted: ran out of tool-calling iterations, no final message.
        #   - generation_truncated: wrote a final message but the COMPLETION limit cut
        #     it off mid-sentence, so it carries no answer. Measured on the anchor arm
        #     of eval_web_dr27_20260807 this is 36.7% of HLE items - turns=1, n_search=0.
        # The second one never reached the net below, because ``iteration >= max`` is
        # false on turn 1 and ``final_content`` is non-None (it holds the truncated text).
        #
        # dr@3.0: gated on the assembly, which the flow-off anchor never builds. dr@2.9
        # ran this on both arms on the reasoning that the anchor suffers from the defect
        # most - true, and the wrong trade: it moved the reference frame every published
        # delta is measured against, for a repair that produced zero correct answers on
        # the treated arm over the batch that paid for it. Evaluated before the
        # predicate so a flow-off turn does not even compute it.
        _truncated_answerless = bool(
            getattr(self._dr_flow, "truncation_wrapup", False)
        ) and _is_truncated_answerless(
            final_content, final_finish_reason, salvaged=_salvaged_already
        )
        if _truncated_answerless:
            logger.warning(
                "Generation truncated (finish_reason=length) with no visible answer "
                "after {} iteration(s); synthesizing a wrap-up",
                iteration,
            )
            # NOTE: status is deliberately NOT set to "interrupted" here, unlike the
            # exhaustion branch below. status feeds rollout_health.json, and a batch is
            # voided when non-ok exceeds 10% - so tagging a RECOVERED turn as interrupted
            # would let a successful repair void the batch it repaired. The repair is
            # visible instead through the trajectory marker and counter below.
            final_content, synthesized_on_exhaustion = await self._synthesize_final_on_exhaustion(
                messages,
                effective_model,
                fallback_models,
                on_token_delta=on_token_delta,
                on_reasoning_delta=on_reasoning_delta,
                truncated=True,
                max_tokens=getattr(self._dr_flow, "truncation_wrapup_max_tokens", None),
                reasoning_effort=getattr(self._dr_flow, "truncation_wrapup_effort", None),
            )
            if final_content:
                messages = self.context.add_assistant_message(messages, final_content)
                if synthesized_on_exhaustion:
                    messages[-1]["synthesized_on_exhaustion"] = True
                    # Distinct from the exhaustion marker: these two fire on different
                    # failures and their rates must never be summed into one column.
                    messages[-1]["synthesized_on_truncation"] = True
        elif final_content is None and iteration >= self.max_iterations:
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
            final_content, synthesized_on_exhaustion = await self._synthesize_final_on_exhaustion(
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
                if synthesized_on_exhaustion:
                    # Trajectory marker: wrap-up answers must stay
                    # distinguishable from real completions. Wire-safe —
                    # provider request sanitizers are allowlist-based, so the
                    # key never reaches an API on later turns.
                    messages[-1]["synthesized_on_exhaustion"] = True

        # Answerless terminal, last seam. The iteration hooks only see turns
        # that reached a text response, so every exit that skips one — a
        # provider error (deliberately not persisted, leaving the previous
        # tool-call turn as the last thing a trajectory reader sees), a
        # short-circuit, a wrap-up that came back reasoning-only — ends the turn
        # with no visible answer and no gate consulted. Offer the terminal gate
        # one chance to commit an answer from the evidence already gathered.
        # Answer-shaped check, not truthiness: with prefilled-think serving the
        # content is ``reasoning</think>`` and reads non-empty while carrying no
        # answer at all.
        # A dead call counts as answerless even though ``final_content`` reads
        # non-empty: the provider's error text goes to the user, never to
        # history, so the turn both fails the task and leaves the previous
        # tool-call turn as its last persisted word.
        # A turn cut mid-reasoning carries no think tag at all under prefilled-think
        # serving, so only the flow's serving contract can tell it from an answer.
        # A committed salvage structurally never carries a closing tag (see
        # ForcedFinalizeGate._mark_committed), so recomputing answerless on it
        # always says "answerless" and re-fires the terminal seam on the gate's
        # own output: one batch ran 22 salvage calls over 16 items and 11 over 9,
        # re-running a lossy stage on text it had just produced and under-counting
        # its own LLM calls by ~38%. Consult the commit flag the way
        # pipeline/eval_clean_adapter.py already does.
        salvage_committed = bool(
            (hook_ctx.metadata.get("force_finalize") or {}).get("salvage_committed")
            if hook_ctx is not None
            else False
        )
        answerless = status == "error" or not (
            salvage_committed
            # dr@2.9: same discipline as salvage_committed - consult the commit flag,
            # do not re-derive. A wrap-up written by the truncation/exhaustion net above
            # IS an answer, but it is free-form prose that need not carry a closing think
            # tag, so recomputing "has an answer" on it says no and the terminal seam
            # fires a SECOND lossy stage on text this loop just produced. That is the
            # documented 22-calls-over-16-items shape, one layer up.
            # ``synthesized_on_exhaustion`` is False when synthesis fell back to the
            # static apology, and that case genuinely IS answerless - so the flag, not
            # the branch, is the right thing to test.
            or synthesized_on_exhaustion
            or visible_answer(final_content, closing_tag_required=self._think_closing_tag_required)
        )
        if hook_ctx is not None:
            # answerless is the GATE value and uses self._think_closing_tag_required,
            # which is False whenever the flow is off - so the anchor is judged with a
            # permissive bar and the treated arm with the strict one, and an
            # "answerless rate" row built from it mixes two definitions. Recomputing
            # one 120-item batch with the strict bar moved the anchors 26->36 and
            # 25->33 while the treated arms moved 17->21 and 14->18, i.e. the stamped
            # ruler UNDERSTATES the treated advantage. answerless_shape is the same
            # ruler on every arm; it decides nothing and exists only to be counted.
            hook_ctx.metadata["turn_end"] = {
                "status": status,
                "answerless": answerless,
                "answerless_shape": not visible_answer(final_content, closing_tag_required=True),
                # dr@2.2: answerless_shape alone is NOT arm-neutral, contrary to the note
                # above. A committed salvage structurally never carries a closing tag
                # (ForcedFinalizeGate._mark_committed says so itself), so every salvaged
                # item counts as shape-answerless while plainly having an answer - and
                # anchors never salvage, so the miscount lands on treated arms only, in
                # the direction that understates the treated advantage. Measured on one
                # batch: 6/6 and 3/3 of the salvaged items, 15->9 and 16->13 after the
                # exemption, anchors 29 and 31 unchanged.
                #
                # The raw key is left byte-identical because published numbers used it;
                # this is the corrected column beside it. Both exemption sources are
                # required: an iteration-seam salvage is already committed here, while a
                # terminal-seam one commits AFTER this stamp and is only marked by
                # "salvaged" below - which is why the value is refreshed there.
                "answerless_shape_exempt": not (
                    salvage_committed
                    or visible_answer(final_content, closing_tag_required=True)
                ),
                # dr@2.9: the wrap-up nets, counted so a repeat firing is VISIBLE.
                # The 22-over-16 double-fire went unnoticed for a release precisely
                # because nothing counted it - the only symptom was a ~38% under-report
                # of the loop's own LLM calls, which nobody was reading. A suppression
                # that cannot be counted is indistinguishable from one that never ran.
                "synthesized_on_exhaustion": bool(synthesized_on_exhaustion),
                "synthesized_on_truncation": bool(_truncated_answerless),
                "final_finish_reason": final_finish_reason,
                "closing_tag_required": bool(self._think_closing_tag_required),
                "salvage_committed_at_terminal": salvage_committed,
                "overflows": overflows,
                "clamps": clamp_retries,
                "elisions": compress_retries,
                "prefits": prefits,
                "preelisions": preelisions,
                "iterations_used": iteration,
                "llm_calls": llm_calls,
                "final_completion_cap": completion_cap,
            }
        if hook_ctx is not None and answerless:
            hook_ctx.messages = messages
            hook_ctx.response = None
            decision = await self.hooks.terminal_answerless(hook_ctx)
            if decision.short_circuit_result is not None:
                final_content = str(decision.short_circuit_result)
                messages = self.context.add_assistant_message(messages, final_content)
                hook_ctx.metadata["turn_end"]["salvaged"] = True
                # The terminal seam commits after the stamp above, so the corrected
                # counter has to be refreshed here or it would still read True for an
                # item that just produced an answer.
                hook_ctx.metadata["turn_end"]["answerless_shape_exempt"] = False

        # Drop transient empty-recovery scaffolding before persistence /
        # extraction / return — refactor/Raven's empty-recovery marks
        # synthetic nudge/prefill messages with ``_recovery_synthetic``; strip
        # them so they never persist. (Embedded ``_trigger_local_extraction``
        # was retired on feature/integrate-everos; the after-turn pipeline —
        # ``context_engine.after_turn`` + ``backend.store`` in
        # ``_process_message`` — owns extraction now.)
        if any(m.get("_recovery_synthetic") for m in messages):
            messages = [m for m in messages if not m.get("_recovery_synthetic")]

        # Phase B-1 (feature/integrate-everos): embedded extraction (the
        # ``_trigger_local_extraction`` / ``SkillService.on_execution`` path)
        # was retired here in favor of the after-turn pipeline owned by the
        # caller — ``context_engine.after_turn`` + ``backend.store`` +
        # ``backend.feedback`` run from ``_process_message``. We surface
        # ``outcome.status`` so that pipeline can gate on completion later.

        if messages:
            from raven.agent.hook.observers import terminal_state
            from raven.agent.loop.invariants import turn_invariants

            # Observer terminal state must reach the trajectory, not just
            # stderr: batch attribution (which guard fired, did rollbacks
            # exhaust) was previously reconstructed from a bounded stderr
            # tail and undercounted on long turns. Wire-safe like
            # ``synthesized_on_exhaustion`` — never re-sent to an API,
            # never rendered into input_ids. Stamped on the turn's last
            # message that ``_save_turn`` will keep.
            metadata = hook_ctx.metadata if hook_ctx is not None else {}
            observers = terminal_state(metadata) if hook_ctx is not None else {}
            # Not gated on ``hook_ctx``: that exists only when a hook is
            # registered, so gating the checker on it would stop the checks the
            # moment someone prunes the default observers — the failure class the
            # checker is here to catch. Read-only, so both arms get it.
            observers["invariants"] = turn_invariants(
                # Sliced, not the whole list - see ``turn_base``. On a bench run the
                # slice IS the whole list (history is empty), so no measured stamp
                # moves; in a conversation it stops earlier turns' tool results from
                # being counted again on every later turn.
                messages[turn_base:],
                declared_tools=self.tools.names(),
                metadata=metadata,
                closing_tag_required=self._think_closing_tag_required,
            )
            # Stamped on every turn including a clean one, and outside
            # ``terminal_state`` for the same reason ``invariants`` is: that
            # function exports through a per-key whitelist, which once dropped
            # three new scoring-facing keys and shipped a version that measured
            # as its predecessor. An absent key here can only mean the build
            # predates the gate.
            observers["containment"] = self.benchmark_containment.counters()
            # dr@2.9 truncation wrap-up, read-only, stamped on every turn on both
            # arms. It sits here rather than only in ``turn_end`` metadata because
            # the rate has to be readable per arm without conditioning on it:
            # this fix is not gated on the flow, so it fires on the anchor too, and
            # a batch that cannot say how often it fired on each arm cannot tell an
            # unmoved delta from two cancelling ones. Reporting the rate is not
            # stratifying on it - splitting the score by ``fired`` would be
            # selection on an outcome of the treatment.
            # dr@3.0: ``recovered`` now means what its name says - the SCORER can see an
            # answer - and is computed with the scorer's own predicate. It previously
            # meant "the wrap-up call returned some text", which reported 21 of 21
            # successes on a batch where the scorer could see an answer in 5. The gap is
            # the ``_strip_think`` seam: that helper removes only paired tags, while the
            # serving template prefills the opener, so a wrap-up cut mid-reasoning
            # passes its entire chain of thought through as content. To a counter that
            # is a recovery; to the scorer it is an empty answer.
            # ``synthesized`` is kept beside it rather than replaced: "the call
            # produced a model-written wrap-up rather than the static fallback" is a
            # real and different fact, and collapsing the two is what hid this.
            _wrapup_answer = bool(
                visible_answer(final_content, closing_tag_required=True)
            ) if _truncated_answerless else False
            observers["truncation_wrapup"] = {
                "fired": bool(_truncated_answerless),
                "recovered": _wrapup_answer,
                "synthesized": bool(_truncated_answerless and synthesized_on_exhaustion),
                "final_finish_reason": final_finish_reason,
            }
            # dr@2.5 terminal-answer shaping, read-only. Gated on the assembly, not
            # on config: ``build_dr_flow`` returns None when the flow is off, so the
            # anchor has nothing to read here even if the knob were set on its
            # config. Nothing below mutates ``final_content`` or ``messages`` - the
            # shaped form is recorded beside the raw answer, never instead of it, so
            # the persisted trajectory is byte-identical with this on or off and the
            # score budget is 0 by construction. See flow/final_shape.py for why
            # "additive only" is the whole design.
            if getattr(self._dr_flow, "record_final_shape", False):
                from raven.agent.flow.final_shape import shape_final_answer

                observers["final_shape"] = shape_final_answer(
                    final_content,
                    closing_tag_required=self._think_closing_tag_required,
                ).counters()
            # dr@2.8 research trail. Placement is the design: ``messages`` already
            # carries the raw answer by this point, so appending here reaches the
            # value returned to the caller and nothing else - the persisted
            # trajectory stays byte-identical, and the next turn's history shows the
            # model its own answer rather than our commentary on it. Computed from
            # the ledger rather than asked of the model, so it cannot invent a page
            # it did not open. Gated on the assembly, so the anchor cannot reach it.
            # dr@3.0: a turn the conversation gate answered from context did no
            # research, so an appendix would report an empty trail as though it
            # were the record behind the reply, and a report-shaped answer would
            # be a research report about a formatting request. Both seams read
            # ``_research_turn`` rather than the config, so they are unchanged on
            # every arm that has no conversation gate to set it.
            _research = self._research_turn()
            if self._turn_mode is not None:
                observers["conversation_gate"] = self._turn_mode.counters()
            _wants_appendix = _research and getattr(self._dr_flow, "process_appendix", False)
            # dr@3.0: the memo reads the same file, so the two share one open/close
            # and one read. Kept out of ``build_appendix`` on purpose - the appendix
            # is display-only by contract and the memo is fed back to the model, so
            # merging them would put the appendix's integrity numbers (which the
            # model can then read) into the context of the turn that produces the
            # next value of them.
            # The memo is NOT gated on ``_research``: a non-research turn opens no
            # pages, so its rows are empty and the merge is a no-op - but the file
            # still has to be closed, and gating the whole block would leak it.
            _wants_memo = getattr(self._dr_flow, "research_memo", False)
            if _wants_appendix or _wants_memo:
                from raven.agent.flow.conversation import stash_turn_rows
                from raven.agent.ledger import close_product_ledger, ledger_path
                from raven.agent.process_appendix import build_appendix, read_ledger

                try:
                    path = ledger_path()
                    rows = read_ledger(path) if path else []
                    if _wants_memo:
                        stash_turn_rows(rows)
                    if _wants_appendix:
                        # dr@3.0: pages earlier turns of this conversation opened.
                        # Without them the fabricated-citation check runs a
                        # per-turn denominator under a per-conversation numerator -
                        # the memo hands a later turn URLs it may cite, and citing
                        # one correctly was being reported as a fabrication. Seen on
                        # the first three-turn run: turn three flagged both of turn
                        # one's pages.
                        from raven.agent.flow.conversation import prior_sources

                        appendix, trail = build_appendix(path, final_content or "", prior_sources())
                        observers["process_appendix"] = trail
                        if appendix:
                            final_content = (final_content or "").rstrip() + "\n" + appendix
                finally:
                    # dr@2.9: in ``finally`` so a failed render still releases the file -
                    # otherwise one raising turn leaves a trail on disk that no later turn
                    # reads. Only ever removes a path this turn allocated; a launcher's
                    # RAVEN_WEB_LEDGER is measurement data and is never touched.
                    close_product_ledger()
            if not observers["invariants"].get("ok"):
                logger.error(
                    "turn invariants violated: {}", ", ".join(observers["invariants"]["violations"])
                )
            for m in reversed(messages):
                if m.get("role") == "assistant" and not m.get("content") and not m.get("tool_calls"):
                    continue
                m["observers"] = observers
                break

        outcome = TurnOutcome(status=status, synthesized_on_exhaustion=synthesized_on_exhaustion)
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
        # dr@3.0 product surface. Runs before the tools' ``start_turn`` because the
        # identity scope below depends on nothing else, and before assembly because
        # the memo is injected there. On turn one and with the feature off this is a
        # single attribute read that sets the ContextVar to the value it already had.
        await self._decide_turn_mode(session, content)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()
        # Repeat detection is scoped to the turn: the same query on a later turn
        # is a re-check, not a loop. dr@3.0's ``identityScope="topic"`` narrows that
        # to the budgets for a product conversation, where a follow-up on the same
        # topic re-finding the previous turn's pages is the loop, not a re-check.
        if search_tool := self.tools.get("web_search"):
            if isinstance(search_tool, WebSearchTool):
                search_tool.start_turn(
                    keep_identities=getattr(self._dr_flow, "identity_scope", "turn") == "topic"
                )
        # The fetch tool carries only the retry budget, and it needs the same scope:
        # a budget spent once would leave later turns of a long run unprotected.
        if fetch_tool := self.tools.get("web_fetch"):
            if isinstance(fetch_tool, WebFetchTool):
                fetch_tool.start_turn()
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
        initial_messages = await self._assemble_context_messages(
            session=session,
            session_key=key,
            current_message=content,
            media=media_paths if media_paths else None,
            channel=channel,
            chat_id=chat_id,
            selected_skills=selected_skills or None,
        )

        # ── Model routing (EcoClaw-style) ────────────────────────────────────
        routed_model: str | None = None
        fallback_models: list[str] = []
        if self.router is not None:
            routed_model, fallback_models = await self.router.select_model_chain(content)
            if routed_model and routed_model != self.model:
                logger.info("Router: {} → {}", self.model, routed_model)
            if fallback_models:
                logger.info("Router fallback chain: {}", fallback_models)

        extraction_sid = None  # Phase B-1: embedded extraction removed; always None now.
        turn_start_idx = len(initial_messages) - 1
        # Stamp the inbound user message at turn start so the persisted turn
        # carries its real arrival time, like every message created after it.
        initial_messages[turn_start_idx].setdefault("timestamp", self._now_fn().isoformat())
        journal = TurnJournal(self.sessions.partial_path(key), base=turn_start_idx)
        journal.event("turn_start", session=key, flow_version=self._flow_version)
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
            usage_sink=usage_sink,
            drain=drain,
            journal=journal,
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

        journal.checkpoint(all_msgs)
        self._save_turn(session, all_msgs, turn_start_idx)
        # Before ``sessions.save``: the memo lives on ``session.metadata``, which
        # save() serialises. Updating it afterwards would keep the memo one turn
        # behind on disk and lose the last turn's sources entirely on a restart.
        self._update_research_memo(session)
        self.sessions.save(session)
        # Only after a successful canonical save — a surviving partial file
        # must always mean "this turn never made it to disk".
        journal.discard()
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
        from raven.agent.flow.conversation import MEMO_OPEN, strip_memo

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if entry.get("_recovery_synthetic"):
                continue  # #1a synthetic recovery nudge — never persist scaffolding
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                # dr@3.0: the research memo is assembled fresh from session metadata
                # at every turn, so persisting the injected copy would accumulate -
                # turn three would carry turn two's memo as history alongside a newly
                # rendered one listing the same pages. Stripped before the runtime-tag
                # branch because the memo is injected last and is therefore outermost.
                if isinstance(content, str) and content.startswith(MEMO_OPEN):
                    content = strip_memo(content)
                    entry["content"] = content
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                        ):
                            continue  # Strip runtime context from multimodal messages
                        if (
                            c.get("type") == "text"
                            and isinstance(c.get("text"), str)
                            and c["text"].startswith(MEMO_OPEN)
                        ):
                            continue  # dr@3.0: same reason, multimodal shape
                        if c.get("type") == "image_url" and c.get("image_url", {}).get("url", "").startswith(
                            "data:image/"
                        ):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", self._now_fn().isoformat())
            # Version the harness that produced this line: trajectories from
            # different loop builds must stay distinguishable (a behavior
            # change without a version bump is invisible in the data
            # otherwise). Wire-safe like ``timestamp`` — provider request
            # sanitizers are allowlist-based.
            entry.setdefault("flow_version", self._flow_version)
            session.record(entry)
        session.updated_at = self._now_fn()

    async def run_turn(
        self,
        req: TurnRequest,
        emit: Emit,
        drain: Drain,
        *,
        stream: bool = True,
        usage_sink: dict[str, Any] | None = None,
        text_sink: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        """Spine-native turn entry: consume a TurnRequest, fan the agent's output
        onto the single ``emit``, return a TurnOutcome. Collapses the legacy
        output paths (a str return + the five callbacks) onto one boundary.

        Named ``run_turn`` rather than ``run``: ``run`` is the runtime keep-alive
        (executor / debug server / MCP up, then idle). A spine runner wraps this
        method to satisfy the TurnRunner protocol.

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

        async def on_tool(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                await emit(
                    ToolEvent(
                        phase=ToolPhase.START,
                        tool_call_id=info["tool_call_id"],
                        name=info["name"],
                        arguments=info["arguments"],
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


def _finalize_tool_calls(slots: list[dict[str, Any]]) -> list[ToolCallRequest]:
    """Convert accumulator slots into final ToolCallRequest list."""
    result: list[ToolCallRequest] = []
    for slot in slots:
        name = slot["function"]["name"]
        if not name:
            continue
        args_text = "".join(slot["function"]["arguments_buf"])
        try:
            args = json.loads(args_text) if args_text else {}
        except json.JSONDecodeError:
            args = {"_raw_arguments": args_text}
        result.append(
            ToolCallRequest(
                id=slot["id"] or "",
                name=name,
                arguments=args,
            )
        )
    return result
