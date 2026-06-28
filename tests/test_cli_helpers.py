"""Unit tests for ``raven.cli._helpers``.

Currently focused on ``send_probe`` — the shared LLM probe used by
``onboard`` Step 3 and ``doctor --probe``. Provider and config are
stubbed so the test never touches network or disk.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from raven.cli import _helpers
from raven.cli._helpers import send_probe


@pytest.fixture
def stub_load_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_config`` is lazy-imported inside ``send_probe`` — patch at source."""
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda: object(),
    )


def test_send_probe_success(
    monkeypatch: pytest.MonkeyPatch, stub_load_config: None
) -> None:
    """Happy path: provider returns a normal response → tuple shape correct."""

    class _FakeProvider:
        async def chat_with_retry(self, **_kwargs):
            return SimpleNamespace(
                finish_reason="stop",
                content="Hello world",
                usage={"total_tokens": 42},
            )

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _FakeProvider())

    text, tokens, elapsed = send_probe()

    assert text == "Hello world"
    assert tokens == 42
    assert elapsed >= 0


def test_send_probe_provider_error_raises(
    monkeypatch: pytest.MonkeyPatch, stub_load_config: None
) -> None:
    """``finish_reason='error'`` → ``send_probe`` raises ``RuntimeError``."""

    class _ErrProvider:
        async def chat_with_retry(self, **_kwargs):
            return SimpleNamespace(
                finish_reason="error",
                content="AuthenticationError: bad key",
                usage=None,
            )

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _ErrProvider())

    with pytest.raises(RuntimeError, match="bad key"):
        send_probe()


def test_send_probe_timeout_raises(
    monkeypatch: pytest.MonkeyPatch, stub_load_config: None
) -> None:
    """Slow provider trips ``asyncio.TimeoutError`` when ``timeout_s`` elapses."""

    class _SlowProvider:
        async def chat_with_retry(self, **_kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(finish_reason="stop", content="", usage=None)

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _SlowProvider())

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        send_probe(timeout_s=1)
