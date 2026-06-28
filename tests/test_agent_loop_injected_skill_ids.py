"""Unit tests for ``AgentLoop._collect_injected_skill_ids``.

Verifies the helper builds the de-duplicated injected-skill id list
from selector top-K + always-skills, in selector-first order.

Avoids constructing a real :class:`AgentLoop` (which would require a
full LLM provider, sandbox, bus, etc.) by binding the helper to a
minimal stand-in object that exposes ``self.context.skills``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from raven.agent.loop import AgentLoop


class _FakeSkills:
    def __init__(self, always: list[object]) -> None:
        self._always = always

    def get_always_skills(self) -> list[object]:
        return self._always


def _meta(name: str, source: str = "workspace") -> SimpleNamespace:
    """Minimal SkillMeta-shaped duck typing for the helper."""
    return SimpleNamespace(
        id=f"{source}/{name}",
        name=name,
        source=source,
    )


def _bind(selector_metas: list[object] | None, always_metas: list[object]):
    """Construct a stand-in self and invoke the helper as an unbound method.

    Phase A added a ``_last_injected_skill_ids`` instance attribute that
    ``_collect_injected_skill_ids`` consults before falling back to the
    selector-meta canonicalization path. The mock self must expose it
    (as ``None``) so the fallback path triggers — these legacy tests
    exercise the SkillMeta-based canonicalization, not the new
    metadata-stash path.
    """
    fake_self = SimpleNamespace(
        context=SimpleNamespace(skills=_FakeSkills(always_metas)),
        _last_injected_skill_ids=None,
    )
    return AgentLoop._collect_injected_skill_ids(fake_self, selector_metas)


def test_collect_returns_empty_when_no_selector_no_always() -> None:
    assert _bind(None, []) == []
    assert _bind([], []) == []


def test_collect_lists_selected_in_order() -> None:
    out = _bind(
        selector_metas=[_meta("alpha"), _meta("bravo"), _meta("charlie")],
        always_metas=[],
    )
    assert out == ["workspace/alpha", "workspace/bravo", "workspace/charlie"]


def test_collect_appends_always_after_selected() -> None:
    out = _bind(
        selector_metas=[_meta("alpha")],
        always_metas=[_meta("safety", source="builtin")],
    )
    assert out == ["workspace/alpha", "builtin/safety"]


def test_collect_dedupes_when_selected_overlaps_always() -> None:
    """A skill that appears as both selector hit AND always-skill must
    only appear once, in selector position."""
    overlap = _meta("safety", source="builtin")
    out = _bind(
        selector_metas=[overlap, _meta("alpha")],
        always_metas=[overlap],
    )
    assert out == ["builtin/safety", "workspace/alpha"]


def test_collect_skips_metas_without_id() -> None:
    """Defensive: a malformed SkillMeta-like object without ``id`` is
    silently dropped rather than crashing the agent loop."""
    broken = SimpleNamespace(name="broken")  # no .id attribute
    out = _bind(
        selector_metas=[broken, _meta("alpha")],
        always_metas=[],
    )
    assert out == ["workspace/alpha"]


def test_collect_swallows_get_always_skills_exception() -> None:
    """If get_always_skills raises (e.g. corrupted SqliteStore), the
    helper falls back to selector-only — the agent loop must not crash
    on a telemetry path."""
    class _Boom:
        def get_always_skills(self) -> list[object]:
            raise RuntimeError("boom")

    fake_self = SimpleNamespace(
        context=SimpleNamespace(skills=_Boom()),
        _last_injected_skill_ids=None,
    )
    out = AgentLoop._collect_injected_skill_ids(
        fake_self, [_meta("alpha")],
    )
    assert out == ["workspace/alpha"]


def test_collect_returns_empty_when_no_skill_service() -> None:
    """When SkillService isn't wired (rare) return empty list, don't
    raise."""
    fake_self = SimpleNamespace(
        context=SimpleNamespace(skills=None),
        _last_injected_skill_ids=None,
    )
    out = AgentLoop._collect_injected_skill_ids(
        fake_self, [_meta("alpha")],
    )
    assert out == []
