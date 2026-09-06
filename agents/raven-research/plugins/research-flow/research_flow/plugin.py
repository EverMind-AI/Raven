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
plugin-only keys ``search.provider`` / ``fetch.provider`` (a vendor from
``SEARCH_PROVIDERS`` / ``FETCH_PROVIDERS``; an unknown name degrades to the
default, out loud), ``search.apiKey`` / ``fetch.apiKey`` (each falling back to
the SELECTED vendor's own env var), ``fetch.fallbackApiKey`` (the default
reader's key, for when a keyed reader selected without one degrades back to
it), ``proxy``, and ``stateRoot`` (defaults to ``<workspace>/research_flow``).
"""

from __future__ import annotations

from collections.abc import Callable
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
from research_flow.tools.web import (
    DEFAULT_FETCH_PROVIDER,
    SEARCH_PROVIDERS,
    WebFetchTool,
    WebSearchTool,
    current_session,
    resolve_fetch_provider,
    resolve_search_provider,
)

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.plugins.context import PluginContext

# The trunk's ``agents.defaults.maxToolIterations`` default. A plugin factory
# resolves before any turn runs, so this stands in until the loop's enforced
# cap arrives on the iteration context (``ctx.max_iterations``, hook surface
# v3); a chain built from a turn reads that instead, and explicit config wins
# over both.
_DEFAULT_MAX_ITERATIONS = 40


def _make_digest_fn(
    provider: "LLMProvider",
    model: str | None,
    verbatim_head_chars: int = 0,
    *,
    resolve: "Callable[[], tuple[str | None, int]] | None" = None,
):
    """The fork's digest closure: targeted extraction for web_fetch.

    ``resolve`` is what makes the digest follow the session's mode. The fetch
    tool is built once from the base config, but a mode overlay may move
    ``digest.model`` / ``digest.verbatimHeadChars``; when given, ``resolve()`` is
    consulted per call and returns the pair in force for the current session,
    falling back to the base values handed in here. The threshold and timeout
    stay the tool's constructor arguments and therefore the base config's.
    """

    async def digest(text: str, info_to_extract: str) -> str:
        use_model, use_head = (model, verbatim_head_chars) if resolve is None else resolve()
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
            model=use_model,
            temperature=0.1,
        )
        # visible_answer, not a paired-tag regex: the digest model is the same served
        # student, whose template prefills the opening tag, so its reply is
        # "reasoning</think>extraction" with no opener. A paired regex matches nothing
        # there and the whole chain of thought was being appended to the tool result and
        # persisted into the trajectory.
        digested = visible_answer(getattr(response, "content", None))
        if digested and use_head > 0:
            digested = f"{digested}\n\n[page head, verbatim]\n{text[:use_head]}"
        return digested

    return digest


class _Shared:
    """One activation's shared state: config, store, gear map, lazy instances."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        raw = dict(ctx.config or {})
        self.cfg = FlowConfig.from_slice(raw)
        search_slice = raw.get("search") if isinstance(raw.get("search"), dict) else {}
        fetch_slice = raw.get("fetch") if isinstance(raw.get("fetch"), dict) else {}
        self.search_api_key = search_slice.get("apiKey")
        self.fetch_api_key = fetch_slice.get("apiKey")
        # The default reader's key, for the build below. The slice carries one
        # key per role, so the vendor table the kernel looks a provider up in
        # has no counterpart here; this is the only other vendor the fetch half
        # can end up on.
        self.fetch_fallback_api_key = fetch_slice.get("fallbackApiKey")
        # Degraded rather than refused: an unknown vendor name in the slice is a
        # typo the launcher cannot catch, and falling back to the default keeps
        # the pair serving instead of taking the whole flow down with it.
        self.search_provider = resolve_search_provider(search_slice.get("provider"))
        self.fetch_provider = resolve_fetch_provider(fetch_slice.get("provider"))
        self.proxy = raw.get("proxy")
        state_root = raw.get("stateRoot")
        root = Path(state_root) if state_root else Path(ctx.services.workspace) / "research_flow"
        self.store = SessionStore(root)
        set_ledger_dir(self.store.ledger_dir)
        self.session_gear: dict[str, SessionGear] = {}
        self._web_search: WebSearchTool | None = None
        self._search_built = False
        self._web_fetch: WebFetchTool | None = None
        self._ask_user: DRAskUserTool | None = None
        self._hook: ResearchFlowHook | None = None
        self._installable: bool | None = None

    @property
    def provider(self) -> "LLMProvider | None":
        return self._ctx.services.provider

    def installable(self) -> bool:
        """Whether this plugin may contribute anything at all.

        One answer for the hook and the three tools, because they are one
        contribution: the tools are per-session only through the hook, which
        selects the session (``set_current_session``) and resets its slot
        (``start_turn``) at the top of every turn. Contributed without it, every
        session collapses into the ContextVar's default slot and nothing ever
        resets it -- the replay cache, the dedup sets and the retry budget go
        process-wide and permanent -- and the clarify tool can never be granted
        a round trip. Declining together leaves the kernel's own web tools
        serving, which is what a build without this plugin gets.
        """
        if self._installable is None:
            cfg = self.cfg
            self._installable = True
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
                        "are enabled: {}; the flow and its tools are not installed",
                        ", ".join(llm_gates),
                    )
                    self._installable = False
        return self._installable

    def web_search(self) -> WebSearchTool | None:
        """The search tool, or ``None`` when it has no key to search with.

        The fork refused to register a keyless ``web_search`` rather than
        advertise a tool whose every call is an error string, and the contract
        this flow renders tells the model its tools are exactly these. Ask the
        built tool rather than the config slice: it resolves the key at call
        time from the slice or from the selected vendor's env var, and gating
        on the slice alone would withdraw the tool from a deploy that only
        exports the var.
        """
        if not self._search_built:
            self._search_built = True
            cfg = self.cfg
            gear = self.session_gear

            # The chain owns the session's EvidenceRound / SearchSaturation and
            # registers them in the gear map; these factories hand the tool the
            # SAME instances, so the gate that opens a round and the tool that
            # spends it agree. The base-config fallback covers a tool driven
            # outside a session the chain has geared up (a direct caller, a test).
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

            tool = WebSearchTool(
                api_key=self.search_api_key,
                max_results=cfg.search.rendered_width,
                proxy=self.proxy,
                provider=self.search_provider,
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
            if tool.api_key:
                self._web_search = tool
            else:
                spec = SEARCH_PROVIDERS[self.search_provider]
                logger.warning(
                    "research-flow: web_search is not registered -- no {} key in "
                    "plugins.config['research-flow'].search.apiKey and no {}",
                    spec.label,
                    spec.env_var,
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
                    gear = self.session_gear

                    # Same shape as the search tool's gear factories: the chain
                    # registers the session's (mode-resolved) digest knobs and
                    # the tool reads them at call time, so a deep session's
                    # digest model is deep's and not the base config's. The
                    # base-config fallback covers a call outside any geared
                    # session (a direct caller, a test).
                    def digest_knobs() -> tuple[str | None, int]:
                        slot = gear.get(current_session())
                        if slot is not None:
                            return slot.digest_model, slot.digest_verbatim_head_chars
                        return cfg.digest.model, cfg.digest.verbatim_head_chars

                    kwargs.update(
                        digest_fn=_make_digest_fn(
                            provider,
                            cfg.digest.model,
                            verbatim_head_chars=cfg.digest.verbatim_head_chars,
                            resolve=digest_knobs,
                        ),
                        digest_threshold_chars=cfg.digest.threshold_chars,
                        digest_timeout_s=cfg.digest.timeout_seconds,
                    )
            # Resolved here, at the build, exactly where the kernel resolves it:
            # a keyed backend with no key would be advertised and refuse every
            # call, and this tool replaces the kernel's, so declining to degrade
            # makes a research run the one place that config cannot read a page.
            #
            # The key follows the provider, as it does at the kernel's own build
            # (``wiring`` looks the key up a second time, under the EFFECTIVE
            # provider). Passing the selected vendor's key to the reader that
            # replaced it would run the fallback anonymously while the host had
            # a key configured for exactly that reader.
            #
            # A lookup keyed by destination rather than a "did it change" test:
            # a credential must never come to rest against an endpoint it was
            # not issued for, so a provider neither of these keys belongs to
            # gets none. The selection is written second because it wins when it
            # IS the default reader, whose own key is then ``fetch.apiKey``.
            fetch_provider = WebFetchTool.effective_provider(self.fetch_provider, self.fetch_api_key)
            fetch_keys = {
                DEFAULT_FETCH_PROVIDER: self.fetch_fallback_api_key,
                self.fetch_provider: self.fetch_api_key,
            }
            self._web_fetch = WebFetchTool(
                api_key=fetch_keys.get(fetch_provider),
                proxy=self.proxy,
                provider=fetch_provider,
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
            # Built once from the BASE config, deliberately: no shipped mode
            # overlay touches askUser (the launcher tests pin that), while the
            # gates ARE rebuilt per (session, mode). The day a mode overlay
            # wants askUser knobs, this must consult the per-(session, mode)
            # config the gates already resolve -- the tripwire will say so.
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


def _contributing(ctx: "PluginContext") -> _Shared | None:
    """The activation's shared state, or ``None`` when it contributes nothing."""
    shared = _shared_for(ctx)
    if not shared.cfg.enabled or not shared.installable():
        return None
    return shared


def make_hook(ctx: "PluginContext") -> ResearchFlowHook | None:
    """Factory for the ``research_flow`` hook contribution."""
    shared = _contributing(ctx)
    return shared.hook() if shared else None


def make_web_search(ctx: "PluginContext") -> WebSearchTool | None:
    """Factory for the ``web_search`` replacement tool; declines when it has no key."""
    shared = _contributing(ctx)
    return shared.web_search() if shared else None


def make_web_fetch(ctx: "PluginContext") -> WebFetchTool | None:
    """Factory for the ``web_fetch`` replacement tool."""
    shared = _contributing(ctx)
    return shared.web_fetch() if shared else None


def make_ask_user(ctx: "PluginContext") -> DRAskUserTool | None:
    """Factory for the ``ask_user`` clarify tool; declines when the round is off."""
    shared = _contributing(ctx)
    return shared.ask_user() if shared else None


__all__ = [
    "make_ask_user",
    "make_hook",
    "make_web_fetch",
    "make_web_search",
]
