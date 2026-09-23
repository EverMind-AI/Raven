"""Unit tests for ``import_litellm`` -- banner, level restore, TTY detach."""

import io
import logging
import os
import sys
import threading

import pytest

from raven.providers import litellm_setup
from raven.providers.litellm_setup import import_litellm

_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


@pytest.fixture(autouse=True)
def _first_import_again(monkeypatch):
    """Every case here is about what the first import in a process does, and
    litellm is long imported by the time the suite reaches this file. Forgetting
    the finished pass makes the body run for the case, as it did before the
    pass was remembered."""
    monkeypatch.setattr(litellm_setup, "_READY", None)


def _tty_handlers(name: str) -> list[logging.Handler]:
    logger = logging.getLogger(name)
    return [h for h in logger.handlers if getattr(h, "stream", None) in (sys.stderr, sys.stdout)]


def test_import_litellm_disables_banner() -> None:
    module = import_litellm()

    assert module.suppress_debug_info is True


def test_import_litellm_restores_logger_levels() -> None:
    """The import-time level bump must not persist, or runtime DEBUG would stop
    propagating to the file sink."""
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    import_litellm()

    for name in _LITELLM_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG


def test_import_litellm_is_idempotent() -> None:
    first = import_litellm()
    second = import_litellm()

    assert first is second


def test_a_call_after_a_finished_import_runs_none_of_the_setup(monkeypatch) -> None:
    """The window resolver reaches here once per model, so the pass is paid once
    per process, not once per call: re-running it per model put a third of a
    1095-model catalogue's time into this function."""
    import_litellm()
    entered: list[str] = []
    monkeypatch.setattr(litellm_setup, "_point_oauth_tokens_at_raven", lambda: entered.append("setup"))

    for _ in range(3):
        import_litellm()

    assert entered == []


def test_import_litellm_points_copilot_tokens_at_raven(
    tmp_path,
    monkeypatch,
) -> None:
    """LiteLLM's authenticator reads this in ``__init__`` and creates the
    directory, so it has to be set before litellm is imported at all."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("GITHUB_COPILOT_TOKEN_DIR", raising=False)

    import_litellm()

    assert os.environ["GITHUB_COPILOT_TOKEN_DIR"] == str(tmp_path / ".raven" / "oauth" / "github_copilot")


def test_import_litellm_keeps_an_explicit_copilot_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_COPILOT_TOKEN_DIR", str(tmp_path / "mine"))

    import_litellm()

    assert os.environ["GITHUB_COPILOT_TOKEN_DIR"] == str(tmp_path / "mine")


def test_import_litellm_pins_the_model_cost_map_to_the_installed_wheel(monkeypatch) -> None:
    """litellm reads this while fetching the catalogue in ``__init__``, so it
    has to be published before litellm is imported at all -- and left unset it
    puts an HTTP round trip on every raven startup path."""
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)

    import_litellm()

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_import_litellm_keeps_an_explicit_model_cost_map_setting(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")

    import_litellm()

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "False"


def test_import_litellm_detaches_terminal_handlers() -> None:
    """Every litellm import in raven is deferred, so the handler litellm installs
    lands after the CLI has already stripped terminal handlers. Left attached, it
    writes every record -- DEBUG included, since the stdlib intercept sets the
    root level to 0 -- over whatever is on screen."""
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).addHandler(logging.StreamHandler(sys.stderr))

    import_litellm()

    for name in _LITELLM_LOGGERS:
        assert _tty_handlers(name) == []


def test_import_litellm_keeps_non_terminal_handlers() -> None:
    """Only the terminal is off limits: a file or buffer sink still gets records."""
    sink = logging.StreamHandler(io.StringIO())
    logging.getLogger("LiteLLM").addHandler(sink)
    try:
        import_litellm()

        assert sink in logging.getLogger("LiteLLM").handlers
    finally:
        logging.getLogger("LiteLLM").removeHandler(sink)


# --- Rows raven vouches for itself --------------------------------------------------
#
# 2026-09-11: DeepSeek's own id for V4.1-Flash, ``deepseek/deepseek-flash``, was
# in no LiteLLM table, so the window resolver fell back to 65,536 for a
# 1,048,576-token model and the cost recorder wrote 0. The row is registered
# into the live table at import, where the window, the output ceiling and the
# price are all read from.


def test_import_litellm_registers_the_deepseek_flash_row() -> None:
    litellm = import_litellm()

    row = litellm.model_cost["deepseek/deepseek-flash"]
    assert row["max_input_tokens"] == 1_048_576
    assert row["max_output_tokens"] == 384_000
    assert row["litellm_provider"] == "deepseek"
    # Peak-hour rates per token; off-peak is half, which a flat table cannot say.
    assert row["input_cost_per_token"] == 0.30 / 1_000_000
    assert row["output_cost_per_token"] == 1.20 / 1_000_000


def test_the_registered_row_reaches_the_window_and_ceiling_resolvers() -> None:
    from raven.providers import rates

    import_litellm()

    assert rates.resolve_context_window("deepseek/deepseek-flash") == 1_048_576
    assert rates.declared_max_output_tokens("deepseek/deepseek-flash") == 384_000
    # The row's ceiling is where the resolver starts, not what it returns: a
    # declaration this far inside the window still outruns any one reply, so
    # what a request carries is the per-iteration bound.
    assert rates.resolve_max_output_tokens("deepseek/deepseek-flash") == rates.MAX_OUTPUT_TOKENS_PER_ITERATION


def test_a_row_the_installed_catalogue_already_has_is_left_alone(monkeypatch) -> None:
    """A newer LiteLLM that learned the id wins over raven's copy."""
    from raven.providers import litellm_setup

    litellm = import_litellm()
    theirs = {"max_input_tokens": 1, "litellm_provider": "deepseek", "mode": "chat"}
    monkeypatch.setitem(litellm.model_cost, "deepseek/deepseek-flash", theirs)

    litellm_setup._register_raven_model_rows(litellm)

    assert litellm.model_cost["deepseek/deepseek-flash"] is theirs


def test_warm_up_in_background_imports_on_a_daemon_thread(monkeypatch) -> None:
    """The boot paths call this so the first request that needs litellm does
    not pay the import; the work happens off the calling thread."""
    from raven.providers import litellm_setup

    seen: list[str] = []
    monkeypatch.setattr(litellm_setup, "import_litellm", lambda: seen.append(threading.current_thread().name))

    thread = litellm_setup.warm_up_in_background()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert thread.daemon
    assert seen == ["litellm-warmup"]


def test_warm_up_in_background_swallows_an_import_failure(monkeypatch, caplog) -> None:
    """A broken install must not take the boot thread down with it; the
    on-demand import is what reports the failure to the caller that needs it."""
    from raven.providers import litellm_setup

    def broken() -> None:
        raise ModuleNotFoundError("No module named 'litellm'")

    monkeypatch.setattr(litellm_setup, "import_litellm", broken)

    with caplog.at_level(logging.ERROR, logger="raven.providers.litellm_setup"):
        thread = litellm_setup.warm_up_in_background()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert "warm-up failed" in caplog.text


def test_a_second_caller_during_the_import_does_not_leave_the_levels_raised(monkeypatch) -> None:
    """The levels are raised for the duration of the import and put back after.

    That only holds one thread at a time: a caller arriving inside the window
    reads WARNING as the level to restore and restores it after the first caller
    has put the real one back, and litellm's DEBUG and INFO stop reaching the
    file sink until the process restarts. A server warms the import in the
    background while it serves, so the window is an ordinary one.
    """
    from raven.providers import litellm_setup

    loggers = [logging.getLogger(name) for name in _LITELLM_LOGGERS]
    for logger in loggers:
        logger.setLevel(logging.INFO)

    inside = threading.Event()
    finish = threading.Event()
    register = litellm_setup._register_raven_model_rows

    def hold(module):
        inside.set()
        finish.wait(10)
        register(module)

    monkeypatch.setattr(litellm_setup, "_register_raven_model_rows", hold)

    first = threading.Thread(target=import_litellm, name="first")
    first.start()
    assert inside.wait(10), "the first import never reached the seam"
    second = threading.Thread(target=import_litellm, name="second")
    second.start()
    second.join(0.2)  # long enough to read a level, were it allowed to
    finish.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert [logger.level for logger in loggers] == [logging.INFO] * 3
