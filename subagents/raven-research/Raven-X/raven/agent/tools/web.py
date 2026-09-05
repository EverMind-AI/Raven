"""Web tools: web_search and web_fetch, plus the client-side retrieval ledger."""

import asyncio
import json
import os
import time
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from raven.agent.evidence_round import EvidenceRound
from raven.agent.harness_text import search_closed_notice
from raven.agent.ledger import ledger_append as _ledger_append
from raven.agent.search_saturation import SearchSaturation
from raven.agent.tools.base import Tool
from raven.security.benchmark_containment import BenchmarkContainment
from raven.security.network import validate_url_target_async

DigestFn = Callable[[str, str], Awaitable[str]]

# The ledger itself lives in raven/agent/ledger.py: the flow hooks write to it too, and
# a flow module importing a tool module to reach the writer is the wrong direction.


# Retry policy, shared by both web tools so one outage is answered one way. These
# failures never produced a response, so re-sending is safe, and the statuses are the
# ones a service returns while shedding load. Every other 4xx re-raises at once: the
# batch that lost its entire read surface lost it to a 402 (account balance), and
# retrying that spends wall clock to arrive at the same answer. ProxyError,
# LocalProtocolError and UnsupportedProtocol are configuration or caller faults rather
# than transients, and stay out even though ProxyError is a TransportError subclass.
_RETRY_ON_TRANSPORT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_RETRY_ON_STATUS = frozenset({429, 502, 503, 504})
# Every SERP element a deep-research profile can switch off, sized in characters so the
# ledger can answer "how much did the flow-off anchor get from a channel the DR arm
# turned off". Always present and always an int, so a reader can sum without branching;
# which renderer produced the row is already in ``source``.
_NO_SHAPING = {"answer_box_chars": 0, "knowledge_chars": 0,
               "snippet_chars": 0, "snippet_lines": 0, "dedup_skipped": 0,
               # dr@3.2. How many results the endpoint actually handed back, before
               # any width slice or dedup. Ledger-only, no behaviour change.
               #
               # Why it has to exist: ``n`` on the row is the RENDERED count, written
               # after the slice, so no landed row can express "the call returned 9
               # and we showed 5". That question decided a whole line of work and
               # could only be answered from two standalone side probes -- the run
               # data was structurally silent, so it was never checkable per arm or
               # per question. Measured there: 62.45% of the DR arm's non-suppressed
               # searches render exactly 5 while the same call is served 8-10.
               #
               # ``None`` - not 0 - wherever the call never reached an endpoint
               # (replay, suppression, transport error). "We never asked" and "we
               # asked and got nothing" have different fixes, and a ledger writing 0
               # for both makes the zero-hit rate unreadable. That is the lesson
               # ``dedup_skipped`` two lines up cost, and it is not repeated here.
               "n_served": None,
               # dr@3.0. ``dedup_skipped`` belongs to ``cross_query_dedup`` and reads 0
               # on every landed batch for two independent reasons, neither visible in a
               # 0: no published arm enabled that knob, AND on the live-web path the
               # counting branch is unreachable anyway because the pool is never deeper
               # than the rendered width. Meanwhile ``snippet_dedup_by_docid`` - the
               # knob that IS on in every published DR web arm - had no counter at all,
               # so the only evidence it did anything was an inter-arm difference in
               # snippet characters, i.e. an effect inferred from an aggregate rather
               # than an event recorded when it happened. This counts the marks.
               "snippet_repeat_marks": 0}


def _error_shaping(exc: Exception) -> dict:
    """Shaping payload for a search that died in transport.

    ``transport_err: true`` alone made a drained API key and a network blip the
    same row - during the one mid-run outage this project has already eaten, the
    ledger could not say which it was. The error string and status make the row
    diagnosable; ``quota_err`` singles out the credential/billing statuses the
    rollout-time balance polling watches for.
    """
    shaping = dict(_NO_SHAPING)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    shaping["error"] = f"{type(exc).__name__}: {exc}"[:500]
    shaping["status"] = int(status) if status is not None else None
    shaping["quota_err"] = status in (401, 402, 403)
    return shaping


@dataclass(frozen=True)
class SearchProviderSpec:
    """One ``web_search`` backend, described where every reader can see it.

    ``paginates`` is the load-bearing field: a provider that serves no offset
    cannot answer the saturation rule's page-2 rung, and the rule has to be told
    up front (``SearchSaturation.paginates``) rather than discovering it from a
    request that comes back identical. That is the same declaration the corpus
    path already makes, and for the same reason -- a capability whose activation
    site is unreachable, with no symptom, is the failure this avoids.

    There is no per-tool key field: the credential lives at
    ``tools.web.providers.<vendor>.apiKey``, because one AnySearch account serves
    both web tools and a key held per tool would have to be pasted twice.
    """

    vendor: str
    label: str
    env_var: str
    paginates: bool

    @property
    def config_path(self) -> str:
        return f"tools.web.providers.{self.vendor}.apiKey"


DEFAULT_SEARCH_PROVIDER = "serper"

SEARCH_PROVIDERS: dict[str, SearchProviderSpec] = {
    "serper": SearchProviderSpec(
        vendor="serper",
        label="Serper",
        env_var="SERPER_API_KEY",
        paginates=True,
    ),
    "anysearch": SearchProviderSpec(
        vendor="anysearch",
        label="AnySearch",
        env_var="ANYSEARCH_API_KEY",
        # No offset of any kind, so the ``paginate`` rung is declared unavailable
        # rather than attempted. ``widen`` is NOT affected and stays useful here:
        # probed live at max_results 3 and 10, this endpoint returns exactly what
        # was asked for, where Serper answers 8-10 whatever ``num`` says.
        paginates=False,
    ),
    "serpapi": SearchProviderSpec(
        vendor="serpapi",
        label="SerpApi",
        env_var="SERPAPI_API_KEY",
        # ``start`` is an offset in results, so page N maps onto start=(N-1)*num.
        paginates=True,
    ),
}


@dataclass(frozen=True)
class FetchProviderSpec:
    """One ``web_fetch`` backend.

    ``needs_key`` decides whether the tool may be advertised at all: Jina reads
    pages unauthenticated (at a lower rate limit), AnySearch does not. A tool
    that cannot run must not be registered - the model reaches for it, every
    call fails, and the error text naming a config path is relayed to whoever is
    on the other end of the channel.

    ``extractor`` is an instrument column, not a label: it is what the fetch
    ledger records as having served the page, and the two backends are not
    comparable (see ``WebFetchConfig.provider``).
    """

    vendor: str
    label: str
    env_var: str
    extractor: str
    needs_key: bool

    @property
    def config_path(self) -> str:
        return f"tools.web.providers.{self.vendor}.apiKey"


DEFAULT_FETCH_PROVIDER = "jina"

FETCH_PROVIDERS: dict[str, FetchProviderSpec] = {
    "jina": FetchProviderSpec(
        vendor="jina",
        label="Jina Reader",
        env_var="JINA_API_KEY",
        extractor="jina-reader",
        # Unauthenticated r.jina.ai works, so an absent key is a degradation
        # rather than a broken tool. A DEAD key is worse than none - it answers
        # 402 where no key answers 200 - but that is not something a schema can
        # tell apart from a live one.
        needs_key=False,
    ),
    "anysearch": FetchProviderSpec(
        vendor="anysearch",
        label="AnySearch",
        env_var="ANYSEARCH_API_KEY",
        extractor="anysearch-extract",
        needs_key=True,
    ),
}


def _vendor_key(web_config: Any, vendor: str) -> str:
    """The configured credential for one vendor, or an empty string."""
    providers = getattr(web_config, "providers", None)
    return str(getattr(getattr(providers, vendor, None), "api_key", "") or "")


def selected_search_provider(search_config: Any) -> str:
    """The provider a ``tools.web.search`` section selects, degrading to default."""
    provider = getattr(search_config, "provider", "") or DEFAULT_SEARCH_PROVIDER
    return provider if provider in SEARCH_PROVIDERS else DEFAULT_SEARCH_PROVIDER


def selected_search_key(web_config: Any) -> tuple[str, str]:
    """``(provider, configured key)`` for a ``tools.web`` section."""
    provider = selected_search_provider(getattr(web_config, "search", None))
    return provider, _vendor_key(web_config, provider)


def selected_fetch_provider(fetch_config: Any) -> str:
    """The provider a ``tools.web.fetch`` section selects, degrading to default."""
    provider = getattr(fetch_config, "provider", "") or DEFAULT_FETCH_PROVIDER
    return provider if provider in FETCH_PROVIDERS else DEFAULT_FETCH_PROVIDER


def selected_fetch_key(web_config: Any) -> tuple[str, str]:
    """``(provider, configured key)`` for a ``tools.web`` section."""
    provider = selected_fetch_provider(getattr(web_config, "fetch", None))
    return provider, _vendor_key(web_config, provider)


def web_provider_keys(web_config: Any) -> dict[str, str]:
    """Every configured vendor credential, keyed by vendor.

    Handed to the fetch tool whole rather than one key at a time: a fallback
    entry needs its own vendor's credential, and passing only the selected one
    would send it to a different vendor's endpoint.
    """
    vendors = {spec.vendor for spec in SEARCH_PROVIDERS.values()}
    vendors |= {spec.vendor for spec in FETCH_PROVIDERS.values()}
    return {vendor: key for vendor in sorted(vendors) if (key := _vendor_key(web_config, vendor))}


def selected_fetch_fallback(fetch_config: Any) -> list[str]:
    """The fallback chain a ``tools.web.fetch`` section declares, minus the
    selected provider and anything unknown."""
    selected = selected_fetch_provider(fetch_config)
    chain = getattr(fetch_config, "fallback", None) or []
    seen = {selected}
    out: list[str] = []
    for name in chain:
        if name in FETCH_PROVIDERS and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# The corpus retrieval service clamps to this of its own accord
# (``bcplus_serve.py``), so asking for more would silently return the same rows.
_MAX_SEARCH_DEPTH = 50
_RETRY_BACKOFF_S = (1.0, 4.0)
_RETRY_BUDGET_PER_TURN = 30


class _RetryBudget:
    """A per-turn ceiling on retries, shared by every call one tool makes in a turn.

    Without it the only cap is two retries per call, unbounded per question, and the
    cost lands where the benefit does: the arm that browses hardest issues the most
    calls and absorbs the most waiting. One measured question met 24 disconnects; if
    the failure mode were a read timeout rather than a fast drop, an unbudgeted worst
    case would spend roughly 40% of the 5400s per-question limit on retries alone, on
    the arm that fetches four times more than the anchor.
    """

    __slots__ = ("left",)

    def __init__(self, n: int = _RETRY_BUDGET_PER_TURN) -> None:
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _retry_delay(backoff: float, status: int | None, retry_after: str | None, key: str) -> float:
    """How long to wait before re-sending.

    Transport errors get the fixed schedule: this is an instrument, and a random wait
    is one more thing that differs between two runs of the same arm. A rate limiter is
    the exception, because a fixed schedule has every concurrent worker sleep the same
    duration and re-send in lockstep - which is how the limit was reached. The spread
    is derived from the request key rather than drawn at random, so two workers asking
    for different things separate while a rerun of one arm still replays identically.
    """
    if status != 429:
        return backoff
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 60.0))
        except ValueError:
            pass
    return backoff * (1.0 + (zlib.crc32(key.encode("utf-8")) % 1000) / 1000.0)


async def _send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    op: str,
    key: str,
    budget: "_RetryBudget | None" = None,
) -> httpx.Response:
    """Send, retrying only what never produced a response or asked us to wait.

    Raises the last error once the attempts run out, so each caller's existing
    handlers keep their shape.

    A retry gets its own ledger op rather than being folded into the ``search`` or
    ``fetch`` line. Counting one as a call would inflate an arm's call total by
    however many transient errors it happened to meet, which is the arm-correlated
    artifact the ledger exists to rule out. Leaving it unrecorded is the opposite
    failure and the more dangerous one: the tool-surface gate fires on the fetch
    failure rate, so a repair that quietly retries an outage away would return that
    rate to normal and blind the gate to the outage it was built to catch.
    """
    for attempt, backoff in enumerate((*_RETRY_BACKOFF_S, None)):
        status: int | None = None
        retry_after: str | None = None
        try:
            r = await send()
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _RETRY_ON_STATUS:
                raise
            status = e.response.status_code
            retry_after = e.response.headers.get("Retry-After")
            last: Exception = e
        except _RETRY_ON_TRANSPORT as e:
            last = e
        if backoff is None or (budget is not None and not budget.take()):
            raise last
        _ledger_append({
            "ts": time.time(),
            "op": op,
            "key": key,
            "attempt": attempt + 1,
            "status": status,
            "error": str(last),
        })
        await asyncio.sleep(_retry_delay(backoff, status, retry_after, key))
    raise AssertionError("unreachable: the final backoff slot re-raises")


# Deep research strips snippets so the model cannot answer off the SERP, which
# also removes the only signal telling it whether a query already paid off. It
# then re-issues the query from memory instead of from evidence: measured over
# one 230-question batch, 34.2% of a DR arm's searches were byte-identical
# repeats (24.7% without the stripping), rising to 63% inside the questions that
# exhausted the iteration budget, and 73% of repeats came back later in the turn
# rather than back-to-back. Saying so costs one line and needs no extra turn.
_SNIPPET_REPEAT_NOTE = "[preview already shown above for this result]"
_REPEAT_NOTE = (
    "[repeat] This exact query already ran earlier in this turn; the results "
    "below are the ones it returned then, served from cache. If you still have "
    "them in view, fetch a result you have not opened yet or change the query "
    "terms."
)
# Three claims were removed from this note rather than reworded.
#   * The ordinal: ``self._searches`` only increments on the cache-miss path, so
#     "#n" counted distinct queries, not queries. Measured on one batch, 14 of 47
#     notices named the wrong index, one of them by 16.
#   * "the results below are unchanged": no request was issued, so nothing was
#     compared. What is true is that they are the earlier results.
#   * "Re-running it cannot surface anything new": false once
#     ``_emergency_shrink`` has replaced older tool bodies with a placeholder -
#     re-issuing is then the only way the model gets a lost SERP back, and 120 of
#     201 repeat fetches in one batch happened on questions that ended correct.
#     Telling it the opposite argued against the one recovery move it has.


class WebSearchTool(Tool):
    """Search the web through the configured provider (Serper by default)."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    # The description is part of the tool schema, i.e. it is prompt. With
    # ``include_snippets=False`` the static text promised something the tool
    # never returned, and contradicted the DR contract's "results list titles
    # and links only" twenty lines away in the same context window. A tool that
    # advertises snippets it does not deliver is one more reason to re-issue the
    # query.
    _DESCRIPTION_NO_SNIPPETS = "Search the web. Returns titles and URLs only, no snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        proxy: str | None = None,
        include_answer_box: bool = True,
        include_knowledge_graph: bool = True,
        include_snippets: bool = True,
        snippet_dedup_by_docid: bool = False,
        cross_query_dedup: bool = False,
        search_depth: int = 20,
        corpus_endpoint: str | None = None,
        repeat_notice: bool = False,
        containment: BenchmarkContainment | None = None,
        evidence_round: "EvidenceRound | None" = None,
        saturation: "SearchSaturation | None" = None,
        provider: str = DEFAULT_SEARCH_PROVIDER,
    ):
        if provider not in SEARCH_PROVIDERS:
            logger.warning("WebSearch: unknown provider '{}', using {}", provider, DEFAULT_SEARCH_PROVIDER)
            provider = DEFAULT_SEARCH_PROVIDER
        self.provider = provider
        self._init_api_key = api_key
        # Shared with web_fetch so one arm reports one firing rate; a disabled
        # instance is the neutral default, so ordinary use is unchanged.
        self.containment = containment or BenchmarkContainment()
        self.max_results = max_results
        self.proxy = proxy
        self.corpus_endpoint = corpus_endpoint
        # Deep-research profiles disable these: direct answers (answerBox /
        # knowledgeGraph) and snippets let the model answer without ever
        # opening a page, which shallows browse-and-digest behavior.
        self.include_answer_box = include_answer_box
        self.include_knowledge_graph = include_knowledge_graph
        if not include_snippets:
            # Instance attribute shadows the class one, so the schema the model
            # is shown describes the tool it actually gets.
            self.description = self._DESCRIPTION_NO_SNIPPETS
        self.include_snippets = include_snippets
        # Snippet cost is per result slot, not per search: one is rendered for every
        # rank of every search, so a document surfaced by ten queries is paid for ten
        # times. Scoped to the turn like ``_prior`` below.
        self.snippet_dedup_by_docid = snippet_dedup_by_docid
        self._snippet_seen: set[str] = set()
        # Reset per SEARCH, not per turn: a cumulative counter reports the turn's total
        # on every row, and this project has already had a gate satisfied by a week-old
        # residue. Per-row counts sum to the turn; a turn total cannot be un-summed.
        self._snippet_repeat_marks = 0
        # Result-slot dedup is a separate set from the snippet one on purpose. They
        # answer different questions - "has this document been previewed" against
        # "has this document been listed at all" - and sharing one set would make
        # each knob silently change the other's behaviour, which is how an ablation
        # stops measuring the thing it names.
        self.cross_query_dedup = cross_query_dedup
        self.search_depth = search_depth
        self._result_seen: set[str] = set()
        # Off by default: annotating a repeat changes what the model reads, so
        # only a flow that declares the behavior gets it.
        self.repeat_notice = repeat_notice
        # Carries the ordered result URLs alongside the rendered text so a replay is
        # logged from what was actually served, not re-parsed out of the rendering.
        self._prior: dict[tuple[str, int, int, int], tuple[int, str, list[str], dict]] = {}
        self._searches = 0
        self._retry_budget = _RetryBudget()
        # Set by the verify gate on a rejection; read here. None outside a flow that
        # declares it, which keeps the anchor's behaviour bit-identical.
        self._evidence_round = evidence_round
        # dr@3.0. None outside a flow that declares it, so the anchor never builds one
        # and cannot be moved by it. The corpus service takes a width but no offset,
        # so tell the rule up front that paging is unavailable there rather than
        # letting a paginate-configured corpus arm quietly behave like a stopping one.
        self._saturation = saturation
        # Declared up front for the corpus path and for any provider that serves
        # no offset: ``_escalate`` cannot raise ``page`` once this is False, so
        # the rung is never reached rather than being reached and unanswerable.
        # The ledger still separates the two outcomes -- ``sat_action`` reports
        # ``stopped_degraded`` here against ``stopped`` on a real exhaustion.
        if saturation is not None and (corpus_endpoint or not self.spec.paginates):
            saturation.paginates = False

    @property
    def spec(self) -> SearchProviderSpec:
        return SEARCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get(self.spec.env_var, "")

    def start_turn(self, keep_identities: bool = False) -> None:
        """Drop the per-turn repeat memory.

        Scoped to the turn, not the process: the same query a week later is a
        legitimate re-check, and a gateway keeps one tool instance for the
        lifetime of the loop.

        ``keep_identities`` (dr@3.0, product surface, off by default) narrows that
        to the *budgets* and keeps the "which documents has this conversation
        already been shown" sets. The turn scope above is stated for a benchmark
        item, where each turn is a separate question; across turns of one
        conversation about one topic it makes the follow-up re-search and re-open
        the pages the previous turn already read. What still clears either way is
        everything that is a budget or a decision - the replay cache, the retry
        budget, the search count, the evidence round, the saturation rule's stop
        flag and streak - because carrying a *decision* across a turn boundary
        would let one turn close search for a question that had not been asked yet.
        """
        self._prior.clear()
        self._searches = 0
        self._retry_budget = _RetryBudget()
        if not keep_identities:
            self._snippet_seen.clear()
            self._result_seen.clear()
        self._snippet_repeat_marks = 0
        if self._evidence_round is not None:
            self._evidence_round.reset()
        if self._saturation is not None:
            self._saturation.reset(keep_seen=keep_identities)

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        # Rendered width and requested depth are two different numbers from dr@2.8
        # on. ``n_requested`` is what the model reads and is unchanged; ``k`` is how
        # far into the ranking the service is asked to look so that documents this
        # turn has already listed can be skipped and the list backfilled.
        n_requested = count or self.max_results
        # dr@3.0 "widen": once the rule has escalated, render what the endpoint was
        # already returning. Inert until then and inert without the rule, so the
        # request an un-saturated turn sends is unchanged.
        if self._saturation is not None:
            n_requested = self._saturation.width(n_requested)
        # dr@3.0. Checked before everything else, including the replay cache, because
        # the point of the rule is that the call does not happen - a stopped turn that
        # still got a cached page back would be receiving advice, not a control-flow
        # decision, and advice is the thing already measured not to work here.
        if self._saturation is not None and self._saturation.stopped:
            self._saturation.suppress()
            # dr@3.2: built from ``harness_text`` so the emitter and the two
            # recognisers cannot drift. They drifting apart would be silent, and
            # its symptom is this sentence becoming eligible as evidence again --
            # which is how it once shipped as a run's final answer (hle-256).
            refusal = search_closed_notice(self._saturation.k)
            self._log_search(query, n_requested, [], refusal, dict(_NO_SHAPING),
                             replay=False, k=None, suppressed=True)
            return refusal
        deep = self._evidence_round.depth if (
            self._evidence_round is not None and self._evidence_round.active
        ) else None
        k = self._request_width(n_requested, deep)
        # Read once, before either branch. The replay key below and the request itself
        # must be built from the same value; see ``_search``. Keyed on the normalized
        # query (dr@3.4): the ladder's escalation is turn-global, but a page is only
        # deeper for terms already served the pages before it - a fresh query sent to
        # page 2 skips ranks 1-10 for terms nobody has seen ranked, and its
        # near-certain empty return then scores as dry, feeding the ``stop`` rung.
        norm_query = " ".join((query or "").lower().split())
        page = self._saturation.page_for(norm_query) if self._saturation is not None else 1
        req_page = page if self._saturation is not None else None
        if not self.repeat_notice:
            if deep is not None:
                self._evidence_round.consume()
            result, urls, shaping = await self._search(query, n_requested, k=k, page=page)
            # Latched after the fact, and never on a transport error: an
            # errored request served no page, and latching it would send the
            # retry of the same query one page past results nobody saw.
            if self._saturation is not None and not self._transport_err(result):
                self._saturation.note_page(norm_query, page)
            self._log_search(query, n_requested, urls, result, shaping, replay=False, k=k,
                             page=req_page)
            return result
        # Keyed on every request dimension that changes what comes back: both widths
        # and the page. The same terms asked for more results is a different request,
        # so is the same terms looked up deeper, and so is the same terms one page
        # further in. Resolving any of them after this lookup would let the wider,
        # deeper or later request replay the answer the same query cached earlier in
        # the turn - installed, logged as installed, and returning exactly what it was
        # meant to replace.
        #
        # dr@3.1 added ``page``, which the dr@3.0 key omitted while dr@3.0 was the
        # version that made pagination reachable. Measured on the 302 per-question
        # ledgers of the dr@3.0 live-web arm: of the searches that returned content
        # while the rule had escalated to page 2, 206 replayed a page-1 entry against
        # 83 real page-2 requests - 71.3% of the paginated searches handed back the
        # page the rung existed to move past. Those replays then count as dry
        # (``observe(())`` below), so each one marched the turn one step closer to
        # ``stopped``: the rung did not merely fail, it fed the rung after it.
        #
        # Inert wherever pagination cannot happen. ``page`` is 1 for a turn with no
        # saturation rule, and ``_escalate`` cannot raise it when ``paginates`` is
        # False, which the corpus path forces. A constant extra tuple element moves
        # no key relative to any other, so only an arm that actually turns a page
        # sees a different hit than it saw before.
        key = (norm_query, n_requested, k, page)
        if (prior := self._prior.get(key)) is not None:
            replayed = f"{_REPEAT_NOTE}\n{prior[1]}"
            # A byte-identical repeat is the archetypal dry search, so it counts
            # towards the streak. Reported as an empty identity list rather than the
            # cached URLs: the two axes identify documents differently (link here,
            # docid on the corpus), and "a repeat brought back nothing new" is true in
            # both namespaces without having to pick one.
            if self._saturation is not None:
                self._saturation.observe(())
            self._log_search(
                query, n_requested, prior[2], replayed, prior[3], replay=True, k=k,
                page=req_page,
            )
            return replayed
        self._searches += 1
        if deep is not None:
            self._evidence_round.consume()
        result, urls, shaping = await self._search(query, n_requested, k=k, page=page)
        # Same after-the-fact latch as the no-notice branch above.
        if self._saturation is not None and not self._transport_err(result):
            self._saturation.note_page(norm_query, page)
        # Only a real result set is worth replaying. Caching an error or an
        # empty page would turn a transient retrieval failure into a permanent
        # one for the rest of the turn - a retry is the correct response there.
        if not self._failed(result):
            self._prior[key] = (self._searches, result, urls, shaping)
        self._log_search(query, n_requested, urls, result, shaping, replay=False, k=k,
                         page=req_page)
        return result

    @staticmethod
    def _failed(result: str) -> bool:
        """Whether this result is worth caching for replay. **Behaviour-bearing.**

        ``execute`` reads this to decide what goes into ``_prior``, so redefining it
        changes what the model reads on a repeat. The two classifiers below split the
        same string for the ledger only, and deliberately leave this one alone.
        """
        return result.startswith(("Error:", "Proxy error:", "No results for:"))

    # Three outcomes were compressed into one boolean, and the ledger inherited it.
    # Measured over the n=302 live-web batch, ``failed`` and "the result list was
    # empty" agreed on every single call across all four arms - 1,855 / 2,179 /
    # 1,375 / 2,514 with zero exceptions - so essentially all of it is zero-hit
    # queries, which is a behaviour difference (anchor 18.93% against the DR arm's
    # 8.59%) rather than a broken tool. Real transport errors on the same batch were
    # 3 and 2 calls, i.e. 0.031% and 0.012% - three orders of magnitude apart.
    #
    # The trap is newly live rather than old: the 20260806 rule says tool-surface
    # rates must be computed from this ledger rather than the trajectory, because
    # elision under-counts trajectory-side errors by an arm-correlated amount. The
    # first gate to follow that rule and read a field named ``failed`` would report
    # "anchor tool surface broken, 18.9%" about a perfectly healthy run.
    @staticmethod
    def _zero_hit(result: str) -> bool:
        return result.startswith("No results for:")

    @staticmethod
    def _transport_err(result: str) -> bool:
        return result.startswith(("Error:", "Proxy error:"))

    def _log_search(
        self, query: str, n: int, urls: list[str], rendered: str,
        shaping: dict[str, int], *, replay: bool, k: int | None = None,
        suppressed: bool = False, page: int | None = None,
    ) -> None:
        """Record one search call.

        ``urls`` is ordered, not a set: the rank a document was returned at is the whole
        content of the "it was on screen and never opened" diagnosis, and collapsing it
        to membership throws that away.

        ``shaping`` carries the size of each SERP element the profile can switch off.
        Without it the ledger records that a search happened but not what the model was
        handed, and the flow-off anchor's direct-answer channel - Serper's answerBox,
        which a DR arm turns off - is the one part of that difference with no measured
        magnitude anywhere on disk.
        """
        if replay and shaping.get("n_served") is not None:
            # ``n_served`` is contractually None on any row whose call never reached
            # an endpoint, and a replay is exactly that - but the cached shaping dict
            # carries the ORIGINAL call's count, so spreading it as-is made a reader
            # of "non-null means the endpoint was reached" overstate reach by the
            # replay rate (38.1% on one measured DR arm). The original count stays
            # readable under its own name.
            shaping = dict(shaping)
            shaping["n_served_at_capture"] = shaping.pop("n_served")
            shaping["n_served"] = None
        _ledger_append({
            "ts": time.time(),
            "op": "search",
            "query": query,
            # Unchanged meaning: how many results the call asked to be shown. dr@2.8
            # split the request into shown-width and looked-at-depth; the second one
            # is the new ``k_requested`` below, because landed batches are read
            # against this key's old definition.
            "k": n,
            "urls": urls,
            "n": len(urls),
            "replay": replay,
            "source": "corpus" if self.corpus_endpoint else "web",
            # Kept, unchanged, and meaning what it always meant: "this result was not
            # cached for replay". Renaming it would break readers of landed batches.
            "failed": self._failed(rendered) if not replay else False,
            "zero_hit": self._zero_hit(rendered) if not replay else False,
            "transport_err": self._transport_err(rendered) if not replay else False,
            # ``k`` is what the service was asked for and ``k`` differing from the
            # rendered width is the only on-disk proof that dr@2.8 did anything.
            # Null rather than 0 on an arm without the mechanism, so "this arm never
            # went deep" stays a different row from "this call did not".
            "k_requested": k if self.cross_query_dedup else None,
            # The page this call was keyed at (live rows: the page on the wire;
            # replay rows: the page in the replay key). ``sat_page`` (in the
            # saturation counters) is the ladder's escalation level; since
            # ``page_for`` (dr@3.4) the two legitimately diverge - a fresh query
            # under an escalated ladder still goes to page 1 - and without this
            # key that behaviour cannot be audited from the ledger. Null when
            # the arm has no saturation rule (same discipline as k_requested)
            # and on suppressed rows, which never key a request at all.
            "req_page": page,
            "evidence_round_open": (
                self._evidence_round.active if self._evidence_round is not None else None
            ),
            # dr@3.0. ``suppressed`` marks a row where no request was issued at all, so
            # a reader can subtract the rule's refusals from an arm's call total instead
            # of finding a search that mysteriously returned nothing.
            "suppressed": suppressed,
            # Written on EVERY search row, including the ones where nothing fired, and
            # null-valued when the arm has no rule at all. That distinction is the whole
            # lesson of ``dedup_skipped``, which reads 0 on every row of every landed
            # batch: "the mechanism did not fire" and "the mechanism was never built"
            # were the same row, so no batch can say whether dr@2.8's dedup ever ran.
            **(self._saturation.counters() if self._saturation is not None
               else {"sat_action": None, "sat_event": None}),
            **shaping,
        })
        # The row is over. Done here rather than at the escalation sites because this
        # is the one place that defines a row, and every logged search reaches it -
        # including a suppressed row (no ``observe``) and a transport error (returns
        # before ``observe``). Ending the row where the row ends is what keeps
        # ``sat_event`` from surviving into the next one.
        if self._saturation is not None:
            self._saturation.end_row()

    def _request_width(self, n: int, depth_override: int | None = None) -> int:
        """How deep to ask, given how many lines will be rendered.

        Equal to the rendered width unless cross-query dedup is on, which is what
        keeps this inert for every arm that did not ask for it - including the
        flow-off anchor, which shares this code path on the corpus axis.
        """
        if not self.cross_query_dedup:
            return n
        return max(n, min(depth_override or self.search_depth, _MAX_SEARCH_DEPTH))

    def _select_fresh(
        self, results: list[dict[str, Any]], n: int, identity_key: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Take the first ``n`` results the turn has not listed yet.

        Returns the selection and how many were dropped, so the ledger can say
        whether the mechanism did anything rather than only that it was enabled.

        **The rendered width is preserved unconditionally.** Slots that dedup cannot
        fill from deeper are given back to the documents it wanted to skip, in the
        service's own order. Without that rule the mechanism is only safe where a
        deeper pool exists: the corpus path asks for ``search_depth`` and has one,
        while the live-web path sends ``num=n`` and has nothing underneath, so on
        that path skipping is pure subtraction - measured on the dr@2.7 web batch,
        52.89% of the DR arm's result slots were documents seen by an earlier query
        (anchor 43.97%), i.e. plain dedup would have deleted about half the result
        lines. That is a different change from the one that was priced offline, and
        it was priced as "same number of lines, different contents".

        Preserving the width also keeps the degenerate case honest: a search that
        rendered nothing reads as "no results for this query", a claim about the
        corpus rather than about what the model has already seen, and it would push
        the model to re-issue the query it just ran.
        """
        if not self.cross_query_dedup:
            return results[:n], 0
        picked: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for item in results:
            identity = str(item.get(identity_key) or item.get("url") or item.get("link") or "")
            if identity and identity in self._result_seen:
                deferred.append(item)
                continue
            if identity:
                self._result_seen.add(identity)
            picked.append(item)
            if len(picked) >= n:
                break
        want = min(n, len(results))
        topped_up = 0
        if len(picked) < want:
            topped_up = want - len(picked)
            picked.extend(deferred[:topped_up])
        # Only the ones that stayed out count as dropped. Counting every skip would
        # report a busy mechanism on the live-web path, where nearly all of them are
        # handed straight back.
        return picked, len(deferred) - topped_up

    def _snippet_line(self, item: dict[str, Any], identity: str) -> str | None:
        """The indented snippet line for one result, or ``None`` when there is none.

        With dedup on, a result already previewed this turn gets a marker instead of a
        second window. The marker is not decoration: a blank where neighbouring results
        carry text reads as "this result has no content", which is a different claim
        than "you have already seen this one".
        """
        if not self.include_snippets:
            return None
        if not (desc := item.get("snippet")):
            return None
        if not self.snippet_dedup_by_docid or not identity:
            return f"   {desc}"
        if identity in self._snippet_seen:
            self._snippet_repeat_marks += 1
            return f"   {_SNIPPET_REPEAT_NOTE}"
        self._snippet_seen.add(identity)
        return f"   {desc}"

    async def _search(
        self, query: str, count: int | None, k: int | None = None, page: int = 1
    ) -> tuple[str, list[str], dict[str, int]]:
        """Returns the rendered text, the ordered result URLs, and the shaping sizes.

        The URL list is returned rather than parsed back out of the text: the ledger
        must record what the tool actually received, and a rendering is a lossy view
        of it. The shaping sizes are measured here for the same reason - reading them
        back off the rendered string would be parsing our own output.

        ``page`` is a parameter rather than a second read of ``_saturation.page``
        because the caller has already keyed the replay cache on it. Two reads of one
        piece of mutable state, where the first decides the cache key and the second
        decides the request, is the shape that produced the dr@3.0 pagination defect;
        making it one read makes them unable to disagree.
        """
        if self.corpus_endpoint:
            return await self._search_corpus(query, count, k)
        if not self.api_key:
            return (
                f"Error: {self.spec.label} API key not configured. Set it in "
                f"~/.raven/config.json under {self.spec.config_path} "
                f"(or export {self.spec.env_var}), then restart the gateway.",
                [],
                dict(_NO_SHAPING),
            )

        try:
            # ``k`` reaches this path but must not become a larger ``num``. Probed
            # against the endpoint this build uses, ``num`` changes nothing: 10, 20,
            # 50 and 100 all came back HTTP 200 with 7-8 organic results, while
            # ``page=2`` returned the next ten. Depth here therefore needs pagination
            # and costs one API call per page - a separate change with a separate
            # quota profile. Sending a larger ``num`` would return the same eight
            # rows and log as though the request had gone deep, which is a no-op
            # indistinguishable from a working feature. Cross-query dedup still
            # applies to what does come back; it just has less to work with.
            n = min(max(count or self.max_results, 1), 10)
            # dr@3.0. `num` is inert on this endpoint - probed at 10/20/50/100, all
            # return 8-10 organic rows - but `page` is not: page 2 and 3 each return a
            # full ten, 93.75% of them absent from page one. Artifact:
            # data_dr/runs/PROBE_serper_num_page_20260812.json. Omitted entirely at
            # page 1 so an arm without the rule sends the byte-identical body it
            # always sent.
            #
            # dr@3.1: ``page`` arrives as an argument. It used to be read here, after
            # the caller had already built the replay key without it - so a page-2
            # request could be answered from the page-1 entry the same query cached
            # minutes earlier, and 71.3% of them were.
            logger.debug("WebSearch: {}", "proxy enabled" if self.proxy else "direct connection")

            # Retried on the same policy as the reader: on the batch that motivated
            # it, search took the larger share of the damage (761 disconnects against
            # fetch's 405), and search volume is itself a treatment effect, so leaving
            # this half single-shot would keep a smaller copy of the same arm-correlated
            # tax on the arm that searches most.
            async def _send() -> httpx.Response:
                async with httpx.AsyncClient(proxy=self.proxy) as client:
                    return await self._provider_request(client, query, n, page)

            r = await _send_with_retry(
                _send, op="search_retry", key=query, budget=self._retry_budget
            )
            # Normalised to the Serper shape before anything downstream reads it,
            # so containment, dedup, shaping and rendering stay one code path and
            # the instrument columns keep meaning the same thing on every backend.
            data = self._normalise_response(r.json())
            # Filtered before the slice, not after: dropping a row post-slice
            # would hand back a shorter list than asked for, and how often that
            # happens depends on how dataset-adjacent the arm's queries are -
            # i.e. it would make result-list length arm-correlated. Backfilling
            # from deeper hits keeps the returned count identical across arms.
            organic = data.get("organic", [])
            filtered = self.containment.filter_results(organic)
            results, n_skipped = self._select_fresh(filtered, n, "link")
            if not results:
                # A zero-hit search is the purest dry search, and it returns from
                # here - before the rendering loop, which is where the observation
                # first sat. Placing it only there meant the rule never saw the very
                # shape that dominates the measured tail spin. Transport errors
                # deliberately do NOT reach either site: an outage means the pool was
                # never consulted, and counting it as exhaustion would let a network
                # fault close search on a turn whose queries were fine.
                if self._saturation is not None:
                    self._saturation.observe(())
                # dr@3.2: the endpoint WAS reached, so n_served is a number even
                # though nothing survived. Leaving it null here would merge this row
                # with a replay/suppression, which is the distinction the field exists
                # for -- and zero-hit is the single most load-bearing row type in the
                # saturation analysis.
                return (f"No results for: {query}", [],
                        {**_NO_SHAPING, "n_served": len(organic)})

            shaping = dict(_NO_SHAPING)
            shaping["n_served"] = len(organic)
            shaping["dedup_skipped"] = n_skipped
            self._snippet_repeat_marks = 0
            lines = [f"Results for: {query}\n"]
            if self.include_answer_box and (answer := data.get("answerBox")):
                snippet = answer.get("answer") or answer.get("snippet")
                if snippet:
                    lines.append(f"Answer: {snippet}\n")
                    # Sized where it is emitted. This is the direct-answer channel a
                    # deep-research profile switches off, and until now the only part
                    # of that difference with no number anywhere on disk.
                    shaping["answer_box_chars"] = len(str(snippet))
            if self.include_knowledge_graph and (knowledge := data.get("knowledgeGraph")):
                title = knowledge.get("title")
                description = knowledge.get("description")
                if title or description:
                    lines.append(f"Knowledge: {title or ''}")
                    shaping["knowledge_chars"] = len(str(title or ""))
                    if description:
                        lines.append(f"   {description}")
                        shaping["knowledge_chars"] += len(str(description))
            urls = []
            for i, item in enumerate(results, 1):
                link = str(item.get("link") or "")
                urls.append(link)
                lines.append(f"{i}. {item.get('title', '')}\n   {link}")
                if (line := self._snippet_line(item, link)):
                    lines.append(line)
                    shaping["snippet_lines"] += 1
                    shaping["snippet_chars"] += len(line)
            # Observed here rather than in ``execute`` because this is where the
            # identity is unambiguous: the caller sees only ``urls``, and the corpus
            # path identifies documents by docid, so a shared observation site would
            # have to guess which namespace it was in.
            if self._saturation is not None:
                self._saturation.observe(urls)
            shaping["snippet_repeat_marks"] = self._snippet_repeat_marks
            return "\n".join(lines), urls, shaping
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}", [], _error_shaping(e)
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}", [], _error_shaping(e)


    async def _provider_request(
        self, client: httpx.AsyncClient, query: str, n: int, page: int
    ) -> httpx.Response:
        """One search request, built the way the selected provider expects.

        The Serper branch is byte-identical to what this tool has always sent -
        it is the anchor's wire traffic, and ``test_web_search_saturation_wiring``
        compares the body against a literal. Do not "tidy" it into the others.
        """
        if self.provider == "serper":
            return await client.post(
                "https://google.serper.dev/search",
                json=({"q": query, "num": n} if page <= 1
                      else {"q": query, "num": n, "page": page}),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-KEY": self.api_key,
                },
                timeout=10.0,
            )
        if self.provider == "serpapi":
            # ``start`` is an offset in results, not a page index, so the rung's
            # page N is (N-1) whole pages in. Omitted at page 1 so the request an
            # un-escalated turn sends carries nothing the rule did not ask for.
            params = {"engine": "google", "q": query, "num": n, "api_key": self.api_key}
            if page > 1:
                params["start"] = (page - 1) * n
            return await client.get(
                "https://serpapi.com/search",
                params=params,
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        # AnySearch. ``page`` cannot appear here: the provider serves no offset,
        # which is why ``spec.paginates`` is False and the saturation rule is told
        # so at construction - the rung is unreachable rather than unanswerable.
        return await client.post(
            "https://api.anysearch.com/v1/search",
            json={"query": query, "max_results": n},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=10.0,
        )

    def _normalise_response(self, data: Any) -> dict[str, Any]:
        """A provider payload in the Serper shape this tool renders from.

        Only the three keys the render path reads are produced. Anything a
        provider does not carry is absent rather than empty, so a missing
        answer box and a suppressed one stay distinguishable in the shaping row.
        """
        if self.provider == "serper" or not isinstance(data, dict):
            return data if isinstance(data, dict) else {}
        if self.provider == "serpapi":
            out: dict[str, Any] = {"organic": list(data.get("organic_results") or [])}
            if box := data.get("answer_box"):
                out["answerBox"] = box
            if kg := data.get("knowledge_graph"):
                out["knowledgeGraph"] = kg
            return out
        # AnySearch publishes the request shape but not the response. Parse
        # tolerantly: results may sit at the top level or inside the
        # ``{code, message, data}`` envelope its auth endpoint uses, and an item
        # spells the URL ``url`` or ``link`` and the text ``snippet`` or ``content``.
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = body.get("results") if isinstance(body, dict) else None
        organic: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            organic.append({
                "title": str(item.get("title") or ""),
                "link": str(item.get("url") or item.get("link") or ""),
                "snippet": item.get("snippet") or item.get("content") or "",
            })
        return {"organic": organic}

    async def _search_corpus(
        self, query: str, count: int | None, k: int | None = None
    ) -> tuple[str, list[str], dict[str, int]]:
        """Same result shape as the live-web path, sourced from the fixed corpus.

        ``trust_env=False``: the box exports a global HTTP proxy for outbound
        traffic, and a local retrieval service must never be routed through it.

        This renderer emits no answer box and no knowledge panel - there is nothing in
        the corpus response to render one from. So ``include_answer_box`` and
        ``include_knowledge_graph`` are no-ops here while both are live on the web path,
        which means a corpus-axis "flow on minus flow off" and a web-axis one differ by
        two switches beyond the flow itself. Their shaping counts stay 0, and ``source``
        says which renderer produced the row.
        """
        # Two numbers, and the gap between them is the whole of dr@2.8 on this axis.
        # ``n`` is what the model reads - unchanged, so the context is the same size
        # as it was at dr@2.7. ``depth`` is what the service is asked for; ranks
        # beyond ``n`` have always been served here and only ever discarded on this
        # side. Without cross-query dedup the two are equal and nothing changes.
        n = min(max(count or self.max_results, 1), 10)
        depth = min(max(k or n, n), _MAX_SEARCH_DEPTH)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
                r = await client.get(
                    f"{self.corpus_endpoint.rstrip('/')}/search", params={"q": query, "k": depth}
                )
                r.raise_for_status()
            served = (r.json() or {}).get("results", [])
        except Exception as e:
            logger.error("WebSearch(corpus) error: {}", e)
            return f"Error: {e}", [], _error_shaping(e)
        results, n_skipped = self._select_fresh(served, n, "docid")
        if not results:
            # Same reasoning as the live path: an empty result set is exhaustion,
            # a transport error is not.
            if self._saturation is not None:
                self._saturation.observe(())
            return (f"No results for: {query}", [],
                    {**_NO_SHAPING, "n_served": len(served)})
        shaping = dict(_NO_SHAPING)
        shaping["n_served"] = len(served)
        shaping["dedup_skipped"] = n_skipped
        self._snippet_repeat_marks = 0
        lines = [f"Results for: {query}\n"]
        urls = []
        identities: list[str] = []
        for i, item in enumerate(results, 1):
            url = str(item.get("url") or "")
            urls.append(url)
            lines.append(f"{i}. {item.get('title', '')}\n   {url}")
            identity = str(item.get("docid") or url)
            identities.append(identity)
            if (line := self._snippet_line(item, identity)):
                lines.append(line)
                shaping["snippet_lines"] += 1
                shaping["snippet_chars"] += len(line)
        if self._saturation is not None:
            self._saturation.observe(identities)
        shaping["snippet_repeat_marks"] = self._snippet_repeat_marks
        return "\n".join(lines), urls, shaping


def fetch_result_ok(out: object) -> bool:
    """True when a ``web_fetch`` return value carries a page rather than an error.

    One implementation, two callers: the client-side ledger's ``ok`` column and
    the fetch gate's release condition. They must agree, because the gate's whole
    contract is "search returns once a page is opened" and the ledger is what an
    acceptance check reads to decide whether that ever happened. Two spellings of
    "did the fetch work" drifting apart would show up as a gate that looks stuck
    in a ledger that says it should have opened - a disagreement with no runtime
    symptom, diagnosable only by reading both.

    A body that is not a JSON object counts as a failure. That is the same
    caliber the ledger already used, and it is the safe direction here: an
    unparseable body cannot be shown to be a page, and a gate that released on
    one would release on every malformed response.
    """
    try:
        payload = json.loads(out)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return "error" not in payload


_ENCODING_LOST_WARNING = (
    "the source lost its text encoding upstream; non-ASCII characters on this "
    "page are unreliable - do not quote them, prefer another source"
)


def _encoding_lost(text: str) -> bool:
    """True when the page text is dominated by U+FFFD replacement characters.

    A page that declares its charset only in an HTML meta tag while the HTTP
    header says none (GB2312 sites commonly do) is decoded as UTF-8 somewhere
    upstream and arrives as runs of U+FFFD - one measured page carried 4060 of
    them, title included. The floor tolerates the odd replacement character a
    legitimate page carries; a run of them means the decode itself failed.
    """
    return text.count("\ufffd") > max(8, len(text) // 200)


class _ProviderPageError(Exception):
    """A fetch backend answered, but with no page.

    Its own type so the fallback chain can tell "this vendor could not serve the
    page" apart from a transport failure while still treating both as reasons to
    try the next one - and so neither is confused with a containment refusal or a
    failed URL validation, which are rules and never fall through.
    """


class WebFetchTool(Tool):
    """Fetch and extract content from a URL through the configured provider."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content via Jina Reader."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {
                "type": "integer",
                "minimum": 100,
                "description": "Narrow the returned text below the configured cap; cannot raise it",
            },
            "info_to_extract": {
                "type": "string",
                "description": "What to look for in the page; long pages are distilled to exactly this",
            },
        },
        "required": ["url"],
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_chars: int = 50000,
        proxy: str | None = None,
        digest_fn: DigestFn | None = None,
        digest_threshold_chars: int = 8000,
        digest_timeout_s: float = 30.0,
        corpus_endpoint: str | None = None,
        containment: BenchmarkContainment | None = None,
        provider: str = DEFAULT_FETCH_PROVIDER,
        fallback: Sequence[str] | None = None,
        provider_keys: dict[str, str] | None = None,
    ):
        if provider not in FETCH_PROVIDERS:
            logger.warning(
                "web_fetch: unknown provider {!r}, using {}", provider, DEFAULT_FETCH_PROVIDER
            )
            provider = DEFAULT_FETCH_PROVIDER
        self.provider = provider
        # Deduplicated and filtered here rather than at each use: an entry naming
        # the selected provider would re-issue the request that just failed, and
        # one naming a provider that does not exist would raise inside the very
        # path that exists to survive a failure.
        seen = {provider}
        self.fallback: list[str] = []
        for name in fallback or ():
            if name in FETCH_PROVIDERS and name not in seen:
                seen.add(name)
                self.fallback.append(name)
        self._provider_keys = dict(provider_keys or {})
        self._init_api_key = api_key
        # Instance attribute shadowing the class one. With the default provider
        # this renders the class string byte for byte, so the anchor's tool
        # schema is unchanged; a non-default provider must not advertise a
        # vendor it will not call.
        self.description = f"Fetch URL and extract readable content via {FETCH_PROVIDERS[provider].label}."
        self.containment = containment or BenchmarkContainment()
        self.max_chars = max_chars
        self.proxy = proxy
        self.corpus_endpoint = corpus_endpoint
        # Targeted-extraction path: long pages are distilled by a cheap model
        # instead of blind truncation. The tool never talks to a provider
        # itself — the flow assembly injects ``digest_fn`` so this module
        # stays free of provider knowledge. Digest failures always degrade
        # to the plain truncation path; a broken digest must never make
        # fetch worse than it is today.
        self.digest_fn = digest_fn
        self.digest_threshold_chars = digest_threshold_chars
        self.digest_timeout_s = digest_timeout_s
        self._retry_budget = _RetryBudget()

    def start_turn(self) -> None:
        """Refill the retry budget.

        Scoped to the turn like the search tool's repeat memory: a gateway keeps one
        tool instance for the life of the loop, so a per-process budget would silently
        stop retrying partway through a long run.
        """
        self._retry_budget = _RetryBudget()

    @property
    def spec(self) -> FetchProviderSpec:
        return FETCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._key_for(self.provider)

    @property
    def registrable(self) -> bool:
        """Whether this tool may be advertised to the model.

        One predicate, two registries - the main loop's and the sub-agent's.
        They must agree, and two conditionals in two files with nothing tying
        them together is how they stop agreeing: gating only the main loop
        leaves the tool advertised to every nested sub-agent.

        Differs from ``web_search``'s rule in the one way that matters: Jina
        reads pages unauthenticated, so an absent key there is a rate limit
        rather than a broken tool, and the default build stays registered
        exactly as it always was.
        """
        return not self.spec.needs_key or bool(self.api_key) or bool(self.corpus_endpoint)

    def _key_for(self, provider: str) -> str:
        """The credential for one backend, selected or fallback.

        ``api_key`` names the SELECTED provider only. A fallback entry has to
        find its own key, or a chain would quietly send the selected vendor's
        credential to a different vendor's endpoint.
        """
        if provider == self.provider and self._init_api_key:
            return self._init_api_key
        if key := self._provider_keys.get(provider):
            return key
        return os.environ.get(FETCH_PROVIDERS[provider].env_var, "")

    async def execute(
        self,
        url: str,
        extractMode: str = "markdown",  # noqa: N803  (LLM tool schema uses camelCase)
        maxChars: int | None = None,  # noqa: N803
        info_to_extract: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Fetch a URL, then record the call in the client-side ledger.

        The ledger write sits here rather than at each return point below: this method
        has six of them (corpus, containment refusal, invalid URL, digest, truncation,
        failure), and an instrument that has to be repeated six times is one that will
        eventually be missing from one of them.
        """
        # ``try/finally`` rather than a bare await: the tool registry wraps every call in
        # ``asyncio.wait_for`` with a 300s ceiling, and a timeout CANCELS this coroutine
        # mid-await, so a plain sequential write is skipped and the fetch leaves no trace.
        # Structurally only a flow-on arm can reach that ceiling (its worst case runs past
        # it while the anchor's does not), so the missing rows are arm-correlated and in
        # the direction that makes the treated arm look healthier - the one direction an
        # instrument must never fail in. Measured at <=3 of 13,076 fetches across the
        # eight dr@2.7 arms (0.023%, all three on treated arms): the bias is real and the
        # magnitude is negligible, so this is fixed as instrument correctness, not as a
        # correction to any published number.
        out = None
        try:
            out = await self._fetch(url, extractMode, maxChars, info_to_extract, **kwargs)
            return out
        finally:
            # ``out`` is None only when _fetch raised or was cancelled; record that as its
            # own outcome rather than skipping the line, so "cancelled" and "never issued"
            # stay distinguishable in the ledger.
            _ledger_append(
                self._fetch_record(url, out) if out is not None
                else {"ts": time.time(), "op": "fetch", "url": url, "ok": False,
                      "outcome": "aborted", "chars": 0}
            )

    def _fetch_record(self, url: str, out: str) -> dict[str, Any]:
        """Ledger line for one fetch, read off the tool's own structured envelope."""
        record: dict[str, Any] = {
            "ts": time.time(),
            "op": "fetch",
            "url": url,
            "source": "corpus" if self.corpus_endpoint else "web",
        }
        record["ok"] = fetch_result_ok(out)
        try:
            payload = json.loads(out)
        except (TypeError, ValueError):
            return record
        if not isinstance(payload, dict):
            return record
        # ``extractor`` names the backend that actually served the page. It is a
        # measurement column, not a label: the two live-web backends are not
        # interchangeable (AnySearch keeps the navigation chrome Jina strips, so
        # the same article arrives ~1.8x longer), and a fallback chain means the
        # selected provider is not always the one that answered.
        for key, out_key in (("length", "chars"), ("source_chars", "source_chars"),
                             ("digested", "digested"), ("docid", "docid"),
                             ("truncated", "truncated"), ("error", "error"),
                             ("encoding_lost", "encoding_lost"),
                             ("extractor", "extractor")):
            if key in payload:
                record[out_key] = payload[key]
        return record

    async def _get_with_retry(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """GET through the reader, retrying only what never produced a response.

        A transient reader outage otherwise costs a whole arm rather than a page: one
        measured arm abandoned 686 fetches to "Server disconnected without sending a
        response" inside a 51-minute window, while its three sibling arms, which ran
        outside that window, recorded none. The abandonment was not inevitable - the
        model itself re-fetched 163 of those URLs later and 124 of them came back.
        """

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                # The fragment marker must be escaped: bare concatenation makes
                # everything after ``#`` a fragment of the OUTER r.jina.ai URL,
                # so an SPA route fetched the site root while ``finalUrl``
                # reported the requested value.
                return await client.get(f"https://r.jina.ai/{url.replace('#', '%23')}", headers=headers)

        return await _send_with_retry(
            _send, op="fetch_retry", key=url, budget=self._retry_budget
        )

    async def _fetch(
        self,
        url: str,
        extractMode: str = "markdown",  # noqa: N803
        maxChars: int | None = None,  # noqa: N803
        info_to_extract: str | None = None,
        **kwargs: Any,
    ) -> str:
        # The request can narrow the configured cap, never raise it. The schema
        # says so too, but a schema is advice to the model, not a bound on it:
        # unclamped, one maxChars=10000000 call pulls a multi-MB page into the
        # context and the elision machinery then spends the turn's budget
        # clawing it back out.
        max_chars = min(maxChars, self.max_chars) if maxChars else self.max_chars
        if self.corpus_endpoint:
            return await self._fetch_corpus(url, extractMode, max_chars, info_to_extract)
        # After the corpus branch: the corpus axis is contained by construction
        # (its retrieval service serves only the corpus), so running the gate
        # there would only add a way for a docid to be misclassified.
        if (refusal := self.containment.check_fetch(url)) is not None:
            return json.dumps({"error": refusal, "url": url}, ensure_ascii=False)
        # ``strict_dns=False``: the request below goes to the reader service, which
        # opens the connection to ``url`` from its own network. This process never
        # does, so a local resolver failure says nothing about whether the target is
        # internal. The private-address block still applies whenever the name
        # resolves; only the refusal-on-failure is dropped. The other caller of this
        # validator follows redirects itself and must stay strict.
        is_valid, error_msg = await validate_url_target_async(url, strict_dns=False)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # "no explicit proxy" is not "direct": with trust_env on, httpx
        # still honours HTTP(S)_PROXY from the environment.
        logger.debug(
            "WebFetch: {}",
            "proxy enabled" if self.proxy else "no explicit proxy (environment proxies may apply)",
        )
        # The chain starts below the two refusals above, never around them: a
        # containment refusal and a failed URL validation are rules, and retrying
        # a rule on another vendor is an end-run around the gate that produced it.
        # Only a vendor that could not serve the page falls through.
        #
        # The turn's retry budget is shared across the chain, so a vendor that
        # spent it leaves the next one without retries - but never without its
        # first attempt, which is the property that makes a fallback worth having
        # during exactly the outage that drains the budget.
        attempts = [self.provider, *self.fallback]
        served, text, status = "", "", 0
        extras: dict[str, Any] = {}
        failure: str | None = None
        for index, candidate in enumerate(attempts):
            remaining = attempts[index + 1:]
            try:
                text, status, extras = await self._provider_fetch(candidate, url)
                # A 200 carrying nothing is a vendor that did not serve the page,
                # which the AnySearch branch already raises on and the Jina one
                # cannot see. Conditioned on there being a next vendor: with none,
                # this returns what the vendor sent, which is what the default
                # single-provider path has always returned and what the anchor
                # measures. Widening it to that path is a Flow change (AGENTS.md
                # 0.4), not a fix to fold in here.
                if not text.strip() and remaining:
                    raise _ProviderPageError(f"{candidate} returned no page content")
                served = candidate
                break
            except httpx.ProxyError as e:
                logger.error("WebFetch proxy error for {} via {}: {}", url, candidate, e)
                failure = f"Proxy error: {e}"
            except Exception as e:
                logger.error("WebFetch error for {} via {}: {}", url, candidate, e)
                failure = str(e)
            if not remaining:
                return json.dumps({"error": failure, "url": url}, ensure_ascii=False)
            # Recorded rather than logged only: a run that leaned on its fallback
            # must not be indistinguishable from one that never needed it.
            _ledger_append({
                "ts": time.time(),
                "op": "fetch_fallback",
                "url": url,
                "provider": candidate,
                "next": remaining[0],
                "error": failure,
            })

        spec = FETCH_PROVIDERS[served]
        encoding_lost = _encoding_lost(text)
        source_chars = len(text)

        # A page that lost its encoding is not worth a digest call: the
        # model would distill replacement characters.
        extracted = None if encoding_lost else await self._try_digest(text, info_to_extract, url)
        if extracted is not None:
            return json.dumps(
                {
                    "url": url,
                    "finalUrl": url,
                    "status": status,
                    "extractor": f"{spec.extractor}+digest",
                    "extractMode": extractMode,
                    "digested": True,
                    "info_to_extract": info_to_extract,
                    "source_chars": source_chars,
                    "length": len(extracted),
                    "text": extracted,
                    **extras,
                },
                ensure_ascii=False,
            )

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        payload: dict[str, Any] = {
            "url": url,
            "finalUrl": url,
            "status": status,
            "extractor": spec.extractor,
            "extractMode": extractMode,
            "truncated": truncated,
            "length": len(text),
            "text": text,
        }
        payload.update(extras)
        if served != DEFAULT_FETCH_PROVIDER:
            # ``truncated`` is measured against this tool's own cap, so a backend
            # that truncated first reports False on a page it cut. Only added off
            # the default path: this is what the model reads, and adding a field
            # to the DEFAULT payload would move the anchor's distribution. The
            # digest branch above already carries it on every path.
            payload["source_chars"] = source_chars
        if encoding_lost:
            payload["encoding_lost"] = True
            payload["warning"] = _ENCODING_LOST_WARNING
        return json.dumps(payload, ensure_ascii=False)

    async def _provider_fetch(self, provider: str, url: str) -> tuple[str, int, dict[str, Any]]:
        """One page, read the way the given backend serves it.

        Returns the page text, the status to report, and any extra payload
        fields that backend can fill in. Raises on anything that did not produce
        a page, which is what the caller's fallback chain runs on.

        The Jina branch is byte-identical to what this tool has always sent - it
        is the anchor's wire traffic. Do not "tidy" it into the other.
        """
        if provider == "jina":
            headers = {"Accept": "text/plain"}
            if key := self._key_for("jina"):
                headers["Authorization"] = f"Bearer {key}"
            r = await self._get_with_retry(url, headers)
            text = r.text
            if _encoding_lost(text):
                # A poisoned cached extraction is the common case, and a fresh
                # render usually comes back clean.
                r = await self._get_with_retry(url, {**headers, "x-no-cache": "true"})
                text = r.text
            return text, r.status_code, {}

        # AnySearch. No cache-bust header is published, so the garbled-page
        # re-read above has no counterpart here; the encoding check still runs on
        # what comes back.
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key := self._key_for(provider):
            headers["Authorization"] = f"Bearer {key}"

        async def _send() -> httpx.Response:
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                return await client.post(
                    "https://api.anysearch.com/v1/extract", json={"url": url}, headers=headers
                )

        r = await _send_with_retry(_send, op="fetch_retry", key=url, budget=self._retry_budget)
        data = r.json()
        if not isinstance(data, dict):
            raise _ProviderPageError("AnySearch returned a non-object body")
        # A transport-level failure already raised in ``_send_with_retry``; this
        # catches the other shape, a 200 whose envelope reports the failure.
        if data.get("code") not in (0, None):
            raise _ProviderPageError(f"AnySearch: {data.get('message') or 'extract failed'}")
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        text = str(body.get("content") or "")
        if not text:
            raise _ProviderPageError("AnySearch returned no page content")
        # Carried as its own field rather than prepended to the text the way Jina
        # embeds its header block: synthesising a partial imitation of that block
        # would misreport a missing publication date as an undated page.
        extras = {}
        if title := body.get("title"):
            extras["title"] = str(title)
        return text, r.status_code, extras

    async def _fetch_corpus(
        self, url: str, extract_mode: str, max_chars: int, info_to_extract: str | None
    ) -> str:
        """Serve the page from the fixed corpus, in the live-web response shape.

        A URL the corpus does not contain is reported as such, not as a fetch
        failure: under this protocol the corpus is the whole accessible world,
        so "not in corpus" is information the agent should act on (search
        again) rather than an outage it should retry through.
        """
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=60.0) as client:
                r = await client.get(f"{self.corpus_endpoint.rstrip('/')}/fetch", params={"url": url})
            if r.status_code == 404:
                return json.dumps(
                    {"error": "not in corpus", "url": url, "hint": "this run is restricted to a fixed corpus"},
                    ensure_ascii=False,
                )
            r.raise_for_status()
            doc = r.json()
        except Exception as e:
            logger.error("WebFetch(corpus) error: {}", e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

        text = doc.get("text") or ""
        source_chars = len(text)
        extracted = await self._try_digest(text, info_to_extract, url)
        if extracted is not None:
            return json.dumps(
                {
                    "url": url,
                    "finalUrl": doc.get("url") or url,
                    "status": 200,
                    "extractor": "corpus+digest",
                    "extractMode": extract_mode,
                    "digested": True,
                    "info_to_extract": info_to_extract,
                    "docid": doc.get("docid"),
                    "source_chars": source_chars,
                    "length": len(extracted),
                    "text": extracted,
                },
                ensure_ascii=False,
            )
        truncated = source_chars > max_chars
        return json.dumps(
            {
                "url": url,
                "finalUrl": doc.get("url") or url,
                "status": 200,
                "extractor": "corpus",
                "extractMode": extract_mode,
                "docid": doc.get("docid"),
                "truncated": truncated,
                "length": min(source_chars, max_chars),
                "text": text[:max_chars],
            },
            ensure_ascii=False,
        )

    async def _try_digest(self, text: str, info_to_extract: str | None, url: str) -> str | None:
        """Distill a long page down to what the caller asked for.

        Returns ``None`` whenever the digest path does not apply or fails,
        so ``execute`` falls back to plain truncation.
        """
        if self.digest_fn is None or not info_to_extract or len(text) <= self.digest_threshold_chars:
            return None
        try:
            extracted = await asyncio.wait_for(
                self.digest_fn(text, info_to_extract),
                timeout=self.digest_timeout_s,
            )
        except Exception as e:
            logger.warning(
                "WebFetch digest failed for {} ({}: {}); falling back to truncation",
                url,
                type(e).__name__,
                e,
            )
            return None
        if isinstance(extracted, str) and extracted.strip():
            return extracted
        logger.warning("WebFetch digest returned empty for {}; falling back to truncation", url)
        return None
