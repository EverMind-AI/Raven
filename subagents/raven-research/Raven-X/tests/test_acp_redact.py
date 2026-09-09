"""Credential redaction on the publishing surface."""

import pytest

from raven.acp.redact import REPLACEMENT, pattern_names, redact, redact_value


@pytest.mark.parametrize(
    ("text", "survives"),
    [
        ('curl -H "Authorization: Bearer abcdef123456789"', "Authorization: Bearer"),
        ("api_key=sk-abcdefghijklmnop1234", "api_key="),
        ("--token deadbeef123456 push", "--token "),
        ("https://user:hunter2secret@host/path", "://user:"),
        ("ghp_ABCDEFGHIJKLMNOP123456", "ghp_"[:0]),
        ("export MY_API_TOKEN=abc123def", "export MY_API_TOKEN="),
    ],
)
def test_redact_replaces_the_secret_and_keeps_the_shape(text, survives):
    out = redact(text)
    assert REPLACEMENT in out
    if survives:
        assert survives in out


def test_redact_is_idempotent():
    once = redact("api_key=sk-abcdefghijklmnop1234")
    assert redact(once) == once


def test_redact_leaves_plain_text_alone():
    text = "read_file: /home/user/notes.md"
    assert redact(text) == text


def test_redact_value_redacts_by_key_name():
    value = {"env": {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI"}, "path": "/tmp/x"}
    out = redact_value(value)
    assert out["env"]["AWS_SECRET_ACCESS_KEY"] == REPLACEMENT
    assert out["path"] == "/tmp/x"


def test_redact_value_bounds_depth():
    deep: dict = {"k": "v"}
    for _ in range(20):
        deep = {"nest": deep}
    out = redact_value(deep)
    text = str(out)
    assert "too deeply nested" in text


def test_pattern_table_is_pinned():
    # A pattern added or removed is a deliberate decision; keep in step with the
    # main repo's table when syncing (raven/acp/redact.py there).
    assert pattern_names() == (
        "http-auth",
        "named-secret",
        "cli-flag",
        "url-userinfo",
        "openai",
        "github",
        "gitlab",
        "slack",
        "google",
        "aws-access-key",
        "anthropic-legacy",
        "jwt",
        "pem",
        "env-export",
    )
