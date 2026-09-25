"""GitHub Copilot's refusals, in the words Copilot CLI 1.0.88 sent back.

A missing GitHub token is a bare ``Authentication required`` and is named by
the shared sign-in reading. The model call's own refusals come back as the
assistant message of a turn Copilot marks finished. Each sentence below is one
of those, from a stand-in provider. A reply that is not one of them stays a
reply.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.subagent import github_copilot
from raven.agent.subagent.probe_state import Remedy

_KEY_401 = (
    "Error: Authentication failed with provider at http://127.0.0.1:9 (HTTP 401).\n"
    "  Check your COPILOT_PROVIDER_API_KEY, COPILOT_PROVIDER_API_KEY_COMMAND, "
    "or COPILOT_PROVIDER_BEARER_TOKEN."
)
_KEY_403 = _KEY_401.replace("401", "403")
_CREDIT = "Error: 402 Your credit balance is too low. Please add credits."
_EXPIRED = "Error: 400 Your Copilot subscription has expired. Please renew to continue."
_MODEL = (
    "Error: Model 'not-a-model' not found on provider at http://127.0.0.1:9 (HTTP 404).\n"
    "  Check that the model is available on your provider."
)
_LOCAL = "Error: Could not connect to local model provider at http://127.0.0.1:9."
_TUNNEL = (
    "Error: Execution failed: Failed to get response from the AI model; retried 5 times "
    "Last error: Failed native model HTTP request: error sending request for url "
    "(https://api.githubcopilot.com/chat/completions): client error (Connect): tunnel error"
)
_SERVER = "Error: Failed to get response from the AI model; retried 5 times Last error: 500 Internal server error"
_GATEWAY = "Error: Failed to get response from the AI model; retried 5 times Last error: 502 Bad Gateway"


def _cfg(preset: str = "github_copilot") -> SimpleNamespace:
    return SimpleNamespace(name="GitHub Copilot", preset=preset, kind="acp", command="copilot --acp")


@pytest.mark.parametrize(
    ("said", "kind"),
    [
        (_KEY_401, "api_key"),
        (_KEY_403, "api_key"),
        (_CREDIT, "billing"),
        (_EXPIRED, "billing"),
        (_MODEL, "model"),
        (_LOCAL, "network"),
        (_TUNNEL, "network"),
    ],
)
def test_copilot_names_the_fix_its_own_sentence_asked_for(said: str, kind: str) -> None:
    found = github_copilot.read(said)
    assert found is not None
    text, remedy = found
    assert remedy is not None and remedy.kind == kind
    assert said in text
    assert "copilot login" not in text


@pytest.mark.parametrize("said", [_SERVER, _GATEWAY])
def test_a_provider_server_error_is_a_failure_without_a_guessed_fix(said: str) -> None:
    found = github_copilot.read(said)
    assert found is not None
    text, remedy = found
    assert remedy is None
    assert "server error" in text
    assert said in text


@pytest.mark.parametrize(
    "said",
    [
        "PONG",
        "Reply with exactly: PONG",
        "Authentication required",
        "the model has a context window of 404 tokens",
        "Rate limit reached. Please slow down.",
    ],
)
def test_a_reply_that_is_not_one_of_those_refusals_is_left_alone(said: str) -> None:
    assert github_copilot.read(said) is None


def test_a_bad_provider_key_is_not_told_to_sign_in() -> None:
    """The sentence says Authentication, which the shared rule reads as a sign-in.

    Copilot uses that word for a refused provider key and tells the reader which
    variable holds it. Sending them to `copilot login` does not change that key.
    """
    from raven.agent.subagent.probe import _refusal

    text, remedy = _refusal(_cfg(), _KEY_401, _KEY_401)
    assert remedy == Remedy("api_key")
    assert "copilot login" not in text
    assert "COPILOT_PROVIDER_API_KEY" in text


def test_a_bare_authentication_required_still_names_copilot_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import probe as probe_mod

    monkeypatch.setattr(probe_mod.shutil, "which", lambda exe, path=None: "/opt/homebrew/bin/copilot")
    text, remedy = probe_mod._refusal(_cfg(), "request failed: [-32000] Authentication required")
    assert remedy == Remedy("sign_in", "copilot login")
    assert "sign in with `copilot login`" in text


def test_another_agents_reply_is_not_read_as_copilots() -> None:
    assert github_copilot.applies(_cfg("qwen_code")) is False
    assert github_copilot.applies(_cfg("grok")) is False


async def test_a_finished_turn_whose_message_is_the_refusal_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect records a non-empty reply as success. Copilot's refusals are those replies."""
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return _CREDIT

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(_cfg())
    assert result.ok is False
    assert result.remedy == Remedy("billing")
    assert _CREDIT in result.detail


async def test_grok_is_not_read_with_copilots_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grok's refusals do not come back in these sentences, so they are not borrowed."""
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return _CREDIT

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(
        SimpleNamespace(name="Grok Build", preset="grok", kind="acp", command="grok agent stdio")
    )
    assert result.ok is True


async def test_an_ordinary_reply_is_still_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return "PONG"

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(_cfg())
    assert result.ok is True
    assert result.remedy is None
