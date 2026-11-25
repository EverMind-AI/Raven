"""The hint a provider error carries, said the way the surface can.

The knowledge lives in one module so a second surface does not have to guess
what a category means, and the two surfaces are held apart on purpose: a
terminal may be handed a command, a chat reader may not.
"""

import re
from pathlib import Path

import pytest

from raven.providers.error_hints import provider_error_hint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TUI_COMMANDS = _REPO_ROOT / "ui-tui" / "src" / "app" / "slash" / "commands"
_SLASH_COMMAND = re.compile(r"/[a-z][a-z0-9-]*")
_COMMAND_NAME = re.compile(r"\bname:\s*'([^']+)'")


def test_the_cli_is_handed_a_command_for_a_rejected_key() -> None:
    """The auth case is the one a terminal can act on alone: the credential
    is wrong, so the fix is to test the provider or run the onboarding."""
    assert (
        provider_error_hint("auth", "openrouter", surface="cli")
        == "Fix: raven provider test openrouter  or  raven onboard"
    )
    assert provider_error_hint("auth", None, surface="cli") == "Fix: raven provider test <name>  or  raven onboard"


def test_the_cli_gets_an_apt_hint_and_no_key_guidance_for_the_rest() -> None:
    """A rate limit or a network drop is not fixed by re-checking the key, so
    those categories get their own advice; the ones with no apt one-liner are
    left with the diagnosis alone."""
    cases = {
        "rate_limit": "the provider is rate limiting; retry in a moment.",
        "network": "network problem; check connectivity and the provider apiBase, then retry.",
        "context_overflow": "the input exceeds the model's context window; shorten it.",
        "server": "provider-side error; retry later or switch models.",
        "model_unavailable": "model not served; pick another with raven provider use <name>/<model>.",
        "billing": "billing or quota issue; check your provider account.",
        "protocol_mismatch": "the endpoint does not speak this provider's protocol; check its protocol and base URL.",
    }
    for category, body in cases.items():
        assert provider_error_hint(category, "openrouter", surface="cli") == f"Hint: {body}"

    assert provider_error_hint("unknown", "openrouter", surface="cli") == ""
    assert provider_error_hint("invalid_request", "openrouter", surface="cli") == ""


def test_the_channel_is_handed_something_it_can_act_on_without_a_command() -> None:
    """A chat reader holds no credentials and cannot run anything, so the
    channel wording carries no command and points the credential case at the
    operator instead."""
    assert provider_error_hint("auth", "openrouter", surface="channel") == "The operator can check the provider key."
    assert provider_error_hint("rate_limit", "openrouter", surface="channel") == "Try again in a moment."

    for category in (
        "auth",
        "billing",
        "context_overflow",
        "model_unavailable",
        "network",
        "rate_limit",
        "server",
        "protocol_mismatch",
    ):
        hint = provider_error_hint(category, "openrouter", surface="channel")
        assert hint
        assert "raven " not in hint, category

    assert (
        provider_error_hint("protocol_mismatch", "openrouter", surface="channel")
        == "The operator can check that provider's protocol and base URL."
    )

    assert provider_error_hint("unknown", "openrouter", surface="channel") == ""


def test_an_unknown_surface_is_refused_rather_than_guessed() -> None:
    """A surface that does not say which reader it addresses has not said what
    it may promise, so it gets no hint at all."""
    with pytest.raises(ValueError):
        provider_error_hint("auth", "openrouter", surface="log")


def test_the_tui_is_pointed_at_the_picker_it_owns() -> None:
    """The TUI sets the provider's key through `/model`, and a key already on
    file has to be disconnected there before it can be replaced, so the
    credential case names both steps rather than a command nobody there can
    run; every other category reads as it does in a chat."""
    assert (
        provider_error_hint("auth", "openrouter", surface="tui")
        == "Run /model to check the provider key (disconnect first if it is already saved)."
    )
    for category in (
        "billing",
        "context_overflow",
        "model_unavailable",
        "network",
        "rate_limit",
        "server",
        "protocol_mismatch",
    ):
        assert provider_error_hint(category, "openrouter", surface="tui") == provider_error_hint(
            category, "openrouter", surface="channel"
        )

    assert provider_error_hint("unknown", "openrouter", surface="tui") == ""


def test_the_command_the_tui_hint_names_is_one_the_tui_registers() -> None:
    """The hint and the TUI's slash command table are two sources with nothing
    shared between them: renaming or removing `/model` in the TUI would leave
    the line pointing at a command the screen no longer has, and no other test
    would fail. This is a denotation check, not a shared definition."""
    named = set(_SLASH_COMMAND.findall(provider_error_hint("auth", "openrouter", surface="tui")))
    assert named, "the TUI auth hint no longer names a slash command"
    sources = sorted(_TUI_COMMANDS.glob("*.ts"))
    assert sources, f"no slash command sources under {_TUI_COMMANDS}"
    registered = {f"/{name}" for path in sources for name in _COMMAND_NAME.findall(path.read_text(encoding="utf-8"))}
    assert named <= registered, f"the hint names {sorted(named - registered)}, which the TUI does not register"


def test_a_protocol_mismatch_never_offers_a_retry_on_any_surface() -> None:
    """An endpoint that does not speak the provider's protocol is a fixed
    configuration, not a transient fault: the same request a minute later is
    answered the same way. The verdict had landed under ``network`` before the
    category existed, which told a chat reader "A retry may work." -- advice
    that spends a turn and changes nothing. Every surface now names the two
    settings the operator has to look at instead, and none of them asks for a
    retry."""
    for surface in ("cli", "channel", "tui"):
        hint = provider_error_hint("protocol_mismatch", "zai", surface=surface)
        assert hint, surface
        assert "retry" not in hint.lower(), surface
        assert "protocol and base URL" in hint, surface

    # The control: the category this one used to be filed under still does.
    assert "retry" in provider_error_hint("network", "zai", surface="channel").lower()
