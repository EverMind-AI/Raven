"""Unit tests for ``import_litellm`` -- banner, level restore, TTY detach."""

import io
import logging
import os
import sys

from raven.providers.litellm_setup import import_litellm

_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


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
