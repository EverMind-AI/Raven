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
def no_vendored_subagents(monkeypatch):
    """Pin agent-table discovery off, so the suite sees the same table everywhere.

    ``AgentRegistry.apply`` discovers rows from the ``subagents/`` tree, and the
    suite runs inside a checkout that has one. Left alone, every table assertion
    would depend on machine state a test never set: four extra rows, each enabled
    or not according to whether that developer had built the folder's venv and
    supplied its key. A test that wants the discovered rows patches
    ``subagents_root`` itself to a tree it built.
    """
    monkeypatch.setattr("raven.agent.subagent.vendored_agents.subagents_root", lambda: None)


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
def _no_real_raven_home(tmp_path_factory, monkeypatch):
    """Keep the suite out of the config file of whoever is running it.

    ``get_config_path()`` answers ``RAVEN_HOME/config.json``, and anything reading
    it at test time therefore reads a real person's preferences. The tool array is
    assembled through one of those reads (``LiveConfig``, so an off switch takes
    effect on the next turn rather than the next restart), which makes the
    developer who has actually used that switch the one whose suite fails: a home
    config carrying ``tools.disabledTools: ["deep_research"]`` reds three tests in
    ``test_deep_research_tool.py`` and nothing in the failure points at the cause.

    ``_current_config_path`` is reset alongside it because it wins over both and
    is module-global: one test calling ``set_config_path`` otherwise aims every
    later test in the process at that path.

    The knob is ``HOME`` because it is the only one every test that isolates the
    home itself can still beat, and this fixture must lose to all of them. Tests
    do it two ways -- ``monkeypatch.setattr(Path, "home", ...)`` and
    ``monkeypatch.setenv("HOME", ...)`` -- and the precedence runs
    ``RAVEN_HOME`` > ``Path.home`` > ``HOME``. Setting ``RAVEN_HOME`` here beats
    both camps (measured: 17 unrelated failures), patching ``Path.home`` beats the
    ``setenv`` camp (measured: 3), and setting ``HOME`` beats neither: an attribute
    patch shadows it, and a later ``setenv`` replaces it.
    """
    from raven.config import loader

    # Outside ``tmp_path`` rather than under it, and fresh per test. Tests use
    # ``tmp_path`` as a workspace root and enumerate it, so a directory this
    # fixture leaves in there shows up in their assertions; and a session-shared
    # home would let one test read the config another one wrote.
    home = tmp_path_factory.mktemp("default_home")
    monkeypatch.setenv("HOME", str(home))
    # Unset rather than set: ``RAVEN_HOME`` outranks everything above, so a
    # developer who exports it hands their own directory to every test that
    # isolates the home some other way. Deleting it is the one move that closes
    # that without overriding anybody -- a test that wants the variable sets it
    # itself, which lands after this.
    monkeypatch.delenv("RAVEN_HOME", raising=False)
    monkeypatch.setattr(loader, "_current_config_path", None)
    yield


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


@pytest.fixture(autouse=True)
def _no_real_acp_journal(tmp_path, monkeypatch):
    """Keep ACP wire journals out of the real home.

    Every pooled connection opens one (``raven/agent/acp/journal.py``), so any
    test that launches a stub server would otherwise write the whole exchange --
    the prompt, every tool result, every stderr line -- under the developer's
    ``~/.raven/traces``, and leave it there after the run.
    """
    from raven.agent.acp import journal

    monkeypatch.setattr(journal, "journal_root", lambda: tmp_path / "acp-frames")
    yield


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Keep the browser opener off the desktop of whoever runs the suite.

    ``web`` and ``serve`` finish by opening the page they just brought up, so a
    test that drives either one without stubbing the opener launches real tabs
    on the machine running pytest -- pointed at a port nothing is listening on,
    since the port under test is a fixture's invention. Raising here turns that
    into a failure of the test that forgot to stub it, instead of a green run
    that hijacks the screen.
    """
    import webbrowser

    def _refuse(url, *_args, **_kwargs):
        raise AssertionError(f"a test opened a real browser at {url}; stub the opener instead")

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, _refuse)
