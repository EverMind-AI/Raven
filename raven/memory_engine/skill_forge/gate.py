"""LLM gate — relevance filter over RRF-fused router candidates.

Ported from the pre-integrate-everos
``SkillService._llm_gate_filter`` and adapted to operate on
:class:`RouterHit` instead of the legacy ``SkillMeta``.

The gate runs after :class:`SkillForgeRouter` fan-out + RRF: it sees
the candidate name + description + a short body excerpt and asks an LLM
to plan, filter against the agent's available tools, and pick at most
``max_select`` skills. Empty result is a valid "inject nothing"
decision. Infra failures (parse error, timeout, provider error) fall
back to ``candidates[:legacy_top_k]`` rather than [] so a broken gate
never silently empties the ``# Skills`` block.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING

from raven.memory_engine.skill_forge.types import RouterHit

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

log = logging.getLogger(__name__)

_TIMEOUT_S = 180.0
_BODY_EXCERPT_CHARS = 300
_GATE_LOG_PATH_ENV = "RAVEN_GATE_LOG_PATH"


class LLMGateFilter:
    """LLM-based selector that picks 0..N relevant skills from a pool.

    Constructed once with the agent's shared :class:`LLMProvider` and
    gate-tuning knobs from :class:`SkillForgeConfig`. ``filter`` is
    called once per ``# Skills`` segment build.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        max_select: int = 2,
        legacy_top_k: int = 5,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> None:
        self._provider = provider
        self._max_select = max_select
        self._legacy_top_k = legacy_top_k
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def filter(
        self,
        task: str,
        candidates: list[RouterHit],
        available_tools: list[str] | None = None,
    ) -> list[RouterHit]:
        if not candidates:
            return []
        catalog, by_id = self._build_catalog(candidates)
        prompt = self._build_prompt(task, catalog, available_tools)

        try:
            resp = await asyncio.wait_for(
                self._provider.chat_with_retry(
                    messages=[{"role": "user", "content": prompt}],
                    model=self._model or None,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                ),
                timeout=_TIMEOUT_S,
            )
            content = resp.content or ""
            if getattr(resp, "finish_reason", None) == "error":
                raise RuntimeError(content or "provider error")
        except Exception as exc:
            log.warning("LLM gate call failed (%s); falling back to top-N", exc)
            return candidates[: self._legacy_top_k]

        try:
            plan, selected_ids = self._parse_response(content)
        except ValueError as exc:
            log.warning(
                "LLM gate response unparseable (%s); falling back to top-N", exc,
            )
            return candidates[: self._legacy_top_k]

        out: list[RouterHit] = []
        for sid in selected_ids:
            if len(out) >= self._max_select:
                break
            hit = by_id.get(sid)
            if hit is not None:
                out.append(hit)
        log.info(
            "LLM gate: candidates=%d → selected=%d %s",
            len(candidates), len(out), [h.name for h in out],
        )
        self._optional_trace(task, candidates, plan, out, content)
        return out

    @staticmethod
    def _build_catalog(
        candidates: list[RouterHit],
    ) -> tuple[str, dict[str, RouterHit]]:
        lines: list[str] = []
        by_id: dict[str, RouterHit] = {}
        for h in candidates:
            # qualified_id is the natural selection key — globally
            # unique and what the segment builder consumes for feedback.
            sid = h.qualified_id
            desc = (h.meta.get("description") or "").strip().replace("\n", " ")
            if not desc:
                desc = "(no description)"
            if len(desc) > 200:
                desc = desc[:197] + "..."
            body = (h.content or "").strip()
            body_excerpt = " ".join(body.split())[:_BODY_EXCERPT_CHARS]
            if not body_excerpt:
                body_excerpt = "(no body)"
            lines.append(
                f"- {sid}: {desc}\n  Body excerpt: {body_excerpt}"
            )
            by_id[sid] = h
        return "\n".join(lines), by_id

    def _build_prompt(
        self,
        task: str,
        catalog: str,
        available_tools: list[str] | None,
    ) -> str:
        # Verbatim port of the pre-integrate-everos
        # ``SkillService._llm_gate_filter`` prompt. The ONLY semantic
        # change vs. that prompt is the selection-id format:
        # ``skill_id`` → ``qualified_id`` (because the new router
        # routes across multiple sources, ``local/foo`` vs ``hub/foo``
        # must be disambiguated).
        tools_block = ""
        if available_tools:
            tools_block = (
                "# Agent Tools\n\n"
                f"The agent's ONLY available tools are: "
                f"{', '.join(sorted(set(available_tools)))}.\n\n"
                "**Hard rule**: a skill is NOT relevant if its workflow "
                "requires any tool, file, or environment that the agent "
                "lacks. Inspect EACH candidate's body excerpt and "
                "exclude it if you see any of:\n"
                "- A specific external API / SDK / vendor "
                "(e.g. ``nyne-deep-research``, ``musicbrainz``, "
                "``bandcamp``, ``-api`` suffix, vendor wrapper).\n"
                "- Environment placeholders or paths that won't exist "
                "in this runtime: ``${CLAUDE_PLUGIN_ROOT}``, "
                "``{baseDir}``, ``{overrides}``, ``.aiwg/``, ``${SKILL_HOME}``, "
                "``$ARGUMENTS`` as a slot, references to "
                "``${...}`` template variables.\n"
                "- Slash-command triggers (e.g. ``/research-query``) — "
                "the agent has no slash dispatcher.\n"
                "- ``Parent agent:`` style multi-agent framework "
                "assumptions, or references to other SKILL.md files "
                "under unspecified directories.\n"
                "- Agent personas, role-play, creative writing, content "
                "generation — these are not research procedures.\n\n"
                "**Only include** skills whose body describes a "
                "self-contained procedure that the agent can execute "
                "with just the listed tools (e.g. query-writing "
                "strategies, verification workflows, "
                "search-result interpretation).\n\n"
            )
        return (
            "You are a skill selector for an autonomous agent.\n\n"
            f"# Task\n\n{task}\n\n"
            f"{tools_block}"
            f"# Candidate Skills\n\n{catalog}\n\n"
            "# Instructions\n\n"
            "1. **Plan**: briefly think about what the task requires "
            "and which sequence of available-tool calls would achieve it.\n"
            "2. **Filter**: for EACH candidate skill, ask "
            "\"can the agent execute this skill's workflow using only the "
            "available tools above?\" If no, drop it — no matter how "
            "topically relevant.\n"
            "3. **Match**: among the survivors, a skill is relevant ONLY "
            "if it provides a procedure or strategy directly useful for "
            "a core part of your plan. Vague topical overlap is not enough.\n"
            f"4. **Decide**: select AT MOST {self._max_select} skill(s). "
            "If no skill survives both the tool check and the relevance "
            "check, you MUST return an empty list. Selecting an "
            "irrelevant or unexecutable skill is strictly worse than "
            "selecting none.\n\n"
            "Return ONLY a JSON object on a single line:\n"
            '{"plan": "1-sentence plan", "skills": ["qualified_id_1"]}\n\n'
            'Or when nothing applies: {"plan": "...", "skills": []}\n\n'
            "Use the EXACT qualified_id strings from the candidate list above."
        )

    @staticmethod
    def _parse_response(content: str) -> tuple[str, list[str]]:
        if not content:
            raise ValueError("empty content")
        # Strip <think> blocks (qwen3.5 reasoning).
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if m:
            content = m.group(1).strip()
        else:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                content = m.group()
        try:
            data = json.loads(content)
        except Exception as exc:
            raise ValueError(f"not valid JSON: {content[:200]!r}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"not a JSON object: {type(data).__name__}")
        if "skills" not in data:
            raise ValueError("missing 'skills' key")
        skills = data["skills"]
        if not isinstance(skills, list):
            raise ValueError(f"'skills' is not a list: {type(skills).__name__}")
        plan = str(data.get("plan", "") or "").strip()
        return plan, [str(s).strip() for s in skills if s]

    @staticmethod
    def _optional_trace(
        task: str,
        candidates: list[RouterHit],
        plan: str,
        selected: list[RouterHit],
        raw: str,
    ) -> None:
        path = os.environ.get(_GATE_LOG_PATH_ENV)
        if not path:
            return
        try:
            selected_ids = {h.qualified_id for h in selected}
            rec = {
                "ts": time.time(),
                "task": task[:1000],
                "candidates": [h.qualified_id for h in candidates],
                "plan": plan,
                "selected": [h.qualified_id for h in selected],
                "rejected": [
                    h.qualified_id for h in candidates
                    if h.qualified_id not in selected_ids
                ],
                "raw_response": raw[:4000],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


__all__ = ["LLMGateFilter"]
