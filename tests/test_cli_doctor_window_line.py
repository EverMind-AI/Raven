"""``raven doctor``'s Context win line says where the number came from.

Separate from ``test_cli_doctor_commands.py`` because that module's autouse
fixture needs the everos distribution; this line needs nothing but the
resolver. 2026-09-11: ``auto`` alone read the same for a model whose window the
catalogue held and for one no catalogue knew, and the second was being sized
against a 65,536 default.
"""

from __future__ import annotations

from types import SimpleNamespace

from raven.cli.doctor_commands import _describe_window
from raven.providers import rates


def _blind(monkeypatch):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {})
    monkeypatch.setattr(litellm, "get_model_info", lambda _m: (_ for _ in ()).throw(Exception("unmapped")))


def test_a_pinned_window_is_shown_as_pinned():
    line = _describe_window(SimpleNamespace(context_window_tokens=1_048_576, model="any/model"))
    assert line == "1,048,576 (pinned)"


def test_a_catalogued_window_names_the_catalogue(monkeypatch):
    monkeypatch.setattr(rates, "resolve_context_window", lambda m, **_kw: 1_048_576)
    line = _describe_window(SimpleNamespace(context_window_tokens=None, model="deepseek/deepseek-flash"))
    assert line == "auto (1,048,576 from the catalogue)"


def test_an_unknown_model_shows_the_default_it_fell_back_to(monkeypatch):
    _blind(monkeypatch)
    line = _describe_window(SimpleNamespace(context_window_tokens=None, model="nobody/unmapped-model"))
    assert line.startswith("auto -> 65,536 default")
    assert "no catalogue knows this model" in line
