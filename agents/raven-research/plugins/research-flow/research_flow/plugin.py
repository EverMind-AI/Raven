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
it), ``proxy``, ``stateRoot`` (defaults to ``<workspace>/research_flow``), and
``corpusEndpoint`` (a retrieval service over one fixed document collection;
when set, both web tools read it instead of any vendor, keyless, and the
vendor, key and proxy keys above are not consulted).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from research_flow.config import DigestConfig, FlowConfig, ModelEndpointConfig
from research_flow.flow import ResearchFlowHook, SessionGear, ToolHandles, evidence_round_for, saturation_for
from research_flow.prompts import _DIGEST_SYSTEM, _EXTRACT_SYSTEM, _INCREMENTAL_EXTRACTOR
from research_flow.state import SessionStore
from research_flow.support.answer_text import visible_answer
from research_flow.support.guard_audit import set_audit_dir
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
    from raven.config.schema import ModelEndpoint
    from raven.contracts.llm_provider import LLMProvider
    from raven.plugins.context import PluginContext

# The trunk's ``agents.defaults.maxToolIterations`` default. A plugin factory
# resolves before any turn runs, so this stands in until the loop's enforced
# cap arrives on the iteration context (``ctx.max_iterations``, hook surface
# v3); a chain built from a turn reads that instead, and explicit config wins
# over both.
_DEFAULT_MAX_ITERATIONS = 40


_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_D = DigestConfig.model_fields


def _split_by_chars(text: str, max_chars: int) -> list[str]:
    """HyWe's ``_split_text_by_chars``: cut at the last paragraph, line or space break.

    Ported rather than imported, and denominated in characters rather than tokens,
    because this flow carries no tokenizer (``DigestConfig.chunk_chars`` says what the
    two denominations are worth to each other). A break counts only in the window's
    second half, so a page with no whitespace still advances a whole chunk instead of
    crawling forward a character at a time.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start, length = 0, len(text)
    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            split_at = text.rfind("\n\n", start, end)
            if split_at <= start + max_chars // 2:
                split_at = text.rfind("\n", start, end)
            if split_at <= start + max_chars // 2:
                split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
        while start < length and text[start].isspace():
            start += 1
    return chunks or [text[:max_chars]]


def _parsed_json(raw: str) -> "dict[str, Any] | None":
    """HyWe's ``_parse_json_output``: a fenced block first, then the whole reply."""
    if not raw:
        return None
    candidates = [raw.strip()]
    if fenced := _FENCED_JSON.search(raw):
        candidates.insert(0, fenced.group(1).strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except Exception:
            continue
        return value if isinstance(value, dict) else None
    return None


def _field_text(value: Any) -> str:
    """HyWe's ``_stringify_summary_field``: a list becomes lines, a dict its own JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(t for t in (_field_text(v) for v in value) if t).strip()
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2).strip()
        except Exception:
            return str(value).strip()
    return str(value).strip()


def _carried(parsed: "dict[str, Any] | None", raw: str) -> str:
    """What the next chunk is shown as ``existing_summary`` (HyWe's accumulator).

    The reply's own JSON when it parsed, its raw text when it did not: a chunk whose
    output the next call cannot read would otherwise drop everything read so far.
    """
    if isinstance(parsed, dict):
        sections = []
        if evidence := _field_text(parsed.get("evidence")):
            sections.append(f"Evidence:\n{evidence}")
        if summary := _field_text(parsed.get("summary")):
            sections.append(f"Summary:\n{summary}")
        if sections:
            return "\n\n".join(sections).strip()
    return (raw or "").strip()


def _assembled(parsed: "dict[str, Any] | None", raw: str, notes: "list[str]") -> str:
    """The last chunk's extraction as the model reads it, HyWe's section order.

    Without HyWe's ``The useful information in '<url>' for user goal '<goal>'`` header:
    the fetch result is a JSON envelope that already carries ``url`` and
    ``info_to_extract``, so the header would repeat them inside the one field the
    model is meant to read as page content.
    """
    note = " ".join(n for n in notes if n)
    if isinstance(parsed, dict):
        parts = []
        if evidence := _field_text(parsed.get("evidence")):
            parts.append(f"Evidence in page:\n{evidence}")
        if summary := _field_text(parsed.get("summary")):
            parts.append(f"Summary:\n{summary}")
        if parts:
            body = "\n\n".join(parts)
            return f"{body}\n\nNote:\n{note}" if note else body
    body = (raw or "").strip()
    if not note:
        return body
    return f"Note:\n{note}\n\n{body}" if body else f"Note:\n{note}"


def _answer(response: Any) -> str:
    """The reply's visible text, with an exhausted retry ladder raised, not read.

    ``chat_with_retry`` does not raise when its ladder runs out: it returns
    ``finish_reason == "error"`` carrying the provider's error text as the content.
    Read as an extraction, that text becomes the page the model is shown, and the
    documented fallback to truncation never fires because nothing raised.
    """
    if getattr(response, "finish_reason", None) == "error":
        raise RuntimeError(f"digest call failed: {getattr(response, 'content', '')}"[:300])
    # visible_answer, not a paired-tag regex: unless ``digest.model`` names another
    # endpoint, the digest model is the same served student, whose template prefills
    # the opening tag, so its reply is "reasoning</think>extraction" with no opener. A
    # paired regex matches nothing there and the whole chain of thought was being
    # appended to the tool result and persisted into the trajectory.
    return visible_answer(getattr(response, "content", None)) or ""


def _make_digest_fn(
    provider: "LLMProvider",
    model: str | None,
    verbatim_head_chars: int = 0,
    *,
    source_cap_chars: int = _D["source_cap_chars"].default,
    map_reduce: bool = _D["map_reduce"].default,
    chunk_chars: int = _D["chunk_chars"].default,
    max_chunks: int = _D["max_chunks"].default,
    per_call_timeout_s: float = _D["timeout_seconds"].default,
    total_timeout_s: float = _D["total_timeout_seconds"].default,
    resolve: "Callable[[], tuple[str | None, int]] | None" = None,
):
    """The fork's digest closure: targeted extraction for web_fetch.

    Under ``map_reduce`` the page is read in ``chunk_chars`` pieces and the extraction
    rolls forward from one to the next (HyWe's ``INCREMENTAL_EXTRACTOR_PROMPT``, same
    prompt, same section order), so the tail of a long page reaches the model instead
    of falling off ``source_cap_chars``. Off, the single call on a truncated page that
    every run before 2026-11-12 made.

    ``resolve`` is what makes the digest follow the session's mode. The fetch
    tool is built once from the base config, but a mode overlay may move
    ``digest.model`` / ``digest.verbatimHeadChars``; when given, ``resolve()`` is
    consulted per call and returns the pair in force for the current session,
    falling back to the base values handed in here. The threshold, the timeouts and
    the chunking stay the tool's constructor arguments and therefore the base
    config's.
    """

    def _with_head(digested: str | None, text: str, use_head: int) -> str | None:
        if digested and use_head > 0:
            return f"{digested}\n\n[page head, verbatim]\n{text[:use_head]}"
        return digested

    async def _extract(prompt: str, use_model: str | None, timeout: float) -> str:
        # No max_tokens, where HyWe caps its summariser at 4000: unless ``digest.model``
        # names another endpoint this is the served student, whose reasoning is billed
        # against the same cap, and a cap that cuts inside the reasoning returns nothing.
        response = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                model=use_model,
                temperature=0.0,
            ),
            timeout=timeout,
        )
        return _answer(response)

    async def digest(text: str, info_to_extract: str) -> str | None:
        use_model, use_head = (model, verbatim_head_chars) if resolve is None else resolve()
        notes: list[str] = []
        source = text
        if len(source) > source_cap_chars:
            notes.append(f"Reader output was capped at {source_cap_chars} of {len(text)} characters.")
            source = source[:source_cap_chars]

        if not map_reduce:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _DIGEST_SYSTEM},
                    {
                        "role": "user",
                        "content": (f"Information needed: {info_to_extract}\n\nPage content:\n{source}"),
                    },
                ],
                model=use_model,
                temperature=0.1,
            )
            return _with_head(_answer(response), text, use_head)

        chunks = _split_by_chars(source, chunk_chars)
        if len(chunks) > max_chunks:
            read = sum(len(c) for c in chunks[:max_chunks])
            chunks = chunks[:max_chunks]
            notes.append(f"Only the first {max_chunks} chunks ({read} characters) were read.")
        if len(chunks) > 1:
            notes.append(
                f"Webpage content summarized incrementally in {len(chunks)} chunks of up to {chunk_chars} characters."
            )

        carried, raw, parsed = "", "", None
        deadline = time.monotonic() + total_timeout_s
        for index, chunk in enumerate(chunks, 1):
            left = deadline - time.monotonic()
            if index > 1 and left <= 0:
                notes.append(f"Extraction stopped after chunk {index - 1} of {len(chunks)} on its time budget.")
                break
            prompt = _INCREMENTAL_EXTRACTOR.format(
                existing_summary=carried,
                webpage_content=chunk,
                goal=info_to_extract,
                chunk_index=index,
                chunk_count=len(chunks),
            )
            try:
                reply = await _extract(prompt, use_model, max(1.0, min(per_call_timeout_s, left)))
            except Exception as e:
                # The first chunk's failure is the whole digest's, and the tool falls
                # back to truncation, which is where a digest that cannot run belongs.
                # A later chunk's leaves the ones already read standing, which is
                # strictly more of the page than truncation would have shown.
                if index == 1:
                    raise
                logger.warning(
                    "research-flow: digest chunk {}/{} failed ({}: {}); keeping the {} already read",
                    index,
                    len(chunks),
                    type(e).__name__,
                    e,
                    index - 1,
                )
                notes.append(f"Extraction stopped after chunk {index - 1} of {len(chunks)}: {type(e).__name__}.")
                break
            if not reply.strip():
                # HyWe would overwrite the accumulator here and lose the page. An empty
                # reply is a call that said nothing, not a page that holds nothing.
                logger.warning(
                    "research-flow: digest chunk {}/{} returned nothing; keeping what was read", index, len(chunks)
                )
                continue
            raw = reply
            parsed = _parsed_json(raw)
            carried = _carried(parsed, raw)

        return _with_head(_assembled(parsed, raw, notes), text, use_head)

    return digest


def _resolve_endpoints(endpoints: Sequence[ModelEndpointConfig]) -> "list[ModelEndpoint]":
    """``modelEndpoints`` with each key read from its env var, refusing one that is unset.

    Refused rather than skipped: a skipped entry would send its model's calls to the
    session's provider, and a reviewer or digest silently becoming another model is the
    substitution this knob exists to rule out.
    """
    from raven.config.schema import ModelEndpoint

    resolved = []
    for ep in endpoints:
        key = "EMPTY"
        if ep.api_key_env:
            key = os.environ.get(ep.api_key_env, "")
            if not key:
                raise ValueError(
                    f"research-flow: modelEndpoints entry {ep.model!r} reads its key from "
                    f"{ep.api_key_env}, which is not set"
                )
        resolved.append(ModelEndpoint(model=ep.model, api_base=ep.api_base, api_key=key))
    return resolved


def _routed(provider: "LLMProvider | None", endpoints: "list[ModelEndpoint]") -> "LLMProvider | None":
    """The lent provider, with ``endpoints``' models dispatched to their own endpoints."""
    if provider is None or not endpoints:
        return provider
    from raven.providers.per_model_provider import PerModelProvider

    class _FlowRoutedProvider(PerModelProvider):
        # PerModelProvider reports its first routed model as the default, which suits
        # a router; here a call naming no model, and the ledger's reviewer column,
        # must still mean the session's own model.
        def get_default_model(self) -> str:
            return self._fallback.get_default_model()

    return _FlowRoutedProvider(endpoints, fallback=provider)


class _Shared:
    """One activation's shared state: config, store, gear map, lazy instances."""

    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        raw = dict(ctx.config or {})
        self.cfg = FlowConfig.from_slice(raw)
        self._endpoints = _resolve_endpoints(self.cfg.model_endpoints)
        self._provider: LLMProvider | None = None
        self._provider_built = False
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
        # One key for both halves, so a run cannot search the collection while it
        # reads pages off the web.
        self.corpus_endpoint = raw.get("corpusEndpoint") or None
        if self.corpus_endpoint:
            logger.info("research-flow: web_search and web_fetch read the corpus at {}", self.corpus_endpoint)
        state_root = raw.get("stateRoot")
        root = Path(state_root) if state_root else Path(ctx.services.workspace) / "research_flow"
        self.store = SessionStore(root)
        set_ledger_dir(self.store.ledger_dir)
        # Beside the ledger, not in it: the ledger writes only between
        # open_product_ledger and close_product_ledger, which the evaluation path
        # never calls -- so a guard row routed through it would have gone nowhere.
        _guard_cfg = self.cfg.benchmark_guard
        set_audit_dir(self.store.ledger_dir if _guard_cfg.enabled and _guard_cfg.audit else None)
        self.session_gear: dict[str, SessionGear] = {}
        self._web_search: WebSearchTool | None = None
        self._search_built = False
        self._web_fetch: WebFetchTool | None = None
        self._ask_user: DRAskUserTool | None = None
        self._hook: ResearchFlowHook | None = None
        self._installable: bool | None = None

    @property
    def provider(self) -> "LLMProvider | None":
        if not self._provider_built:
            self._provider_built = True
            self._provider = _routed(self._ctx.services.provider, self._endpoints)
        return self._provider

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
        """The search tool, or ``None`` when it has no key to search with and no corpus.

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
                corpus_endpoint=self.corpus_endpoint,
                benchmark_guard=cfg.benchmark_guard.enabled,
                benchmark_guard_queries=cfg.benchmark_guard.block_queries,
            )
            if tool.corpus_endpoint or tool.api_key:
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
                            source_cap_chars=cfg.digest.source_cap_chars,
                            map_reduce=cfg.digest.map_reduce,
                            chunk_chars=cfg.digest.chunk_chars,
                            max_chunks=cfg.digest.max_chunks,
                            per_call_timeout_s=cfg.digest.timeout_seconds,
                            total_timeout_s=cfg.digest.total_timeout_seconds,
                            resolve=digest_knobs,
                        ),
                        digest_threshold_chars=cfg.digest.threshold_chars,
                        # Under map-reduce the closure budgets the page itself and
                        # returns what it managed to read, so the tool's own deadline is
                        # the outer one: it must catch a closure that is stuck, never cut
                        # a page short, because a cut here drops the digest and truncates
                        # the page at ``fetchMaxChars``.
                        digest_timeout_s=(
                            cfg.digest.total_timeout_seconds + 60.0
                            if cfg.digest.map_reduce
                            else cfg.digest.timeout_seconds
                        ),
                    )
            if self.corpus_endpoint:
                self._web_fetch = WebFetchTool(corpus_endpoint=self.corpus_endpoint, **kwargs)
                return self._web_fetch
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
                benchmark_guard=cfg.benchmark_guard.enabled,
                benchmark_guard_question=cfg.benchmark_guard.block_question_pages,
                benchmark_guard_question_chars=cfg.benchmark_guard.question_run_chars,
                benchmark_guard_question_shadow=cfg.benchmark_guard.question_pages_shadow,
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
