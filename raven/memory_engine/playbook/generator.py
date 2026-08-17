"""PlaybookGenerator — user input to a validated draft, with a repair loop.

Flow (one strong call, then only repair rounds):

    assemble context -> forced tool call -> pydantic + structure + asset
    checks -> on error: feed the numbered errors back, ask for a minimal
    edit -> on pass: fill the code-owned fields and return.

Unknown skills/mcps degrade into ``provenance.missing_capabilities`` (the
playbook stays usable, just annotated); an unknown agent name is a hard
error (nothing could run the node). ``revise`` reuses the same loop with
the immutable-region check added.

No prompt text lives here — see :mod:`raven.memory_engine.playbook.prompt`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger
from pydantic import ValidationError

from raven.memory_engine.playbook.prompt import (
    EMIT_TOOL_NAME,
    SYSTEM_PROMPT,
    build_generation_prompt,
    build_repair_prompt,
    build_revise_prompt,
    emit_tool,
)
from raven.memory_engine.playbook.triggers import TriggerGuardError, guard_triggers
from raven.memory_engine.playbook.types import BUILTIN_AGENTS, PlaybookSpec, slugify
from raven.memory_engine.playbook.validate import (
    check_assets,
    check_immutable_region,
    validate_structure,
)

if TYPE_CHECKING:
    from raven.memory_engine.skill_forge import SkillForgeRouter
    from raven.providers.base import LLMProvider

_MAX_REPAIR_ROUNDS = 3
_SKILL_CANDIDATES_K = 12


class CapabilityInventory(Protocol):
    """Read-only view of what the runtime can actually offer."""

    def known_mcp(self) -> list[str]: ...

    def known_tools(self) -> list[str]: ...


@dataclass
class StaticInventory:
    """Inventory from plain lists — tests and early wiring."""

    mcp: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def known_mcp(self) -> list[str]:
        return self.mcp

    def known_tools(self) -> list[str]:
        return self.tools


class PlaybookGenerationError(RuntimeError):
    """The model failed to produce a valid spec within the repair budget."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "no tool call returned")
        self.errors = errors


class PlaybookGenerator:
    """Generate and revise playbook drafts. Stateless between calls.

    ``agent_roster`` maps agent name -> capability description — the v1
    roster is the four builtin bases; when the unified agent registry grows
    configurable builtin rows, the roster is read from there instead.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        skill_router: "SkillForgeRouter | None",
        agent_roster: dict[str, str],
        inventory: CapabilityInventory,
        *,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._router = skill_router
        self._roster = agent_roster
        self._inventory = inventory
        self._model = model

    async def generate(self, user_input: str, skills: list[str] | None = None) -> PlaybookSpec:
        """One draft from user input plus optional pinned skills.

        ``skills`` entries are names, or paths to a skill file whose content
        is inlined into the generation context (never registered anywhere).
        """
        pinned, inline_docs = _split_skill_refs(skills or [])
        candidates = await self._retrieve_candidates(user_input)
        user_msg = build_generation_prompt(
            user_input,
            agent_roster=self._roster,
            skill_candidates=candidates,
            user_pinned_skills=pinned,
            known_mcp=self._inventory.known_mcp(),
            inline_skill_docs=inline_docs,
        )
        known_skills = [name for name, _ in candidates] + pinned + [name for name, _ in inline_docs]
        return await self._loop(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            source_input=user_input,
            known_skills=known_skills,
            fixed_name=None,
            previous=None,
        )

    async def revise(self, spec: PlaybookSpec, user_feedback: str) -> PlaybookSpec:
        """One revision round: immutable region enforced, name fixed."""
        candidates = await self._retrieve_candidates(spec.description + "\n" + user_feedback)
        known_skills = [name for name, _ in candidates]
        known_skills += [s for node in spec.nodes or [] for s in node.skills]
        return await self._loop(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_revise_prompt(spec, user_feedback)},
            ],
            source_input=spec.provenance.source_input,
            known_skills=known_skills,
            fixed_name=spec.name,
            previous=spec,
        )

    async def _retrieve_candidates(self, query: str) -> list[tuple[str, str]]:
        if self._router is None:
            return []
        hits = await self._router.select(query, history=[], k=_SKILL_CANDIDATES_K)
        return [(h.name, _first_line(h.meta.get("description") or h.content)) for h in hits]

    async def _loop(
        self,
        *,
        messages: list[dict[str, Any]],
        source_input: str,
        known_skills: list[str],
        fixed_name: str | None,
        previous: PlaybookSpec | None,
    ) -> PlaybookSpec:
        errors: list[str] = []
        for round_no in range(1 + _MAX_REPAIR_ROUNDS):
            response = await self._provider.chat_with_retry(
                messages=messages,
                tools=emit_tool(),
                model=self._model or None,
                tool_choice={"type": "function", "function": {"name": EMIT_TOOL_NAME}},
            )
            args = _tool_args(response)
            if args is None:
                errors = ["no emit_playbook tool call in the response"]
                messages.append({"role": "user", "content": build_repair_prompt({}, errors)})
                continue

            spec, errors, missing = self._check(args, source_input, known_skills, fixed_name)
            if spec is not None and previous is not None:
                errors = errors + check_immutable_region(previous, spec)
            if spec is not None and not errors:
                # The model proposes the L1 vocabulary; the guards decide what
                # is indexable. Without this the schema hands the model a
                # direct write to the index, and stop words ("帮我"), entries
                # below the length rule, and duplicate case variants reach it
                # unfiltered -- each one costing a gate call on every message
                # that contains it, for as long as the playbook exists.
                try:
                    spec = spec.model_copy(update={"triggers": guard_triggers(spec.triggers, what=spec.name)})
                except TriggerGuardError as exc:
                    errors = [str(exc)]
                    messages.append({"role": "user", "content": build_repair_prompt(args, errors)})
                    continue
                if missing:
                    spec = spec.model_copy(
                        update={"provenance": spec.provenance.model_copy(update={"missing_capabilities": missing})}
                    )
                logger.info("playbook {} generated in {} round(s)", spec.name, round_no + 1)
                return spec
            messages.append({"role": "user", "content": build_repair_prompt(args, errors)})
        raise PlaybookGenerationError(errors)

    def _check(
        self,
        args: dict[str, Any],
        source_input: str,
        known_skills: list[str],
        fixed_name: str | None,
    ) -> tuple[PlaybookSpec | None, list[str], list[str]]:
        """Fill code-owned fields, then run all validation layers."""
        data = dict(args)
        # Weaker models sometimes wrap the whole spec in one envelope key.
        if len(data) == 1 and isinstance(next(iter(data.values())), dict):
            data = dict(next(iter(data.values())))
        for key in ("version", "status"):
            data.pop(key, None)
        provenance = dict(data.get("provenance") or {})
        provenance["sourceInput"] = source_input
        provenance.pop("source_input", None)
        provenance.pop("missingCapabilities", None)
        provenance.pop("missing_capabilities", None)
        data["provenance"] = provenance
        data["name"] = fixed_name or slugify(str(data.get("name", "")))

        try:
            spec = PlaybookSpec.model_validate(data)
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
            return None, errors[:20], []

        errors = validate_structure(spec, known_agents=self._roster.keys() or BUILTIN_AGENTS)
        asset_errors, missing = check_assets(
            spec,
            known_skills=known_skills,
            known_mcp=self._inventory.known_mcp(),
        )
        return spec, errors + asset_errors, missing


def _split_skill_refs(refs: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Names stay pins; readable paths become inlined docs."""
    pinned: list[str] = []
    inline: list[tuple[str, str]] = []
    for ref in refs:
        path = Path(ref).expanduser()
        if path.is_file():
            inline.append((path.stem, path.read_text(encoding="utf-8")))
        else:
            pinned.append(ref)
    return pinned, inline


def _tool_args(response: Any) -> dict[str, Any] | None:
    if not getattr(response, "has_tool_calls", False):
        return None
    args = response.tool_calls[0].arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    return args if isinstance(args, dict) else None


def _first_line(text: str, limit: int = 150) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]
