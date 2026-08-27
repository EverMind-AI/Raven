"""One session's engine: an agent loop and the spine it runs turns through.

Per session rather than per process, which is the one structural decision in this
package that is not the reference implementation's. The reason is
``raven/ppt/contracts/project.py``: ``Project.root`` is ``workspace / "deck"`` and
its docstring is explicit -- "a workspace is one task: the launcher makes a
directory per spawn and the agent is fenced inside it". An ``AgentLoop`` takes its
workspace once, at construction, and every ppt tool reads it from there. Two
sessions sharing one engine would build two decks over each other in one ``deck/``.

So the launcher's "a directory per spawn" becomes "a directory per session", and
the object that is fenced inside it is built here.

The construction mirrors ``raven/cli/agent_commands.py``'s, minus what this
surface is not: no sentinel stack (there is no channel manager to deliver a nudge
through), no cron service (nothing here fires jobs). That duplication is
deliberate rather than extracted -- the one-shot ``agent -m`` path is what
``run.py`` still drives today, and refactoring it to share a builder would put the
working path at risk for a change that is additive everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.acp.session import AcpSession
from raven.acp.spine import AcpOutlet, Frames, build_acp
from raven.spine import Scheduler
from raven.spine.delivery import DeliveryHub


def bind_ask_user(agent_loop: Any, questions: Any) -> bool:
    """Give this session's ``ask_user`` a broker, reporting whether one was bound.

    A named function rather than four lines inside ``_build`` because it is the
    single point where this surface becomes able to ask a question at all: delete
    it and every test in the package still passes while ``ask_user`` answers
    "not configured" and the turn proceeds on a guess. Something has to be able to
    assert it happened.
    """
    if questions is None:
        return False
    ask_tool = agent_loop.tools.get("ask_user")
    if ask_tool is None or not hasattr(ask_tool, "set_broker"):
        return False
    ask_tool.set_broker(questions.new_broker())
    return True


@dataclass
class SessionEngine:
    """What one session needs to run turns, and how to give it back."""

    agent_loop: Any
    scheduler: Scheduler
    hub: DeliveryHub
    outlet: AcpOutlet
    _spine_teardown: Any
    _memory_backend: Any = None

    async def teardown(self) -> None:
        """Stop this session's engine. Best-effort at every step.

        Ordered: the scheduler first (no more turns, so nothing new is emitted),
        then the hub's outlet workers, then MCP, then the memory backend -- whose
        detached indexing writes have to be drained before its HTTP client is
        closed under them.
        """
        for label, step in (
            ("spine", self._spine_teardown()),
            ("mcp", self.agent_loop.close_mcp()),
        ):
            try:
                await step
            except Exception:
                logger.exception("acp: tearing down {} failed", label)
        if self._memory_backend is not None:
            try:
                await self.agent_loop.drain_backend_stores()
                await self._memory_backend.stop()
            except Exception:
                logger.exception("acp: stopping the memory backend failed")


def build_engine_factory(config: Any, emit: Frames, sessions: Any, questions: Any = None) -> Any:
    """A factory that builds one :class:`SessionEngine` per session.

    ``config`` is the already-loaded runtime config, passed in rather than read
    here: loading it calls ``set_config_path`` as a side effect, which everything
    downstream (the data directory, ``load_raven_config``) then derives from -- so
    it has to happen once, in the process shell, before the jobs root is even
    known. What is per session is the workspace, which is the whole point.
    """

    async def factory(session: AcpSession) -> SessionEngine:
        return await _build(config, session, emit, sessions, questions)

    return factory


async def _build(config: Any, session: AcpSession, emit: Frames, sessions: Any, questions: Any = None) -> SessionEngine:
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.recovery import limits_from_defaults
    from raven.cli._helpers import make_provider
    from raven.cli._plugin_stack import build_plugin_registry, build_plugin_tools, maybe_build_memory_backend
    from raven.config.raven import load_raven_config
    from raven.session.manager import SessionManager
    from raven.utils.helpers import sync_workspace_templates

    workspace: Path = session.root
    ec_config = load_raven_config()
    sync_workspace_templates(workspace)

    # One registry for both the memory backend and the plugin tools, so discovery
    # and activation run once.
    plugin_registry = build_plugin_registry(ec_config)
    backend = maybe_build_memory_backend(workspace, ec_config, registry=plugin_registry)
    plugin_tools = build_plugin_tools(workspace, ec_config, registry=plugin_registry)

    defaults = config.agents.defaults
    agent_loop = AgentLoop(
        provider=make_provider(config),
        workspace=workspace,
        model=defaults.model,
        max_iterations=defaults.max_tool_iterations,
        empty_recovery=limits_from_defaults(defaults),
        context_window_tokens=defaults.context_window_tokens,
        max_concurrent_subagents=defaults.max_concurrent_subagents,
        max_subagent_spawns_per_hour=defaults.max_subagent_spawns_per_hour,
        brave_api_key=config.tools.web.search.api_key or None,
        web_search_max_results=config.tools.web.search.max_results,
        jina_api_key=config.tools.web.jina_api_key or None,
        web_proxy=config.tools.web.proxy or None,
        media_config=config.effective_media_config(),
        deep_research_config=config.tools.deep_research,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=SessionManager(workspace),
        mcp_servers=config.tools.mcp_servers,
        disabled_tools=config.tools.disabled_tools,
        tool_search_config=config.tools.tool_search,
        ppt_config=config.tools.ppt,
        sandbox_config=config.tools.sandbox,
        channels_config=config.channels,
        skill_forge_config=ec_config.skill_forge,
        context_config=ec_config.context,
        runtime_config=ec_config.runtime,
        # The per-turn checkpoint is a shadow ``git add -A`` over the workspace, and
        # this workspace is a deck tree carrying every rendered page -- megabytes of
        # PNG per turn, snapshotted for a recovery prompt a deck run has never
        # needed. Same value ``agent -m`` passes today, so this is unchanged
        # behaviour; ``runtime.checkpoint.policy: "always"`` is the opt-in.
        interactive=False,
        backend=backend,
        memory_config=ec_config.memory,
        skill_forge_router_config=ec_config.skill_forge.router,
        plugin_tools=plugin_tools,
    )
    agent_loop.configure_personalization(defaults.enable_personalization)

    if backend is not None:
        try:
            await backend.start()
        except Exception:
            logger.exception("acp: memory backend start failed; continuing with the legacy memory path")

    bind_ask_user(agent_loop, questions)

    scheduler, hub, outlet, spine_teardown = build_acp(agent_loop, emit, sessions)
    # Wired for parity with the other surfaces: a sub-agent this loop spawns
    # submits its result-reinjection turn through the same scheduler.
    agent_loop.subagents.set_submit(scheduler.submit)
    return SessionEngine(
        agent_loop=agent_loop,
        scheduler=scheduler,
        hub=hub,
        outlet=outlet,
        _spine_teardown=spine_teardown,
        _memory_backend=backend,
    )


__all__ = ["SessionEngine", "bind_ask_user", "build_engine_factory"]
