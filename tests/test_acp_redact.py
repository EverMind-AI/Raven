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
   replaced, so the shape survives. And a secret with no label, no vendor prefix
   this table matches and no bearer scheme passes through, which is stated as a
   test rather than left for somebody to discover.

The table is Codex's four rules plus one structural rule for PEM armour. Several
vendor classes this repository does use (GitHub, GitLab, Slack, Google) are
deliberately outside it; ``test_channels_outside_the_table_pass_through`` records
what that costs, so the loss is visible rather than discovered in a transcript.
The fifth rule is structural rather than a vendor enumeration, and
``test_the_table_is_the_codex_rules_plus_pem`` pins the count at exactly five.
"""

from __future__ import annotations

import random
import time

import pytest

import raven.security.redact as redact_module
from raven.security.redact import MAX_SCAN_CHARS, REPLACEMENT, pattern_names, redact, redact_value

#: PEM fixtures are built from a label constant, never written out. The repo's
#: ``detect-private-key`` hook rejects any *added* line containing a literal
#: header, and this file's older fixtures only pass because they predate the
#: rule; splitting the header means no line carries both halves, and the strings
#: under test are unchanged. Please do not "tidy" these into one literal.
_PRIVATE_KEY_LABEL = "PRIVATE KEY"
_PEM_OPEN = f"-----BEGIN {_PRIVATE_KEY_LABEL}-----"
_PEM_CLOSE = f"-----END {_PRIVATE_KEY_LABEL}-----"
PEM_WITH_SYNTHETIC_BODY = f"{_PEM_OPEN}\nMIIEowIBAAKCAQEA\n{_PEM_CLOSE}"


class TestWhatItCatches:
    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ('curl -H "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD" https://x', "sk-ant-api03"),
            ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnop", "sk-proj-abcdefghijklmnop"),
            ("Authorization: Bearer abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
            ("aws sts get-caller-identity  # AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            ("aws sts get-caller-identity  # ASIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE"),
            ('{"password": "correct-horse-battery"}', "correct-horse-battery"),
            ("access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
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

    def test_an_aws_credentials_file_loses_both_values(self):
        """The channel that does not depend on the user making a mistake.

        ``read_file ~/.aws/credentials`` is an ordinary turn (the default
        ``restrict_to_workspace`` is false), and its result preview is the
        file's body. The key id is caught by its vendor prefix; the secret is
        caught by the ``access_key`` label at the tail of
        ``aws_secret_access_key``. Both must go, because neither pattern alone
        covers the file -- and a literal copy of Codex's assignment rule, whose
        leading ``\\b`` cannot match after the ``_`` in that name, catches
        neither the secret nor the key id.
        """
        text = (
            "[default]\n"
            "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
            "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        )

        result = redact(text)

        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "wJalrXUtnFEMI" not in result
        assert result == "[default]\naws_access_key_id = [redacted]\naws_secret_access_key = [redacted]\n"

    def test_the_provider_prefixed_openai_forms_go_whole(self):
        """``sk-ant-`` and ``sk-proj-`` are a hyphen in the value.

        A value class copied from Codex (``[A-Za-z0-9]``) stops at the first
        hyphen and matches nothing, so both of these would publish whole. Pinned
        as a pair with the plain form, which any value class reaches.
        """
        for text in (
            "sk-ant-api03-AAAABBBBCCCCDDDD",
            "sk-proj-abcdefghijklmnop",
            "sk-abcdefghijklmnop",
        ):
            assert redact(text) == REPLACEMENT, text

    def test_a_private_key_body_goes_but_the_armour_stays(self):
        """The fifth rule, which Codex does not carry.

        Only the body is replaced, so the reader still learns a key was there.
        ``RSA`` in the header is pinned too: the rule matches any header label
        ending in the key words, not just the bare form, because the per-format
        variants are one delimiter rather than one rule each.
        """
        head = f"-----BEGIN RSA {_PRIVATE_KEY_LABEL}-----"
        pem = f"{head}\nMIIEowIBAAKCAQEA\nabc123\n{_PEM_CLOSE}"

        result = redact(pem)

        assert "MIIEowIBAAKCAQEA" not in result
        assert head in result, "the reader needs to know what was there"
        assert _PEM_CLOSE in result

    @pytest.mark.parametrize("closed", [False, True], ids=["cut", "complete"])
    def test_a_private_key_keeps_no_body_when_its_closing_armour_is_missing(self, closed):
        """The ``\\Z`` arm of the rule's lookahead, which is what the cut case needs.

        A body a cap ended has no ``-----END``, and that half key is exactly what
        ``agent/loop/failure_text.py`` persists. Without ``\\Z`` the body would be
        published whole.
        """
        tail = f"\n{_PEM_CLOSE}" if closed else ""
        result = redact(f"{_PEM_OPEN}\nsynthetic-private-material{tail}")

        assert "synthetic-private-material" not in result
        assert result.startswith(_PEM_OPEN + REPLACEMENT)
        assert redact(result) == result

    def test_a_pem_header_without_its_end_keeps_nothing_after_it(self):
        """A preview that quotes the header as text loses what it showed below it.

        The trade-off the missing-END rule buys: a key whose closing armour a cut
        removed cannot be told from a header a person is talking about, and
        leaving the body in would publish the key. It reaches every surface this
        module scrubs, not only a failure record.
        """
        preview = f'PEM_HEADER = "{_PEM_OPEN}"\ndef load(path):\n    return parse(open(path).read())\n'

        assert redact(preview) == f'PEM_HEADER = "{_PEM_OPEN}{REPLACEMENT}'
        closed = preview.replace(f'{_PEM_OPEN}"', f'{_PEM_OPEN}"\nbody\n{_PEM_CLOSE}')
        assert "def load" in redact(closed)

    def test_one_bearer_rule_covers_the_header_and_the_bare_form(self):
        """Codex matches ``Bearer`` anywhere rather than behind a header label.

        That is what lets one rule replace the old header rule and the old bare
        rule: a ``-H "Authorization: Bearer ..."`` argument and a token quoted on
        its own both lose the value, and the scheme word stays so the row still
        says what happened.
        """
        for text in (
            "Authorization: Bearer abcdefghijklmnopqrst",
            'curl -H "Authorization: Bearer abcdefghijklmnopqrst" https://x',
            "Bearer abcdefghijklmnopqrst",
            "authorization: bearer abcdefghijklmnopqrst",
        ):
            assert "abcdefghijklmnopqrst" not in redact(text), text
            assert "earer" in redact(text), text

    def test_the_scheme_word_is_kept_but_basic_and_token_are_not_matched(self):
        """A ``Basic`` value is base64 of ``user:password`` with no label inside
        it, and neither scheme is in Codex's table; both pass through."""
        for text in (
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "Authorization: Token abcdefghijklmnopqrst",
        ):
            assert redact(text) == text, text

    def test_a_key_that_names_a_secret_redacts_its_value(self):
        """The ``mcpServers`` ``env`` channel. A per-string pass cannot do this:
        on its own, the value is indistinguishable from a hash -- the label is in
        a different string."""
        result = redact_value({"env": {"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI-K7MDENG-bPxRfiCY"}})

        assert result["env"]["AWS_SECRET_ACCESS_KEY"] == REPLACEMENT
        assert "AWS_SECRET_ACCESS_KEY" in result["env"], "the key stays; the reader needs to know what was set"

    def test_a_key_that_names_a_token_redacts_its_value(self):
        """Docker's ``identitytoken`` is caught because its name ends in
        ``token``, which the mapping-key table matches."""
        result = redact_value({"identitytoken": "abc-token-value-0123456789"})

        assert result["identitytoken"] == REPLACEMENT

    @pytest.mark.parametrize("key", ["auth", "credential", "private_key"])
    def test_a_structured_credential_key_is_redacted(self, key):
        """The mapping-key table stays wider than the pattern table on purpose.

        A key is a name, not prose, so ``auth``, ``credential`` and
        ``private_key`` name a value that must go even though the text rules no
        longer treat those words as secret labels. Dropping them let a
        structured ``{"headers": {"Authorization": ...}, "credential": ...}``
        payload publish its values to ``rawInput`` in plaintext.
        """
        result = redact_value({key: "plain-value-not-shaped-like-a-secret"})

        assert result[key] == REPLACEMENT

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
            "args": ["--api-key", "sk-proj-abcdefghijklmnop"],
            "retries": 3,
            "ok": True,
        }

        result = redact_value(payload)

        assert result["retries"] == 3, "shape and non-strings are untouched"
        assert result["ok"] is True
        assert "wJalrXUtnFEMI" not in str(result)
        assert "sk-proj-abcdefghijklmnop" not in str(result)
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
            "redis-cli -u redis://cache:6379/0 ping",
            "token: 12345",
            "the password is wrong",
            "authorization: read",
            "make -j8 build",
            "grep -rn 'secret' docs/",
            "ssh-keygen -t ed25519 -C me@example.com",
            "SELECT id, password_hash FROM users LIMIT 10",
            # The labels the table does not treat as secret names. ``PWD`` is the
            # shell's working-directory variable, ``auth`` and ``credential`` are
            # ordinary config words, and ``private_key`` can be a path.
            "cookie: string",
            "pwd = /home/user/work",
            "auth: required",
            "private_key: /etc/ssl/key.pem",
            "token: string",
            'api_key: "abc"',
            "passwd: compat",
            "credential: none",
        ],
    )
    def test_ordinary_text_is_unchanged(self, text):
        """A table that redacts these makes every tool row unreadable -- and an
        unreadable row is approved without being read."""
        assert redact(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "glpat-abcdefghijklmnopqrst",
            "xoxb-1234567890-abcdefghij",
            "AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI",
            "psql postgres://admin:hunter2secret@db:5432/app",
            "glab auth login --token glpat-abcdefghijklmnopqrst",
            "raven provider set --api-key abcdefghijklmnop",
        ],
    )
    def test_channels_outside_the_table_pass_through(self, text):
        """What the table costs, stated rather than discovered.

        Each of these is a credential shape the table used to match and no longer
        does: three vendor prefixes, a URL's inline password, and a CLI flag. The
        vendor and flag lines are what a person types, so the person had already
        put the secret on the command line; the URL's inline password is not, and
        its loss is recorded in the PR description.

        A GitHub token is deliberately not named here in either direction, so no
        behaviour test covers that class; ``test_the_table_is_the_codex_rules_plus_pem``
        is the only guard that notices if a rule is added back.
        """
        assert redact(text) == text

    def test_a_replacement_marker_in_a_labelled_slot_is_left_alone(self):
        """``held_secrets`` writes ``[redacted: providers...apiKey]`` into the same
        slot a pattern reads, and that marker says *which* key it was -- so the
        value class stops at ``[`` and the marker survives a second pass.

        Without that, the exact-match pass would be undone by the pattern pass
        that follows it, and every known key would read as a bare ``[redacted]``.
        """
        text = '{"apiKey": "[redacted: providers.openrouter.apiKey]"}'

        assert redact(text) == text
        assert redact("token=[redacted]") == "token=[redacted]"

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
        [
            '"Authorization": "Bearer opaqu',
            "Bearer abc",
            "password=abcde",
            "token=ab",
            "AKIA1234",
            "sk-abc",
        ],
    )
    def test_a_known_cut_removes_short_credential_tails(self, text):
        assert REPLACEMENT in redact(text, truncated=True)
        assert redact(text) == text

    def test_a_known_cut_keeps_ordinary_unlabelled_text(self):
        text = "ordinary diagnostic " + "x" * 1000
        assert redact(text, truncated=True) == text

    def test_the_assignment_floor_is_eight_characters(self):
        """Codex's floor, pinned as a pair so a smaller quantifier and a larger
        one both show up. ``abcdefg`` is a placeholder; ``abcdefgh`` is not."""
        assert redact("token: abcdefg") == "token: abcdefg"
        assert redact("token: abcdefgh") == "token: [redacted]"

    def test_a_standalone_jwt_is_left_to_its_label(self):
        """A JWT with no label and no bearer prefix is not treated as a secret.

        Codex has no JWT rule either. A bare ``eyJ...`` triple is
        indistinguishable from a base64 blob a row legitimately shows, and a
        pattern for it fires at every accidental ``eyJ`` in a long body; the
        labeled form (``token=``, ``id_token``) and the bearer form are the
        shapes a credential actually arrives in, and both are still caught.
        """
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"

        assert redact(jwt) == jwt
        assert redact(f"/verify-{jwt}") == f"/verify-{jwt}"
        assert REPLACEMENT in redact(f"token={jwt}")
        assert REPLACEMENT in redact(f"Bearer {jwt}")

    @pytest.mark.production_timing
    @pytest.mark.parametrize(
        ("head", "unit", "stop", "size"),
        [
            ("", ".sk-", "!x", 64_000),
            ("", "token=", ",x", 32_000),
            ("", " ", "x y", 32_000),
            ("Authorization:", " ", "abcde", 32_000),
        ],
        ids=["prefix-run", "named-secret-run", "whitespace-gap", "label-gap"],
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
        tails = ["x.sk-", "token=", '"password": "', "Bearer ", "AKIA", "ASIA"]
        for tail in tails:
            for lead in ("", "x " * 300, "token=" * 100 + ","):
                text = lead + tail + "aZ9" * 140
                assert REPLACEMENT in whole_text(text), text
                assert redact_module._redact_cut_tail(text) == whole_text(text), text

        rng = random.Random(978)
        heads = ["password", "api_key", "token", "access_key"]
        heads += ["sk-", "AKIA", "ASIA", "Bearer"]
        noise = ["sk-", ".sk-", "token=", "://", ":", "@", "/", "!", ",", "'", '"', " ", "\n", "x", "Z9"]
        for _ in range(3000):
            text = "".join(rng.choice(noise) for _ in range(rng.randint(0, 12)))
            for part in (heads, ["", '"', "'"], None, ["", ":", "=", " "], None, ["", '"', "Bearer ", "bearer  "]):
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
            "token:" + spaces + "ab",
            '"password":' + spaces + '"abc',
            "Bearer" + spaces + "ab",
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

    def test_the_scrubbers_own_scan_cut_removes_a_partial_bearer_token(self):
        """The scan cut lands inside a token, so the tail pass has to remove what
        is left -- a credential cut in half is still a credential."""
        text = "x" * (MAX_SCAN_CHARS - 20) + " Bearer " + "a" * 100

        result = redact(text)

        assert "a" * 40 not in result
        assert "truncated before scanning" in result

    def test_an_unlabelled_secret_passes_through(self):
        """Stated as a test rather than left to be discovered. This is not a
        secret scanner: a high-entropy string with no label and no vendor prefix
        is indistinguishable from a hash, a build id or a commit sha -- and
        redacting those would break every row that legitimately shows one."""
        opaque = "Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0"

        assert redact(f"echo {opaque}") == f"echo {opaque}"

    def test_the_table_is_the_codex_rules_plus_pem(self):
        """Five patterns: Codex's four plus PEM, not one per credential vendor.

        Pinned as an exact list rather than a range: a range with a low floor let
        eight rules be deleted with the suite green, which is how the table grew
        the first time. The fifth entry is the structural one -- it matches a
        delimiter, not a vendor -- and it is the only place this table goes past
        Codex.
        """
        assert pattern_names() == ("bearer", "openai", "aws-access-key", "named-secret", "pem")

    def test_no_pattern_matches_the_replacement_token(self):
        """What makes it idempotent, and what stops a second pass from eating the
        token itself."""
        assert redact(REPLACEMENT) == REPLACEMENT
        assert redact(f"api_key={REPLACEMENT}") == f"api_key={REPLACEMENT}"
