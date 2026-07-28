"""Shared pytest fixtures.

Autouse fixtures live here so every test sees them without explicit
declaration.
"""

from __future__ import annotations

import pytest


def pytest_unconfigure(config: pytest.Config) -> None:
    """Hard-exit past interpreter finalization when lancedb's runtime is live.

    Tests that build the agent loop leave lancedb's Rust/tokio daemon thread
    running with no shutdown hook, and finalizing the interpreter around it
    segfaults (exit 139) after a fully green run. The CLI guards its own exit
    the same way; see raven.cli._exit for the analysis.
    """
    from raven.cli._exit import flush_and_hard_exit, lancedb_finalization_hazard

    if not lancedb_finalization_hazard():
        return
    session = getattr(config, "_raven_exitstatus", 0)
    flush_and_hard_exit(int(session))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Stash the real exit status so pytest_unconfigure can preserve it."""
    session.config._raven_exitstatus = int(exitstatus)  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _restore_loguru_enabled_state():
    """Undo any ``loguru.logger.disable("raven")`` left over from a
    prior test.

    ``raven/cli/agent_commands.py`` toggles ``logger.disable("raven")``
    based on a ``--no-logs`` flag. The disable is process-global on
    loguru's singleton logger, so once a CliRunner-based test exercises
    that branch the flag persists for the rest of the pytest session,
    silently dropping every ``raven.*`` log emission and breaking
    any later test that asserts on loguru output via a sink.
    """
    from loguru import logger

    yield
    logger.enable("raven")


@pytest.fixture(autouse=True)
def _no_openrouter_network(tmp_path):
    """Keep the OpenRouter catalog fetch off the network and off the real disk.

    The cross-provider pricing/context fallback fetches OpenRouter's /models for
    any LiteLLM-miss model, so an un-mocked test would hit the network. Default
    to an empty catalog; tests that exercise the catalog restore the real fetch
    and mock the transport. The disk cache path is also redirected to a temp
    file so the real ~/.raven/cache/ is never read or written.
    """
    from raven.token_wise import model_catalog_cache, pricing

    original_fetch = pricing._fetch_openrouter_models
    original_path = model_catalog_cache._CACHE_PATH
    pricing._fetch_openrouter_models = lambda: {}
    model_catalog_cache._CACHE_PATH = tmp_path / "model-catalog.json"
    try:
        yield
    finally:
        pricing._fetch_openrouter_models = original_fetch
        model_catalog_cache._CACHE_PATH = original_path
        pricing._OPENROUTER_CACHE.clear()
        pricing._OPENROUTER_CACHE_TIME = 0.0
