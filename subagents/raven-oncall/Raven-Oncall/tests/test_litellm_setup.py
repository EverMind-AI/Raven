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
