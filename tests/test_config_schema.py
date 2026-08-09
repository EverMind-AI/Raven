"""Tests for ``raven.config.schema.AgentDefaults.context_window_tokens``.

None (or 0) means "figure it out" against the model's real window; a positive
value pins it. See ``raven.providers.rates.effective_context_window`` for the
ladder that reads this field.
"""

from __future__ import annotations

from raven.config.schema import AgentDefaults


def test_context_window_tokens_defaults_to_none() -> None:
    assert AgentDefaults().context_window_tokens is None


def test_context_window_tokens_explicit_value_round_trips() -> None:
    defaults = AgentDefaults(context_window_tokens=200_000)
    assert defaults.context_window_tokens == 200_000


def test_context_window_tokens_camel_alias_round_trips() -> None:
    defaults = AgentDefaults.model_validate({"contextWindowTokens": 200_000})
    assert defaults.context_window_tokens == 200_000
    assert defaults.model_dump(by_alias=True)["contextWindowTokens"] == 200_000
