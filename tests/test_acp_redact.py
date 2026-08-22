"""The redaction table: what it catches, what it leaves alone, and what it cannot.

An ACP payload is rendered in an editor and often kept in its transcript, so a
tool title, a permission prompt and an error message are all publishing surfaces.
Three claims are pinned here, and the third is the one most worth a test:

1. **It catches the four measured channels.** A command line with a header token,
   a result preview containing a key, an error string, an ``env`` dict.
2. **It leaves ordinary text alone.** A table that redacts ``set -e`` or a plain
   ``git clone`` URL makes every tool row unreadable, and an unreadable row is
   approved without being read -- which is worse than not redacting at all.
3. **It is not a scanner, and the gap has a name.** Only the capture group is
   replaced, so the shape survives. And a secret with no label and no vendor
   prefix passes through, which is stated as a test rather than left for somebody
   to discover.
"""

from __future__ import annotations

import pytest

from raven.acp.redact import MAX_SCAN_CHARS, REPLACEMENT, pattern_names, redact, redact_value


class TestWhatItCatches:
    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ('curl -H "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD" https://x', "sk-ant-api03"),
            ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnop", "sk-proj-abcdefghijklmnop"),
            ("gh auth login --with-token ghp_ABCDEFGHIJKLMNOPQRSTUV", "ghp_ABCDEFGHIJKLMNOPQRSTUV"),
            ("glab auth login --token glpat-abcdefghijklmnopqrst", "glpat-abcdefghijklmnopqrst"),
            ('api_key: "AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI"', "AIzaSy"),
            ("aws sts get-caller-identity  # AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            ("psql postgres://admin:hunter2secret@db:5432/app", "hunter2secret"),
            ("SLACK_BOT=xoxb-1234567890-abcdefghij", "xoxb-1234567890"),
            ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", "eyJhbGciOi"),
            ('{"password": "correct-horse-battery"}', "correct-horse-battery"),
        ],
    )
    def test_the_secret_is_gone_and_the_shape_remains(self, text, secret):
        result = redact(text)

        assert secret not in result
        assert REPLACEMENT in result
        # Everything before the secret survives, which is what keeps the row
        # readable: `curl -H "Authorization: Bearer [redacted]"` still says what
        # was about to run.
        prefix = text[: text.index(secret)]
        assert prefix in result, "redacting the label as well leaves a row nobody can act on"

    def test_a_private_key_body_goes_but_the_armour_stays(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc123\n-----END RSA PRIVATE KEY-----"

        result = redact(pem)

        assert "MIIEowIBAAKCAQEA" not in result
        assert "BEGIN RSA PRIVATE KEY" in result, "the reader needs to know what was there"

    def test_a_key_that_names_a_secret_redacts_its_value(self):
        """The ``mcpServers`` ``env`` channel. A per-string pass cannot do this:
        on its own, the value is indistinguishable from a hash -- the label is in
        a different string."""
        result = redact_value({"env": {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI-K7MDENG-bPxRfiCY"}})

        assert result["env"]["AWS_SECRET_ACCESS_KEY"] == REPLACEMENT
        assert "AWS_SECRET_ACCESS_KEY" in result["env"], "the key stays; the reader needs to know what was set"

    def test_a_secret_named_key_holding_a_structure_is_still_walked(self):
        """Replacing a whole subtree would lose the shape the client asked for,
        and a nested dict has its own keys to judge."""
        result = redact_value({"credentials": {"user": "alice", "password": "hunter2secret"}})

        assert result["credentials"]["user"] == "alice"
        assert result["credentials"]["password"] == REPLACEMENT

    def test_it_reaches_inside_a_structure_without_flattening_it(self):
        payload = {
            "command": "deploy",
            "env": {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"},
            "args": ["--token", "ghp_AAAAAAAAAAAAAAAAAAAA"],
            "retries": 3,
            "ok": True,
        }

        result = redact_value(payload)

        assert result["retries"] == 3, "shape and non-strings are untouched"
        assert result["ok"] is True
        assert "wJalrXUtnFEMI" not in str(result)
        assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in str(result)
        assert result["command"] == "deploy"

    def test_a_deeply_nested_value_is_bounded_rather_than_half_scanned(self):
        payload: object = "sk-proj-abcdefghijklmnop"
        for _ in range(30):
            payload = {"next": payload}

        result = redact_value(payload)

        assert "sk-proj" not in str(result)
        assert "too deeply nested" in str(result)


class TestWhatItLeavesAlone:
    @pytest.mark.parametrize(
        "text",
        [
            "set -e && npm test",
            "git clone https://github.com/user/repo",
            "token: 12345",
            "the password is wrong",
            "authorization: read",
            "make -j8 build",
            "grep -rn 'secret' docs/",
            "ssh-keygen -t ed25519 -C me@example.com",
            "SELECT id, password_hash FROM users LIMIT 10",
        ],
    )
    def test_ordinary_text_is_unchanged(self, text):
        """A table that redacts these makes every tool row unreadable -- and an
        unreadable row is approved without being read."""
        assert redact(text) == text

    def test_it_is_idempotent(self):
        once = redact("export API_KEY=sk-proj-abcdefghijklmnop")

        assert redact(once) == once

    def test_empty_text_is_returned_as_it_came(self):
        assert redact("") == ""
        assert redact_value(None) is None
        assert redact_value(7) == 7


class TestTheLimits:
    def test_a_huge_string_is_truncated_rather_than_scanned(self):
        """Titles and previews reach here, not file bodies; a half-scanned string
        is worse than an honestly shortened one."""
        result = redact("a" * (MAX_SCAN_CHARS + 100))

        assert len(result) < MAX_SCAN_CHARS + 100
        assert "truncated before scanning" in result

    def test_an_unlabelled_secret_passes_through(self):
        """Stated as a test rather than left to be discovered. This is not a
        secret scanner: a high-entropy string with no label and no vendor prefix
        is indistinguishable from a hash, a build id or a commit sha -- and
        redacting those would break every row that legitimately shows one."""
        opaque = "Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0"

        assert redact(f"echo {opaque}") == f"echo {opaque}"

    def test_the_table_stays_small_on_purpose(self):
        """Sized like the sixteen-pattern table openclaw uses for this job, and
        deliberately not the 409-line RFC-7235 header scanner beside it."""
        names = pattern_names()

        assert 10 <= len(names) <= 20
        assert len(set(names)) == len(names)

    def test_no_pattern_matches_the_replacement_token(self):
        """What makes it idempotent, and what stops a second pass from eating the
        token itself."""
        assert redact(REPLACEMENT) == REPLACEMENT
        assert redact(f"api_key={REPLACEMENT}") == f"api_key={REPLACEMENT}"
