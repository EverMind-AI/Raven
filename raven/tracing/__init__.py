"""Raven in-tree tracing: audit.span.v1 observability.

Instrumentation is done by annotating raven's own methods with the
``@trace.instrument(...)`` decorator (see :mod:`raven.tracing.trace` and the
standard in ``docs/TRACING_STANDARD_API.md``). Nothing is monkeypatched — the
decorators live in raven's source and are no-op when tracing is disabled, so a
tracing failure can never alter the host's behavior.

Turn off with ``RAVEN_TRACING=0`` or ``[tracing] enabled = false`` in the raven
config. Spans land at ``~/.raven/traces/logs/audit-spans.log`` (override with
``RAVEN_TRACING_DIR``). Open the dashboard with ``raven tracing`` or ``/tracing``.
"""

from __future__ import annotations

from . import config, trace

__all__ = ["enabled", "trace"]


def enabled() -> bool:
    return config.enabled()
