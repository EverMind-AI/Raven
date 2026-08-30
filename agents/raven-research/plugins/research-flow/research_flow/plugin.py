"""Factories the manifest names: the hook and the three replacement tools.

The registry calls one factory per contribution, in its own order, and each
factory receives its own ``PluginContext`` - so the hook and the tools cannot
be handed to each other directly. A per-workspace :class:`_Shared` object
bridges them: whichever factory runs first creates it, and both sides pull the
same lazily built instances (the hook holds the tool handles its ``AskUserGate``
grants through; the web tools' evidence-round / saturation factories read the
per-session gear map the hook writes).

Config slice (``plugins.config["research-flow"]``): the product's ``drFlow``
block verbatim (camelCase keys, validated by :class:`FlowConfig`), plus the
plugin-only keys ``search.apiKey`` (falls back to ``SERPER_API_KEY``),
``fetch.apiKey`` (falls back to ``JINA_API_KEY``), ``proxy``, and ``stateRoot``
(defaults to ``<workspace>/research_flow``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from research_flow.config import FlowConfig
from research_flow.flow import ResearchFlowHook, SessionGear, ToolHandles, evidence_round_for, saturation_for
from research_flow.prompts import _DIGEST_SOURCE_CAP_CHARS, _DIGEST_SYSTEM
from research_flow.state import SessionStore
from research_flow.support.answer_text import visible_answer
from research_flow.support.ledger import set_ledger_dir
from research_flow.tools.ask_user import DRAskUserTool
from research_flow.tools.web import WebFetchTool, WebSearchTool, current_session

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.plugins.context import PluginContext

# The trunk's ``agents.defaults.maxToolIterations`` default. The loop's own
# resolved cap never reaches a plugin factory, so this stands in for it when
# neither ``drFlow.maxIterations`` nor a mode's ``maxToolIterations`` is set.
_DEFAULT_MAX_ITERATIONS = 40


def _make_digest_fn(provider: "LLMProvider", model: str | None, verbatim_head_chars: int = 0):
    """The fork's digest closure, verbatim: targeted extraction for web_fetch."""

    async def digest(text: str, info_to_extract: str) -> str:
        response = await provider.chat_with_retry(
            messages=[
                {"role": "system", "content": _DIGEST_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Information needed: {info_to_extract}\n\nPage content:\n{text[:_DIGEST_SOURCE_CAP_CHARS]}"
                    ),
                },
            ],
            model=model,
            temperature=0.1,
        )
        # visible_answer, not a paired-tag regex: the digest model is the same served
        # student, whose template prefills the opening tag, so its reply is
        # "reasoning</think>extraction" with no opener. A paired regex matches nothing
        # there and the whole chain of thought was being appended to the tool result and
        # persisted into the trajectory.
        digested = visible_answer(getattr(response, "content", None))
        if digested and verbatim_head_chars > 0:
            digested = f"{digested}\n\n[page head, verbatim]\n{text[:verbatim_head_chars]}"
        return digested

    return digest


class _Shared:
    """One activation's shared state: config, store, gear map, lazy instances."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        raw = dict(ctx.config or {})
        self.cfg = FlowConfig.from_slice(raw)
        self.search_api_key = (raw.get("search") or {}).get("apiKey") if isinstance(raw.get("search"), dict) else None
        self.fetch_api_key = (raw.get("fetch") or {}).get("apiKey") if isinstance(raw.get("fetch"), dict) else None
        self.proxy = raw.get("proxy")
        state_root = raw.get("stateRoot")
        root = Path(state_root) if state_root else Path(ctx.services.workspace) / "research_flow"
        self.store = SessionStore(root)
        set_ledger_dir(self.store.ledger_dir)
        self.session_gear: dict[str, SessionGear] = {}
        self._web_search: WebSearchTool | None = None
        self._web_fetch: WebFetchTool | None = None
        self._ask_user: DRAskUserTool | None = None
        self._hook: ResearchFlowHook | None = None

    @property
    def provider(self) -> "LLMProvider | None":
        return self._ctx.services.provider

    def web_search(self) -> WebSearchTool:
        if self._web_search is None:
            cfg = self.cfg
            gear = self.session_gear

            # The chain owns the session's EvidenceRound / SearchSaturation and
            # registers them in the gear map; these factories hand the tool the
            # SAME instances, so the gate that opens a round and the tool that
            # spends it agree. The base-config fallback covers a tool used with
            # no hook installed (single-session, no modes).
            def evidence_round_factory():
                slot = gear.get(current_session())
                if slot is not None:
                    return slot.evidence_round
                return evidence_round_for(cfg)

            def saturation_factory():
                slot = gear.get(current_session())
                if slot is not None:
                    return slot.saturation
                return saturation_for(cfg)

            self._web_search = WebSearchTool(
                api_key=self.search_api_key,
                max_results=cfg.search.rendered_width,
                proxy=self.proxy,
                include_answer_box=cfg.search.include_answer_box,
                include_knowledge_graph=cfg.search.include_knowledge_graph,
                include_snippets=cfg.search.include_snippets,
                snippet_dedup_by_docid=cfg.search.snippet_dedup_by_docid,
                cross_query_dedup=cfg.search.cross_query_dedup,
                search_depth=cfg.search.search_depth,
                repeat_notice=cfg.search.repeat_notice,
                evidence_round_factory=evidence_round_factory,
                saturation_factory=saturation_factory,
            )
        return self._web_search

    def web_fetch(self) -> WebFetchTool:
        if self._web_fetch is None:
            cfg = self.cfg
            kwargs: dict[str, Any] = {"max_chars": cfg.fetch_max_chars}
            if cfg.digest.enabled:
                provider = self.provider
                if provider is None:
                    logger.warning(
                        "research-flow: digest is enabled but no provider was lent to the "
                        "plugin; web_fetch falls back to plain truncation"
                    )
                else:
                    kwargs.update(
                        digest_fn=_make_digest_fn(
                            provider,
                            cfg.digest.model,
                            verbatim_head_chars=cfg.digest.verbatim_head_chars,
                        ),
                        digest_threshold_chars=cfg.digest.threshold_chars,
                        digest_timeout_s=cfg.digest.timeout_seconds,
                    )
            self._web_fetch = WebFetchTool(
                api_key=self.fetch_api_key,
                proxy=self.proxy,
                **kwargs,
            )
        return self._web_fetch

    def ask_user(self) -> DRAskUserTool | None:
        """The clarify tool, or None when the base config leaves the round off.

        Registered only when the feature can fire, exactly as the fork built it
        only when ``ask_user_on`` - a registered tool the contract never asks
        for is the mismatch the clause exists to prevent.
        """
        if not self.cfg.ask_user_on:
            return None
        if self._ask_user is None:
            cfg = self.cfg
            self._ask_user = DRAskUserTool(
                outline=cfg.ask_user.outline,
                mode=cfg.ask_user.mode,
                max_questions=cfg.ask_user.max_questions,
                max_outline_items=cfg.ask_user.max_outline_items,
                delivery=cfg.ask_user.delivery,
            )
        return self._ask_user

    def hook(self) -> ResearchFlowHook | None:
        if self._hook is None:
            cfg = self.cfg
            if self.provider is None:
                llm_gates = [
                    name
                    for name, on in (
                        ("sufficiency", cfg.sufficiency.enabled),
                        ("verify", cfg.verify.enabled),
                        ("forceFinalize", cfg.force_finalize.enabled),
                        ("conversation.gate=agentic", cfg.conversation.enabled and cfg.conversation.gate == "agentic"),
                    )
                    if on
                ]
                if llm_gates:
                    logger.warning(
                        "research-flow: no provider was lent to the plugin and these LLM gates "
                        "are enabled: {}; the hook is not installed",
                        ", ".join(llm_gates),
                    )
                    return None
            self._hook = ResearchFlowHook(
                cfg=cfg,
                provider=self.provider,
                tools=ToolHandles(
                    web_search=self.web_search(),
                    web_fetch=self.web_fetch(),
                    ask_user=self.ask_user(),
                ),
                store=self.store,
                max_iterations=cfg.max_iterations or _DEFAULT_MAX_ITERATIONS,
                context_window_tokens=cfg.context_window_tokens or 0,
                session_gear=self.session_gear,
            )
        return self._hook


# Keyed by workspace rather than by ServiceLocator identity: the host builds a
# fresh locator per contribution kind, so identity would split the two sides.
_SHARED: dict[str, _Shared] = {}


def _shared_for(ctx: "PluginContext") -> _Shared:
    key = str(ctx.services.workspace)
    shared = _SHARED.get(key)
    if shared is None:
        shared = _Shared(ctx)
        _SHARED[key] = shared
    return shared


def make_hook(ctx: "PluginContext") -> ResearchFlowHook | None:
    """Factory for the ``research_flow`` hook contribution."""
    shared = _shared_for(ctx)
    if not shared.cfg.enabled:
        return None
    return shared.hook()


def make_web_search(ctx: "PluginContext") -> WebSearchTool | None:
    """Factory for the ``web_search`` replacement tool."""
    shared = _shared_for(ctx)
    if not shared.cfg.enabled:
        return None
    return shared.web_search()


def make_web_fetch(ctx: "PluginContext") -> WebFetchTool | None:
    """Factory for the ``web_fetch`` replacement tool."""
    shared = _shared_for(ctx)
    if not shared.cfg.enabled:
        return None
    return shared.web_fetch()


def make_ask_user(ctx: "PluginContext") -> DRAskUserTool | None:
    """Factory for the ``ask_user`` clarify tool; declines when the round is off."""
    shared = _shared_for(ctx)
    if not shared.cfg.enabled:
        return None
    return shared.ask_user()


__all__ = [
    "make_ask_user",
    "make_hook",
    "make_web_fetch",
    "make_web_search",
]
