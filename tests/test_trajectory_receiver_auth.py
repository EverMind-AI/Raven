"""Tests for the Upload Challenge (`raven.trajectory.receiver_auth`)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from raven.trajectory import receiver_auth as auth

KEY = b"k" * 32
OTHER_KEY = b"o" * 32
NOW = 1_760_000_000
TTL = 300.0
SHA = "a" * 64


def _issue(now=NOW, key=KEY):
    return auth.issue_challenge(key, now)


def _segments(challenge):
    return challenge.split(".")


def test_a_fresh_challenge_passes_for_its_whole_lifetime():
    challenge = _issue()
    assert auth.challenge_problem(challenge, KEY, NOW, TTL) is None
    assert auth.challenge_problem(challenge, KEY, NOW + TTL, TTL) is None
    assert auth.challenge_problem(challenge, KEY, NOW + TTL + 1, TTL) == auth.CHALLENGE_EXPIRED


def test_two_challenges_issued_at_once_differ():
    assert _issue() != _issue()


def test_a_challenge_issued_within_a_second_counts_from_that_second():
    challenge = auth.issue_challenge(KEY, NOW + 0.9)
    assert auth.challenge_problem(challenge, KEY, NOW + TTL, TTL) is None


def _tamper(challenge, index, value):
    parts = _segments(challenge)
    parts[index] = value(parts[index])
    return ".".join(parts)


def _flip(text):
    return text[:-1] + ("1" if text.endswith("0") else "0")


@pytest.mark.parametrize(
    "index,change",
    [(0, lambda _p: "c2"), (1, lambda p: str(int(p) + 1)), (2, _flip), (3, _flip)],
    ids=["version", "time", "nonce", "seal"],
)
def test_any_tampered_segment_is_unauthorized(index, change):
    challenge = _tamper(_issue(), index, change)
    assert auth.challenge_problem(challenge, KEY, NOW, TTL) == auth.UNAUTHORIZED


def test_a_challenge_sealed_by_another_key_is_unauthorized():
    assert auth.challenge_problem(_issue(key=OTHER_KEY), KEY, NOW, TTL) == auth.UNAUTHORIZED


def test_a_challenge_from_the_future_is_unauthorized_beyond_the_slack():
    assert auth.challenge_problem(_issue(now=NOW + 5), KEY, NOW, TTL) is None
    assert auth.challenge_problem(_issue(now=NOW + 6), KEY, NOW, TTL) == auth.UNAUTHORIZED


@pytest.mark.parametrize(
    "challenge",
    [
        "",
        "c1",
        "c1...",
        "c1.1760000000." + "a" * 32 + "." + "A" * 64,
        "c1.\uff11\uff17\uff16." + "a" * 32 + "." + "a" * 64,
        "c1." + "9" * 13 + "." + "a" * 32 + "." + "a" * 64,
        "c1.1760000000." + "a" * 32 + "." + "a" * 64 + "\n",
        "\udcff",
        "x" * 10_000,
    ],
    ids=["empty", "prefix", "dots", "upper-seal", "fullwidth-time", "long-time", "newline", "surrogate", "huge"],
)
def test_malformed_challenges_are_unauthorized_without_raising(challenge):
    assert auth.challenge_problem(challenge, KEY, NOW, TTL) == auth.UNAUTHORIZED


def test_the_response_wire_format_is_pinned():
    challenge = "c1.1760000000." + "0" * 32 + "." + "f" * 64
    message = f"raven-bugreport-auth-v1\n{challenge}\nbr-20261012-abcdef\n{SHA}\n1234".encode()
    key = hashlib.sha256(b"raven bug report client gate v1").digest()
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    assert auth.CLIENT_KEY == key
    assert auth.client_response(challenge, report_id="br-20261012-abcdef", sha256=SHA, size_bytes=1234) == expected


def _respond(challenge, **overrides):
    fields = {"report_id": "br-20261012-abcdef", "sha256": SHA, "size_bytes": 1234, **overrides}
    return auth.client_response(challenge, **fields)


def test_a_matching_response_is_accepted():
    challenge = _issue()
    response = _respond(challenge)
    assert len(response) == 64 and response == response.lower()
    assert auth.response_matches(challenge, response, report_id="br-20261012-abcdef", sha256=SHA, size_bytes=1234)


@pytest.mark.parametrize(
    "field,value",
    [("report_id", "br-20261012-000000"), ("sha256", "b" * 64), ("size_bytes", 1235)],
)
def test_a_response_binds_the_package(field, value):
    challenge = _issue()
    fields = {"report_id": "br-20261012-abcdef", "sha256": SHA, "size_bytes": 1234}
    response = _respond(challenge, **{field: value})
    assert not auth.response_matches(challenge, response, **fields)


def test_a_response_binds_the_challenge():
    challenge, other = _issue(), _issue()
    fields = {"report_id": "br-20261012-abcdef", "sha256": SHA, "size_bytes": 1234}
    assert not auth.response_matches(other, _respond(challenge), **fields)


@pytest.mark.parametrize(
    "mangle",
    [str.upper, lambda r: r[:-1], lambda r: r + "0", lambda r: "z" + r[1:], lambda _r: ""],
    ids=["upper", "short", "long", "not-hex", "empty"],
)
def test_malformed_responses_are_refused(mangle):
    challenge = _issue()
    fields = {"report_id": "br-20261012-abcdef", "sha256": SHA, "size_bytes": 1234}
    assert not auth.response_matches(challenge, mangle(_respond(challenge)), **fields)


@pytest.mark.parametrize("challenge", ["\udcff\udcfe", "caf\u00e9", "x" * 10_000])
def test_odd_challenges_never_make_matching_raise(challenge):
    fields = {"report_id": "br-20261012-abcdef", "sha256": SHA, "size_bytes": 1234}
    assert not auth.response_matches(challenge, "0" * 64, **fields)
