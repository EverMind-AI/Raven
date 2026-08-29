"""Engine assembly for the local surfaces: one runtime for a TUI, page or ACP host.

The gateway assembles per generation with its transports around it; the local
surfaces (the TUI launcher, ``raven serve``, the ACP server) all want the same
smaller thing -- config, a lazy provider behind model routing, a session
manager grouped by the launch directory, a workdir resolver, a cron service
scoped to the local channel, and the loop. It is assembly, so it lives here;
each surface keeps only its own error translation.
"""

from __future__ import annotations

from pathlib import Path

from raven.core.runtime import RavenRuntime


def build_engine(*, workspace: str | None = None, home: str | None = None, channel: str = "tui") -> RavenRuntime:
    """Assemble the local-surface runtime; raises what the parts raise.

    ``MissingCredentialsError`` for an unfinished install and pydantic's
    ``ValidationError`` for a malformed config come through untranslated: the
    calling surface decides whether that is a red line, an RPC error or a log.

    No ``response_modifier``: Sentinel proactivity belongs to the gateway
    process, and the local surfaces deliberately wire none.
    """
    from raven.agent.loop.bundles import HostWiring, TurnPolicy
    from raven.agent.loop.recovery import limits_from_defaults
    from raven.agent.workdir import WorkdirPolicy, WorkdirResolver, validate_override
    from raven.config.raven import load_raven_config
    from raven.core.config_stack import load_runtime_config
    from raven.core.cron_stack import build_cron_service, chain_cron_activity_reset
    from raven.core.provider_stack import build_model_routing
    from raven.core.runtime import build_runtime
    from raven.proactive_engine.schedulers.cron.tool import CronTool
    from raven.providers.factory import make_lazy_provider
    from raven.session.manager import SessionManager
    from raven.utils.paths import project_slug

    config = load_runtime_config(None, home=home)
    ec_config = load_raven_config()

    provider = make_lazy_provider(config)
    # Model routing (config.routing). A no-op when routing is disabled; the
    # knn wrapper only reads the lazy provider's plain fields, so deferring
    # the litellm build survives it.
    router, provider = build_model_routing(config, provider)
    # Sessions group by launch directory here, the way Claude Code groups
    # by project: one terminal session belongs to the checkout it was
    # started in. The gateway passes no slug -- one daemon serves every
    # project, so its grouping is the channel instead.
    launch_dir = Path.cwd()
    session_manager = SessionManager(
        config.workspace_path, project_slug=project_slug(launch_dir), project_dir=launch_dir
    )
    workdir_resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=config.workspace_path,
        launch_dir=Path.cwd(),
        explicit_workdir=validate_override(workspace, config.workspace_path) if workspace else None,
        sessions=session_manager,
    )

    cron = build_cron_service(allowed_channels={channel})

    runtime = build_runtime(
        config,
        ec_config,
        provider=provider,
        session_manager=session_manager,
        router=router,
        workdir_resolver=workdir_resolver,
        policy=TurnPolicy(
            max_iterations=config.agents.defaults.max_tool_iterations,
            empty_recovery=limits_from_defaults(config.agents.defaults),
            interactive=True,
        ),
        host=HostWiring(
            cron_service=cron,
            channels_config=config.channels,
            on_user_inbound=chain_cron_activity_reset(cron),
        ),
    )

    registered_cron_tool = runtime.loop.tools.get("cron")
    if isinstance(registered_cron_tool, CronTool):
        registered_cron_tool.set_context(channel, "default")

    # cron.on_job is wired by the surface once its spine scheduler exists: a
    # reminder runs as a CRON turn through the scheduler and its reply is
    # fanned out as a cron.delivered event.
    return runtime


__all__ = ["build_engine"]
