"""autonomyMode drives the ask-vs-act prompt text and subagent announce tail."""

from __future__ import annotations

import json

import pytest

from raven.config.live import LiveConfig, autonomy_mode
from raven.config.schema import AgentDefaults
from raven.context_engine.segments import render


def test_autonomy_schema_default_and_validation():
    assert AgentDefaults().autonomy_mode == "balanced"
    assert AgentDefaults.model_validate({"autonomyMode": "auto"}).autonomy_mode == "auto"
    for ok in ("manual", "balanced", "auto"):
        assert AgentDefaults(autonomy_mode=ok).autonomy_mode == ok
    with pytest.raises(ValueError):
        AgentDefaults(autonomy_mode="reckless")


def test_live_autonomy_mode_follows_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    live = LiveConfig(cfg)
    cfg.write_text(json.dumps({"agents": {"defaults": {"autonomyMode": "manual"}}}), encoding="utf-8")
    assert autonomy_mode(live) == "manual"
    cfg.write_text(json.dumps({"agents": {"defaults": {"autonomyMode": "auto"}}}), encoding="utf-8")
    assert autonomy_mode(live) == "auto"
    cfg.write_text(json.dumps({"agents": {"defaults": {"autonomyMode": "bogus"}}}), encoding="utf-8")
    assert autonomy_mode(live) == "balanced"


@pytest.mark.parametrize("mode,marker", [("manual", "confirm-first"), ("balanced", "escalation ladder"), ("auto", "end-to-end")])
def test_render_guideline_differs_per_mode(monkeypatch, mode, marker):
    monkeypatch.setattr(render, "_autonomy_mode", lambda: mode)
    assert marker in render.autonomy_guideline()
    others = [m for m in render.AUTONOMY_GUIDELINES if m != mode]
    assert all(render.AUTONOMY_GUIDELINES[o] != render.autonomy_guideline() for o in others)


@pytest.mark.parametrize("mode,marker", [("manual", "confirm-first"), ("balanced", "escalation ladder"), ("auto", "end-to-end")])
async def test_manager_tail_uses_mode(tmp_path, monkeypatch, mode, marker):
    from raven.agent.subagent.manager import SubagentManager

    class _StubProvider:
        def get_default_model(self) -> str:
            return "stub-model"

    mgr = SubagentManager(provider=_StubProvider(), workspace=tmp_path, max_concurrent=1)
    submitted = []
    mgr.set_submit(submitted.append)
    monkeypatch.setattr(render, "_autonomy_mode", lambda: mode)

    origin = {"channel": "cli", "chat_id": "c1", "session_key": "cli:c1"}
    await mgr._announce_result("abcd1234", "Research", "do it", "done", origin, "ok")

    text = submitted[0].text
    assert render.AUTONOMY_GUIDELINES[mode] in text
    assert marker in text
    assert text.index("[END UNTRUSTED subagent") < text.index(render.AUTONOMY_GUIDELINES[mode])
    for other, guideline in render.AUTONOMY_GUIDELINES.items():
        if other != mode:
            assert guideline not in text


def test_self_surface_exposes_autonomy_mode():
    from raven.config import self_surface as surface

    autonomy = surface.find("agents.defaults.autonomyMode")
    assert autonomy is not None
    setting_auto, bound_auto = autonomy
    assert bound_auto == []
    assert setting_auto.kind == "enum"
    assert setting_auto.choices == ("manual", "balanced", "auto")
    assert setting_auto.effect is surface.Effect.NEXT_TURN
    assert surface.default_of("agents.defaults.autonomyMode") == "balanced"
    for ok in ("manual", "balanced", "auto"):
        assert surface.check_value(setting_auto, ok) == ok
    with pytest.raises(ValueError):
        surface.check_value(setting_auto, "reckless")