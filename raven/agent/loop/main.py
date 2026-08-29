"""AgentLoop -- the default harness.

Readability split: module-level names live in _shared (re-exported here so
imports and monkeypatch targets keep working); method groups live in mixins
(turn_path / wiring / mcp_glue / organ_glue). Bodies are verbatim.
"""

from __future__ import annotations

from raven.agent.loop._shared import (  # noqa: F401 -- moved verbatim from main.py
    _ABORTED_ACTION_REPLY,
    _ATTACHED_IMAGE_KEY,
    _DELEGATED_KEY,
    _FILE_CHANGE_MAX_CHARS,
    _MAX_ITER_STATIC_FALLBACK,
    _MAX_ITER_SYNTHESIS_PROMPT,
    _NOTICE_KEY,
    _ORIGIN_KEY,
    _REASONING_MS_KEY,
    _SKIP_AFTER_SEND_ORIGINS,
    _SKIP_USER_INBOUND_ORIGINS,
    _STORE_DRAIN_BUDGET_S,
    _TOOL_DURATION_MS_KEY,
    _TOOL_METADATA_KEY,
    _TOOL_PREVIEW_MAX_CHARS,
    POST_TOOL_NUDGE,
    SKIPPED_AFTER_BLOCKED_CALL,
    TYPE_CHECKING,
    Any,
    AskUserTool,
    AsyncExitStack,
    Awaitable,
    Callable,
    ContextBuilder,
    Continuation,
    DeepResearchManager,
    DeepResearchOfferTool,
    DeepResearchTool,
    DirectChatHandoff,
    EditFileTool,
    ExecTool,
    FindTool,
    GrepTool,
    ImageGenerateTool,
    ListDirTool,
    LLMProvider,
    LLMResponse,
    LoopOutcome,
    MemoryConsolidator,
    MemoryStore,
    MessageTool,
    ModelBinding,
    Origin,
    Path,
    ReadFileTool,
    RecoveryAction,
    RecoveryLimits,
    SandboxConfig,
    SandboxExecutor,
    SandboxInitError,
    Session,
    SessionManager,
    SpawnTool,
    SpeechGenerateTool,
    StorePipeline,
    SubagentManager,
    TokenBudget,
    ToolOutput,
    ToolRegistry,
    VideoGenerateTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
    _display_label,
    _file_change_payload,
    _filter_qualified_ids,
    _first_line,
    _runtime_origin,
    _stamp_reasoning_ms,
    _strip_inline_images,
    active_binding,
    asyncio,
    autofill_resolver,
    build_executor,
    classify_empty_response,
    current_autofill,
    dataclass,
    datetime,
    deep_research_mode,
    estimate_prompt_tokens,
    failure_class,
    field,
    image_placeholder_text,
    is_hard_tool_failure,
    is_image_part,
    is_inline_image,
    is_only_think_debris,
    json,
    logger,
    loop_break_nudge,
    replace,
    resolve_context_window,
    semconv,
    send_max_tokens,
    session_of,
    stream_llm_call,
    strip_think_blocks,
    supports_image_tool_result,
    suppress,
    time,
    trace,
    use_binding,
    uuid4,
    vision_verdict,
    workdir,
)
from raven.agent.loop.bundles import (
    EngineWiring,
    HostWiring,
    SubagentWiring,
    ToolWiring,
    TurnPolicy,
    resolve_wiring,
)
from raven.agent.loop.mcp_glue import McpGlueMixin
from raven.agent.loop.organ_glue import OrganGlueMixin
from raven.agent.loop.turn_path import TurnPathMixin
from raven.agent.loop.wiring import WiringMixin

if TYPE_CHECKING:
    from raven.agent.hook import CompositeHook
    from raven.agent.loop.checkpoint import CheckpointService
    from raven.agent.tools.ask_user import QuestionResponder
    from raven.config.schema import (
        DeepResearchToolConfig,
    )
    from raven.context_engine import ContextEngine
    from raven.contracts.memory import MemoryBackend
    from raven.contracts.tool import Tool
    from raven.mcp.manager import MCPConnectionManager
    from raven.providers.pool import ProviderPool
    from raven.routing.router import ModelRouter
    from raven.sandbox.debug_server import SandboxDebugServer
    from raven.spine.runner import Drain, Emit
    from raven.spine.turn import TurnRequest


class AgentLoop(TurnPathMixin, WiringMixin, McpGlueMixin, OrganGlueMixin):
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

    # The tool that can read an attachment for a model that cannot see it.
    # Contributed by the EverOS plugin, so absent on a default install.
    _DESCRIBE_TOOL = "understand_media"

    _MEMORY_FAILURES_BEFORE_ALARM = 3

    """How many consecutive store failures make this a standing fault rather
    than a blip. One failed write is a network hiccup nobody needs told about;
    three in a row is a backend that is not coming back on its own."""

    _WATCHED_TOOLS = {
        "list_dir": "path",
        "read_file": "path",
        "grep": "path",
        "find": "path",
        "exec": "command",
        "web_fetch": "url",
    }

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        *,
        session_manager: SessionManager | None = None,
        provider_pool: "ProviderPool | None" = None,
        router: "ModelRouter | None" = None,
        sandbox_config: SandboxConfig | None = None,
        mcp_servers: dict | None = None,
        tools: "ToolWiring | None" = None,
        subagents: "SubagentWiring | None" = None,
        engine: "EngineWiring | None" = None,
        policy: "TurnPolicy | None" = None,
        host: "HostWiring | None" = None,
        **legacy: Any,
    ):
        tools, subagents, engine, policy, host = resolve_wiring(tools, subagents, engine, policy, host, legacy)
        exec_config = tools.exec_config
        ask_user_config = tools.ask_user_config
        brave_api_key = tools.brave_api_key
        jina_api_key = tools.jina_api_key
        web_proxy = tools.web_proxy
        restrict_to_workspace = tools.restrict_to_workspace
        disabled_tools = tools.disabled_tools
        tool_search_config = tools.tool_search_config
        media_config = tools.media_config
        deep_research_config = tools.deep_research_config
        plugin_tools = tools.plugin_tools
        deliverables = tools.deliverables
        agents = subagents.agents
        max_concurrent_subagents = subagents.max_concurrent_subagents
        max_subagent_spawns_per_hour = subagents.max_subagent_spawns_per_hour
        subagent_dag_config = subagents.subagent_dag_config
        subagent_questions_config = subagents.subagent_questions_config
        workdir_resolver = subagents.workdir_resolver
        context_config = engine.context_config
        runtime_config = engine.runtime_config
        context_window_tokens = engine.context_window_tokens
        strategies = engine.strategies
        skill_forge_config = engine.skill_forge_config
        skill_forge_router_config = engine.skill_forge_router_config
        memory_config = engine.memory_config
        backend = engine.backend
        playbook_config = engine.playbook_config
        max_iterations = policy.max_iterations
        empty_recovery = policy.empty_recovery
        interactive = policy.interactive
        response_modifier = policy.response_modifier
        now_fn = policy.now_fn
        hooks = host.hooks
        on_user_inbound = host.on_user_inbound
        decision_consumer = host.decision_consumer
        cron_service = host.cron_service
        channels_config = host.channels_config
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
        from raven.config.raven import MemoryConfig, SubagentDagConfig, SubagentQuestionsConfig
        from raven.config.schema import DeepResearchToolConfig, MediaGenConfig

        self.media_config = media_config or MediaGenConfig()
        self.deep_research_config = deep_research_config or DeepResearchToolConfig()
        self.subagent_dag_config = subagent_dag_config or SubagentDagConfig()
        self.subagent_questions_config = subagent_questions_config or SubagentQuestionsConfig()
        # Stored, not only forwarded to the context engine: the autofill resolver
        # runs outside the assembler and needs the same user_id the recall in
        # `# Memory` uses, or it would read a different store than the one the
        # turn's own memory came from.
        self.memory_config = memory_config or MemoryConfig()
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
        # A turn enqueues and returns; the pipeline owns ordering, retry and
        # what teardown does with whatever is left.
        self._store_pipeline = StorePipeline(
            lambda: self.backend,
            on_ok=self._note_memory_ok,
            on_failure=self._note_memory_failure,
        )
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
            memory_config=self.memory_config,
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
        # the page's skill panel (the host wires it to the page's emitter,
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
        self._mcp_prewarm_task: asyncio.Task | None = None
        self._mcp_prewarm_attempts: dict[str, object] = {}

        from raven.agent.subagent.dag_mcp_scope import run_mcp_servers
        from raven.agent.subagent.mcp_grant import LiveMcpSource

        self.subagents.set_mcp_source(
            LiveMcpSource(
                lambda: self._mcp_servers,
                lambda: self._mcp_manager,
                self.tools,
                self.tools.withheld_names,
                # A bridged upstream is spawned by the endpoint, not by this
                # manager, so the confinement has to travel with the source or a
                # granted stdio server escapes the sandbox the host configured.
                self._mcp_executor,
                # The one place a playbook's own ``mcpServers`` becomes
                # resolvable in a conversation, and a read rather than a write:
                # ``self._mcp_servers`` is this process's configuration and stays
                # that, so a run cannot leave a definition behind and the main
                # agent's own tool list does not move because a playbook named a
                # server. Handed over as a second mapping rather than merged into
                # the first so the source can tell whose definition it answered
                # with -- see ``LiveMcpSource`` and ``subagent.dag_mcp_scope``.
                run_mcp_servers,
            )
        )
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

    def set_deep_research_broker(self, broker: QuestionResponder) -> None:
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

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

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
    ) -> LoopOutcome:
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
        # The tools a session brought with it become visible here, for the same
        # reason the model binding does: this is where the turn's task begins.
        # The request handler that accepted them cannot open the scope itself --
        # it submits the turn onto the spine and the turn runs on a task that
        # inherits nothing from it.
        with use_binding(self.binding_for_session(session_key)), self.tools.session_scope_for(session_key):
            return await self._run_turn(
                req,
                emit,
                drain,
                stream=stream,
                inline_tool_stream=inline_tool_stream,
                usage_sink=usage_sink,
                text_sink=text_sink,
            )
