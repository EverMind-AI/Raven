"""Bedrock is a warning-suppress stub, not a real backend.

Two things are pinned so that actually wiring a Bedrock `converse` backend
later will break a test and force this file to be revisited:

  (a) the import-time log filter drops only the two known LiteLLM
      botocore-preload warnings and nothing else;
  (b) there is no Bedrock code path — no `bedrock` provider spec. The sole
      bedrock touchpoint is the ambient-credential declaration in
      providers/auth.py (the AWS chain, not an api_key), which is what lets a
      ``bedrock/...`` model pass the key gate and fall through to LiteLLM
      (no `converse` call).
"""

from __future__ import annotations

import logging
from pathlib import Path

import raven.cli._helpers as helpers_mod
from raven import _LiteLLMBotocorePreloadFilter
from raven.providers.registry import PROVIDERS, find_by_model, find_by_name


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("LiteLLM", logging.WARNING, __file__, 1, msg, (), None)


def test_filter_drops_the_two_botocore_preload_warnings():
    filt = _LiteLLMBotocorePreloadFilter()
    assert filt._patterns == (
        "could not pre-load bedrock-runtime response stream shape",
        "could not pre-load sagemaker-runtime response stream shape",
    )
    for pattern in filt._patterns:
        assert filt.filter(_record(pattern)) is False


def test_filter_keeps_unrelated_log_records():
    filt = _LiteLLMBotocorePreloadFilter()
    assert filt.filter(_record("a perfectly normal log line")) is True


def test_no_bedrock_provider_in_registry():
    assert "bedrock" not in {spec.name for spec in PROVIDERS}
    assert find_by_name("bedrock") is None
    # A bedrock-prefixed model without provider keywords resolves to no spec —
    # nothing in the registry claims to route Bedrock traffic.
    assert find_by_model("bedrock/amazon.titan-text-express-v1") is None


def test_only_bedrock_touchpoint_is_the_ambient_auth_declaration():
    # cli/_helpers.py no longer special-cases bedrock: the key gate is
    # providers.auth, where bedrock is declared ambient (the AWS chain).
    source = Path(helpers_mod.__file__).read_text(encoding="utf-8")
    assert source.count("bedrock") == 0

    from raven.providers.auth import credential_status

    assert credential_status("bedrock", None, include_external=True).ok
    # No Bedrock Converse backend has been wired in.
    assert "converse" not in source
