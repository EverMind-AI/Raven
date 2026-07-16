"""Query rewriter — judges whether skill retrieval is needed and rewrites
verbose queries into concise skill-routing queries.

Ported from the pre-integrate-everos branch
(``raven/memory_engine/skill/rewriter.py``). One LLM call does two
things: (1) decide whether the user query needs external skill retrieval
at all (chat / greetings / general knowledge → skip the router fan-out
entirely); (2) when retrieval IS needed, strip noise (paths, IDs,
timestamps) and keep task type + domain so BM25 / dense fan-outs hit the
relevant skills.

Failures default to ``need_retrieval=True`` (safe fallback: keep doing
retrieval) so a flaky provider never silently turns off the skill lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

log = logging.getLogger(__name__)

_REWRITE_PROMPT = """\
Given a user query, first decide if it needs external skill/tool retrieval. \
Casual chat, greetings, simple follow-ups, and general knowledge tasks do not. \
Specialized tools, domain-specific workflows, or specific frameworks/APIs do.

If retrieval is needed, rewrite the query for skill retrieval. \
Remove noise (paths, IDs, timestamps, boilerplate). \
Keep task type, domain, required capabilities, and key technical details. \
Do NOT answer or solve the query — only rewrite it.

When in doubt, choose retrieval.

Return JSON: {{"need_retrieval": true/false, "rewritten_query": "..." or null}}

{query}"""

_QUERY_MAX_LENGTH = 2000
_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class RewriteResult:
    need_retrieval: bool
    rewritten_query: str | None = None


class QueryRewriter:
    """Judges retrieval necessity and rewrites queries via the agent's
    shared :class:`LLMProvider`.

    Routed through ``chat_with_retry`` so retry policy, generation
    defaults and provider extras (cache control, routing affinity) match
    the agent's main path.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        max_tokens: int = 8192,
        temperature: float = 0.3,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature

    @trace.instrument("skill.rewrite", kind="skill", extract=semconv.skill_rewrite)
    async def analyze(self, query: str) -> RewriteResult:
        truncated = (query or "").strip()[:_QUERY_MAX_LENGTH]
        if not truncated:
            return RewriteResult(need_retrieval=False)

        prompt = _REWRITE_PROMPT.format(query=truncated)
        try:
            resp = await asyncio.wait_for(
                self._provider.chat_with_retry(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                ),
                timeout=_TIMEOUT_S,
            )
            content = resp.content or ""
            if getattr(resp, "finish_reason", None) == "error":
                raise RuntimeError(content or "provider error")
        except Exception as e:
            log.warning("query rewrite failed (%s); defaulting to retrieval", e)
            return RewriteResult(need_retrieval=True)
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> RewriteResult:
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("rewrite response not JSON; defaulting to retrieval")
            return RewriteResult(need_retrieval=True)

        if not isinstance(data, dict):
            return RewriteResult(need_retrieval=True)

        need = bool(data.get("need_retrieval", True))
        if not need:
            return RewriteResult(need_retrieval=False)

        rewritten = data.get("rewritten_query")
        if isinstance(rewritten, str):
            rewritten = rewritten.strip() or None
        else:
            rewritten = None
        return RewriteResult(need_retrieval=True, rewritten_query=rewritten)


__all__ = ["QueryRewriter", "RewriteResult"]
