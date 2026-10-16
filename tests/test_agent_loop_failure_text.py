"""The long account of a failed turn: unbounded, scrubbed, capped, and attached
only when it says more than the 200-character text already does."""

from __future__ import annotations

import time

import pytest

from raven.agent.loop import failure_text
from raven.agent.loop.failure_text import (
    attach_full_detail,
    exception_failure_text,
    full_failure_text,
    response_failure_text,
)
from raven.contracts.llm_provider import CallRecord
from raven.providers.call_record import MAX_BODY_CHARS, REPLACEMENT, from_exception
from raven.spine.scheduler import describe_failure
from raven.spine.turn import AnswerlessTurnError

KEY = "sk-or-v1-" + "a" * 40
GOOGLE_KEY = "AIza" + "Ab1_-" * 7
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
SLACK_TOKEN = "xoxb-" + "1234567890-abcdefghijKLMNOP"


class _Frozen(Exception):
    def __setattr__(self, name, value):
        raise AttributeError(name)


def test_the_long_account_keeps_what_the_short_one_cuts():
    assert full_failure_text("upstream said: " + "x" * 600) == "upstream said: " + "x" * 600


def test_a_credential_in_the_account_is_scrubbed():
    text = full_failure_text(f"401 from the gateway, Authorization: Bearer {KEY}")
    assert KEY not in text
    assert REPLACEMENT in text


def test_a_json_quoted_authorization_value_in_a_body_is_scrubbed():
    """A provider can echo the request's headers back as JSON. A token with no
    vendor shape is caught only by the call record's body scrubber, which reads a
    JSON-quoted ``Authorization`` value; ``redact`` alone passes it through."""
    token = "gw_" + "q7" * 20
    body = '{"error": {"message": "unauthorized", "headers": {"Authorization": "Bearer ' + token + '"}}}'
    text = full_failure_text(f"401 from the gateway\n\n{body}")
    assert token not in text
    assert REPLACEMENT in text


def test_the_account_is_capped_at_the_call_record_body_ceiling():
    text = full_failure_text("y" * (MAX_BODY_CHARS * 2))
    assert len(text) == MAX_BODY_CHARS
    assert text.endswith("...")


def test_an_account_exactly_at_the_ceiling_is_kept_whole():
    text = "y" * MAX_BODY_CHARS
    assert full_failure_text(text) == text


def test_an_exception_is_named_the_way_the_lane_names_it():
    assert exception_failure_text(ValueError("boom")) == "ValueError: boom"
    assert exception_failure_text(TimeoutError()) == "TimeoutError"


def test_an_answerless_turn_prefers_its_own_long_account():
    exc = AnswerlessTurnError(
        "Error calling LLM (rate_limit@openrouter): Provider returned error", full_detail="the body"
    )
    assert exception_failure_text(exc) == "the body"


def test_a_failed_call_reads_as_its_line_then_its_body():
    record = CallRecord(http_status=429, body='{"error":{"metadata":{"raw":"rate-limited upstream"}}}')
    text = response_failure_text("Error calling LLM (rate_limit@openrouter): Provider returned error", record)
    assert text == (
        "Error calling LLM (rate_limit@openrouter): Provider returned error\n\n"
        '{"error":{"metadata":{"raw":"rate-limited upstream"}}}'
    )


def test_a_body_the_line_already_quotes_is_not_repeated():
    record = CallRecord(body="no first byte")
    line = "Error calling LLM (first_byte_timeout): no first byte"
    assert response_failure_text(line, record) == line


def test_a_call_with_nothing_to_say_has_no_account():
    assert response_failure_text(None, None) is None
    assert response_failure_text("  ", CallRecord()) is None


def test_attached_only_when_it_says_more_than_the_short_text():
    short = ValueError("boom")
    assert attach_full_detail(short) is None
    assert short.full_detail is None

    long = ValueError("z" * 500)
    attached = attach_full_detail(long)
    assert attached == "ValueError: " + "z" * 500
    assert long.full_detail == attached


def test_a_second_attach_files_the_same_text():
    exc = AnswerlessTurnError("Error calling LLM (server@x): 503", full_detail="503 body " + "b" * 400)
    first = attach_full_detail(exc)
    assert attach_full_detail(exc) == first


def test_a_foreign_full_detail_is_never_passed_on_unscrubbed():
    exc = RuntimeError("boom " + "c" * 300)
    exc.full_detail = f"leaked {KEY}"
    attached = attach_full_detail(exc)
    assert attached == "RuntimeError: boom " + "c" * 300
    assert KEY not in (exc.full_detail or "")


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("boom"), id="plain-message"),
        pytest.param(RuntimeError("RuntimeError: already named"), id="colon-after-name"),
        pytest.param(RuntimeError("RuntimeError already named"), id="space-after-name"),
        pytest.param(RuntimeError("RuntimeError"), id="bare-name"),
        pytest.param(TimeoutError(), id="no-message"),
        pytest.param(ValueError("  padded  "), id="padded-message"),
    ],
)
def test_a_short_exception_has_the_same_long_account_as_short_one(exc):
    assert exception_failure_text(exc) == describe_failure(exc)


@pytest.mark.parametrize("detail", ["   ", ["a"]], ids=["blank", "not-text"])
def test_a_blank_or_non_text_full_detail_is_no_account(detail):
    exc = AnswerlessTurnError("msg", full_detail=detail)
    assert exception_failure_text(exc) == "msg"
    assert attach_full_detail(exc) is None


def test_an_exception_that_refuses_the_attribute_still_gets_its_long_account():
    assert attach_full_detail(_Frozen("x" * 500)) == "_Frozen: " + "x" * 500


def test_a_failed_call_with_only_a_body_reads_as_that_body():
    assert response_failure_text(None, CallRecord(body="the body")) == "the body"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        pytest.param(
            f"GET https://maps.example.com/v1/geocode?key={GOOGLE_KEY}&address=x",
            GOOGLE_KEY,
            id="key-in-a-url",
        ),
        pytest.param(
            f"clone https://user:{GITHUB_TOKEN}@github.com/owner/repo.git failed",
            GITHUB_TOKEN,
            id="userinfo-in-a-url",
        ),
        pytest.param("login failed: password=hunter2hunter2 for svc", "hunter2hunter2", id="password-assignment"),
        pytest.param(f"slack rejected {SLACK_TOKEN}", SLACK_TOKEN, id="slack-bot-token"),
    ],
)
def test_a_credential_in_a_crash_message_is_scrubbed(text, secret):
    out = full_failure_text(text)
    assert secret not in out
    assert REPLACEMENT in out


def test_the_scrubbers_never_read_more_than_their_window(monkeypatch):
    """Two of the scrubbers' patterns cost more than linear time, and the long
    account is built on the event loop while a turn fails: a huge crash message
    must not reach them whole."""
    seen: list[int] = []

    def _recording(text: str) -> str:
        seen.append(len(text))
        return text

    monkeypatch.setattr(failure_text, "scrub_credentials", _recording)
    text = full_failure_text("z" * (10 * MAX_BODY_CHARS))
    assert seen == [4 * MAX_BODY_CHARS]
    assert len(text) == MAX_BODY_CHARS


class _ClientError(Exception):
    def __init__(self, message: str, body: object, status_code: int = 400) -> None:
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.response = type("R", (), {"text": "", "headers": {}})()


def test_a_client_error_reads_as_its_message_then_its_body():
    body = {"error": {"message": "stub refusal", "echo": f"Authorization: Bearer {KEY}", "tail": "TAIL-MARKER"}}
    exc = _ClientError("litellm.BadRequestError: OpenAIException - stub refusal", body)
    text = exception_failure_text(exc)
    assert text.startswith("_ClientError: litellm.BadRequestError: OpenAIException - stub refusal\n\n")
    assert "TAIL-MARKER" in text
    assert KEY not in text


def test_a_client_error_is_handed_on_with_its_body():
    exc = _ClientError("litellm.BadRequestError: OpenAIException - stub refusal", {"error": {"tail": "TAIL-MARKER"}})
    attached = attach_full_detail(exc)
    assert attached is not None and "TAIL-MARKER" in attached


def test_a_body_the_message_already_quotes_is_not_repeated():
    body = "the whole body"
    exc = _ClientError("APIError: the whole body", body)
    assert exception_failure_text(exc).count("the whole body") == 1


def _unreadable(attribute: str) -> Exception:
    """An exception whose ``attribute`` raises something other than AttributeError when read."""

    def _raise(self):
        raise RuntimeError(f"{attribute} is unreadable")

    members = {"status_code": 400, attribute: property(_raise)}
    return type("_Unreadable", (Exception,), members)("z" * 500)


@pytest.mark.parametrize("attribute", ["response", "status_code", "body"])
def test_an_unreadable_attribute_costs_the_body_not_the_account(attribute):
    assert attach_full_detail(_unreadable(attribute)) == "_Unreadable: " + "z" * 500


def test_a_scan_cut_private_key_stays_hidden_after_an_earlier_secret_shrinks():
    secret = "synthetic-private-material-" + "a" * 1600
    pem = "-----BEGIN " + f"PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    text = "Authorization: Bearer " + "b" * 31000 + "\n" + pem

    full = full_failure_text(text)

    assert "synthetic-private-material" not in full
    assert len(full) <= MAX_BODY_CHARS
    assert REPLACEMENT in full


@pytest.mark.parametrize(
    ("tail", "visible", "kept"),
    [
        ("eyJ" + "A" * 1400 + "." + "B" * 20 + "." + "C" * 20, "eyJ" + "A" * 30, 978),
        ("https://user:synthetic-password-" + "a" * 1400 + "@host", "synthetic-password", 978),
        ("ghp_" + "A" * 30, "ghp_" + "A" * 8, 12),
        ("password=abcdefghijk", "abcde", 14),
        ('"Authorization": "Bearer opaque-token-value"', "opaqu", 30),
    ],
    ids=["jwt", "url-userinfo", "vendor-token", "named-secret", "quoted-header"],
)
def test_a_scan_cut_credential_tail_is_removed_before_earlier_redactions_move_it(tail, visible, kept):
    prefix = "Authorization: Bearer "
    text = prefix + "b" * (4 * MAX_BODY_CHARS - len(prefix) - kept - 1) + "\n" + tail
    assert visible not in full_failure_text(text)


def test_a_previously_capped_record_does_not_expose_a_private_key_fragment():
    body = "diagnostic " * 700 + "\n-----BEGIN " + "PRIVATE KEY-----\nsynthetic-private-material-" + "a" * 1600
    full = response_failure_text("provider refused", CallRecord(body=body[:MAX_BODY_CHARS], body_truncated=True))
    assert "synthetic-private-material" not in full


def test_a_previously_capped_record_removes_an_incomplete_jwt():
    token = "eyJ" + "a" * 100
    full = response_failure_text("provider refused", CallRecord(body=token, body_truncated=True))
    assert token not in full
    assert REPLACEMENT in full


def test_a_client_body_loses_its_private_key_before_the_record_is_capped():
    body = "diagnostic " * 700 + "\n-----BEGIN " + "PRIVATE KEY-----\nsynthetic-private-material-" + "a" * 1600
    record = from_exception(_ClientError("provider refused", body), status=400)
    assert "synthetic-private-material" not in record.body


@pytest.mark.production_timing
@pytest.mark.parametrize(
    "tail",
    ["Authorization:" + " " * 32000 + "!", "eyJ" * 10000 + "!"],
    ids=["authorization-whitespace", "jwt-prefixes"],
)
def test_detail_processing_does_not_hold_the_event_loop_for_seconds(tail):
    exc = RuntimeError("diagnostic " * 25 + tail)
    start = time.thread_time()
    attach_full_detail(exc)
    attach_full_detail(exc)
    assert time.thread_time() - start < 0.5


@pytest.mark.parametrize("value", [None, "x" * 500], ids=["no-detail", "long-detail"])
def test_the_same_failure_is_scrubbed_only_once(monkeypatch, value):
    calls = []
    original = failure_text.exception_failure_text

    def record(exc):
        calls.append(exc)
        return original(exc)

    monkeypatch.setattr(failure_text, "exception_failure_text", record)
    exc = RuntimeError(value or "boom")
    first = attach_full_detail(exc)
    assert attach_full_detail(exc) == first
    assert calls == [exc]


@pytest.mark.parametrize("getter_raises", [False, True], ids=["readonly", "unreadable"])
def test_a_foreign_property_cannot_replace_the_runners_safe_detail(getter_raises):
    class ForeignError(Exception):
        @property
        def full_detail(self):
            if getter_raises:
                raise RuntimeError("cannot read full_detail")
            return "x" * 9000 + " password=synthetic-password"

    exc = ForeignError("diagnostic " * 25 + " password=synthetic-password")
    expected = attach_full_detail(exc)
    assert "synthetic-password" not in expected
    assert failure_text.full_detail_of(exc) == expected
