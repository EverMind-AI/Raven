"""PlaybookGenerator — user input to a validated draft, with a repair loop.

Flow (one strong call, then only repair rounds):

    assemble context -> forced tool call -> pydantic + structure + asset
    checks -> on error: feed the numbered errors back, ask for a minimal
    edit -> on pass: fill the code-owned fields and return.

Unknown skills/mcps degrade into review notes (the playbook stays usable,
just annotated); an unknown agent name is a hard error (nothing could run
the node). ``revise`` reuses the same loop over an existing spec plus the
user's feedback. The model's self-report (open questions, assumptions)
rides the same tool call but never enters the machine fields — it comes
back as :attr:`GeneratedPlaybook.notes` for the store to render into the
body's review section.

No prompt text lives here — see :mod:`raven.playbook.prompt`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, get_args, get_origin

from loguru import logger
from pydantic import BaseModel, ValidationError

from raven.playbook.agent_profiles import AgentProfileSource, validate_agent_capabilities
from raven.playbook.llm_result import (
    ProviderFailure,
    ProviderResponseError,
    RequiredToolError,
    required_tool_arguments,
)
from raven.playbook.prompt import (
    EMIT_TOOL_NAME,
    SYSTEM_PROMPT,
    build_generation_prompt,
    build_repair_prompt,
    build_revise_prompt,
    emit_tool,
)
from raven.playbook.triggers import TriggerGuardError, guard_triggers
from raven.playbook.types import PlaybookSpec, slugify
from raven.playbook.validate import check_assets, unusable_mcp_servers, validate_structure

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.memory_engine import SkillForgeRouter
    from raven.playbook.agent_profiles import PlaybookAgentProfile

_MAX_REPAIR_ROUNDS = 3
_SKILL_CANDIDATES_K = 12


def _structural_fields() -> frozenset[str]:
    """Emitted keys whose value is an object or an array, both spellings.

    Read off the contract rather than listed by hand, so a field added to
    :class:`PlaybookSpec` is covered without a second list to remember. The two
    report arrays ride the same tool call without being spec fields.
    """
    names = {"blockingQuestions", "assumptions"}
    for name, spec_field in PlaybookSpec.model_fields.items():
        annotation = spec_field.annotation
        for candidate in (annotation, *get_args(annotation)):
            origin = get_origin(candidate) or candidate
            structural = origin in (dict, list) or (isinstance(origin, type) and issubclass(origin, BaseModel))
            if structural:
                names.add(name)
                names.add(spec_field.alias or name)
                break
    return frozenset(names)


_STRUCTURAL_FIELDS = _structural_fields()


class CapabilityInventory(Protocol):
    """Read-only view of what the runtime can actually offer."""

    def known_mcp(self) -> list[str]: ...

    def known_tools(self) -> list[str]: ...


@dataclass
class StaticInventory:
    """Inventory from plain lists — tests, and callers that hold the lists already.

    Constructed with no arguments it reports *nothing* available, and
    ``check_assets`` then judges every skill and mcp server the generator
    proposed to be unknown. Use :func:`live_inventory` for a real one.
    """

    mcp: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def known_mcp(self) -> list[str]:
        return self.mcp

    def known_tools(self) -> list[str]:
        return self.tools


def live_inventory(mcp_servers: Any = None, tools: Any = None) -> StaticInventory:
    """What this installation can actually offer, for ``check_assets`` to judge against.

    ``mcp_servers`` is the configured server map (its keys are the names a node
    would write); ``tools`` is anything iterable of tool names -- a
    ``ToolRegistry.names()`` list, or nothing where no registry is built.

    Both are read from the caller rather than looked up here, because the two
    callers have different things in hand: the agent loop holds a live registry,
    the CLI holds only config. What matters is that neither passes an empty
    inventory by accident, which is what ``StaticInventory()`` did at both sites.
    """
    return StaticInventory(mcp=sorted(mcp_servers or {}), tools=sorted(tools or []))


class PlaybookGenerationError(RuntimeError):
    """The model failed to produce a valid spec within the repair budget."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) or "no tool call returned")
        self.errors = errors


class PlaybookProtocolError(PlaybookGenerationError):
    """A successful response violated the required structured-result contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__([f"{code}: {detail}"])
        self.code = code


class PlaybookProviderError(PlaybookGenerationError):
    """The provider exhausted recovery without producing a model response."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__([failure.message])
        self.classification = failure.classification
        self.code = "provider_call_failed"
        self.category = failure.category


@dataclass
class GeneratedPlaybook:
    """A validated spec plus the review notes that belong in the body."""

    spec: PlaybookSpec
    notes: list[str] = field(default_factory=list)
    """Open questions, assumptions and missing capabilities, one line each —
    rendered by the store into the body's review section, never into the
    machine block."""


class PlaybookGenerator:
    """Generate and revise playbook drafts. Stateless between calls.

    ``agent_profiles`` reads the enabled agent table at the start of each call.
    It exposes only generation-relevant capabilities, so the model can choose a
    graph the runtime can execute without seeing transport configuration.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        skill_router: "SkillForgeRouter | None",
        agent_profiles: AgentProfileSource,
        inventory: CapabilityInventory,
        *,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._router = skill_router
        self._agent_profiles = agent_profiles
        self._inventory = inventory
        self._model = model

    async def generate(self, user_input: str, skills: list[str] | None = None) -> GeneratedPlaybook:
        """One draft from user input plus optional pinned skills.

        ``skills`` entries are names, or paths to a skill file whose content
        is inlined into the generation context (never registered anywhere).
        """
        pinned, inline_docs = _split_skill_refs(skills or [])
        candidates = await self._retrieve_candidates(user_input)
        profiles = self._agent_profiles()
        user_msg = build_generation_prompt(
            user_input,
            agent_profiles=profiles,
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
            known_skills=known_skills,
            fixed_name=None,
            agent_profiles=profiles,
        )

    async def revise(self, spec: PlaybookSpec, user_feedback: str) -> GeneratedPlaybook:
        """One revision round over an existing spec; the name stays fixed."""
        candidates = await self._retrieve_candidates(spec.description + "\n" + user_feedback)
        known_skills = [name for name, _ in candidates]
        profiles = self._agent_profiles()
        known_skills += [s for node in spec.nodes or [] for s in node.skills or []]
        return await self._loop(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_revise_prompt(spec, user_feedback, profiles)},
            ],
            known_skills=known_skills,
            fixed_name=spec.name,
            agent_profiles=profiles,
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
        known_skills: list[str],
        fixed_name: str | None,
        agent_profiles: dict[str, "PlaybookAgentProfile"],
    ) -> GeneratedPlaybook:
        errors: list[str] = []
        for round_no in range(1 + _MAX_REPAIR_ROUNDS):
            response = await self._provider.chat_with_retry(
                messages=messages,
                tools=emit_tool(),
                model=self._model or None,
                tool_choice={"type": "function", "function": {"name": EMIT_TOOL_NAME}},
            )
            args = _required_tool_args(response)

            spec, errors, missing, reported = self._check(args, known_skills, fixed_name, agent_profiles)
            if spec is not None and not errors:
                # The model proposes the L1 vocabulary; the guards decide what
                # is indexable. Without this the schema hands the model a
                # direct write to the index, and stop words ("help me"),
                # entries below the length rule, and duplicate case variants
                # reach it unfiltered, crowding more specific playbooks out of
                # the model-facing top-K descriptions.
                try:
                    spec = spec.model_copy(update={"triggers": guard_triggers(spec.triggers, what=spec.name)})
                except TriggerGuardError as exc:
                    errors = [str(exc)]
                    messages.append({"role": "user", "content": build_repair_prompt(args, errors)})
                    continue
                questions, assumptions = reported
                notes = [f"Open question: {q}" for q in questions]
                notes += [f"Assumption: {a}" for a in assumptions]
                notes += [f"Missing capability: {m}" for m in missing]
                # A server definition the file cannot honour is dropped when the
                # playbook loads (``store._drop_unusable_mcp_servers``), so the
                # generated file has to say so where a reviewer will see it --
                # otherwise the section reads as delivered and the run reports
                # ``not_configured`` with no trail back to the file.
                notes += [
                    f"Unusable mcpServers.{name}: {why}" for name, why in sorted(unusable_mcp_servers(spec).items())
                ]
                logger.info("playbook {} generated in {} round(s)", spec.name, round_no + 1)
                return GeneratedPlaybook(spec=spec, notes=notes)
            messages.append({"role": "user", "content": build_repair_prompt(args, errors)})
        raise PlaybookGenerationError(errors)

    def _check(
        self,
        args: dict[str, Any],
        known_skills: list[str],
        fixed_name: str | None,
        agent_profiles: dict[str, "PlaybookAgentProfile"],
    ) -> tuple[PlaybookSpec | None, list[str], list[str], tuple[list[str], list[str]]]:
        """Fill code-owned fields, then run all validation layers."""
        data = dict(args)
        # Weaker models sometimes wrap the whole spec in one envelope key.
        if len(data) == 1 and isinstance(next(iter(data.values())), dict):
            data = dict(next(iter(data.values())))
        data = _decode_stringified(data)
        questions = [str(q) for q in data.pop("blockingQuestions", None) or []]
        assumptions = [str(a) for a in data.pop("assumptions", None) or []]
        data.pop("version", None)
        data["name"] = fixed_name or slugify(str(data.get("name", "")))

        try:
            spec = PlaybookSpec.model_validate(data)
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()]
            return None, errors[:20], [], (questions, assumptions)

        # The roster is the agent table; empty means no table was reachable, and the
        # agent names then go unchecked rather than being checked against a
        # stand-in list that would both reject configured agents and pass deleted
        # ones.
        errors = validate_structure(spec, known_agents=agent_profiles.keys() or None)
        errors += validate_agent_capabilities(spec, agent_profiles)
        asset_errors, missing = check_assets(
            spec,
            known_skills=known_skills,
            known_mcp=self._inventory.known_mcp(),
        )
        return spec, errors + asset_errors, missing, (questions, assumptions)


def _decode_stringified(data: dict[str, Any]) -> dict[str, Any]:
    """Decode fields the model serialised twice.

    Some models emit a nested object or array as a JSON *string* inside the
    tool-call arguments -- ``"triggers": "{\\"keywords\\": [...]}"`` instead of
    the object. The content is right and only the encoding is wrong, but
    pydantic sees a str and the repair round cannot help: the error says the
    field is not a dictionary, which the model reads as a complaint about
    content it can already see is a dictionary, so it resends the same bytes
    until the budget runs out.

    Only fields the contract types as an object or an array are touched, so
    free text that happens to open with a brace stays the text it is.
    """
    decoded = dict(data)
    for key in _STRUCTURAL_FIELDS:
        value = decoded.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            decoded[key] = parsed
    return decoded


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


def _required_tool_args(response: Any) -> dict[str, Any]:
    try:
        return required_tool_arguments(response, EMIT_TOOL_NAME)
    except ProviderResponseError as exc:
        raise PlaybookProviderError(exc.failure) from exc
    except RequiredToolError as exc:
        raise PlaybookProtocolError(exc.code, exc.detail) from exc


def _first_line(text: str, limit: int = 150) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]
