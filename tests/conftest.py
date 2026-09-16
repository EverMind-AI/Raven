"""Shared pytest fixtures.

Autouse fixtures live here so every test sees them without explicit
declaration.
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

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

# matplotlib builds its font list into its config dir the first time a process
# typesets anything: every system font is opened, and on macOS the list comes
# from a `system_profiler` call -- 8 to 12 s. The dir defaults to a path under
# HOME, and `_no_real_raven_home` hands every test a fresh HOME, so the list was
# rebuilt on every run and thrown away with the temp dir. A git-ignored dir in
# the checkout keeps it warm across runs; `setdefault` respects a developer's own.
_MPL_CACHE = Path(__file__).resolve().parent.parent / ".pytest_cache" / "matplotlib"
if os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE)) == str(_MPL_CACHE):
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)


_IDLE_EXEMPT_MARKERS = ("slow", "production_timing")
_IDLE_CEILING_S = 0.0
_IDLE_PROPERTY = "raven_idle_s"
_WALL_PROPERTY = "raven_wall_s"
_CLOCKS = pytest.StashKey[tuple[float, float]]()
_idle_hits: list[tuple[float, float, str]] = []


#: Three seconds rather than two, which is where this started. Measured across
#: the suite: the honest waits cluster at one second (a test that sets a one
#: second timeout and lets it expire), the highest unmarked one is 1.6 s, and
#: nothing sits between that and the marked renders. A runner stretches those
#: to about 2.2 s, so a two second line failed shards over tests that were
#: waiting the second they meant to. Every production ladder this guards
#: against is longer than three: the shortest constant the audit found was a
#: three second grace, and the rest run 5, 15, 30 and 60.
_DEFAULT_IDLE_CEILING_S = 3.0


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("raven")
    group.addoption(
        "--idle-ceiling",
        type=float,
        default=_DEFAULT_IDLE_CEILING_S,
        help="seconds an unmarked test may spend waiting (wall clock minus CPU) before it is reported; 0 disables",
    )
    group.addoption(
        "--idle-ceiling-strict",
        action="store_true",
        help="fail an otherwise green session when any unmarked test is over the idle ceiling",
    )
    group.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="collect only the K-th of N slices of the test files (1-based); files are dealt round-robin in sorted order",
    )


_SHARD: tuple[int, int] | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _IDLE_CEILING_S, _SHARD
    _IDLE_CEILING_S = float(config.getoption("--idle-ceiling"))
    _idle_hits.clear()
    _SHARD = None
    spec = config.getoption("--shard")
    if spec is not None:
        try:
            k, n = (int(part) for part in spec.split("/"))
        except ValueError:
            raise pytest.UsageError(f"--shard wants K/N, got {spec!r}") from None
        if not 1 <= k <= n:
            raise pytest.UsageError(f"--shard {spec}: K must be between 1 and N")
        _SHARD = (k, n)
    if config.getoption("--idle-ceiling-strict"):
        _warm_the_heaviest_import()


def _warm_the_heaviest_import() -> None:
    """Pay litellm's import here rather than inside whichever test is first.

    Opening its few thousand files costs about 1.7 s that no CPU accounts for,
    so it lands as idle on one test, and which test that is depends on the
    order the shard collected. A gate cannot be held to a moving target. Most
    runs import it during collection anyway, from the twenty test modules that
    name a provider at module level, and this is then a no-op.
    """
    # Under a temporary home for the length of the call. This runs before the
    # autouse fixtures that redirect the home, and the import publishes the
    # OAuth token directories, which creates them: warming it as-is put an
    # `oauth` directory in the developer's own ~/.raven.
    home = tempfile.mkdtemp(prefix="raven-warmup-")
    pinned = {
        "RAVEN_HOME": home,
        "GITHUB_COPILOT_TOKEN_DIR": os.path.join(home, "oauth", "github_copilot"),
        "CHATGPT_TOKEN_DIR": os.path.join(home, "oauth", "chatgpt"),
    }
    previous = {name: os.environ.get(name) for name in pinned}
    os.environ.update(pinned)
    try:
        from raven.providers.litellm_setup import import_litellm

        import_litellm()
    except Exception as exc:  # noqa: BLE001 -- a missing extra is not this hook's business
        print(f"idle ceiling: litellm did not warm up ({exc}); a first import may be charged to a test")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(home, ignore_errors=True)


def _no_recurse(pattern: str, directory: Path) -> bool:
    """pytest's norecursedirs rule: a pattern with a slash matches the whole path."""
    if "/" in pattern:
        return fnmatch.fnmatch(str(directory), f"*/{pattern}")
    return fnmatch.fnmatch(directory.name, pattern)


@functools.cache
def _test_file_index(
    tests_root: Path, python_files: tuple[str, ...], norecursedirs: tuple[str, ...]
) -> dict[Path, int]:
    """Every test file pytest would collect under ``tests_root``, sorted.

    Built from the ini values pytest itself collects by, so the index and the
    collection agree: a file in a directory pytest never enters has no index,
    and a file pytest would collect always has one. A collected file that had
    no index would be run by every shard.
    """
    files = {
        path
        for pattern in python_files
        for path in tests_root.rglob(pattern)
        if "__pycache__" not in path.parts
        and not any(
            _no_recurse(rule, tests_root / parent)
            for parent in path.relative_to(tests_root).parents
            for rule in norecursedirs
        )
    }
    return {path: index for index, path in enumerate(sorted(files))}


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Under ``--shard K/N``, leave the test files of the other shards alone.

    Decided here, before the file is imported, so a shard pays collection for
    its own modules only: importing all of them is two minutes of a CI runner.
    Round-robin over the sorted list, so a slow family whose files sort together
    (the ppt engine's) is spread over the shards rather than handed to one.
    Answers True or None, never False: the hook is firstresult, and False would
    stop pytest's own implementation, which is where --ignore and collect_ignore
    are honoured.
    """
    if _SHARD is None or collection_path.suffix != ".py":
        return None
    python_files = tuple(config.getini("python_files"))
    if not any(fnmatch.fnmatch(collection_path.name, pattern) for pattern in python_files):
        return None
    index = _test_file_index(config.rootpath / "tests", python_files, tuple(config.getini("norecursedirs"))).get(
        collection_path
    )
    if index is None:
        return None
    k, n = _SHARD
    return True if index % n != k - 1 else None


def _clocks() -> tuple[float, float]:
    """The wall clock, and every CPU second spent on this process's behalf.

    ``os.times`` counts the CPU of children this process has reaped as well as
    its own, and POSIX makes that recursive, so a subprocess doing real work --
    a LibreOffice conversion, a browser, eight spawned workers -- lands on the
    account of the test that waited for it.
    """
    spent = os.times()
    return time.perf_counter(), spent.user + spent.system + spent.children_user + spent.children_system


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    item.stash[_CLOCKS] = _clocks()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Write the test's idle time onto its teardown report.

    Idle is the wall clock of setup, call and teardown minus the CPU anyone
    spent on the test in that window. It is what the ceiling judges, rather
    than wall clock, because the class it guards against is a test waiting:
    on a production backoff, on a timeout it arranged, on a process that
    answers nothing. A wait costs the same seconds on every machine, where
    work costs two seconds on a laptop and seven on a CI runner and is not the
    problem. The report carries the number so the xdist controller, which sees
    only reports, can judge it.
    """
    outcome = yield
    if call.when != "teardown":
        return
    started = item.stash.get(_CLOCKS, None)
    if started is None:
        return
    now = _clocks()
    wall = now[0] - started[0]
    idle = wall - (now[1] - started[1])
    report = outcome.get_result()
    report.user_properties.append((_IDLE_PROPERTY, idle))
    report.user_properties.append((_WALL_PROPERTY, wall))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Hold every unmarked test to the idle ceiling.

    The suite's slow tail was production backoff waited out by tests with error
    stubs, and nothing failed when one more was added. A test that waits for a
    reason it can name carries ``slow`` or ``production_timing``.
    """
    if _IDLE_CEILING_S <= 0 or report.when != "teardown":
        return
    clocks = dict(report.user_properties)
    idle = clocks.get(_IDLE_PROPERTY)
    if idle is None or idle <= _IDLE_CEILING_S:
        return
    if any(marker in report.keywords for marker in _IDLE_EXEMPT_MARKERS):
        return
    _idle_hits.append((float(idle), float(clocks[_WALL_PROPERTY]), report.nodeid))


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    if not _idle_hits:
        return
    strict = config.getoption("--idle-ceiling-strict")
    verdict = "failing the session" if strict else "warning only; --idle-ceiling-strict fails it"
    terminalreporter.write_sep(
        "=",
        f"{len(_idle_hits)} unmarked test(s) waited more than {_IDLE_CEILING_S:g}s (wall clock minus CPU) ({verdict})",
    )
    for idle, wall, nodeid in sorted(_idle_hits, reverse=True)[:50]:
        terminalreporter.write_line(f"{idle:7.2f}s idle of {wall:6.2f}s  {nodeid}")
    terminalreporter.write_line("mark it slow or production_timing with the reason, or take the wait out of the test")


def pytest_unconfigure(config: pytest.Config) -> None:
    """On CI, hard-exit past interpreter finalization once the run is over.

    A fully green run still exited 139 on Linux: the suite finalizes with
    native state live (asyncio subprocess transports collected during GC),
    and Py_FinalizeEx segfaults on it, masking the recorded status. The
    helper lives in ``tests/_hard_exit.py``; nothing under ``raven/`` needs it.

    Local runs keep normal semantics so nothing masks an exit-time error, and
    the recorded status is preserved either way -- a failing run still exits
    non-zero.
    """
    import os

    if not os.environ.get("CI"):
        return

    from tests._hard_exit import flush_and_hard_exit

    flush_and_hard_exit(int(getattr(config, "_raven_exitstatus", 0)))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Stash the real exit status so pytest_unconfigure can preserve it.

    The idle verdict is applied here, on the controller only: an xdist
    worker's exit status is not the run's, and the hits it saw were forwarded.
    """
    if (
        _idle_hits
        and exitstatus == 0
        and session.config.getoption("--idle-ceiling-strict")
        and not hasattr(session.config, "workerinput")
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        exitstatus = int(session.exitstatus)
    session.config._raven_exitstatus = int(exitstatus)  # type: ignore[attr-defined]


@contextlib.contextmanager
def stopping_skill_watchers() -> Iterator[None]:
    """Stop every SKILL.md watcher started inside the block.

    ``LocalSkillCatalog`` starts one unless the caller passes
    ``start_watcher=False``, and the daemon thread it starts holds a strong
    reference back to the catalog through the ``on_change`` bound method -- so
    nothing collects it when the test that built it returns. The catalog says as
    much in its own comment, and every short-lived *production* consumer already
    passes the flag; a test is a short-lived consumer that never did.

    Tracking rather than suppressing: the watcher still starts, so a test that
    asserts on watcher behaviour keeps working and a production path that
    regresses into starting one is still visible. Only the cleanup is added.
    """
    from raven.memory_engine.skill_local.watcher import SkillFileWatcher

    started: list[SkillFileWatcher] = []
    real_start = SkillFileWatcher.start

    def _tracking_start(self: SkillFileWatcher) -> bool:
        ok = real_start(self)
        if ok:
            started.append(self)
        return ok

    SkillFileWatcher.start = _tracking_start  # type: ignore[method-assign]
    try:
        yield
    finally:
        SkillFileWatcher.start = real_start  # type: ignore[method-assign]
        for watcher in started:
            watcher.stop()


@pytest.fixture(autouse=True)
def _no_leaked_skill_watchers() -> Iterator[None]:
    """Keep watcher threads from accumulating across the session.

    Left alone this is not a slow leak: six of the catalog-building test files
    finish with 81 live watcher threads between them, and a full xdist worker
    carried hundreds, each holding its own inotify handles.
    """
    with stopping_skill_watchers():
        yield


@pytest.fixture(autouse=True)
def no_discovered_products(monkeypatch):
    """Pin agent-table discovery off, so the suite sees the same table everywhere.

    ``AgentRegistry.apply`` discovers rows from the ``agents/`` product tree, and
    the suite runs inside a checkout that has one. Left alone, every table
    assertion would depend on machine state a test never set: five extra rows,
    each enabled or not according to which engine wheels that developer has
    installed. A test that wants the discovered rows patches ``agents_root``
    itself to a tree it built.
    """
    monkeypatch.setattr("raven.agent.subagent.vendored_agents.agents_root", lambda: None)


@pytest.fixture(autouse=True, scope="session")
def _console_socket_registered():
    """Register the real CLI into the rpc console socket, once per worker.

    Production hosts register at assembly (the surfaces law forbids rpc from
    importing the cli, so the table arrives by registration); the suite
    registers the same way so method-level tests keep driving dispatch and
    the catalog as a hosted console. Tests that pin the UNregistered
    behaviour call ``cli_socket.reset()`` and re-register in their own
    cleanup.
    """
    from raven.cli._console_feature import register_console_feature

    register_console_feature()
    yield


@pytest.fixture(autouse=True)
def _restore_i18n_language():
    """Undo any ``raven.i18n.set_language`` left over from a prior test.

    The reply language is a module-level global only the CLI seeds at startup
    and the console's live switch now mutates in-process. Once one test flips
    it -- directly or through any path that applies a config -- every later
    test on that xdist worker renders the other language's templates, and
    which tests share a worker moves whenever the suite grows.
    """
    from raven import i18n

    before = i18n.current_language()
    yield
    i18n.set_language(before)


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
def _no_real_raven_home(tmp_path, monkeypatch):
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

    # Outside ``tmp_path`` rather than under it, and fresh per test. Tests use
    # ``tmp_path`` as a workspace root and enumerate it, so a directory this
    # fixture leaves in there shows up in their assertions; and a session-shared
    # home would let one test read the config another one wrote. A sibling of
    # ``tmp_path`` rather than a second numbered directory: pytest picks the next
    # number by scanning the whole base temp dir, and with 22k tests that scan
    # cost 44 s per worker for the numbered dirs this fixture alone created.
    home = tmp_path.with_name(f"{tmp_path.name}-home")
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # The suite's baseline permission mode is full access -- the behaviour the
    # whole suite was written against before the gate existed, and what a test
    # about streaming or diffs should keep seeing. The product default is ask;
    # tests about the gate itself write their own permissions node (LiveConfig
    # re-reads on byte change, so overwriting this file mid-test takes effect
    # on the next tool call).
    raven_home = home / ".raven"
    raven_home.mkdir()
    (raven_home / "config.json").write_text('{"permissions": {"mode": "full"}}')
    # Unset rather than set: ``RAVEN_HOME`` outranks everything above, so a
    # developer who exports it hands their own directory to every test that
    # isolates the home some other way. Deleting it is the one move that closes
    # that without overriding anybody -- a test that wants the variable sets it
    # itself, which lands after this.
    monkeypatch.delenv("RAVEN_HOME", raising=False)
    monkeypatch.setattr("raven.home._current_config_path", None)
    yield


@pytest.fixture(autouse=True)
def _no_production_waits(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the production retry and grace ladders to a millisecond for the suite.

    A stub that answers every call with an error drives the code under test
    into its backoff, and the backoff is tuned for a real outage: the loop's
    LLM-error ladder is 105 s, the provider's 7 s per model, a memory store
    retries for 52 s, a subagent steer waits 3 s for a hook, an ACP cancel is
    given 5 s to settle, the gateway holds delivery 2 s at teardown. One
    checkpoint test slept 133 s that way and proved nothing by it.

    The lengths stay. ``len(ladder)`` is how both retry ladders count their
    attempts, so an empty tuple would change behaviour rather than speed; the
    seconds collapse to a millisecond. Not to zero: the store pipeline waits
    its backoff out with ``wait_for(stopping.wait(), timeout=delay)``, and a
    zero timeout cancels that wait before it runs, so the stop it listens for
    could never be heard. The steer grace keeps one poll's worth so the loop
    body it guards stays exercised. A test that proves a timing property, or
    asserts a default's value, opts out with ``@pytest.mark.production_timing``
    and sets what it needs itself.
    """
    if request.node.get_closest_marker("production_timing"):
        return
    from raven.acp_client import client as acp_client
    from raven.agent.subagent import manager
    from raven.config import schema
    from raven.gateway import spine
    from raven.memory_engine import store_pipeline
    from raven.providers.base import LLMProvider

    def shortened(delays):
        return tuple(0.001 for _ in delays)

    monkeypatch.setattr(LLMProvider, "_CHAT_RETRY_DELAYS", shortened(LLMProvider._CHAT_RETRY_DELAYS))
    monkeypatch.setattr(schema, "LLM_ERROR_RETRY_DELAYS_DEFAULT", shortened(schema.LLM_ERROR_RETRY_DELAYS_DEFAULT))
    monkeypatch.setattr(store_pipeline, "BACKOFF_S", shortened(store_pipeline.BACKOFF_S))
    monkeypatch.setattr(manager, "_STEER_HOOK_GRACE_S", 0.05)
    monkeypatch.setattr(acp_client, "_CANCEL_SETTLE_S", 0.05)
    monkeypatch.setattr(spine, "_DELIVERY_GRACE", 0.05)


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
    from raven.updates import update_notice

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
        rates.reset_openrouter_cache()


@pytest.fixture(autouse=True)
def _no_huggingface_lookup(monkeypatch):
    """Keep LiteLLM's context-window lookup off huggingface.co.

    `rates._try_litellm_context_window` asks `litellm.get_model_info`, and for a
    model whose row carries no window LiteLLM fetches
    `huggingface.co/<model>/raw/main/config.json` to read `max_position_embeddings`.
    It is one request per process, cached afterwards, which is why it lands on
    whichever test in a worker happens to ask first -- a different name on every
    run, each charged one to four seconds of waiting it did not cause.

    The sibling above keeps raven's own catalogue fetch off the wire for the same
    reason; this one was missed because the request is made inside LiteLLM rather
    than here. `None` is the answer that function already gives when the fetch
    fails, so nothing downstream sees a shape it does not handle.
    """
    from litellm import utils as litellm_utils

    monkeypatch.setattr(litellm_utils, "_get_max_position_embeddings", lambda model_name: None)
    yield


@pytest.fixture(autouse=True)
def _no_real_acp_journal(tmp_path, monkeypatch):
    """Keep ACP wire journals out of the real home.

    Every pooled connection opens one (``raven/acp_client/journal.py``), so any
    test that launches a stub server would otherwise write the whole exchange --
    the prompt, every tool result, every stderr line -- under the developer's
    ``~/.raven/traces``, and leave it there after the run.
    """
    from raven.acp_client import journal

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


@pytest.fixture(autouse=True)
def _unbind_the_acp_turn() -> Iterator[None]:
    """Put the ACP turn's ContextVars back where the test found them.

    ``start_ask_turn`` binds an asker and an autofill for one turn. Called from
    a *sync* test the write lands in the context every later test in the same
    xdist worker inherits, so a neighbouring file asserting the unbound default
    reads the previous file's asker instead. Here rather than in the one file
    that noticed, because the next file to bind one from a sync test would
    reintroduce the leak.

    Restored through the tokens rather than by writing the defaults back: a
    token also carries "this var was never set", which no assignment can
    express.
    """
    from raven.acp_client import asker

    turn_token = asker._TURN.set(asker._TURN.get())
    autofill_token = asker._AUTOFILL.set(asker._AUTOFILL.get())
    try:
        yield
    finally:
        asker._TURN.reset(turn_token)
        asker._AUTOFILL.reset(autofill_token)


def wired_kwarg(kwargs: dict, name: str):
    """Resolve a wiring value from captured AgentLoop kwargs, bundle-aware.

    Entrances pass grouped bundles now; a test that asserts one wire reads it
    through the bundle the field lives in, or straight off the dict for the
    top-level keywords.
    """
    if name in kwargs:
        return kwargs[name]
    from raven.agent.loop.bundles import FIELD_OWNER

    owner = FIELD_OWNER.get(name)
    bundle = kwargs.get(owner) if owner else None
    return getattr(bundle, name, None) if bundle is not None else None


def make_channel_config(channel: str, **overrides):
    """A dispensed channel config for adapter tests: declared defaults, with
    ``overrides`` split between socket fields and cargo (cargo overrides run
    through the admission door, so an invalid test value bites here too)."""
    from raven.channels.registry import discover_specs
    from raven.config.admission import DispensedSlice, admit_slice
    from raven.config.schema import ChannelSocket

    schema = discover_specs()[channel].config_schema or {}
    socket_keys = {"enabled", "allow_from", "workspace"}
    socket = ChannelSocket(**{k: v for k, v in overrides.items() if k in socket_keys})
    cargo = admit_slice(
        schema,
        {k: v for k, v in overrides.items() if k not in socket_keys},
        plugin_id=f"test:{channel}",
    )
    for key, decl in schema.items():
        if key not in cargo and isinstance(decl.get("fields"), dict):
            cargo[key] = admit_slice(decl["fields"], {}, plugin_id=f"test:{channel}.{key}")
    return DispensedSlice(socket, cargo)


def with_channel_fields(view, **overrides):
    """A copy of a dispensed channel config with fields overridden -- the
    test-side mutation path now that the production view is frozen."""
    from raven.config.admission import DispensedSlice

    cargo = dict(object.__getattribute__(view, "_cargo"))
    section = object.__getattribute__(view, "_section")
    socket_keys = {"enabled", "allow_from", "workspace"}
    socket_over = {k: v for k, v in overrides.items() if k in socket_keys}
    if socket_over:
        section = section.model_copy(update=socket_over)
    cargo.update({k: v for k, v in overrides.items() if k not in socket_keys})
    return DispensedSlice(section, cargo)
