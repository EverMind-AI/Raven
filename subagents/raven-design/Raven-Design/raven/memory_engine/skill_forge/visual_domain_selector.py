"""Full-body routing over Raven-Design's fixed visual domain Skills."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from raven.providers.binding import ModelBinding, active_binding
from raven.skill_hub.policy import is_blocked, normalize_blocklist

if TYPE_CHECKING:
    from raven.memory_engine.skill_local.registry import SkillRegistry
    from raven.providers.base import LLMProvider

log = logging.getLogger(__name__)

VISUAL_DOMAIN_SKILL_NAMES: tuple[str, ...] = (
    "design-brand-identities",
    "design-icons-and-symbols",
    "design-typefaces-and-lettering",
    "create-illustrations-and-scenes",
    "create-marketing-graphics",
    "design-editorial-and-presentations",
    "create-data-visualizations",
    "create-technical-diagrams",
    "create-maps-and-spatial-plans",
    "build-ui-components-and-systems",
    "build-content-websites",
    "build-product-and-tool-interfaces",
    "build-interactive-explainers",
    "build-games-and-playful-experiences",
    "build-polished-visual-frontends",
)

_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class SkillCard:
    qualified_id: str
    name: str
    description: str


@dataclass(frozen=True)
class VisualDomainSelection:
    preferred: tuple[SkillCard, ...]
    alternatives: tuple[SkillCard, ...]
    degraded: bool = False


class VisualDomainSkillSelector:
    """Classify a query by comparing every fixed domain Skill body."""

    def __init__(
        self,
        provider: "LLMProvider",
        cards: Iterable[SkillCard],
        *,
        bodies: Mapping[str, str],
        preferred_max: int = 2,
        alternatives_max: int = 3,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        pin: ModelBinding | None = None,
    ) -> None:
        self._fallback_provider = provider
        self._cards = tuple(cards)
        self._by_id = {card.qualified_id: card for card in self._cards}
        self._bodies = {
            card.qualified_id: (bodies.get(card.qualified_id) or "").strip()
            for card in self._cards
        }
        empty_bodies = [skill_id for skill_id, body in self._bodies.items() if not body]
        if empty_bodies:
            raise ValueError(f"visual domain Skills have empty bodies: {', '.join(empty_bodies)}")
        self._preferred_max = max(0, preferred_max)
        self._alternatives_max = max(0, alternatives_max)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._pin = pin
        self._pin_warned = False
        self._system_prompt = self._build_system_prompt()

    @classmethod
    def from_registry(
        cls,
        provider: "LLMProvider",
        registry: "SkillRegistry",
        *,
        preferred_max: int = 2,
        alternatives_max: int = 3,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        pin: ModelBinding | None = None,
        blocklist: Iterable[str] | None = None,
    ) -> "VisualDomainSkillSelector":
        blocked = normalize_blocklist(blocklist)
        cards: list[SkillCard] = []
        bodies: dict[str, str] = {}
        missing: list[str] = []
        for name in VISUAL_DOMAIN_SKILL_NAMES:
            meta = registry.get(name, source="builtin")
            if meta is None:
                missing.append(name)
                continue
            if is_blocked(blocked, name):
                continue
            qualified_id = f"builtin/{name}"
            cards.append(
                SkillCard(
                    qualified_id=qualified_id,
                    name=name,
                    description=" ".join((meta.description or name).split()),
                )
            )
            bodies[qualified_id] = meta.content
        if missing:
            raise ValueError(f"missing packaged visual domain Skills: {', '.join(missing)}")
        return cls(
            provider,
            cards,
            bodies=bodies,
            preferred_max=preferred_max,
            alternatives_max=alternatives_max,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            pin=pin,
        )

    @property
    def cards(self) -> tuple[SkillCard, ...]:
        return self._cards

    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        del model
        self._fallback_provider = provider

    async def select(self, query: str) -> VisualDomainSelection:
        query = (query or "").strip()
        if not query or not self._cards:
            return VisualDomainSelection((), ())

        provider, model = self._binding()
        try:
            response = await asyncio.wait_for(
                provider.chat_with_retry(
                    messages=self._messages(query),
                    model=model,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                ),
                timeout=_TIMEOUT_S,
            )
            content = response.content or ""
            if getattr(response, "finish_reason", None) == "error":
                raise RuntimeError(content or "provider error")
            preferred_ids, alternative_ids = self._parse_response(content)
            preferred, alternatives = self._resolve(preferred_ids, alternative_ids)
        except Exception as exc:
            log.warning(
                "Visual Domain Selector failed (%s); exposing the description catalog as alternatives",
                exc,
            )
            return VisualDomainSelection((), self._cards, degraded=True)

        log.info(
            "Visual Domain Selector: candidates=%d preferred=%s alternatives=%s",
            len(self._cards),
            [card.name for card in preferred],
            [card.name for card in alternatives],
        )
        return VisualDomainSelection(tuple(preferred), tuple(alternatives))

    def _binding(self) -> tuple["LLMProvider", str | None]:
        if self._model and self._pin is None and not self._pin_warned:
            self._pin_warned = True
            log.warning(
                "skill_forge.visual_domain_selector.model=%r has no usable credentials of its own; "
                "the selector follows the conversation's model instead",
                self._model,
            )
        if self._pin is not None:
            return self._pin.provider, self._pin.model
        turn = active_binding()
        if turn is not None:
            return turn.provider, turn.model
        return self._fallback_provider, None

    def _messages(self, query: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": query},
        ]

    def _build_system_prompt(self) -> str:
        catalog = [
            {
                "id": card.qualified_id,
                "name": card.name,
                "description": card.description,
                "skill_md": self._bodies[card.qualified_id],
            }
            for card in self._cards
        ]
        return (
            "You are Raven-Design's Visual Domain Selector. Classify the user query by comparing "
            "the complete packaged domain catalog below, including each full SKILL.md body. Treat the "
            "bodies as candidate specifications to compare, not procedures to execute in this call.\n\n"
            "Preferred Skills directly own a core part of the requested deliverable. Alternative Skills "
            "are plausible supporting domains or resolve a genuine ambiguity. Omit merely related or "
            "irrelevant Skills. Return empty lists when the query does not ask to create, edit, diagnose, "
            "or review a visual deliverable. Use only exact catalog ids, with no duplicates.\n\n"
            f"Select at most {self._preferred_max} preferred and {self._alternatives_max} alternatives.\n\n"
            "Return only one JSON object with this shape:\n"
            '{"preferred": ["builtin/example"], "alternatives": ["builtin/other"]}\n\n'
            "Catalog:\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def _parse_response(content: str) -> tuple[list[str], list[str]]:
        if not content:
            raise ValueError("empty response")
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if fenced:
            content = fenced.group(1).strip()
        else:
            obj = re.search(r"\{.*\}", content, re.DOTALL)
            if obj:
                content = obj.group()
        try:
            data = json.loads(content)
        except Exception as exc:
            raise ValueError("response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        preferred = data.get("preferred")
        alternatives = data.get("alternatives")
        if not isinstance(preferred, list) or not isinstance(alternatives, list):
            raise ValueError("response must contain preferred and alternatives lists")
        return (
            [str(value).strip() for value in preferred if str(value).strip()],
            [str(value).strip() for value in alternatives if str(value).strip()],
        )

    def _resolve(
        self,
        preferred_ids: list[str],
        alternative_ids: list[str],
    ) -> tuple[list[SkillCard], list[SkillCard]]:
        seen: set[str] = set()
        unknown: list[str] = []

        def take(ids: list[str], limit: int) -> list[SkillCard]:
            selected: list[SkillCard] = []
            for skill_id in ids:
                if skill_id in seen:
                    continue
                card = self._by_id.get(skill_id)
                if card is None:
                    seen.add(skill_id)
                    unknown.append(skill_id)
                    continue
                if len(selected) >= limit:
                    continue
                seen.add(skill_id)
                selected.append(card)
            return selected

        preferred = take(preferred_ids, self._preferred_max)
        alternatives = take(alternative_ids, self._alternatives_max)
        if (preferred_ids or alternative_ids) and not preferred and not alternatives:
            raise ValueError(f"selector returned only unknown Skill ids: {unknown}")
        if unknown:
            log.warning("Visual Domain Selector ignored unknown Skill ids: %s", unknown)
        return preferred, alternatives


__all__ = [
    "SkillCard",
    "VISUAL_DOMAIN_SKILL_NAMES",
    "VisualDomainSelection",
    "VisualDomainSkillSelector",
]
