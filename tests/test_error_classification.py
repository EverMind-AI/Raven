"""Structured error classification + jittered backoff (LLMProvider).

``classify_error`` is the seam that drives retry / fallback / compress
decisions. It works on a live exception (HTTP status + class name, walking the
__cause__ chain) or, degraded, on the swallowed error string.
"""

from __future__ import annotations

import pytest

from raven.providers.base import ErrorClassification, LLMProvider

# --- fakes mimicking provider exception shapes (no SDK import needed) -------- #


class _StatusError(Exception):
    def __init__(self, msg: str, status_code: int):
        super().__init__(msg)
        self.status_code = status_code


class RateLimitError(Exception):
    pass


class ContextWindowExceededError(Exception):
    pass


def _c(exc=None, content=None) -> ErrorClassification:
    return LLMProvider.classify_error(exc, content)


# --- by HTTP status code ---------------------------------------------------- #


@pytest.mark.parametrize(
    "status,category,retry,fb,comp",
    [
        (429, "rate_limit", True, True, False),
        (503, "server", True, True, False),
        (500, "server", True, True, False),
        (401, "auth", False, False, False),
        (403, "auth", False, False, False),
        (402, "billing", False, True, False),
        (404, "model_unavailable", False, True, False),
        (400, "invalid_request", False, False, False),
    ],
)
def test_classify_by_status_code(status, category, retry, fb, comp):
    c = _c(_StatusError("boom", status))
    assert c.category == category
    assert (c.retryable, c.should_fallback, c.should_compress) == (retry, fb, comp)


# --- by exception class name ------------------------------------------------ #


def test_classify_by_class_name_rate_limit():
    c = _c(RateLimitError("slow down"))
    assert c.category == "rate_limit" and c.retryable and c.should_fallback


def test_classify_context_window_by_class_name_compresses_not_fallback():
    # A bare 400 would look like invalid_request; the class name disambiguates.
    c = _c(ContextWindowExceededError("400"))
    assert c.category == "context_overflow"
    assert c.should_compress is True
    assert c.should_fallback is False
    assert c.retryable is False


# --- walks the __cause__ chain for the status code -------------------------- #


def test_classify_follows_cause_chain():
    inner = _StatusError("upstream 429", 429)
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("wrapped") from e
    except Exception as outer:
        c = _c(outer)
    assert c.category == "rate_limit" and c.should_fallback


# --- degraded string path (provider already swallowed the exception) -------- #


@pytest.mark.parametrize(
    "text,category",
    [
        ("429 rate limit hit", "rate_limit"),
        ("503 service unavailable", "server"),
        ("connection reset by peer", "network"),
        ("insufficient credit / billing", "billing"),
        ("model not found", "model_unavailable"),
        ("This model's maximum context length is 8192 tokens", "context_overflow"),
        ("401 unauthorized: invalid api key", "auth"),
        ("400 invalid request: bad schema", "invalid_request"),
        ("something totally unexpected", "unknown"),
    ],
)
def test_classify_by_string(text, category):
    assert _c(content=text).category == category


def test_unknown_is_conservative():
    c = _c(content="???")
    assert not c.retryable and not c.should_fallback and not c.should_compress


# --- jitter ----------------------------------------------------------------- #


def test_jitter_within_ten_percent():
    for _ in range(50):
        j = LLMProvider._jittered(4.0)
        assert 3.6 <= j <= 4.4


def test_jitter_zero_stays_zero():
    assert LLMProvider._jittered(0) == 0.0


# --- Retry-After ------------------------------------------------------------ #


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def test_retry_after_from_response_headers():
    exc = Exception("429")
    exc.response = _Resp({"retry-after": "7"})
    assert LLMProvider._retry_after_seconds(exc) == 7.0


def test_retry_after_direct_headers_attr():
    exc = Exception("429")
    exc.headers = {"Retry-After": "2.5"}
    assert LLMProvider._retry_after_seconds(exc) == 2.5


@pytest.mark.parametrize(
    "raw",
    [None, "", "Wed, 21 Oct 2026 07:28:00 GMT", "-3", "0", "999999"],
)
def test_retry_after_ignores_absent_dates_and_absurd_values(raw):
    exc = Exception("429")
    exc.headers = {"retry-after": raw} if raw is not None else {}
    assert LLMProvider._retry_after_seconds(exc) is None


def test_retry_after_none_exception():
    assert LLMProvider._retry_after_seconds(None) is None


# --- 424 Failed Dependency (gateway upstream failure) ----------------------- #


def test_424_status_classifies_as_retryable_server():
    exc = Exception("Provider returned error")
    exc.status_code = 424
    c = _c(exc=exc)
    assert c.category == "server" and c.retryable and c.should_fallback


def test_failed_dependency_phrase_classifies_as_server():
    c = _c(content="APIError: failed dependency from upstream")
    assert c.category == "server" and c.retryable


def test_bare_424_digits_in_content_do_not_trigger_server():
    # "424" appears in ordinary output (line numbers, byte counts); only the
    # status code or the explicit phrase may classify.
    c = _c(content="wrote 424 bytes to output.bin")
    assert c.category == "unknown"
