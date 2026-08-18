"""Credential redaction for log records.

The regression these pin: a gateway run wrote 15 working Telegram bot tokens
into its rotating log file, because httpx logs the request line at INFO and the
Bot API puts the token in the URL path.
"""

from __future__ import annotations

import pytest

from raven.utils.log_redaction import REDACTED, combine_filters, redact, redacting_filter

TELEGRAM_LINE = 'HTTP Request: POST https://api.telegram.org/bot8642349359:AAFUU84G42yL-ONi3gk_RtdEprx__Mup0c/getMe "HTTP/1.1 200 OK"'


def test_telegram_token_is_masked_but_the_bot_id_survives():
    """The numeric id identifies which bot for debugging; only the secret half
    is a credential."""
    out = redact(TELEGRAM_LINE)
    assert "AAFUU84G42yL-ONi3gk_RtdEprx__Mup0c" not in out
    assert "8642349359" in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "line, secret",
    [
        ("GET https://x.googleapis.com/v1/models?key=AIzaSyC7xR2mQ1abcdef", "AIzaSyC7xR2mQ1abcdef"),
        ("POST https://h/v1?api_key=abcdef123456&z=1", "abcdef123456"),
        ("POST https://h/v1?access_token=tok_abcdef123456", "tok_abcdef123456"),
        ("connecting to https://user:hunter2pass@example.com/x", "hunter2pass"),
        ("Authorization: Bearer sk-or-v1-722b6e7d2ac1bc73", "sk-or-v1-722b6e7d2ac1bc73"),
        ("using key sk-proj-AbCdEf0123456789 now", "sk-proj-AbCdEf0123456789"),
        ("slack token xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ],
)
def test_credential_shapes_are_masked(line, secret):
    assert secret not in redact(line)


@pytest.mark.parametrize(
    "line",
    [
        "HTTP Request: GET https://api.telegram.org/health 200 OK",
        "Cron: executing job 'regr' (5a3bb214)",
        "Uvicorn running on http://localhost:18791",
        "loaded 5 skills from https://skillhub.evermind.ai/openapi/v1/skills?limit=20",
        "path is /Users/admin/.raven/logs/gateway.log",
    ],
)
def test_ordinary_lines_are_left_alone(line):
    """Over-redaction is its own failure: a log that masks ports, paths and
    plain URLs stops being usable for debugging."""
    assert redact(line) == line


def test_filter_rewrites_the_record_and_keeps_it():
    record = {"message": TELEGRAM_LINE}
    assert redacting_filter(record) is True
    assert "AAFUU84G42yL-ONi3gk_RtdEprx__Mup0c" not in record["message"]


def test_filter_tolerates_a_non_string_message():
    record = {"message": None}
    assert redacting_filter(record) is True


def test_combine_filters_semantics():
    assert combine_filters(None, None) is None
    assert combine_filters(redacting_filter, None) is redacting_filter

    combined = combine_filters(redacting_filter, lambda r: "drop" not in r["message"])
    kept = {"message": TELEGRAM_LINE}
    assert combined(kept) is True
    assert REDACTED in kept["message"], "redaction runs even when composed"
    assert combined({"message": "please drop this"}) is False


def test_gateway_log_file_never_receives_a_live_token(tmp_path, monkeypatch):
    """End-to-end through the real sink wiring: what lands on disk is what a
    user attaches to a bug report."""
    from loguru import logger

    import raven.cli._log_file as log_file

    monkeypatch.setattr(log_file, "get_logs_dir", lambda: tmp_path)
    try:
        path = log_file.redirect_loguru_to_file("probe.log", terminal_level=None)
        logger.info(TELEGRAM_LINE)
        logger.complete()
        written = path.read_text()
    finally:
        logger.remove()

    assert "AAFUU84G42yL-ONi3gk_RtdEprx__Mup0c" not in written
    assert REDACTED in written


def test_the_debug_stderr_sink_redacts_too(tmp_path, monkeypatch, capsys):
    """RAVEN_CLI_DEBUG adds a second sink. Debugging a channel is exactly when
    the request line gets read aloud, so that sink needs the same filter."""
    from loguru import logger

    import raven.cli._log_file as log_file

    monkeypatch.setattr(log_file, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setenv("RAVEN_CLI_DEBUG", "1")
    try:
        log_file.redirect_loguru_to_file("probe.log", terminal_level=None)
        logger.info(TELEGRAM_LINE)
        logger.complete()
    finally:
        logger.remove()

    err = capsys.readouterr().err
    assert "AAFUU84G42yL-ONi3gk_RtdEprx__Mup0c" not in err
    assert REDACTED in err
