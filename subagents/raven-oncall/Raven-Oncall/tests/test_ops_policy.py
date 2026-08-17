"""Tests for the Ops retry policy (the minimal decision brain)."""

from __future__ import annotations

from raven.ops import RetryPolicy


def test_default_policy_never_retries() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(1) is False


def test_max_retries_allows_that_many_further_attempts() -> None:
    policy = RetryPolicy(max_retries=2)
    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True
    assert policy.should_retry(3) is False
