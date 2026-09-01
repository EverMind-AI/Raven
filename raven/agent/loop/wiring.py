"""Construction-time wiring: providers, bindings, tool registration, playbooks,
workdir and sinks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.agent.loop._shared import (
    Any,
    AskUserTool,
    Callable,
    DeepResearchManager,
    DeepResearchOfferTool,
    EditFileTool,
    ExecTool,
    FindTool,
    GrepTool,
    ImageGenerateTool,
    ListDirTool,
    LLMProvider,
    MessageTool,
    ModelBinding,
    Path,
    ReadFileTool,
    SpawnTool,
    SpeechGenerateTool,
    VideoGenerateTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
    active_binding,
    deep_research_mode,
    logger,
    workdir,
)

if TYPE_CHECKING:
    from raven.agent.tools.deliverables import DeliverableStore
    from raven.config.schema import PlaybookConfig, SkillForgeRouterConfig
    from raven.contracts.asking import QuestionResponder
    from raven.providers.pool import ProviderPool
    from raven.skill_hub import SkillHubClient


class WiringMixin:
    """Construction-time wiring: providers, bindings, tool registration, playbooks,
    workdir and sinks."""

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

    def set_session_policy(
        self,
        session_key: str,
        *,
        max_iterations: int | None = None,
        mode: str = "",
        mode_overlay: dict | None = None,
    ) -> None:
        """Record the operating policy this session's next turn runs under.

        Nothing running is touched: a turn reads its policy once at its start,
        so a switch lands on the session's next turn -- the same contract the
        model picker states.
        """
        from raven.agent.loop._shared import SessionPolicy

        self._session_policies[session_key] = SessionPolicy(
            max_iterations=max_iterations, mode=mode, mode_overlay=dict(mode_overlay or {})
        )

    def session_policy(self, session_key: str):
        """The session's policy, or the loop-wide defaults as one."""
        from raven.agent.loop._shared import SessionPolicy

        return self._session_policies.get(session_key, SessionPolicy())

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
                        restrict_to_workspace=self.restrict_to_workspace,
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
        self._deep_research_broker: QuestionResponder | None = None
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
        from raven.agent.subagent.dag_tool import SubAgentDagTool

        self.tools.register(
            SubAgentDagTool(
                workspace=self.subagents.workspace,
                registry=self.subagents.registry,
                guide_skill_id=self._dag_guide_skill_id(),
                session_dir=self.sessions.session_dir,
                is_paused=lambda: self.subagents.paused,
                state_for=self.subagents.instance_state,
                everos_for=self.subagents.everos_identity,
                mode_for=self.subagents.resolve_mode,
                gate=self.subagents.dispatch_gate,
                announce=self.subagents.announce_dag_result,
                announce_exception=self.subagents.announce_dag_exception,
                adopt=self.subagents.adopt_background_run,
                charge=self.subagents.charge_dag_run,
                ask=self._confirm_graph,
                control_reachable=self.dag_control_reachable,
                provider_for=self._verdict_provider,
                adjudicate=self._adjudicate_node,
                verdict_config=self.subagent_dag_config,
            )
        )
        # The graph tool's own acceptance text is the only advertisement these
        # three get: hidden from the schema so the per-turn tool list carries
        # nothing a conversation that never starts a DAG has any use for, they
        # stay reachable through the registry (and tool_call where it exists).
        from raven.agent.subagent.dag_control_tools import CancelDagTool, DagStatusTool, ResolveDagNodeTool

        self.tools.register(CancelDagTool(loop=self))
        self.tools.register(DagStatusTool(loop=self))
        self.tools.register(ResolveDagNodeTool(loop=self))
        self.tools.hide_from_schema("cancel_dag", "dag_status", "resolve_dag_node")
        # The question responder is a per-transport singleton, late-bound via
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
            # Function-scope import on purpose: the cron tool is cargo the loop must
            # not name at module level (tests/test_l3_open_world.py counts module-level
            # imports), so the edge stays lazy and the loop is fully loaded when it fires.
            from raven.proactive_engine.schedulers.cron.tool import CronTool

            self.tools.register(CronTool(self.cron_service))

        # Plugin-contributed tools (e.g. EverOS's ``understand_media``).
        # Registered last so a plugin can override a built-in by name if
        # it deliberately contributes the same name; ``_withheld_tool_names``
        # still runs afterward and can strip any of them. A tool that declares
        # ``bind_runtime`` is granted the loop's late-bound handles in
        # ``_bind_plugin_runtime``, not here: the handles carry organs (the
        # playbook funnel) assembled after even this registry is populated.
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
        #
        # ``tool_call`` is registered whatever the config says, because folding
        # is not the only thing that keeps a tool out of the schema: every
        # ``hide_from_schema`` tool above (the DAG controls) is dispatchable and
        # unnameable without it, and this feature is off by default. Only
        # ``tool_search`` and the fold itself turn on with the switch.
        from raven.agent.tools.tool_search import (
            DEFAULT_ALWAYS_VISIBLE,
            ToolCallTool,
            ToolSearchController,
            ToolSearchStrategy,
            ToolSearchTool,
        )
        from raven.config.schema import ToolSearchConfig

        cfg = self._tool_search_config or ToolSearchConfig()
        always = set(DEFAULT_ALWAYS_VISIBLE) | set(cfg.always_visible)
        self.tool_search_controller = ToolSearchController(
            self.tools,
            always_visible=always,
            search_result_limit=cfg.search_result_limit,
            compaction_threshold=cfg.compaction_threshold,
        )
        self.tools.register(ToolCallTool(self.tool_search_controller))
        if cfg.enabled:
            self.tools.register(ToolSearchTool(self.tool_search_controller))
            # ``first=True``: filter the tool list before CacheOptimizer marks
            # the final tool with ``cache_control`` (else the marked tool may be
            # filtered out and the breakpoint lost).
            self.strategies.register(
                ToolSearchStrategy(self.tool_search_controller),
                first=True,
            )

    def _bind_plugin_runtime(self) -> None:
        """Grant the late-bound handles to every plugin tool that declared.

        Runs once from the constructor, after the loop has finished assembling
        itself: the factories ran before this loop existed, and the handles
        carry organs (the playbook funnel) built after even the tool registry
        is populated, so this is the first moment every grant exists. A tool
        that raises :class:`BindDeclinedError` is unregistered quietly -- the grant
        it needs is off in this loop, which is configuration, not a bug. Any
        other exception unregisters loudly rather than leaving a tool
        half-bound. A tool that was withheld or shadowed after registering is
        skipped: binding what the table no longer serves grants power to a
        dead reference.

        The registry's cast gates bind through the same minting path, with
        the opposite failure rule: a gate whose bind raises fails the whole
        assembly (the candidate generation is discarded, or first boot
        refuses to serve). Quietly taking a gate off the table -- the tools'
        unregister path, declines included -- would turn a binding bug into
        a silent permission grant; a gate's sanctioned opt-out is its
        factory returning None, before anything was cast.
        """
        from raven.plugins.context import BindDeclinedError

        for tool in self.plugin_tools:
            bind = getattr(tool, "bind_runtime", None)
            if not callable(bind):
                continue
            if self.tools.get(tool.name) is not tool:
                continue
            namespace = getattr(tool, "contributed_by", None)
            handles = self.mint_runtime_handles(str(namespace) if namespace else None)
            try:
                bind(handles)
            except BindDeclinedError as decline:
                logger.info("plugin tool {} declined its runtime binding ({}); unregistering it", tool.name, decline)
                self.tools.unregister(tool.name)
            except Exception:
                logger.exception("plugin tool {} raised in bind_runtime; unregistering it", tool.name)
                self.tools.unregister(tool.name)
        for gate in self.tools.tool_gates:
            bind = getattr(gate, "bind_runtime", None)
            if not callable(bind):
                continue
            namespace = getattr(gate, "contributed_by", None)
            bind(self.mint_runtime_handles(str(namespace) if namespace else None))

    def mint_runtime_handles(self, namespace: "str | None"):
        """The late-bound grants, minted per holder.

        One minting path for tools, services and gates alike. The wake grant
        is namespaced to the contributing plugin (paper: contracts/scheduling.py):
        a holder's keys can neither see nor move another plugin's wakes, nor
        any plain reminder. None where the host runs no scheduler, or the
        holder carries no stamped identity to namespace by.
        """
        from raven.plugins.context import RuntimeHandles

        wake = None
        if self.cron_service is not None and namespace:
            from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler

            wake = NamespacedWakeScheduler(self.cron_service, namespace)
        return RuntimeHandles(
            session_dir=self.sessions.session_dir,
            subagent_registry=self.subagents.registry,
            subagents_paused=lambda: self.subagents.paused,
            playbook_runtime=self._playbooks,
            wake_scheduler=wake,
            direct_ask=self._direct_ask,
            rebind_workdir=self._rebind_workdir,
        )

    async def _direct_ask(
        self,
        prompt: str,
        choices: "list[str] | None",
        conversation_id: str,
        timeout_s: "float | None" = None,
    ) -> "str | None":
        """The loop's own user-question face, lent as ``RuntimeHandles.direct_ask``.

        Resolved per call, never at mint: the ask broker is injected by the
        transport after this loop is built, so a mint-time snapshot would
        forever answer None on those hosts. Answers None when no asking
        transport is bound, exactly as the graph-confirm flow experiences it.
        """
        tool = self.tools.get("ask_user")
        if not isinstance(tool, AskUserTool):
            return None
        return await tool.ask_direct(prompt, choices, conversation_id, timeout_s)

    def _rebind_workdir(self, session_key: str, target: "str | Path") -> Path:
        """Repoint one session's working directory, now and from now on
        (``RuntimeHandles.rebind_workdir``).

        Persists the override into the session's metadata -- the durable
        truth ``WorkdirResolver`` reads back (explicit > persisted >
        default) -- then repoints the live binding so the very next tool
        call in the current task resolves the new root. The turn-end reset
        of ``workdir.bind`` still runs; the next turn reads the persisted
        value. Targets pass ``workdir.validate_override`` against this
        loop's agent home.
        """
        resolved = workdir.validate_override(target, self.workspace)
        session = self.sessions.get_or_create(session_key)
        session.metadata["workdir"] = str(resolved)
        self.sessions.save(session)
        workdir.repoint(resolved)
        return resolved

    # Contributed background services, attached by the assembly root (inert)
    # and run only by a resident host; a one-shot turn never starts them.
    plugin_services: tuple = ()
    _plugin_services_started = False
    _started_services: tuple = ()

    async def start_plugin_services(self) -> None:
        """Start every contributed background service, once.

        Loud on error per the paper (contracts/services.py): a service that
        fails to start is reported and left out of the started set -- never
        retried silently, because a watcher that is secretly dead is the lie
        a watching product exists to prevent.
        """
        if self._plugin_services_started:
            return
        self._plugin_services_started = True
        started = []
        for service in self.plugin_services:
            namespace = getattr(service, "contributed_by", None)
            handles = self.mint_runtime_handles(str(namespace) if namespace else None)
            try:
                await service.start(handles)
            except Exception:
                logger.exception(
                    "plugin service {} failed to start; it stays stopped",
                    getattr(service, "contributed_by", service),
                )
                continue
            started.append(service)
        self._started_services = tuple(started)

    async def stop_plugin_services(self) -> None:
        """Stop started services, newest first; idempotent, loud on error."""
        services, self._started_services = self._started_services, ()
        self._plugin_services_started = False
        for service in reversed(services):
            try:
                await service.stop()
            except Exception:
                logger.exception(
                    "plugin service {} raised while stopping; continuing shutdown",
                    getattr(service, "contributed_by", service),
                )

    def _build_playbooks(self) -> None:
        """Build the playbook runtime for the bundled entry tools to bind.

        Runs after ``_register_default_tools`` because the runtime's generator
        needs a real inventory of what this install offers, and that is
        ``self._mcp_servers`` plus a *populated* tool registry. Registering the
        two playbook tools from inside ``_register_default_tools`` was what made
        the two orderings look compatible: the runtime had to exist before
        registration, yet could not be built until after it.

        The two entry tools are plugin cargo now (``raven/plugins/bundled/playbook``):
        they register with the other plugin tools and receive this runtime in
        ``_bind_plugin_runtime``, so the loop assembles the funnel and the
        plugin serves it.

        It read ``self._mcp_servers`` several assignments before that field
        existed, so ``enabled: true`` raised ``AttributeError``, the guard below
        swallowed it, and the whole feature was off on every install that asked
        for it -- with one warning line as the only trace. No test caught it
        because none of them constructed an ``AgentLoop`` with a playbook
        config; the loop-level playbook tests now do.

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

    def _build_playbook_runtime(self, cfg: "PlaybookConfig"):
        """Assemble the playbook funnel from pieces this loop already owns.

        The executor gets a private SubAgentDagTool instance (never
        registered, so no LLM-facing surface changes) wired to the same
        manager hooks as the registered one: one dispatch gate, one quota,
        one announce path.
        """
        from raven.agent.subagent.dag_tool import SubAgentDagTool
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
            mode_for=self.subagents.resolve_mode,
            gate=self.subagents.dispatch_gate,
            announce=self.subagents.announce_dag_result,
            announce_exception=self.subagents.announce_dag_exception,
            adopt=self.subagents.adopt_background_run,
            charge=self.subagents.charge_dag_run,
            ask=self._confirm_graph,
            # Same manager hooks as the registered tool (see the docstring above):
            # a playbook step is an ordinary DAG node, so it is judged on the same
            # terms once judgement is wired in.
            provider_for=self._verdict_provider,
            adjudicate=self._adjudicate_node,
            verdict_config=self.subagent_dag_config,
        )
        from raven.playbook import (
            PlaybookGenerator,
            RouterSizes,
            agent_profiles_from_registry,
            live_inventory,
        )

        executor = PlaybookExecutor(
            dag_tool=dag_tool,
            provider=self.provider,
            compose_model=cfg.model,
        )
        # Both Playbook model calls use the live, capability-aware agent view.
        executor.set_agent_profiles(lambda: agent_profiles_from_registry(self.subagents.registry))
        store = PlaybookStore(user_layer)
        # Rides on the runtime below: creation shares the library and generator
        # with the funnel, so both entry tools write the same place.
        generator = PlaybookGenerator(
            self.provider,
            None,
            lambda: agent_profiles_from_registry(self.subagents.registry),
            # A real inventory: with the empty one this used to pass,
            # ``check_assets`` judged every skill and mcp server a draft named to be
            # unknown, so a good draft came back annotated as missing everything.
            live_inventory(self._mcp_servers, self.tools.names()),
            model=cfg.model,
        )

        return PlaybookRuntime(
            store=store,
            executor=executor,
            generator=generator,
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

    def _verdict_provider(self) -> Any:
        """The provider the node judge should call, resolved per dispatch.

        `provider` is a property over the running turn's binding, so reading it
        while this loop was still being built froze the default one onto the
        graph tool: a session that switched model never reached the judge. And a
        pinned `verdictModel` has to travel with the provider holding its
        credential -- a bare model id sent through another vendor's provider
        fails, and the judge fails open, so verdicts would go quietly off.
        """
        pinned = self.subagent_dag_config.verdict_model
        if pinned and self._provider_pool is not None:
            binding = self._provider_pool.bind_pin(pinned)
            if binding is not None:
                return binding.provider
        return self.provider

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

    async def _adjudicate_node(self, conversation_id: str, report: str, timeout_s: float | None = None) -> str | None:
        """A foreground graph's route to a decision about a node that fell short.

        A backgrounded run reports to the model and waits for `resolve_dag_node`,
        which works because the turn that submitted it has already returned. A
        foreground run has not: its own tool call is still on the stack, the
        scheduler serialises the conversation's lane, and the turn that would
        answer cannot start until this one ends. Every foreground suspension
        would therefore wait out its whole timeout and then blame the agent.

        So in foreground the question goes to the person watching the blocking
        call instead, over the same round trip the confirm gate uses. Free text,
        not a yes/no: a continuation with nothing to say is already treated as a
        failure, so a bool could only ever abandon.

        ``timeout_s`` is the runner's remaining budget for the whole round, not
        this question's own: the broker allows one pending question per
        conversation, so a graph with several suspended nodes asks them in
        series, and without a shared budget N nodes would cost N timeouts.

        ``None`` means the round trip is structurally unavailable -- no broker,
        no conversation -- and the caller falls back to failing the node, which
        is what happened before any of this existed.
        """
        tool = self.tools.get("ask_user")
        if not isinstance(tool, AskUserTool):
            return None
        return await tool.ask_direct(report, ["Continue", "Abandon"], conversation_id, timeout_s)

    def _dag_guide_skill_id(self) -> str | None:
        """The orchestration guide's id for the DAG tool description, or None.

        The tool tells the agent to load this skill before its first call, so
        the pointer must be real: a deployment that removed the builtin skills
        would otherwise burn a turn on a read_skill that cannot resolve. When
        the registry itself is unreachable, keep the pointer — the shipped
        skill is there by default, and losing the instruction is the worse
        failure of the two.
        """
        from raven.agent.subagent.dag_tool import GUIDE_SKILL_ID

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
            "resolve_dag_node",
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
                    "resolve_dag_node",
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

    def resolve_dag_node(self, run_id: str, node_id: str, decision: str, message: str | None) -> bool:
        """Answer one suspended node, whichever instance owns its run."""
        return any(tool.resolve_node(run_id, node_id, decision, message) for tool in self.dag_tools())

    def dag_control_reachable(self) -> bool:
        """Whether the schema-hidden dag control tools have a call path.

        The graph tool advertises ``dag_status`` / ``cancel_dag`` /
        ``resolve_dag_node`` in its acceptance text, and this is the gate that
        keeps the advertisement
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
