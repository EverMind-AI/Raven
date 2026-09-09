"""Shared loguru-suppression helper for CLI subapps.

Both ``cron_app`` and ``sentinel_app`` install a typer callback that
mutes raven-subsystem INFO chatter (``CronService: jobs.json
modified...``, ``PendingDecisionStore: superseded...``) so the CLI's
table output stays clean. Set ``RAVEN_CLI_DEBUG=1`` to keep the
default loguru config when diagnosing why a CLI command failed.

NOTE: ``logger.remove()`` is process-global. CLI invocations are
their own short-lived processes so this is fine in practice. If a
test ever invokes a CLI subapp via ``CliRunner`` and also asserts on
loguru output from another component, this helper will clobber that
output — at that point switch to a per-sink filter.
"""

from __future__ import annotations

import os
import sys

from loguru import logger


def mute_subsystem_logs_unless_debug() -> None:
    """Replace loguru's default handler with a WARNING-only stderr sink.
    No-op when ``RAVEN_CLI_DEBUG`` is set."""
    if os.environ.get("RAVEN_CLI_DEBUG"):
        return
    logger.remove()
    logger.add(sys.stderr, level="WARNING")


__all__ = ["mute_subsystem_logs_unless_debug"]
