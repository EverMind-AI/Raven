"""Unit tests for the shared loguru→file redirect helper.

Covers parameterization (filename / file_level / terminal_level / RAVEN_CLI_DEBUG)
and the stdlib-logging interception. The third-party TTY-handler stripping
has its own real-litellm regression guard in
``test_cli_tui_logging_isolation.py``.
"""

from __future__ import annotations

import logging
import logging.handlers
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


# ---------------------------------------------------------------------------
# Root-logger TTY StreamHandler stripping
# ---------------------------------------------------------------------------


def test_strip_tty_stream_handlers_removes_root_stdout_handler(tmp_logs: Path) -> None:
    """_strip_tty_stream_handlers() must remove a StreamHandler(sys.stdout)
    installed on the ROOT logger (not just on named loggers)."""
    import sys

    from raven.cli._log_file import _strip_tty_stream_handlers

    root = logging.getLogger()
    stdout_handler = logging.StreamHandler(sys.stdout)
    root.addHandler(stdout_handler)

    try:
        _strip_tty_stream_handlers()
        assert stdout_handler not in root.handlers, "_strip_tty_stream_handlers must remove root stdout StreamHandler"
    finally:
        root.removeHandler(stdout_handler)


def test_strip_tty_stream_handlers_keeps_root_non_tty_handler(tmp_logs: Path) -> None:
    """_strip_tty_stream_handlers() must NOT remove a non-TTY handler (e.g. a
    MemoryHandler) from the ROOT logger."""
    from raven.cli._log_file import _strip_tty_stream_handlers

    root = logging.getLogger()
    mem_handler = logging.handlers.MemoryHandler(capacity=10)
    root.addHandler(mem_handler)

    try:
        _strip_tty_stream_handlers()
        assert mem_handler in root.handlers, "_strip_tty_stream_handlers must not remove non-TTY root handlers"
    finally:
        root.removeHandler(mem_handler)


# ---------------------------------------------------------------------------
# redirect_terminal_fds_to_file — fd-level stdout/stderr capture
# ---------------------------------------------------------------------------


def test_redirect_terminal_fds_captures_print_and_raw_fd_write(tmp_path, capfd) -> None:
    """Inside redirect_terminal_fds_to_file, both print() (flushed to the real fd)
    and os.write(1, ...) must land in the target file, not the terminal.

    This proves the context manager captures the structlog PrintLogger path
    (which does print(message, file=None) writing to the live fd 1) AND raw fd writes.
    We run under capfd.disabled() so pytest is not fighting for the fds during
    the redirect — the fd-level dup2 takes exclusive effect.
    """
    import os

    from raven.cli._log_file import redirect_terminal_fds_to_file

    target = tmp_path / "capture.log"

    with capfd.disabled():
        with redirect_terminal_fds_to_file(target):
            # Real print() through sys.stdout — this is the structlog PrintLogger path.
            print("print-leak-line", flush=True)
            # Raw fd write — exercises the os.write path directly.
            os.write(1, b"raw-fd-write\n")

    content = target.read_bytes()
    assert b"print-leak-line" in content, "print() output must land in the redirect file (structlog PrintLogger path)"
    assert b"raw-fd-write" in content, "os.write(1, ...) during redirect must land in the file"


def test_redirect_terminal_fds_restores_fd1_after_exit(tmp_path, capfd) -> None:
    """After the context manager exits, fd1 must be restored to the original
    target (e.g. the original stdout) — writes after exit must NOT go to the
    redirect file."""
    import os

    from raven.cli._log_file import redirect_terminal_fds_to_file

    target = tmp_path / "capture.log"
    marker_after = b"marker-after-restore\n"

    with capfd.disabled():
        with redirect_terminal_fds_to_file(target):
            pass
        # After exit: write a marker — it should NOT appear in the file.
        os.write(1, marker_after)

    content = target.read_bytes() if target.exists() else b""
    assert marker_after not in content, "writes after CM exit must not go to the redirect file"


def test_redirect_terminal_fds_restores_on_exception(tmp_path, capfd) -> None:
    """fd1/fd2 must be restored even when an exception is raised inside the CM."""
    import os

    from raven.cli._log_file import redirect_terminal_fds_to_file

    target = tmp_path / "capture.log"
    marker_after = b"marker-after-exception\n"

    with capfd.disabled():
        try:
            with redirect_terminal_fds_to_file(target):
                raise RuntimeError("boom inside CM")
        except RuntimeError:
            pass
        os.write(1, marker_after)

    content = target.read_bytes() if target.exists() else b""
    assert marker_after not in content, "fd1 must be restored on exception so writes land back on real stdout"
