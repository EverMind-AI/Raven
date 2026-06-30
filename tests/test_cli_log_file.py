"""Unit tests for the shared loguru→file redirect helper.

Covers parameterization (filename / file_level / terminal_level / RAVEN_CLI_DEBUG)
and the stdlib-logging interception. The third-party TTY-handler stripping
has its own real-litellm regression guard in
``test_cli_tui_logging_isolation.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from raven.cli._log_file import redirect_loguru_to_file
from raven.config.loader import set_config_path


@pytest.fixture
def tmp_logs(tmp_path: Path, monkeypatch):
    """Point get_logs_dir() at a tmp instance + restore global log state.

    redirect_loguru_to_file() mutates process-global logging: it installs a
    root InterceptHandler and strips stderr StreamHandlers off every named
    logger. Snapshot root + all named-logger handlers so this fixture does not
    leak that mutation into sibling tests (notably the litellm-based
    regression guard).
    """
    set_config_path(tmp_path / "config.json")
    monkeypatch.delenv("RAVEN_CLI_DEBUG", raising=False)

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_level = root.level
    manager = logging.root.manager
    saved_named = {name: list(lg.handlers) for name, lg in manager.loggerDict.items() if isinstance(lg, logging.Logger)}
    try:
        yield tmp_path / "logs"
    finally:
        from loguru import logger

        logger.remove()
        root.handlers = saved_handlers
        root.filters = saved_filters
        root.level = saved_level
        for name, handlers in saved_named.items():
            lg = manager.loggerDict.get(name)
            if isinstance(lg, logging.Logger):
                lg.handlers = handlers
        set_config_path(None)  # type: ignore[arg-type]


def _flush_and_read(path: Path) -> str:
    """Remove sinks (joins the enqueued file thread → flush) then read."""
    from loguru import logger

    logger.remove()
    return path.read_text() if path.exists() else ""


def test_writes_to_named_file_under_logs_dir(tmp_logs: Path) -> None:
    from loguru import logger

    log_path = redirect_loguru_to_file("gateway.log")
    assert log_path == tmp_logs / "gateway.log"

    logger.info("hello-gateway-marker")
    assert "hello-gateway-marker" in _flush_and_read(log_path)


def test_terminal_warning_keeps_warnings_drops_info(tmp_logs: Path, capsys) -> None:
    from loguru import logger

    redirect_loguru_to_file("gateway.log", terminal_level="WARNING")
    logger.info("info-not-on-terminal")
    logger.warning("warn-on-terminal")

    err = capsys.readouterr().err
    assert "warn-on-terminal" in err
    assert "info-not-on-terminal" not in err


def test_terminal_none_silences_stderr(tmp_logs: Path, capsys) -> None:
    from loguru import logger

    redirect_loguru_to_file("tui.log", terminal_level=None)
    logger.warning("should-not-appear-on-terminal")
    assert "should-not-appear-on-terminal" not in capsys.readouterr().err


def test_record_filter_drops_matching_records(tmp_logs: Path) -> None:
    """record_filter lets a caller drop sink-specific noise (the TUI
    suppresses watchfiles spam) without it reaching the file."""
    from loguru import logger

    def _drop_spam(record: dict) -> bool:
        return "rust notify timeout" not in record["message"]

    log_path = redirect_loguru_to_file("tui.log", record_filter=_drop_spam)
    logger.info("rust notify timeout, continuing")
    logger.info("real-turn-activity-marker")

    text = _flush_and_read(log_path)
    assert "real-turn-activity-marker" in text
    assert "rust notify timeout" not in text


def test_record_filter_none_keeps_all(tmp_logs: Path) -> None:
    """Default (gateway) path: no filter → every record kept."""
    from loguru import logger

    log_path = redirect_loguru_to_file("gateway.log")
    logger.info("rust notify timeout, continuing")
    assert "rust notify timeout" in _flush_and_read(log_path)


def test_env_debug_mirrors_to_stderr(tmp_logs: Path, capsys, monkeypatch) -> None:
    from loguru import logger

    monkeypatch.setenv("RAVEN_CLI_DEBUG", "1")
    redirect_loguru_to_file("gateway.log", terminal_level=None)
    logger.debug("debug-mirror-marker")
    assert "debug-mirror-marker" in capsys.readouterr().err


def test_file_level_filters_below_threshold(tmp_logs: Path) -> None:
    from loguru import logger

    log_path = redirect_loguru_to_file("gateway.log", file_level="WARNING")
    logger.info("info-below-threshold")
    logger.warning("warning-at-threshold")

    content = _flush_and_read(log_path)
    assert "warning-at-threshold" in content
    assert "info-below-threshold" not in content


def test_stdlib_logging_intercepted_to_file(tmp_logs: Path) -> None:
    log_path = redirect_loguru_to_file("gateway.log", file_level="DEBUG")
    logging.getLogger("some.third.party").warning("stdlib-intercepted-marker")
    assert "stdlib-intercepted-marker" in _flush_and_read(log_path)


def test_file_sink_does_not_dump_local_variable_values(tmp_logs: Path) -> None:
    """diagnose=False: a secret bound in the failing frame must not be written
    into the persisted file via loguru's variable-annotated traceback."""
    from loguru import logger

    log_path = redirect_loguru_to_file("gateway.log", file_level="DEBUG")
    api_token = "SECRET-TOKEN-9z9z9z"
    try:
        # api_token is referenced on the raising line, so diagnose=True WOULD
        # annotate it with its value; the exception message itself is "boom".
        raise RuntimeError(api_token[:0] or "boom")
    except RuntimeError:
        logger.exception("processing failed")

    content = _flush_and_read(log_path)
    assert "processing failed" in content
    assert "SECRET-TOKEN-9z9z9z" not in content
