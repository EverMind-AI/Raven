"""Unit tests for Raven-Design's full-body domain Skill selector."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from raven.memory_engine.skill_forge.visual_domain_selector import (
    VISUAL_DOMAIN_SKILL_NAMES,
    SkillCard,
    VisualDomainSkillSelector,
)
from raven.providers.base import LLMResponse
from raven.providers.binding import ModelBinding, use_binding


class _Provider:
    def __init__(self, content: str, *, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content=self.content, finish_reason=self.finish_reason)


class _Registry:
    def __init__(self, *, missing: str | None = None) -> None:
        self.metas = {
            name: SimpleNamespace(
                name=name,
                description=f"description for {name}",
                content=f"SECRET BODY FOR {name}",
            )
            for name in VISUAL_DOMAIN_SKILL_NAMES
            if name != missing
        }
        self.lookups: list[tuple[str, str | None]] = []

    def get(self, name: str, source: str | None = None):
        self.lookups.append((name, source))
        return self.metas.get(name)


def _cards(count: int = 4) -> list[SkillCard]:
    return [
        SkillCard(
            qualified_id=f"builtin/skill-{index}",
            name=f"skill-{index}",
            description=f"description {index}",
        )
        for index in range(count)
    ]


def _bodies(cards: list[SkillCard]) -> dict[str, str]:
    return {card.qualified_id: f"BODY FOR {card.name}" for card in cards}


async def test_selector_prompt_contains_all_descriptions_and_bodies() -> None:
    preferred = f"builtin/{VISUAL_DOMAIN_SKILL_NAMES[0]}"
    alternative = f"builtin/{VISUAL_DOMAIN_SKILL_NAMES[1]}"
    provider = _Provider(json.dumps({"preferred": [preferred], "alternatives": [alternative]}))
    registry = _Registry()
    selector = VisualDomainSkillSelector.from_registry(provider, registry)

    selection = await selector.select("build a brand identity")

    assert [card.qualified_id for card in selection.preferred] == [preferred]
    assert [card.qualified_id for card in selection.alternatives] == [alternative]
    assert registry.lookups == [(name, "builtin") for name in VISUAL_DOMAIN_SKILL_NAMES]
    messages = provider.calls[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[1]["content"] == "build a brand identity"
    system = messages[0]["content"]
    for name in VISUAL_DOMAIN_SKILL_NAMES:
        assert f"builtin/{name}" in system
        assert f"description for {name}" in system
        assert f"SECRET BODY FOR {name}" in system


async def test_selector_deduplicates_validates_and_caps_both_groups() -> None:
    cards = _cards()
    provider = _Provider(
        json.dumps(
            {
                "preferred": [
                    cards[0].qualified_id,
                    cards[0].qualified_id,
                    "builtin/unknown",
                    cards[1].qualified_id,
                    cards[2].qualified_id,
                ],
                "alternatives": [
                    cards[0].qualified_id,
                    cards[2].qualified_id,
                    cards[3].qualified_id,
                ],
            }
        )
    )
    selector = VisualDomainSkillSelector(
        provider,
        cards,
        bodies=_bodies(cards),
        preferred_max=2,
        alternatives_max=1,
    )

    selection = await selector.select("visual task")

    assert selection.preferred == (cards[0], cards[1])
    assert selection.alternatives == (cards[2],)
    assert selection.degraded is False


async def test_valid_empty_selection_exposes_nothing() -> None:
    provider = _Provider('{"preferred": [], "alternatives": []}')
    cards = _cards()
    selector = VisualDomainSkillSelector(provider, cards, bodies=_bodies(cards))

    selection = await selector.select("what time is it")

    assert selection.preferred == ()
    assert selection.alternatives == ()
    assert selection.degraded is False


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"preferred": ["builtin/unknown"], "alternatives": []}',
        '{"preferred": []}',
    ],
)
async def test_invalid_selector_output_degrades_to_full_alternative_catalog(content: str) -> None:
    cards = _cards()
    selector = VisualDomainSkillSelector(_Provider(content), cards, bodies=_bodies(cards))

    selection = await selector.select("visual task")

    assert selection.preferred == ()
    assert selection.alternatives == tuple(cards)
    assert selection.degraded is True


async def test_empty_query_does_not_spend_a_model_call() -> None:
    provider = _Provider('{"preferred": [], "alternatives": []}')
    cards = _cards()
    selector = VisualDomainSkillSelector(provider, cards, bodies=_bodies(cards))

    selection = await selector.select("   ")

    assert selection == selection.__class__((), ())
    assert provider.calls == []


async def test_selector_follows_the_active_turn_binding() -> None:
    fallback = _Provider('{"preferred": [], "alternatives": []}')
    active = _Provider('{"preferred": ["builtin/skill-0"], "alternatives": []}')
    cards = _cards()
    selector = VisualDomainSkillSelector(fallback, cards, bodies=_bodies(cards))

    with use_binding(ModelBinding(active, "active/model")):
        selection = await selector.select("visual task")

    assert selection.preferred == (cards[0],)
    assert fallback.calls == []
    assert active.calls[0]["model"] == "active/model"


def test_registry_construction_fails_when_a_packaged_skill_is_missing() -> None:
    provider = _Provider('{"preferred": [], "alternatives": []}')

    with pytest.raises(ValueError, match="missing packaged visual domain Skills"):
        VisualDomainSkillSelector.from_registry(
            provider,
            _Registry(missing=VISUAL_DOMAIN_SKILL_NAMES[-1]),
        )


def test_selector_rejects_a_catalog_with_an_empty_body() -> None:
    cards = _cards(1)

    with pytest.raises(ValueError, match="empty bodies"):
        VisualDomainSkillSelector(
            _Provider('{"preferred": [], "alternatives": []}'),
            cards,
            bodies={cards[0].qualified_id: ""},
        )


def test_blocklist_removes_a_skill_from_the_selector_catalog() -> None:
    blocked = VISUAL_DOMAIN_SKILL_NAMES[0]
    selector = VisualDomainSkillSelector.from_registry(
        _Provider('{"preferred": [], "alternatives": []}'),
        _Registry(),
        blocklist=[blocked.upper()],
    )

    assert len(selector.cards) == len(VISUAL_DOMAIN_SKILL_NAMES) - 1
    assert all(card.name != blocked for card in selector.cards)
