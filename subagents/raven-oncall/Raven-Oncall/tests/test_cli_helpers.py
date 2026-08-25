"""Unit tests for ``raven.cli._helpers``.

Currently focused on ``send_probe`` — the shared LLM probe used by
``onboard`` Step 3 and ``doctor --probe``. Provider and config are
stubbed so the test never touches network or disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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


def test_send_probe_success(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
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


def test_send_probe_provider_error_raises(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
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


def test_send_probe_timeout_raises(monkeypatch: pytest.MonkeyPatch, stub_load_config: None) -> None:
    """Slow provider trips ``asyncio.TimeoutError`` when ``timeout_s`` elapses."""

    class _SlowProvider:
        async def chat_with_retry(self, **_kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(finish_reason="stop", content="", usage=None)

    monkeypatch.setattr(_helpers, "make_provider", lambda _config: _SlowProvider())

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        send_probe(timeout_s=1)


# ---------------------------------------------------------------------------
# make_provider — custom routes through LiteLLM (so it gets streaming)
# ---------------------------------------------------------------------------


def test_make_provider_custom_routes_through_litellm(tmp_path: Path) -> None:
    from raven.config.loader import load_config
    from raven.providers.litellm_provider import LiteLLMProvider

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
                "providers": {"custom": {"apiKey": "sk-x", "apiBase": "http://localhost:9000/v1"}},
            }
        ),
        encoding="utf-8",
    )
    provider = _helpers.make_provider(load_config(p))
    assert isinstance(provider, LiteLLMProvider)


# ---------------------------------------------------------------------------
# check_provider_credentials — fail-fast without importing litellm
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, api_key: str | None) -> Path:
    provider: dict = {"apiBase": "http://localhost:9000/v1"}
    if api_key is not None:
        provider["apiKey"] = api_key
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "my-model", "provider": "custom"}},
                "providers": {"custom": provider},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_check_provider_credentials_exits_when_no_key(tmp_path: Path) -> None:
    import typer

    from raven.config.loader import load_config

    with pytest.raises(typer.Exit):
        _helpers.check_provider_credentials(load_config(_write_config(tmp_path, api_key=None)))


def test_check_provider_credentials_passes_with_key(tmp_path: Path) -> None:
    from raven.config.loader import load_config

    _helpers.check_provider_credentials(load_config(_write_config(tmp_path, api_key="sk-x")))  # no raise


def test_make_lazy_provider_returns_lazy_without_building(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``make_lazy_provider`` returns a LazyProvider that answers ``get_default_model``
    from config without building the real (litellm-importing) provider."""
    from raven.config.loader import load_config
    from raven.providers.lazy import LazyProvider

    # Stub the real build so prewarm never imports litellm.
    monkeypatch.setattr(_helpers, "make_provider", lambda _c: SimpleNamespace(name="real"))

    provider = _helpers.make_lazy_provider(load_config(_write_config(tmp_path, api_key="sk-x")))

    assert isinstance(provider, LazyProvider)
    assert provider.get_default_model() == "my-model"
