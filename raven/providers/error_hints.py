"""What a failed model call asks the reader to do, said the way the surface can.

The category of a failure is decided once, in :mod:`raven.providers.base`; what
the reader should do about it depends on who the reader is. A terminal can be
handed a command to run. A chat reader cannot: they hold no credentials, and
the operator is the one who fixes them. So the knowledge lives here, and each
surface asks for the wording it can honour.
"""

from __future__ import annotations

#: The advice a terminal user can act on. Credential guidance would mislead
#: under most of these: a rate limit or a network drop is not fixed by
#: re-checking the key. Categories absent from this map (invalid_request,
#: unknown, ...) have no apt one-liner and render the error line alone.
_CLI_HINTS = {
    "rate_limit": "the provider is rate limiting; retry in a moment.",
    "network": "network problem; check connectivity and the provider apiBase, then retry.",
    "context_overflow": "the input exceeds the model's context window; shorten it.",
    "server": "provider-side error; retry later or switch models.",
    "model_unavailable": "model not served; pick another with raven provider use <name>/<model>.",
    "billing": "billing or quota issue; check your provider account.",
}

#: The advice a chat reader can act on. The same categories, worded for
#: someone in the room rather than at a terminal: no command to run, and the
#: credential case points at the operator instead of at a key the reader has
#: never seen.
_CHANNEL_HINTS = {
    "auth": "The operator can check the provider key.",
    "billing": "The provider account needs attention.",
    "context_overflow": "A shorter message may fit the model.",
    "model_unavailable": "The model is not being served right now.",
    "network": "A retry may work.",
    "rate_limit": "Try again in a moment.",
    "server": "A retry later may work.",
}

#: The advice a TUI reader can act on. The chat's advice, with the one
#: difference the screen makes: the credential case points at the `/model`
#: picker, where a key can be set -- or a saved one replaced after its provider
#: is disconnected -- rather than at a command nobody there can run or a
#: settings screen the TUI does not have.
_TUI_HINTS = {
    **_CHANNEL_HINTS,
    "auth": "Run /model to check the provider key (disconnect first if it is already saved).",
}


def provider_error_hint(category: str, provider: str | None = None, *, surface: str) -> str:
    """The line that tells a reader what to do about ``category``.

    ``surface`` picks the wording, and there is no default: a surface that
    does not say which reader it is addressing has not said what it may
    promise. An empty string means the category has no apt advice, and the
    caller renders the diagnosis alone.
    """
    if surface == "cli":
        if category == "auth":
            return f"Fix: raven provider test {provider or '<name>'}  or  raven onboard"
        body = _CLI_HINTS.get(category)
        return f"Hint: {body}" if body else ""
    if surface == "channel":
        return _CHANNEL_HINTS.get(category, "")
    if surface == "tui":
        return _TUI_HINTS.get(category, "")
    raise ValueError(f"unknown surface: {surface!r}")
