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

import random
import time

import pytest

import raven.security.redact as redact_module
from raven.security.redact import MAX_SCAN_CHARS, REPLACEMENT, pattern_names, redact, redact_value


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
        # Assembled rather than written out, and not for taste: a literal armour
        # line makes this file look like a leaked key to the repo's own
        # secret scanner, which fails the commit that adds the test for
        # redacting keys. The regex under test sees the same string either way.
        # Please do not "tidy" this back into one literal.
        head = "-----BEGIN RSA " + "PRIVATE KEY-----"
        tail = "-----END RSA " + "PRIVATE KEY-----"
        pem = f"{head}\nMIIEowIBAAKCAQEA\nabc123\n{tail}"

        result = redact(pem)

        assert "MIIEowIBAAKCAQEA" not in result
        assert head in result, "the reader needs to know what was there"

    @pytest.mark.parametrize("closed", [False, True], ids=["cut", "complete"])
    def test_a_private_key_keeps_no_body_when_its_closing_armour_is_missing(self, closed):
        head = "-----BEGIN " + "PRIVATE KEY-----"
        tail = "\n-----END " + "PRIVATE KEY-----" if closed else ""
        result = redact(f"{head}\nsynthetic-private-material{tail}")
        assert "synthetic-private-material" not in result
        assert result.startswith(head + REPLACEMENT)
        assert redact(result) == result

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
    @pytest.mark.parametrize(
        "text",
        ['"Authorization": "Bearer opaqu', "password=abcde", "--token abcde", "ghp_ABCD", "eyJabcdefghijk"],
    )
    def test_a_known_cut_removes_short_credential_tails(self, text):
        assert REPLACEMENT in redact(text, truncated=True)
        assert redact(text) == text

    def test_a_known_cut_keeps_ordinary_unlabelled_text(self):
        text = "ordinary diagnostic " + "x" * 1000
        assert redact(text, truncated=True) == text

    def test_a_jwt_is_matched_only_where_a_token_can_start(self):
        """The boundary the JWT pattern trades coverage for.

        ``eyJ`` is three characters a base64 body contains by chance, so an
        unbounded pattern spent its time at every one of them -- 21 seconds of
        CPU on a 256,000-character body of them. Matching only at a token
        boundary removes that, and costs the one shape where a real token is
        glued to a word: ``/verify-eyJ...`` is published whole. The loss stops
        there; a separator of any kind, or the start of the text, still matches.
        """
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"

        assert redact(f"/verify-{jwt}") == f"/verify-{jwt}"
        assert REPLACEMENT in redact(f"token={jwt}")
        assert REPLACEMENT in redact(f"Bearer {jwt}")
        assert REPLACEMENT in redact(jwt)

    def test_a_pem_header_without_its_end_keeps_nothing_after_it(self):
        """A preview that quotes the header as text loses what it showed below it.

        The trade-off the missing-END rule buys: a key whose closing armour a cut
        removed cannot be told from a header a person is talking about, and
        leaving the body in would publish the key. It reaches every surface this
        module scrubs, not only a failure record.
        """
        head = "-----BEGIN " + "PRIVATE KEY-----"
        preview = f'PEM_HEADER = "{head}"\ndef load(path):\n    return parse(open(path).read())\n'

        assert redact(preview) == f'PEM_HEADER = "{head}{REPLACEMENT}'
        closed = preview.replace(f'{head}"', f'{head}"\nbody\n-----END PRIVATE KEY-----')
        assert "def load" in redact(closed)

    @pytest.mark.production_timing
    @pytest.mark.parametrize(
        ("head", "unit", "stop", "size"),
        [
            ("", ".sk-", "!x", 64_000),
            ("", "pwd=", ",x", 32_000),
            ("", "--token=", "'x", 32_000),
            ("", " ", "x y", 32_000),
            ("Authorization:", " ", "abcde", 32_000),
        ],
        ids=["prefix-run", "named-secret-run", "cli-flag-run", "whitespace-gap", "label-gap"],
    )
    def test_a_cut_tail_is_found_in_time_linear_in_the_text(self, head, unit, stop, size):
        """A run of label-shaped text that something stops before the end, and no
        whitespace to split it: each tail pattern used to retry at every position
        of the run and walk on to the stop. A long gap before the last two words
        did the same to a search for the final run, and a label's own gap is read
        whole however wide it is."""
        text = head + unit * (size // len(unit)) + stop
        start = time.thread_time()
        redact(text, truncated=True)
        assert time.thread_time() - start < 0.5

    def test_each_tail_search_starts_where_the_whole_text_search_would_find_it(self):
        """The claim that keeps the cut-tail pass linear, checked against the search it replaced.

        A tail search starts behind the run of its value's characters that ends
        the text, the label's gap, scheme word and gap, and room for its name.
        That is safe only while no match can begin earlier, so the generated texts
        put every label, quote, separator, scheme and gap width in front of a cut.
        """

        def whole_text(text: str) -> str:
            for pattern, _value in redact_module._TRUNCATED_SECRETS:
                text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
            return text

        # One long value per pattern: a run longer than the label room, which a
        # value class narrower than the pattern's own would stop short of.
        tails = ["eyJ", "x.sk-", "pwd=", '"password": "', "Authorization: Bearer ", "--token ", "https://user:"]
        for tail in tails:
            for lead in ("", "x " * 300, "pwd=" * 100 + ","):
                text = lead + tail + "aZ9" * 140
                assert REPLACEMENT in whole_text(text), text
                assert redact_module._redact_cut_tail(text) == whole_text(text), text

        rng = random.Random(978)
        heads = ["password", "PWD", "api_key", "auth", "token", "Authorization", "Proxy-Authorization"]
        heads += ["--token", "--with-token", "https://user", "sk-", "ghp_", "eyJ", "AKIA", "xoxb-"]
        noise = ["sk-", ".sk-", "pwd=", "--token=", "://", ":", "@", "/", "!", ",", "'", '"', " ", "\n", "x", "Z9"]
        for _ in range(3000):
            text = "".join(rng.choice(noise) for _ in range(rng.randint(0, 12)))
            for part in (heads, ["", '"', "'"], None, ["", ":", "=", " "], None, ["", '"', "Bearer ", "basic  "]):
                text += " " * rng.choice([0, 0, 1, 2, 63, 64, 65, 300]) if part is None else rng.choice(part)
            text += "".join(rng.choice("aZ9._-~+/=:!,;@") for _ in range(rng.randint(0, 9)))
            text += "".join(rng.choice(noise) for _ in range(rng.choice([0, 0, 0, 1, 3])))
            assert redact_module._redact_cut_tail(text) == whole_text(text), text

    @pytest.mark.parametrize("gap", [64, 65, 10_000])
    def test_a_short_cut_value_is_a_tail_however_wide_its_label_gap(self, gap):
        """The search start walks back over a label's gap rather than assuming its
        width, so a short value a cut ended is removed behind a gap of any width."""
        spaces = " " * gap
        for text in (
            "Authorization:" + spaces + "abcde",
            '"password":' + spaces + '"abc',
            "Proxy-Authorization:" + spaces + "Basic" + spaces + "ab",
        ):
            result = redact(text, truncated=True)
            assert result.endswith(REPLACEMENT), text[:40]
            assert result.startswith(text[: text.index(spaces)])

    def test_a_huge_string_is_truncated_rather_than_scanned(self):
        """Titles and previews reach here, not file bodies; a half-scanned string
        is worse than an honestly shortened one."""
        result = redact("a" * (MAX_SCAN_CHARS + 100))

        assert len(result) < MAX_SCAN_CHARS + 100
        assert "truncated before scanning" in result

    def test_the_scrubbers_own_scan_cut_removes_a_partial_url_password(self):
        prefix = "x " * ((MAX_SCAN_CHARS - 40) // 2)
        text = prefix + "https://user:synthetic-password-" + "a" * 100 + "@host"
        result = redact(text)
        assert "synthetic-password" not in result
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
