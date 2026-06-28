"""Tests for the auth allowlist consolidation.

Two layers:

1. Direct ``is_allowed(channel, sender, allow_list)`` semantics.
2. ``channels/base.py:ChannelBase.is_allowed`` still behaves as before,
   now that it delegates here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from raven.auth import (
    CapabilityToken,
    ManagedSettings,
    is_allowed,
    issue_token,
    verify_token,
)
from raven.auth.allowlist import reset_warning_state


@pytest.fixture(autouse=True)
def _reset_warnings():
    """The ``empty allowlist`` warning is suppressed after the first hit
    per-channel-per-process. Reset between tests so warning-fire
    assertions stay deterministic."""
    reset_warning_state()
    yield
    reset_warning_state()


# ---------------------------------------------------------------------------
# is_allowed semantics
# ---------------------------------------------------------------------------


class TestAllowlistSemantics:
    def test_empty_list_denies(self):
        assert is_allowed("test_channel", "alice", []) is False

    def test_none_denies(self):
        assert is_allowed("test_channel", "alice", None) is False

    def test_wildcard_allows_everyone(self):
        assert is_allowed("test_channel", "alice", ["*"]) is True
        assert is_allowed("test_channel", "bob", ["*"]) is True

    def test_exact_match_allows(self):
        assert is_allowed("test_channel", "alice", ["alice", "bob"]) is True

    def test_exact_mismatch_denies(self):
        assert is_allowed("test_channel", "carol", ["alice", "bob"]) is False

    def test_int_sender_id_stringified(self):
        # platforms like Telegram pass ints; comparison must coerce.
        assert is_allowed("telegram", 12345, ["12345"]) is True
        assert is_allowed("telegram", 12345, [12345]) is True  # also from list side

    def test_warning_logged_once_for_empty_list(self, caplog):
        with caplog.at_level(logging.WARNING, logger="raven.auth.allowlist"):
            is_allowed("flaky_channel", "x", [])
            is_allowed("flaky_channel", "y", [])
            is_allowed("flaky_channel", "z", [])

        warnings = [
            r for r in caplog.records if "all access denied" in r.message
        ]
        # First call warns; subsequent calls deduplicate per channel.
        assert len(warnings) == 1

    def test_distinct_channels_each_warn_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="raven.auth.allowlist"):
            is_allowed("channel_a", "x", [])
            is_allowed("channel_b", "x", [])
            is_allowed("channel_a", "y", [])  # already warned
        warnings = [
            r for r in caplog.records if "all access denied" in r.message
        ]
        assert len(warnings) == 2


# ---------------------------------------------------------------------------
# ChannelBase.is_allowed integration
# ---------------------------------------------------------------------------


@dataclass
class _StubConfig:
    allow_from: Any = None


class _StubChannel:
    """Minimal stand-in for ChannelBase — just enough surface to call
    the inherited ``is_allowed`` method through the descriptor protocol."""

    name = "stub"

    def __init__(self, allow_from):
        self.config = _StubConfig(allow_from=allow_from)

    # Import is_allowed from the real ChannelBase so we exercise its
    # delegation logic.
    from raven.channels.base import ChannelBase  # noqa: E402

    is_allowed = ChannelBase.is_allowed


class TestChannelBaseDelegation:
    def test_allow_list_passthrough(self):
        ch = _StubChannel(allow_from=["alice"])
        assert ch.is_allowed("alice") is True
        assert ch.is_allowed("bob") is False

    def test_wildcard(self):
        ch = _StubChannel(allow_from=["*"])
        assert ch.is_allowed("anyone") is True

    def test_empty_list_denies(self):
        ch = _StubChannel(allow_from=[])
        assert ch.is_allowed("alice") is False

    def test_missing_config_attribute_denies(self):
        @dataclass
        class _NoFieldConfig:
            pass

        class _BareChannel:
            name = "bare"
            config = _NoFieldConfig()
            from raven.channels.base import ChannelBase
            is_allowed = ChannelBase.is_allowed

        # When the config dataclass has no allow_from attribute,
        # the channel must deny by default.
        assert _BareChannel().is_allowed("alice") is False


# ---------------------------------------------------------------------------
# CapabilityToken roundtrip
# ---------------------------------------------------------------------------


class TestCapabilityTokens:
    def test_issue_and_verify_roundtrip(self):
        secret = "test-secret"
        token = CapabilityToken(
            agent_id="agent-1", capabilities=["read", "write"]
        )
        wire = issue_token(token, secret)
        recovered = verify_token(wire, secret)
        assert recovered is not None
        assert recovered.agent_id == "agent-1"
        assert recovered.capabilities == ["read", "write"]

    def test_tampered_signature_rejected(self):
        secret = "test-secret"
        token = CapabilityToken(agent_id="agent-1", capabilities=["read"])
        wire = issue_token(token, secret)
        # Flip a character in the signature.
        body, sig = wire.split(".", 1)
        tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        assert verify_token(f"{body}.{tampered_sig}", secret) is None

    def test_wrong_secret_rejected(self):
        token = CapabilityToken(agent_id="agent-1")
        wire = issue_token(token, "secret-a")
        assert verify_token(wire, "secret-b") is None

    def test_expired_token_rejected(self):
        # Issued at t=1000, expires at t=1001; check from "now"=10000.
        token = CapabilityToken(
            agent_id="agent-1",
            capabilities=["x"],
            issued_at=1000,
            expires_at=1001,
        )
        wire = issue_token(token, "s")
        recovered = verify_token(wire, "s")
        # The current implementation checks ``time.time()`` for expiry; since
        # 1001 is far in the past, the token is expired and rejected.
        assert recovered is None

    def test_malformed_token_returns_none(self):
        assert verify_token("not.valid.token", "s") is None
        assert verify_token("nodots", "s") is None
        assert verify_token("", "s") is None

    def test_token_payload_roundtrip(self):
        token = CapabilityToken(
            agent_id="a", capabilities=["c1"], metadata={"role": "subagent"}
        )
        payload = token.to_payload()
        rebuilt = CapabilityToken.from_payload(payload)
        assert rebuilt.agent_id == token.agent_id
        assert rebuilt.capabilities == token.capabilities
        assert rebuilt.metadata == token.metadata


# ---------------------------------------------------------------------------
# ManagedSettings
# ---------------------------------------------------------------------------


class TestManagedSettings:
    def test_default_locks_nothing(self):
        s = ManagedSettings()
        assert s.is_locked("any.field") is False

    def test_locked_field_membership(self):
        s = ManagedSettings(
            locked_fields=frozenset({"providers.openrouter.api_key"}),
            description="company key locked",
        )
        assert s.is_locked("providers.openrouter.api_key") is True
        assert s.is_locked("providers.anthropic.api_key") is False

    def test_frozen_dataclass(self):
        s = ManagedSettings()
        with pytest.raises(Exception):  # FrozenInstanceError subclass
            s.description = "mutated"  # type: ignore[misc]
