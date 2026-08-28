"""Shared pytest fixtures.

Autouse fixtures live here so every test sees them without explicit
declaration.
"""

from __future__ import annotations

import os

import pytest

# LiteLLM fetches its price and context table over the network at import unless
# this is set, and setting it before first use is too late -- the remote table is
# already loaded by then. The suite reads that table as a fixed input, the same
# reason `_no_openrouter_network` keeps raven's own catalogue fetch off the wire,
# so leaving it remote makes assertions depend on what a vendor published that
# morning: a newly added row answered a lookup several tests had arranged to
# miss, and they failed on numbers nobody in this repo had touched.
# `setdefault`, so a developer can still point a run at the live table.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def pytest_unconfigure(config: pytest.Config) -> None:
    """On CI, hard-exit past interpreter finalization once the run is over.

    A fully green run still exited 139 on Linux: the suite finalizes with
    native state live (asyncio subprocess transports collected during GC),
    and Py_FinalizeEx segfaults on it, masking the recorded status. The CLI
    routes its exit through the same helper, but on a different trigger --
    see raven.cli._exit for the lancedb-specific gate it uses, which is not
    what fires here.

    Local runs keep normal semantics so nothing masks an exit-time error, and
    the recorded status is preserved either way -- a failing run still exits
    non-zero.
    """
    import os

    if not os.environ.get("CI"):
        return

    from raven.cli._exit import flush_and_hard_exit

    flush_and_hard_exit(int(getattr(config, "_raven_exitstatus", 0)))


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
def _no_update_check(tmp_path, monkeypatch):
    """Keep the startup update check off the network and off the real disk.

    ``raven tui`` fires ``maybe_refresh_async()`` and ``session.create`` reads
    the cache, so any test reaching either path would otherwise fetch the
    GitHub releases API and write ``<cache dir>/update_check.json`` under the
    real home. Redirecting the cache dir alone still leaves an empty cache,
    which is exactly the state that spawns the fetch -- so opt out by env for
    the whole suite. Tests that exercise the notice clear the variable.
    """
    from raven.cli import update_notice

    monkeypatch.setenv(update_notice._OPT_OUT_ENV, "1")
    monkeypatch.setattr(update_notice, "_cache_path", lambda: tmp_path / "update_check.json")
    yield


@pytest.fixture(autouse=True)
def _no_real_oauth_credentials(tmp_path, monkeypatch):
    """Point every OAuth credential lookup at a temp dir for the whole suite.

    ``import_litellm`` publishes these variables so LiteLLM's drivers and raven
    agree on one location, and they outlive the test that triggered the import:
    a later test that fakes the home directory still reads whatever the first one
    resolved. On a developer machine that is a real signed-in credential, which
    makes providers report themselves configured, sends the Codex catalog lookup
    to the network, and puts a real credential file in reach of a test that
    deletes one. All four families are covered, not only the two LiteLLM reads by
    variable: the other two derive their path from the home directory, which a
    test may or may not have faked. Tests that exercise a credential set these
    themselves.
    """
    for name in ("CHATGPT_TOKEN_DIR", "CHATGPT_AUTH_FILE", "GITHUB_COPILOT_TOKEN_DIR", "MINIMAX_OAUTH_TOKEN_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "oauth" / "chatgpt"))
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "oauth" / "github_copilot"))
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path / "oauth"))
    yield


@pytest.fixture(autouse=True)
def _isolate_tracing_state_dir(tmp_path, monkeypatch):
    """Keep span emission off the real ``~/.raven/traces`` for the whole suite.

    Tracing is on by default and ``trace.span()`` calls are embedded in library
    code (agent loop, subagents, TUI RPC), so any test exercising those paths
    emits spans with fabricated session keys (``session-a``, ``weixin:c``, ...)
    into the real trace store — they then surface as phantom sessions in
    ``raven trajectory``. Redirect only the directory, not the enabled switch,
    so the suite keeps exercising the real emission path with zero behavior
    change; leaks land in tmp instead.

    ``raven.tracing.spans._store`` is a lazy module-level singleton pinned to
    the directory resolved at first emit, so it must be reset around each test
    or it keeps pointing at whichever directory was active when some earlier
    test (or the real environment) first emitted. Tests that need their own
    directory keep working: their ``monkeypatch.setenv`` runs after this
    fixture and wins, and they already reset ``_store`` themselves.
    """
    from raven.tracing import spans as _spans

    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path / "traces"))
    _spans._store = None
    yield
    _spans._store = None


@pytest.fixture(autouse=True)
def _no_openrouter_network(tmp_path):
    """Keep the OpenRouter catalog fetch off the network and off the real disk.

    The cross-provider pricing/context fallback fetches OpenRouter's /models for
    any LiteLLM-miss model, so an un-mocked test would hit the network. Default
    to an empty catalog; tests that exercise the catalog restore the real fetch
    and mock the transport. The disk cache path is also redirected to a temp
    file so the real ~/.raven/cache/ is never read or written.
    """
    from raven.providers import model_catalog_cache, rates

    original_fetch = rates._fetch_openrouter_models
    original_path = model_catalog_cache._CACHE_PATH
    rates._fetch_openrouter_models = lambda: {}
    model_catalog_cache._CACHE_PATH = tmp_path / "model-catalog.json"
    try:
        yield
    finally:
        rates._fetch_openrouter_models = original_fetch
        model_catalog_cache._CACHE_PATH = original_path
        rates._OPENROUTER_CACHE.clear()
        rates._OPENROUTER_CACHE_TIME = 0.0
