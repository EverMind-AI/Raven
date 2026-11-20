"""Runtime capability probes: the replay gate resolves the live path's spec."""

from __future__ import annotations

from raven.providers import capabilities


def test_replay_gates_resolve_the_provider_spec(monkeypatch) -> None:
    spec = object()
    calls: dict[str, tuple] = {}

    def fake_vision(model, spec_arg=None, provider=None):
        calls["vision"] = (model, spec_arg, provider)
        return True

    def fake_tool(provider, model, spec_arg=None):
        calls["tool"] = (provider, model, spec_arg)
        return True

    monkeypatch.setattr(capabilities, "supports_vision", fake_vision)
    monkeypatch.setattr(capabilities, "supports_image_tool_result", fake_tool)
    monkeypatch.setattr("raven.providers.registry.find_by_model", lambda model: spec)
    provider = object()

    assert capabilities.replay_gates(provider, "m") == (True, True)
    assert calls["vision"] == ("m", spec, provider)
    assert calls["tool"] == (provider, "m", spec)
