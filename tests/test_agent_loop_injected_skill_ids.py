"""Unit tests for ``AgentLoop._collect_injected_skill_ids``.

Verifies the helper builds the de-duplicated injected-skill id list
from selector top-K + always-skills, in selector-first order.

Avoids constructing a real :class:`AgentLoop` (which would require a
full LLM provider, sandbox, bus, etc.) by binding the helper to a
minimal stand-in object that exposes ``self.context.skills``.
"""

from __future__ import annotations

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
        fake_self,
        [_meta("alpha")],
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
        fake_self,
        [_meta("alpha")],
    )
    assert out == []


# ---------------------------------------------------------------------------
# Skill reporting to the web UI's skill panel
# ---------------------------------------------------------------------------


class _Sink:
    """Records what the skills sink was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, conversation: str, name: str, payload: dict) -> None:
        self.calls.append((conversation, name, payload))


class _Registry:
    """Registry stand-in keyed by native name."""

    def __init__(self, by_name: dict[str, str]) -> None:
        self._by_name = by_name

    def get(self, name: str, source: str | None = None) -> SimpleNamespace | None:
        del source
        found = self._by_name.get(name)
        return SimpleNamespace(name=name, source=found) if found else None


def _reporter(
    sink: _Sink | None,
    *,
    ids: list[str] | None = None,
    sources: dict[str, str] | None = None,
    registry: _Registry | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        _skills_sink=sink,
        _last_injected_skill_ids=ids,
        _last_injected_skill_sources=sources or {},
        context=SimpleNamespace(skills=SimpleNamespace(registry=registry)),
        _emit_skills=None,
    )


async def _emit_injected(fake_self: SimpleNamespace) -> None:
    """Bind both helpers to the stand-in: ``_emit_injected_skills`` calls
    ``_emit_skills``, which is a real method on the class."""
    fake_self._emit_skills = lambda key, skills: AgentLoop._emit_skills(fake_self, key, skills)
    await AgentLoop._emit_injected_skills(fake_self, "web:s1")


async def test_injected_reports_registry_source_not_the_id_prefix() -> None:
    """Every on-disk skill is addressed as ``local/``, so the prefix cannot
    say where it came from. The panel must show the registry source."""
    sink = _Sink()
    fake_self = _reporter(
        sink,
        ids=["local/subagent-dag-orchestration"],
        sources={"local/subagent-dag-orchestration": "builtin"},
    )
    await _emit_injected(fake_self)
    assert sink.calls[0][1] == "skills_injected"
    assert sink.calls[0][2]["skills"] == [
        {
            "id": "local/subagent-dag-orchestration",
            "source": "builtin",
            "name": "subagent-dag-orchestration",
            "kind": "injected",
        }
    ]


async def test_injected_falls_back_to_the_prefix_without_a_source_map() -> None:
    sink = _Sink()
    fake_self = _reporter(sink, ids=["hub/echo"])
    await _emit_injected(fake_self)
    assert sink.calls[0][2]["skills"][0]["source"] == "hub"


async def test_injected_is_a_noop_without_a_sink_or_ids() -> None:
    await _emit_injected(_reporter(None, ids=["local/x"]))  # no sink: must not raise
    sink = _Sink()
    await _emit_injected(_reporter(sink, ids=[]))
    assert sink.calls == []


async def test_read_skill_is_reported_with_its_real_source() -> None:
    """A skill the model loads itself never passes through injection; the
    panel would otherwise show nothing for the turn that used it."""
    sink = _Sink()
    fake_self = _reporter(
        sink,
        registry=_Registry({"subagent-dag-orchestration": "builtin"}),
    )
    fake_self._emit_skills = lambda key, skills: AgentLoop._emit_skills(fake_self, key, skills)
    await AgentLoop._report_skill_read(
        fake_self,
        "web:s1",
        "read_skill",
        {"skill_id": "local/subagent-dag-orchestration"},
    )
    assert sink.calls[0][2]["skills"] == [
        {
            "id": "local/subagent-dag-orchestration",
            "source": "builtin",
            "name": "subagent-dag-orchestration",
            "kind": "read_skill",
        }
    ]


async def test_read_skill_reports_even_when_the_registry_cannot_resolve() -> None:
    """The call happened; an unresolvable id keeps the prefix as source
    rather than dropping the report."""
    sink = _Sink()
    fake_self = _reporter(sink, registry=_Registry({}))
    fake_self._emit_skills = lambda key, skills: AgentLoop._emit_skills(fake_self, key, skills)
    await AgentLoop._report_skill_read(fake_self, "web:s1", "use_skill", {"skill_id": "hub/echo"})
    entry = sink.calls[0][2]["skills"][0]
    assert entry["source"] == "hub"
    assert entry["kind"] == "use_skill"


async def test_read_skill_ignores_a_missing_skill_id() -> None:
    sink = _Sink()
    fake_self = _reporter(sink, registry=_Registry({}))
    await AgentLoop._report_skill_read(fake_self, "web:s1", "read_skill", {})
    assert sink.calls == []


async def test_read_skill_resolves_a_bare_id_the_way_read_skill_itself_does() -> None:
    """``read_skill`` treats an id with no ``<source>/`` prefix as a Hub id.
    Splitting it any other way makes the panel badge the skill's own name as
    its source."""
    sink = _Sink()
    fake_self = _reporter(sink, registry=_Registry({}))
    fake_self._emit_skills = lambda key, skills: AgentLoop._emit_skills(fake_self, key, skills)
    await AgentLoop._report_skill_read(fake_self, "web:s1", "read_skill", {"skill_id": "orchestration-guide"})
    assert sink.calls[0][2]["skills"] == [
        {
            "id": "orchestration-guide",
            "source": "hub",
            "name": "orchestration-guide",
            "kind": "read_skill",
        }
    ]
