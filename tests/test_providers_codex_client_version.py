"""Unit tests for the Codex CLI release a generation request names."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from raven.providers import codex_client_version

#: Captured at import, before the suite's autouse fixture replaces it.
_REAL_FETCH = codex_client_version._fetch_latest


def _serve(monkeypatch: pytest.MonkeyPatch, payload: Any = None, *, error: Exception | None = None) -> list[str]:
    """Put back the real fetch the suite stubs out, over a mocked transport."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if error is not None:
            raise error

        return httpx.Response(200, json=payload)

    def fake_get(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.get(url, **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(codex_client_version, "_fetch_latest", _REAL_FETCH)

    return seen


def _remembered() -> object:
    try:
        return json.loads(codex_client_version.cache_path().read_text(encoding="utf-8")).get("version")
    except OSError:
        return None


def test_the_latest_tag_is_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {"latest": "0.170.0", "alpha": "0.171.0-alpha.1"})

    assert codex_client_version.resolve() == "0.170.0"
    assert seen == [codex_client_version.DIST_TAGS_URL]
    assert _remembered() == "0.170.0"


def test_the_answer_is_not_asked_for_again_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {"latest": "0.170.0"})

    codex_client_version.resolve()
    codex_client_version.resolve()

    assert len(seen) == 1


@pytest.mark.parametrize(
    "latest",
    ["0.164.0-alpha.3", "", None, "v0.170.0", "0.170", "0.0.0", "0.150.0"],
    ids=["prerelease", "empty", "missing", "prefixed", "two-part", "zero", "older-than-ours"],
)
def test_a_tag_that_is_not_a_usable_release_falls_back(monkeypatch: pytest.MonkeyPatch, latest: object) -> None:
    """A prerelease is not a client anyone runs, and nothing older than the bundled
    release may be sent -- `0.0.0` least of all."""
    _serve(monkeypatch, {"latest": latest, "alpha": "0.164.0-alpha.3"})

    assert codex_client_version.resolve() == codex_client_version.FALLBACK_VERSION
    assert _remembered() is None


def test_offline_answers_the_release_last_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"latest": "0.170.0"})
    codex_client_version.resolve()
    codex_client_version.reset_cache()

    _serve(monkeypatch, error=httpx.ConnectError("offline"))

    assert codex_client_version.resolve() == "0.170.0"


def test_a_remembered_release_is_checked_like_a_fetched_one(monkeypatch: pytest.MonkeyPatch) -> None:
    path = codex_client_version.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "0.0.0"}), encoding="utf-8")
    _serve(monkeypatch, error=httpx.ConnectError("offline"))

    assert codex_client_version.resolve() == codex_client_version.FALLBACK_VERSION


def test_offline_with_nothing_remembered_answers_the_bundled_release(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, error=httpx.ConnectError("offline"))

    assert codex_client_version.resolve() == codex_client_version.FALLBACK_VERSION


@pytest.mark.parametrize(
    "payload",
    [["0.170.0"], "0.170.0", {"next": "0.170.0"}],
    ids=["list", "string", "no-latest"],
)
def test_an_unexpected_answer_falls_back_quietly(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    _serve(monkeypatch, payload)

    assert codex_client_version.resolve() == codex_client_version.FALLBACK_VERSION


def test_an_error_status_falls_back_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, **kwargs):
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(codex_client_version, "_fetch_latest", _REAL_FETCH)

    assert codex_client_version.resolve() == codex_client_version.FALLBACK_VERSION


def test_the_bundled_release_is_a_plain_release_past_the_known_gate() -> None:
    """`gpt-6.1-sol` is served from 0.159.0 (#986); the floor must not sit below it."""
    parts = tuple(int(p) for p in codex_client_version.FALLBACK_VERSION.split("."))

    assert len(parts) == 3
    assert parts >= (0, 159, 0)
