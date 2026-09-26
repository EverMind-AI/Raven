"""Wire an owned ACP runtime using Raven's existing local-host and scheduler components."""

import os

from raven.agent.hook.adapters import OnUserInboundAdapter
from raven.agent.loop.bundles import HostWiring
from raven.agent.subagent.role import SUBAGENT_ENV_VAR
from raven.core.cron_stack import build_cron_service, chain_cron_activity_reset, make_on_session_wake
from raven.rpc.cron_events import fanout_cron_missed


def configure_role(baseline):
    """The isolated worker process owns its native role, independently of launch overrides."""
    os.environ[SUBAGENT_ENV_VAR] = "0" if baseline.allow_delegation else "1"


def local_host(config):
    cron = build_cron_service(allowed_channels={"acp"})
    return HostWiring(
        cron_service=cron,
        channels_config=config.channels,
        hooks=[OnUserInboundAdapter(chain_cron_activity_reset(cron))],
    )


async def start_cron(loop, stack):
    cron = loop.cron_service
    if cron is None:
        return
    cron.on_job = make_on_session_wake(submit=stack.turn_scheduler.submit, channel="acp")
    await cron.start()
    if cron.last_startup_drops:
        await fanout_cron_missed(stack.emitter, drops=cron.last_startup_drops)
