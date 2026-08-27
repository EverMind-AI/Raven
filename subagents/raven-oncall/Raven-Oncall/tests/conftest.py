"""Shared pytest fixtures.

Autouse fixtures live here so every test sees them without explicit
declaration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._shared_home_guard import describe_drift, failure_report, snapshot

_HOME_GUARD_KEY = "_raven_shared_home_snapshot"
_HOME_GUARD_OFF = "RAVEN_ALLOW_SHARED_HOME_WRITES"


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get(_HOME_GUARD_OFF):
        return
    setattr(session.config, _HOME_GUARD_KEY, snapshot(Path.home() / ".raven"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    before = getattr(session.config, _HOME_GUARD_KEY, None)
    if before is None:
        return
    problems = describe_drift(before, snapshot(Path.home() / ".raven"))
    if not problems:
        return
    print(failure_report(problems))
    if session.exitstatus == 0:
        session.exitstatus = 1


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


@pytest.fixture(scope="session")
def isolated_raven_home(tmp_path_factory):
    """One directory for the whole session, standing in for ``~/.raven``.

    Session-scoped on purpose. The span store is a process-global singleton bound
    on first use (``raven.tracing.spans._store``), so a per-test directory would
    be deleted underneath it and every later write would be swallowed -- which
    looks exactly like isolation working and would hide a real leak.
    """
    return tmp_path_factory.mktemp("raven-home")


@pytest.fixture(autouse=True)
def _isolate_shared_raven_home(isolated_raven_home, monkeypatch):
    """Keep the suite from writing into the real ``~/.raven``.

    The concrete incident: a test run appended 183 audit spans (and 234 span
    artifacts) to ``~/.raven/traces/logs/audit-spans.log`` while a live on-call
    experiment was writing the same file. Nothing broke -- the test sessions were
    ``s1``/``s2``/``test:c1`` and filtered out cleanly against the run's
    ``cron:*`` sessions -- but that was luck about session naming, not isolation,
    and the audit trail is the evidence a run is judged on.

    Three mechanisms, because the paths resolve three different ways:

      - ``RAVEN_TRACING_DIR`` is read per call by ``tracing.config.state_dir``,
        so an env var moves it -- but only before the store singleton binds.
      - ``raven.tracing.spans._store`` is that singleton. It is reset here so the
        first traced call in a test binds the isolated path instead of inheriting
        whatever an earlier test bound.
      - the ops paths all resolve through ``config.paths.get_ops_home``, which is
        read per call, so patching that one function moves the ledgers, the
        events and the reports together. It used to be three module-level
        constants bound at import; a call that reached a constant the list had
        missed wrote to the real home while looking isolated.

    **What this does not cover.** It redirects the paths a test is known to reach
    by default; it is not a sandbox. Anything that resolves a shared path some
    other way -- a test passing an explicit ``~/.raven/...`` argument, a
    subprocess with its own environment, or ``get_config_path()`` when a test
    sets the config path itself -- still writes to the real home. Adding a
    default that lands in ``~/.raven`` means adding it here too.
    """
    home = isolated_raven_home
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(home / "traces"))

    # Drop the bound singleton so the isolated path takes effect, and drop it
    # again on teardown so a test that pointed tracing somewhere itself does not
    # leave that binding behind for the next one.
    try:
        from raven.tracing import spans as _spans

        _spans._store = None
    except ImportError:
        _spans = None

    ops_home = home / "ops"
    try:
        from raven.config import loader as _config_loader
        from raven.config import paths as _config_paths

        def _isolated_ops_home() -> Path:
            """The isolated home, unless the test chose an instance itself.

            Pinning this unconditionally also disables ``set_config_path``, and
            "two instances keep their campaigns apart" is resolved through exactly
            this path -- so the isolation the product sells became the one thing
            its own tests could not exercise. A test that selects a config gets the
            ops directory that follows from it; every other test still lands here.

            Deliberately narrower than moving the default config path, which was
            tried first: that also moved what ``get_config_path`` returns, and
            several tests read the operator's real config through it. Widening the
            blast radius to fix a test-visibility problem is the wrong trade.
            """
            if _config_loader._current_config_path is not None:
                return _config_loader._current_config_path.parent / "ops"
            return ops_home

        monkeypatch.setattr(_config_paths, "get_ops_home", _isolated_ops_home, raising=False)
    except ImportError:
        pass

    try:
        from raven.ops import connections as _connections

        def _isolated_connections_store() -> Path:
            """The isolated home, unless the test chose an instance itself.

            Added here for the reason this fixture states above: the connection
            registry is read by default from the config file's own directory,
            which this fixture deliberately does not move, so it landed in the
            operator's real ``~/.raven``. That is what decides whether the on-call
            surface is present at all (raven.ops.gate), so leaving it unisolated
            made a machine registered on the developer's laptop change the tool
            schema every test sees -- and the suite passed or failed by whose
            machine it ran on.

            The env override wins here as it does in production: it is how a
            launcher points an instance at the owner's registry, and a test that
            sets it is naming a path of its own rather than reaching for the
            developer's home.
            """
            import os

            chosen = os.environ.get(_connections.CONNECTIONS_ENV, "").strip()
            if chosen:
                return Path(chosen).expanduser()
            if _config_loader._current_config_path is not None:
                return _config_loader._current_config_path.parent / _connections.STORE
            return home / _connections.STORE

        monkeypatch.setattr(_connections, "store_path", _isolated_connections_store, raising=False)
    except ImportError:
        pass

    try:
        from raven.token_wise import usage_tracker

        monkeypatch.setattr(usage_tracker, "_default_telemetry_dir", lambda: home / "telemetry", raising=False)
    except ImportError:
        pass

    yield

    if _spans is not None:
        _spans._store = None



@pytest.fixture(autouse=True)
def _on_call_off_unless_asked(monkeypatch):
    """Pin the on-call switch off for the whole suite.

    ``tools.oncall.enabled`` is read from the operator's own config, which this
    file deliberately does not move (see ``_isolate_shared_raven_home``), so a
    developer who turned on-call on for their own runs would have every test see
    thirteen extra tools and an extra model call per turn -- and the suite would
    pass or fail by whose machine ran it. It did: enabling it locally put four
    upstream tests into failure, over a config file no test had touched.

    A test that wants the surface asks for ``on_call_enabled``, which patches the
    same function afterwards and therefore wins.
    """
    from raven.ops import gate

    monkeypatch.setattr(gate, "on_call_available", lambda: False)
    yield


@pytest.fixture
def on_call_enabled(monkeypatch):
    """The owner's switch, on. It is what puts the on-call surface in an instance.

    Off by default (``tools.oncall.enabled``), because everything it adds costs
    every turn and is inert for someone who never runs work on another machine.
    A test asserting any of it -- the thirteen tools, the work-to-watch
    judgement, exec's ``machine`` parameter, the notes appended to TOOLS.md --
    has to turn it on, the same way an owner does.

    Patched at ``raven.ops.gate`` rather than written into a config file: the
    gate is what every caller asks, and going through the loader would depend on
    which config path this test happens to have selected.
    """
    from raven.ops import gate

    monkeypatch.setattr(gate, "on_call_available", lambda: True)
    yield True


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
