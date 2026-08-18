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
        # None of these carries any of the wordier model_unavailable markers,
        # even though each one has "404" inside a larger number or id --
        # matching it as a bare substring once burned a fallback model and
        # cooled a healthy endpoint for an error no swap could fix. Azure's own
        # rendered non-200 body no longer reaches this degraded path at all:
        # see ``AzureOpenAIProvider.chat``, which classifies from the live
        # status code before the response is turned into a string.
        ("Error: retry after 1404ms", "unknown"),
        ("upstream error id=req_a404bc7f", "unknown"),
        ("invalid JSON at char 4041", "unknown"),
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


# --- format_llm_error / parse_llm_error / _strip_json_error_body ------------- #


def test_format_llm_error_collapses_prefixes_and_json_body():
    from raven.providers.base import format_llm_error, parse_llm_error

    exc = _StatusError(
        "litellm.AuthenticationError: AuthenticationError: OpenrouterException - "
        '{"error":{"message":"User not found.","code":401}}',
        status_code=401,
    )
    content = format_llm_error(exc, LLMProvider.classify_error(exc), provider="openrouter")

    assert content == (
        "Error calling LLM (auth@openrouter): AuthenticationError: OpenrouterException - User not found."
    )
    assert parse_llm_error(content) == (
        "auth",
        "openrouter",
        "AuthenticationError: OpenrouterException - User not found.",
    )


def test_parse_llm_error_rejects_ordinary_content():
    from raven.providers.base import parse_llm_error

    assert parse_llm_error("a normal reply that mentions Error calling LLM") is None
    assert parse_llm_error(None) is None


def test_strip_json_error_body_keeps_trailing_text():
    from raven.providers.base import _strip_json_error_body

    assert _strip_json_error_body('X - {"error":{"message":"boom"}} (request id: abc)') == "X - boom (request id: abc)"


def test_strip_json_error_body_without_a_message_leaves_text_alone():
    from raven.providers.base import _strip_json_error_body

    text = 'Config invalid: {"foo": "bar"} retry with a valid key'
    assert _strip_json_error_body(text) == text
