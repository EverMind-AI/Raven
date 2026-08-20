"""Shared pytest fixtures.

Autouse fixtures live here so every test sees them without explicit
declaration.
"""

from __future__ import annotations

import importlib

import pytest

# The IM-channel adapters import vendor SDKs that are not in this project's
# dependency groups (Raven-X is the DR-only fork). Their seven collection
# errors aborted the entire run, so `uv run pytest` executed ZERO of the 3,958
# tests that do collect - a test suite that cannot start is indistinguishable
# from a test suite that passes, and this one had been "green" by never running.
#
# Skip on genuine absence only, per module, so installing an SDK re-enables its
# test instead of leaving a permanently dead file. Do NOT fix this by installing
# the SDKs: this venv is what the eval subprocesses import, and changing it
# changes the measured artifact.
# The SDK name per channel is deliberately NOT hardcoded here - ask the code.
# Probing a list of guessed distribution names would silently ignore a test whose
# SDK is actually installed (guess wrong -> skip forever), which is the same
# always-on failure mode this block exists to remove.
_CHANNELS = ("dingtalk", "feishu", "matrix", "qq", "slack", "telegram", "wecom")


def _missing_sdk(channel: str) -> bool:
    try:
        importlib.import_module(f"raven.channels.adapters.{channel}.channel")
    except ModuleNotFoundError:
        return True
    except Exception:
        return False        # imports fine but blows up for another reason: let it fail loudly
    return False


collect_ignore = [f"test_channels_{c}.py" for c in _CHANNELS if _missing_sdk(c)]


@pytest.fixture(autouse=True)
def _restore_named_logger_handlers():
    """Undo strips of third-party loggers' TTY handlers.

    ``raven/cli/_log_file.py:strip_tty_stream_handlers()`` walks
    ``logging.Logger.manager.loggerDict`` and removes every StreamHandler bound to
    stderr/stdout - from *all* loggers, by design (the TUI must not let libraries
    write over its alt-screen). Nothing puts them back, and the handler on
    ``LiteLLM`` / ``LiteLLM Router`` / ``LiteLLM Proxy`` is installed once at
    litellm import time, so the first test that drives that path silently
    disarms it for the rest of the session. Measured: after
    ``tests/integration/test_tui_rpc_production_smoke.py`` the count goes 1 -> 0
    and never recovers (10 of 255 file boundaries still had it, all earlier), and
    ``tests/test_cli_tui_logging_isolation.py`` then fails its own
    premise-check - whose message already named this cause.

    Restore per-test snapshots of NAMED loggers only. The root logger is
    deliberately excluded: pytest's caplog attaches its handler there, and
    rewriting root.handlers in teardown would fight it.
    """
    import logging

    mgr = logging.Logger.manager
    prev = {
        name: list(lg.handlers)
        for name, lg in list(mgr.loggerDict.items())
        if isinstance(lg, logging.Logger) and lg.handlers
    }
    try:
        yield
    finally:
        for name, handlers in prev.items():
            lg = mgr.loggerDict.get(name)
            if isinstance(lg, logging.Logger):
                lg.handlers = handlers


@pytest.fixture(autouse=True)
def _restore_config_path_global():
    """Keep one test's config path from being read by the next one.

    ``raven.config.loader._current_config_path`` is a module global, and
    ``get_config_path()`` prefers it over ``Path.home()/".raven"/"config.json"``.
    Any test that drives a CLI command in-process via CliRunner reaches
    ``raven/cli/_helpers.py:204 set_config_path(...)``; passing ``env={"HOME": ...}``
    to CliRunner does NOT isolate that, because the global is process state, not
    an env var. So a later test whose fixture patches ``Path.home`` writes its
    config where nobody looks and silently gets the DEFAULTS instead - measured:
    `test_plugin_command.py` handed `test_tui_rpc_model.py` a stale
    `/tmp/pytest-of-root/.../test_verbose_shows_factory0/config.json`, and six
    assertions then compared the default model against the one just written.

    Same class as ``_restore_current_event_loop`` below: process-global slot,
    snapshot and restore rather than hunting each victim.
    """
    from raven.config import loader

    prev = loader._current_config_path
    try:
        yield
    finally:
        loader._current_config_path = prev


@pytest.fixture(autouse=True)
def _restore_current_event_loop():
    """Keep one test from poisoning the process-global current-loop slot.

    ``asyncio.run()`` called from a *sync* test builds a Runner with
    ``set_event_loop=True``, and ``Runner.close()`` ends with
    ``events.set_event_loop(None)`` - leaving the policy's thread-local at
    ``_loop=None`` with ``_set_called=True`` and nothing restoring it. On a
    fresh interpreter ``asyncio.get_event_loop()`` merely warns and works;
    once ``_set_called`` is True it raises "There is no current event loop"
    instead. So one sync test that calls asyncio.run makes every later test
    using the legacy ``get_event_loop().run_until_complete(...)`` pattern fail
    - passing alone, failing in a full run, purely from collection order.
    ``tests/integration/`` collects before every ``tests/test_*.py``, so the
    offender there poisoned the whole session (measured: 51 failures).

    Snapshot and restore both fields, so no test can hand the next one a
    different global slot, and so this does not itself mask the 3.13
    deprecation warning.
    """
    import asyncio

    loc = asyncio.get_event_loop_policy()._local
    prev_loop = getattr(loc, "_loop", None)
    prev_set = getattr(loc, "_set_called", False)
    try:
        yield
    finally:
        # ★ 只修被毒化的那一个状态,别碰其他。第一版无条件还原,结果把 pytest-asyncio
        # 自己管理的槽位一起改了 —— tests/test_sandbox_unit.py 的一个异步用例因此以
        # anyio.WouldBlock / CancelledError 挂掉。被毒化状态的**唯一**形态是
        # (_loop is None, _set_called True):asyncio.run 的 Runner.close() 留下的正是它,
        # 而任何正常安装的 loop 都不是 None。所以只在那种情况下回填,其余一律不动。
        now_loop = getattr(loc, "_loop", None)
        now_set = getattr(loc, "_set_called", False)
        if now_loop is None and now_set and not (prev_loop is None and prev_set):
            loc._loop, loc._set_called = prev_loop, prev_set


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
    from raven.providers import model_catalog_cache, rates

    original_fetch = rates._fetch_openrouter_models
    original_path = model_catalog_cache._CACHE_PATH
    rates._fetch_openrouter_models = lambda **_: {}
    model_catalog_cache._CACHE_PATH = tmp_path / "model-catalog.json"
    try:
        yield
    finally:
        rates._fetch_openrouter_models = original_fetch
        model_catalog_cache._CACHE_PATH = original_path
        rates._OPENROUTER_CACHE.clear()
        rates._OPENROUTER_CACHE_TIME = 0.0
