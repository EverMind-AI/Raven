"""Evidence floor (DR flow): a draft may not ship on fewer pages than the mode demands.

Every knob that separated ``high`` from ``max`` before this gate was conditional: the
evidence round waits for a reviewer's rejection, the saturation ladder for a dry
search streak. On a 5-question batch neither condition arrived (2 rejections in 15
turns, no dry streak at all), so the two modes ran the same flow under two labels and
their paired differences came out inverted. This gate is evaluated on every draft, so
the demand it carries cannot fail to be consulted.

The check runs the moment the model stops calling tools and writes. This turn's
readable pages -- fetched, not a listing, and above the thin-page bar the trail
reports -- and the distinct sites they came from are counted; below either floor the
draft is bounced back as an assistant message with a note naming the shortfall, the
shape the reviewer's rejection takes, so the model extends its research rather than
restarting it.

Bounded and fail-open toward shipping: past ``max_rollbacks`` the draft goes on to
the reviewer with ``unmet`` recorded, so a topic the corpus cannot supply degrades to
the next mode down instead of spinning. Sits after the salvage gate (an answerless
terminal is not a draft) and before the reviewer (a draft the floor rejects is not
worth a six-minute review).
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.security.trust import unwrap_untrusted
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.ledger import ledger_append
from research_flow.support.process_appendix import _THIN_PAGE_CHARS
from research_flow.tools.web import fetch_result_ok

logger = logging.getLogger(__name__)

_LEDGER_OP = "evidence_floor"
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})

_FLOOR_PROMPT = (
    "The draft above rests on {pages} readable page(s) from {domains} distinct site(s). "
    "This task requires at least {min_pages} readable pages from {min_domains} distinct "
    "sites before an answer ships. Continue the research rather than restarting it: "
    "search for and open further primary sources on the claims the draft already makes, "
    "preferring sites not yet opened, then produce the final answer with the new "
    "evidence folded in."
)


class EvidenceFloorGate(AgentHook):
    """Bounce a draft written on fewer readable pages or sites than the mode demands."""

    def __init__(
        self,
        min_pages: int = 18,
        min_domains: int = 8,
        max_rollbacks: int = 2,
        closing_tag_required: bool = False,
    ) -> None:
        self._min_pages = min_pages
        self._min_domains = min_domains
        self._max_rollbacks = max_rollbacks
        self._closing_tag_required = closing_tag_required
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "event": "installed",
                "min_pages": min_pages,
                "min_domains": min_domains,
                "max_rollbacks": max_rollbacks,
            }
        )

    @property
    def name(self) -> str:
        return "EvidenceFloorGate"

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        # ``tools`` is populated only in this phase. Remembered per iteration so the
        # draft check below knows whether "search for further sources" is an
        # instruction the model can follow at all.
        if ctx.tools is not None:
            names = {(t.get("function") or {}).get("name") for t in ctx.tools if isinstance(t, dict)}
            state = ctx.metadata.setdefault("evidence_floor", {"checks": 0, "rollbacks": 0})
            state["web_tools_absent"] = not (names & _WEB_TOOLS)
        return HookDecision()

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        content = getattr(ctx.response, "content", None) or ""
        draft = visible_answer(
            content,
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(ctx.response, "reasoning_content", None),
            ),
        )
        if not draft:
            return HookDecision()

        state = ctx.metadata.setdefault("evidence_floor", {"checks": 0, "rollbacks": 0})
        if state.get("web_tools_absent"):
            # A bounce is an instruction to search; with no web tool on this
            # iteration it would only spend the two bounces and release anyway.
            # Recorded, not counted as a check, so the floor's numbers stay about
            # drafts it could act on.
            state["skipped"] = "web_tools_absent"
            ledger_append(
                {"ts": time.time(), "op": _LEDGER_OP, "outcome": "skipped_no_web_tools", "iteration": ctx.iteration}
            )
            return HookDecision(notes=["evidence_floor: skipped, no web tools this iteration"])
        pages, domains = self._count(ctx.messages or [], ctx.turn_base or 0)
        state["checks"] += 1
        state["pages"] = pages
        state["domains"] = domains
        if pages >= self._min_pages and domains >= self._min_domains:
            outcome = "met"
            state["met"] = True
        elif state["rollbacks"] >= self._max_rollbacks:
            outcome = "unmet_released"
            state["unmet"] = True
        else:
            outcome = "rollback"
            state["rollbacks"] += 1
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "outcome": outcome,
                "pages": pages,
                "domains": domains,
                "iteration": ctx.iteration,
                "rollback": state["rollbacks"],
            }
        )
        if outcome == "met":
            return HookDecision(notes=[f"evidence_floor: met ({pages} pages / {domains} sites)"])
        if outcome == "unmet_released":
            logger.warning(
                "evidence-floor: %d pages / %d sites still below %d / %d after %d bounce(s); releasing the draft",
                pages,
                domains,
                self._min_pages,
                self._min_domains,
                state["rollbacks"],
            )
            return HookDecision(notes=[f"evidence_floor: unmet after {state['rollbacks']} bounces; released"])
        logger.warning(
            "evidence-floor: draft on %d pages / %d sites, floor is %d / %d; bouncing back %d/%d",
            pages,
            domains,
            self._min_pages,
            self._min_domains,
            state["rollbacks"],
            self._max_rollbacks,
        )
        note = _FLOOR_PROMPT.format(
            pages=pages,
            domains=domains,
            min_pages=self._min_pages,
            min_domains=self._min_domains,
        )
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": draft},
                {"role": "user", "content": note},
            ],
            notes=[f"evidence_floor: bounced, rollback {state['rollbacks']}/{self._max_rollbacks}"],
        )

    def _count(self, messages: list[dict], turn_base: int) -> tuple[int, int]:
        """This turn's readable pages and the distinct sites behind them.

        Same predicates as the trail: ``fetch_result_ok`` for a page rather than an
        error, and the trail's own thin-page bar (imported from where ``thin_pages`` is
        computed, not the fetch tool's copy of the same number) for a page rather than
        a stub -- so the floor a mode promises and the ``pages_ok`` / ``thin_pages`` a
        reader sees cannot drift apart.
        """
        pages = 0
        hosts: set[str] = set()
        for m in messages[turn_base:]:
            if not isinstance(m, dict) or m.get("role") != "tool" or m.get("name") != "web_fetch":
                continue
            body = unwrap_untrusted(m.get("content"))
            if not fetch_result_ok(body):
                continue
            payload = _payload(body)
            length = payload.get("length")
            if not isinstance(length, int):
                length = len(str(payload.get("text") or ""))
            if length < _THIN_PAGE_CHARS:
                continue
            pages += 1
            host = _host(payload.get("finalUrl") or payload.get("url"))
            if host:
                hosts.add(host)
        return pages, len(hosts)


def _payload(body: object) -> dict:
    if not isinstance(body, str):
        return {}
    try:
        payload, _end = json.JSONDecoder().raw_decode(body.lstrip())
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _host(url: object) -> str:
    if not isinstance(url, str):
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


__all__ = ["EvidenceFloorGate"]
