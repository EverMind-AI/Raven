"""Unit tests for the account catalogue that answers what Codex models exist."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from raven.providers import codex_catalog


@pytest.fixture(autouse=True)
def _no_cached_catalogue():
    codex_catalog.reset_cache()
    yield
    codex_catalog.reset_cache()


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "raven.providers.chatgpt_token.access_token_and_account",
        lambda: ("token", "acct"),
    )


def _serve(monkeypatch: pytest.MonkeyPatch, payload: Any, *, status: int = 200) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)

        return httpx.Response(status, json=payload)

    real_get = httpx.get

    def fake_get(url, **kwargs):
        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as client:
            return client.get(url, **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert real_get is not fake_get

    return seen


def test_only_the_models_meant_to_be_offered_come_back(monkeypatch: pytest.MonkeyPatch, signed_in) -> None:
    """The catalogue marks some entries hidden -- reachable, but not to be listed."""
    _serve(
        monkeypatch,
        {
            "models": [
                {"slug": "gpt-5.6-sol", "visibility": "list"},
                {"slug": "gpt-5.6-sol-wm", "visibility": "hide"},
                {"slug": "gpt-5.4", "visibility": "list"},
                {"slug": "codex-auto-review", "visibility": "hide"},
            ]
        },
    )

    assert codex_catalog.account_models() == ("gpt-5.6-sol", "gpt-5.4")


def test_the_client_version_is_sent(monkeypatch: pytest.MonkeyPatch, signed_in) -> None:
    """The value a live account was observed to answer for, pinned so a change to
    it is a decision rather than a typo."""
    seen = _serve(monkeypatch, {"models": [{"slug": "gpt-5.6-sol", "visibility": "list"}]})

    codex_catalog.account_models()

    assert seen[0].url.params["client_version"] == codex_catalog.CLIENT_VERSION


def test_the_answer_is_cached_between_refreshes(monkeypatch: pytest.MonkeyPatch, signed_in) -> None:
    """The picker rebuilds its list on every refresh; entitlements do not change
    between keystrokes."""
    seen = _serve(monkeypatch, {"models": [{"slug": "gpt-5.6-sol", "visibility": "list"}]})

    assert codex_catalog.account_models() == ("gpt-5.6-sol",)
    assert codex_catalog.account_models() == ("gpt-5.6-sol",)

    assert len(seen) == 1


def test_a_provider_list_still_renders_when_the_catalogue_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
    signed_in,
) -> None:
    def boom(url, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)

    assert codex_catalog.account_models() == ()


def test_being_offline_is_asked_about_once_not_once_per_refresh(
    monkeypatch: pytest.MonkeyPatch,
    signed_in,
) -> None:
    """Every picker refresh rebuilds the candidate list. Without a failure cache
    each one waits out the full timeout again, on a machine that has already been
    told there is no network."""
    attempts = 0

    def boom(url, **kwargs):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)

    assert codex_catalog.account_models() == ()
    assert codex_catalog.account_models() == ()
    assert codex_catalog.account_models() == ()

    assert attempts == 1


def test_a_sign_in_is_not_made_to_wait_out_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    signed_in,
) -> None:
    """``reset_cache`` is what ``provider test`` and a fresh sign-in call: a cached
    failure must not outlive the reason it was cached."""

    def boom(url, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", boom)
    assert codex_catalog.account_models() == ()

    codex_catalog.reset_cache()
    _serve(monkeypatch, {"models": [{"slug": "gpt-5.6-sol", "visibility": "list"}]})

    assert codex_catalog.account_models() == ("gpt-5.6-sol",)


def test_not_being_signed_in_is_an_empty_catalogue(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "empty"))

    assert codex_catalog.account_models() == ()
