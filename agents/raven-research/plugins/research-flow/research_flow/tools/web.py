"""Web tools: ``web_search`` and ``web_fetch`` for the research flow.

These replace the kernel's built-in tools of the same names. Both route through a
vendor the deployment picks, the same seven search and five fetch backends the
built-ins offer, declared in :data:`SEARCH_PROVIDERS` and
:data:`FETCH_PROVIDERS`; Serper and Jina remain the defaults and their wire
traffic is byte-identical to what this pair has always sent. What the pair adds
over the built-ins is the research harness around the call - the per-turn replay
cache and repeat notice, snippet and cross-query dedup, the saturation rule, the
evidence round, the digest path, a bounded retry policy, and a ledger row for
every call.

A vendor that serves no result offset cannot answer the saturation rule's page-2
rung, so ``SearchProviderSpec.paginates`` tells the rule up front rather than
letting it escalate into requests that come back identical.

Per-session state
-----------------
The kernel builds ONE instance of each tool for every session it serves, where the
fork built one per session. Everything the fork kept as mutable per-turn state on
``self`` - the retry budget, the replay cache, the seen sets, the counters, and the
tool's own ``SearchSaturation`` / ``EvidenceRound`` instances - therefore lives in a
per-session slot instead. The slot is selected by the ``ContextVar``
:data:`_SESSION`, which the flow hook sets through :func:`set_current_session`
before the model call; tool coroutines run in that same task context, so the slot
they see is the session's. The empty key (the ``ContextVar`` default) is a valid
slot, so a tool used outside the hook still works, single-session.

The saturation rule and the evidence round are per session too. The constructor
takes *factories* - zero-argument callables returning a fresh instance - and calls
one the first time a session's slot is built, from inside that session's context.
A factory that has to hand back the instance a session's gate chain already shares
(the evidence round is opened by the verify gate and spent here) can close over
:func:`current_session` to look it up.

``start_turn`` resets the CURRENT session's slot, which is what the fork's loop did
for its one session at the top of every turn. The hook calls it on turn start.
"""

import asyncio
import json
import os
import re
import time
import zlib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from raven.contracts.tool import Tool
from raven.security.network import validate_url_target
from research_flow.support.evidence_round import EvidenceRound
from research_flow.support.harness_text import search_closed_notice
from research_flow.support.ledger import ledger_append as _ledger_append
from research_flow.support.search_saturation import SearchSaturation

DigestFn = Callable[[str, str], Awaitable[str]]

_SESSION: ContextVar[str] = ContextVar("research_flow_session", default="")
"""Which session's tool state the current task is operating on. See the module docstring."""


def set_current_session(key: str) -> Token[str]:
    """Select the session whose per-turn tool state the current task uses.

    Returns the ``ContextVar`` token so a caller that wants to restore the previous
    value can; the hook, which sets it once per turn in the turn's own task, need not.
    """
    return _SESSION.set(key or "")


def current_session() -> str:
    """The session key the current task's tool calls are attributed to."""
    return _SESSION.get()


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
_NO_SHAPING = {
    "answer_box_chars": 0,
    "knowledge_chars": 0,
    "snippet_chars": 0,
    "snippet_lines": 0,
    "dedup_skipped": 0,
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
    "snippet_repeat_marks": 0,
}


@dataclass(frozen=True)
class SearchProviderSpec:
    """One ``web_search`` backend, described where every reader can see it.

    ``paginates`` is the load-bearing field: a provider that serves no offset
    cannot answer the saturation rule's page-2 rung, and the rule has to be told
    up front (``SearchSaturation.paginates``) rather than discovering it from a
    request that comes back identical. A capability whose activation site is
    unreachable, with no symptom, is the failure this avoids.

    ``label`` is what an error line names when it must identify a vendor without
    quoting the request.
    """

    vendor: str
    label: str
    env_var: str
    paginates: bool


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
    "tavily": SearchProviderSpec(
        vendor="tavily",
        label="Tavily",
        env_var="TAVILY_API_KEY",
        # No documented result-offset parameter; breadth here comes from wider
        # single-shot queries, not paging, so the rung is declared unavailable
        # up front like AnySearch.
        paginates=False,
    ),
    "exa": SearchProviderSpec(
        vendor="exa",
        label="Exa",
        env_var="EXA_API_KEY",
        # Neural search over Exa's own index has no documented page offset.
        paginates=False,
    ),
    "brave": SearchProviderSpec(
        vendor="brave",
        label="Brave Search",
        env_var="BRAVE_API_KEY",
        # ``offset`` is a page index, not a result offset like SerpApi's
        # ``start``, documented up to 9 pages past the first.
        paginates=True,
    ),
    "firecrawl": SearchProviderSpec(
        vendor="firecrawl",
        label="Firecrawl",
        env_var="FIRECRAWL_API_KEY",
        # ``/v1/search`` documents no result-offset parameter.
        paginates=False,
    ),
    "serply": SearchProviderSpec(
        vendor="serply",
        label="Serply",
        env_var="SERPLY_API_KEY",
        # ``start`` is a result offset like SerpApi's, so page N is start=(N-1)*num.
        paginates=True,
    ),
}


@dataclass(frozen=True)
class FetchProviderSpec:
    """One ``web_fetch`` backend.

    ``needs_key`` decides whether the tool may be advertised at all: Jina reads
    pages unauthenticated (at a lower rate limit), the others refuse without a
    key. A tool that cannot run must not be registered - the model reaches for
    it, every call fails, and the error text naming a config path is relayed to
    whoever is on the other end of the channel.

    ``extractor`` is an instrument column, not a label: it is what the fetch
    ledger records as having served the page, and the backends are not
    comparable.
    """

    vendor: str
    label: str
    env_var: str
    extractor: str
    needs_key: bool


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
    "tavily": FetchProviderSpec(
        vendor="tavily",
        label="Tavily",
        env_var="TAVILY_API_KEY",
        extractor="tavily-extract",
        needs_key=True,
    ),
    "exa": FetchProviderSpec(
        vendor="exa",
        label="Exa",
        env_var="EXA_API_KEY",
        extractor="exa-contents",
        needs_key=True,
    ),
    "firecrawl": FetchProviderSpec(
        vendor="firecrawl",
        label="Firecrawl",
        env_var="FIRECRAWL_API_KEY",
        extractor="firecrawl-scrape",
        # No anonymous tier.
        needs_key=True,
    ),
}


def resolve_search_provider(name: str | None) -> str:
    """A configured ``web_search`` vendor name, degrading to the default.

    Degraded rather than refused, and logged: an unknown name is a typo no
    schema catches on a plain config slice, and a silent fallback is the
    symptomless failure this warning exists to avoid.
    """
    if name and name not in SEARCH_PROVIDERS:
        logger.warning("WebSearch: unknown provider '{}', using {}", name, DEFAULT_SEARCH_PROVIDER)
    return name if name in SEARCH_PROVIDERS else DEFAULT_SEARCH_PROVIDER


def resolve_fetch_provider(name: str | None) -> str:
    """A configured ``web_fetch`` vendor name, degrading to the default. Warns
    on an unknown name for the same reason as its search sibling."""
    if name and name not in FETCH_PROVIDERS:
        logger.warning("WebFetch: unknown provider '{}', using {}", name, DEFAULT_FETCH_PROVIDER)
    return name if name in FETCH_PROVIDERS else DEFAULT_FETCH_PROVIDER


def _error_text(exc: Exception) -> str:
    """How an exception is named in a ledger row, which lands on disk.

    A status error's own text repeats the request URL, so the status alone is
    what makes the row diagnosable: a vendor that authenticates by query
    parameter would otherwise write its key here.

    One function rather than the rule spelled at each writer. There are two -
    the ``search``/``fetch`` row and the ``*_retry`` rows beside it in the same
    file - and the second was added carrying ``str(exc)``, which is exactly the
    way a rule written twice fails.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError: HTTP {getattr(exc.response, 'status_code', None)}"
    return f"{type(exc).__name__}: {exc}"[:500]


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
    shaping["error"] = _error_text(exc)
    shaping["status"] = int(status) if status is not None else None
    shaping["quota_err"] = status in (401, 402, 403)
    return shaping


# Upper bound on how deep ``_request_width`` may ask the ranking to be looked into.
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
        _ledger_append(
            {
                "ts": time.time(),
                "op": op,
                "key": key,
                "attempt": attempt + 1,
                "status": status,
                "error": _error_text(last),
            }
        )
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
#   * The ordinal: ``searches`` only increments on the cache-miss path, so
#     "#n" counted distinct queries, not queries. Measured on one batch, 14 of 47
#     notices named the wrong index, one of them by 16.
#   * "the results below are unchanged": no request was issued, so nothing was
#     compared. What is true is that they are the earlier results.
#   * "Re-running it cannot surface anything new": false once
#     ``_emergency_shrink`` has replaced older tool bodies with a placeholder -
#     re-issuing is then the only way the model gets a lost SERP back, and 120 of
#     201 repeat fetches in one batch happened on questions that ended correct.
#     Telling it the opposite argued against the one recovery move it has.


class _SearchTurnState:
    """One session's mutable ``web_search`` state: what the fork kept on the tool.

    Two identity sets, deliberately separate. ``snippet_seen`` answers "has this
    document been previewed", ``result_seen`` answers "has this document been listed
    at all", and sharing one set would make each knob silently change the other's
    behaviour, which is how an ablation stops measuring the thing it names. The
    saturation rule keeps a third set of its own for the same reason.
    """

    __slots__ = (
        "snippet_seen",
        "snippet_repeat_marks",
        "result_seen",
        "prior",
        "searches",
        "retry_budget",
        "evidence_round",
        "saturation",
    )

    def __init__(self, evidence_round: EvidenceRound | None, saturation: SearchSaturation | None) -> None:
        self.snippet_seen: set[str] = set()
        # Reset per SEARCH, not per turn: a cumulative counter reports the turn's total
        # on every row, and this project has already had a gate satisfied by a week-old
        # residue. Per-row counts sum to the turn; a turn total cannot be un-summed.
        self.snippet_repeat_marks = 0
        self.result_seen: set[str] = set()
        # Carries the ordered result URLs alongside the rendered text so a replay is
        # logged from what was actually served, not re-parsed out of the rendering.
        self.prior: dict[tuple[str, int, int, int], tuple[int, str, list[str], dict]] = {}
        self.searches = 0
        self.retry_budget = _RetryBudget()
        # Set by the verify gate on a rejection; read here. None outside a flow that
        # declares it, which keeps the anchor's behaviour bit-identical.
        self.evidence_round = evidence_round
        # dr@3.0. None outside a flow that declares it, so the anchor never builds one
        # and cannot be moved by it.
        self.saturation = saturation

    def start_turn(self, keep_identities: bool) -> None:
        self.prior.clear()
        self.searches = 0
        self.retry_budget = _RetryBudget()
        if not keep_identities:
            self.snippet_seen.clear()
            self.result_seen.clear()
        self.snippet_repeat_marks = 0
        if self.evidence_round is not None:
            self.evidence_round.reset()
        if self.saturation is not None:
            self.saturation.reset(keep_seen=keep_identities)


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
        provider: str = DEFAULT_SEARCH_PROVIDER,
        include_answer_box: bool = True,
        include_knowledge_graph: bool = True,
        include_snippets: bool = True,
        snippet_dedup_by_docid: bool = False,
        cross_query_dedup: bool = False,
        search_depth: int = 20,
        repeat_notice: bool = False,
        evidence_round_factory: Callable[[], EvidenceRound] | None = None,
        saturation_factory: Callable[[], SearchSaturation] | None = None,
    ):
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy
        self.provider = resolve_search_provider(provider)
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
        # times. Scoped to the turn like the replay cache.
        self.snippet_dedup_by_docid = snippet_dedup_by_docid
        self.cross_query_dedup = cross_query_dedup
        self.search_depth = search_depth
        # Off by default: annotating a repeat changes what the model reads, so
        # only a flow that declares the behavior gets it.
        self.repeat_notice = repeat_notice
        self._evidence_round_factory = evidence_round_factory
        self._saturation_factory = saturation_factory
        self._sessions: dict[str, _SearchTurnState] = {}

    @property
    def spec(self) -> SearchProviderSpec:
        return SEARCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get(self.spec.env_var, "")

    def _state(self) -> _SearchTurnState:
        """The current session's slot, built on first use from inside that session's context."""
        key = _SESSION.get()
        state = self._sessions.get(key)
        if state is None:
            saturation = self._saturation_factory() if self._saturation_factory is not None else None
            # Declared up front for a provider that serves no offset: ``_escalate``
            # cannot raise ``page`` once this is False, so the rung is never reached
            # rather than reached and unanswerable. The ledger still separates the
            # two outcomes -- ``sat_action`` reports ``stopped_degraded`` here
            # against ``stopped`` on a real exhaustion.
            if saturation is not None and not self.spec.paginates:
                saturation.paginates = False
            state = _SearchTurnState(
                self._evidence_round_factory() if self._evidence_round_factory is not None else None,
                saturation,
            )
            self._sessions[key] = state
        return state

    def forget_session(self, key: str) -> None:
        """Drop a session's slot once the session is gone; a no-op for an unknown key."""
        self._sessions.pop(key, None)

    def start_turn(self, keep_identities: bool = False) -> None:
        """Drop the current session's per-turn repeat memory.

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
        self._state().start_turn(keep_identities)

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        state = self._state()
        saturation = state.saturation
        evidence_round = state.evidence_round
        # Rendered width and requested depth are two different numbers from dr@2.8
        # on. ``n_requested`` is what the model reads and is unchanged; ``k`` is how
        # far into the ranking the service is asked to look so that documents this
        # turn has already listed can be skipped and the list backfilled.
        n_requested = count or self.max_results
        # dr@3.0 "widen": once the rule has escalated, render what the endpoint was
        # already returning. Inert until then and inert without the rule, so the
        # request an un-saturated turn sends is unchanged.
        if saturation is not None:
            n_requested = saturation.width(n_requested)
        # dr@3.0. Checked before everything else, including the replay cache, because
        # the point of the rule is that the call does not happen - a stopped turn that
        # still got a cached page back would be receiving advice, not a control-flow
        # decision, and advice is the thing already measured not to work here.
        if saturation is not None and saturation.stopped:
            saturation.suppress()
            # dr@3.2: built from ``harness_text`` so the emitter and the two
            # recognisers cannot drift. They drifting apart would be silent, and
            # its symptom is this sentence becoming eligible as evidence again --
            # which is how it once shipped as a run's final answer (hle-256).
            refusal = search_closed_notice(saturation.k)
            self._log_search(
                state, query, n_requested, [], refusal, dict(_NO_SHAPING), replay=False, k=None, suppressed=True
            )
            return refusal
        deep = evidence_round.depth if (evidence_round is not None and evidence_round.active) else None
        k = self._request_width(n_requested, deep)
        # Read once, before either branch. The replay key below and the request itself
        # must be built from the same value; see ``_search``. Keyed on the normalized
        # query (dr@3.4): the ladder's escalation is turn-global, but a page is only
        # deeper for terms already served the pages before it - a fresh query sent to
        # page 2 skips ranks 1-10 for terms nobody has seen ranked, and its
        # near-certain empty return then scores as dry, feeding the ``stop`` rung.
        norm_query = " ".join((query or "").lower().split())
        page = saturation.page_for(norm_query) if saturation is not None else 1
        req_page = page if saturation is not None else None
        if not self.repeat_notice:
            if deep is not None:
                evidence_round.consume()
            result, urls, shaping = await self._search(state, query, n_requested, k=k, page=page)
            # Latched after the fact, and never on a transport error: an
            # errored request served no page, and latching it would send the
            # retry of the same query one page past results nobody saw.
            if saturation is not None and not self._transport_err(result):
                saturation.note_page(norm_query, page)
            self._log_search(state, query, n_requested, urls, result, shaping, replay=False, k=k, page=req_page)
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
        # False. A constant extra tuple element moves no key relative to any other,
        # so only an arm that actually turns a page sees a different hit than it
        # saw before.
        key = (norm_query, n_requested, k, page)
        if (prior := state.prior.get(key)) is not None:
            replayed = f"{_REPEAT_NOTE}\n{prior[1]}"
            # A byte-identical repeat is the archetypal dry search, so it counts
            # towards the streak. Reported as an empty identity list rather than the
            # cached URLs: "a repeat brought back nothing new" is true without having
            # to name which documents it did bring back.
            if saturation is not None:
                saturation.observe(())
            self._log_search(
                state,
                query,
                n_requested,
                prior[2],
                replayed,
                prior[3],
                replay=True,
                k=k,
                page=req_page,
            )
            return replayed
        state.searches += 1
        if deep is not None:
            evidence_round.consume()
        result, urls, shaping = await self._search(state, query, n_requested, k=k, page=page)
        # Same after-the-fact latch as the no-notice branch above.
        if saturation is not None and not self._transport_err(result):
            saturation.note_page(norm_query, page)
        # Only a real result set is worth replaying. Caching an error or an
        # empty page would turn a transient retrieval failure into a permanent
        # one for the rest of the turn - a retry is the correct response there.
        if not self._failed(result):
            state.prior[key] = (state.searches, result, urls, shaping)
        self._log_search(state, query, n_requested, urls, result, shaping, replay=False, k=k, page=req_page)
        return result

    @staticmethod
    def _failed(result: str) -> bool:
        """Whether this result is worth caching for replay. **Behaviour-bearing.**

        ``execute`` reads this to decide what goes into the replay cache, so redefining
        it changes what the model reads on a repeat. The two classifiers below split the
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
        self,
        state: _SearchTurnState,
        query: str,
        n: int,
        urls: list[str],
        rendered: str,
        shaping: dict[str, int],
        *,
        replay: bool,
        k: int | None = None,
        suppressed: bool = False,
        page: int | None = None,
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
        saturation = state.saturation
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
        _ledger_append(
            {
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
                "source": "web",
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
                "evidence_round_open": (state.evidence_round.active if state.evidence_round is not None else None),
                # dr@3.0. ``suppressed`` marks a row where no request was issued at all, so
                # a reader can subtract the rule's refusals from an arm's call total instead
                # of finding a search that mysteriously returned nothing.
                "suppressed": suppressed,
                # Written on EVERY search row, including the ones where nothing fired, and
                # null-valued when the arm has no rule at all. That distinction is the whole
                # lesson of ``dedup_skipped``, which reads 0 on every row of every landed
                # batch: "the mechanism did not fire" and "the mechanism was never built"
                # were the same row, so no batch can say whether dr@2.8's dedup ever ran.
                **(saturation.counters() if saturation is not None else {"sat_action": None, "sat_event": None}),
                **shaping,
            }
        )
        # The row is over. Done here rather than at the escalation sites because this
        # is the one place that defines a row, and every logged search reaches it -
        # including a suppressed row (no ``observe``) and a transport error (returns
        # before ``observe``). Ending the row where the row ends is what keeps
        # ``sat_event`` from surviving into the next one.
        if saturation is not None:
            saturation.end_row()

    def _request_width(self, n: int, depth_override: int | None = None) -> int:
        """How deep to ask, given how many lines will be rendered.

        Equal to the rendered width unless cross-query dedup is on, which is what
        keeps this inert for every arm that did not ask for it.
        """
        if not self.cross_query_dedup:
            return n
        return max(n, min(depth_override or self.search_depth, _MAX_SEARCH_DEPTH))

    def _select_fresh(
        self, state: _SearchTurnState, results: list[dict[str, Any]], n: int, identity_key: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Take the first ``n`` results the turn has not listed yet.

        Returns the selection and how many were dropped, so the ledger can say
        whether the mechanism did anything rather than only that it was enabled.

        **The rendered width is preserved unconditionally.** Slots that dedup cannot
        fill from deeper are given back to the documents it wanted to skip, in the
        service's own order. Without that rule the mechanism is only safe where a
        deeper pool exists: the live-web path sends ``num=n`` and has nothing
        underneath, so on that path skipping is pure subtraction - measured on the
        dr@2.7 web batch, 52.89% of the DR arm's result slots were documents seen by
        an earlier query (anchor 43.97%), i.e. plain dedup would have deleted about
        half the result lines. That is a different change from the one that was
        priced offline, and it was priced as "same number of lines, different
        contents".

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
            if identity and identity in state.result_seen:
                deferred.append(item)
                continue
            if identity:
                state.result_seen.add(identity)
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

    def _snippet_line(self, state: _SearchTurnState, item: dict[str, Any], identity: str) -> str | None:
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
        if identity in state.snippet_seen:
            state.snippet_repeat_marks += 1
            return f"   {_SNIPPET_REPEAT_NOTE}"
        state.snippet_seen.add(identity)
        return f"   {desc}"

    async def _search(
        self, state: _SearchTurnState, query: str, count: int | None, k: int | None = None, page: int = 1
    ) -> tuple[str, list[str], dict[str, int]]:
        """Returns the rendered text, the ordered result URLs, and the shaping sizes.

        The URL list is returned rather than parsed back out of the text: the ledger
        must record what the tool actually received, and a rendering is a lossy view
        of it. The shaping sizes are measured here for the same reason - reading them
        back off the rendered string would be parsing our own output.

        ``page`` is a parameter rather than a second read of the saturation rule's page
        because the caller has already keyed the replay cache on it. Two reads of one
        piece of mutable state, where the first decides the cache key and the second
        decides the request, is the shape that produced the dr@3.0 pagination defect;
        making it one read makes them unable to disagree.
        """
        saturation = state.saturation
        if not self.api_key:
            return (
                f"Error: {self.spec.label} API key not configured. Set it in "
                '~/.raven/config.json under plugins.config["research-flow"].search.apiKey '
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
            # full ten, 93.75% of them absent from page one. Omitted entirely at
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

            r = await _send_with_retry(_send, op="search_retry", key=query, budget=state.retry_budget)
            data = r.json()
            if not isinstance(data, dict):
                data = {}
            data = self._normalise_response(data)
            organic = data.get("organic", [])
            results, n_skipped = self._select_fresh(state, organic, n, "link")
            if not results:
                # A zero-hit search is the purest dry search, and it returns from
                # here - before the rendering loop, which is where the observation
                # first sat. Placing it only there meant the rule never saw the very
                # shape that dominates the measured tail spin. Transport errors
                # deliberately do NOT reach either site: an outage means the pool was
                # never consulted, and counting it as exhaustion would let a network
                # fault close search on a turn whose queries were fine.
                if saturation is not None:
                    saturation.observe(())
                # dr@3.2: the endpoint WAS reached, so n_served is a number even
                # though nothing survived. Leaving it null here would merge this row
                # with a replay/suppression, which is the distinction the field exists
                # for -- and zero-hit is the single most load-bearing row type in the
                # saturation analysis.
                return (f"No results for: {query}", [], {**_NO_SHAPING, "n_served": len(organic)})

            shaping = dict(_NO_SHAPING)
            shaping["n_served"] = len(organic)
            shaping["dedup_skipped"] = n_skipped
            state.snippet_repeat_marks = 0
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
                if line := self._snippet_line(state, item, link):
                    lines.append(line)
                    shaping["snippet_lines"] += 1
                    shaping["snippet_chars"] += len(line)
            # Observed here rather than in ``execute`` because this is where the
            # identity is unambiguous: the caller sees only ``urls``.
            if saturation is not None:
                saturation.observe(urls)
            shaping["snippet_repeat_marks"] = state.snippet_repeat_marks
            return "\n".join(lines), urls, shaping
        except httpx.HTTPStatusError as e:
            # Vendor and status only, never the exception text: httpx puts the
            # full request URL in it, so a key carried as a query parameter
            # would reach the model and the log through this line.
            status = e.response.status_code
            logger.error("WebSearch error: {} answered HTTP {}", self.spec.label, status)
            return f"Error: {self.spec.label} answered HTTP {status}", [], _error_shaping(e)
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}", [], _error_shaping(e)
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}", [], _error_shaping(e)

    async def _provider_request(self, client: httpx.AsyncClient, query: str, n: int, page: int) -> httpx.Response:
        """One search request, built the way the selected provider expects.

        The Serper branch is byte-identical to what this pair has always sent -
        it is the default distribution's wire traffic. Do not "tidy" it into the
        others. A vendor whose ``spec.paginates`` is False takes no ``page``
        here, and the saturation rule was told so when the session's slot was
        built, so the rung is unreachable rather than silently inert.
        """
        if self.provider == "serper":
            return await client.post(
                "https://google.serper.dev/search",
                json=({"q": query, "num": n} if page <= 1 else {"q": query, "num": n, "page": page}),
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
            params: dict[str, Any] = {"engine": "google", "q": query, "num": n, "api_key": self.api_key}
            if page > 1:
                params["start"] = (page - 1) * n
            return await client.get(
                "https://serpapi.com/search",
                params=params,
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        if self.provider == "tavily":
            return await client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=10.0,
            )
        if self.provider == "exa":
            # ``contents.highlights`` asks for a query-scored excerpt, which is
            # what the render path reads as the snippet. Not ``text``: that is
            # the whole page, and ``_snippet_line`` renders whatever it is
            # handed. ``maxCharacters`` is the bound that holds. Highlights are
            # separately billed, so they are only requested when snippets are
            # rendered - a deep-research profile sets ``include_snippets=False``
            # and would discard them unread.
            body: dict[str, Any] = {"query": query, "numResults": n}
            if self.include_snippets:
                body["contents"] = {"highlights": {"maxCharacters": 300}}
            return await client.post(
                "https://api.exa.ai/search",
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                },
                timeout=10.0,
            )
        if self.provider == "brave":
            # ``offset`` is a page index (unlike SerpApi's result-count
            # ``start``), so page N maps onto offset=(N-1) directly.
            params = {"q": query, "count": n}
            if page > 1:
                params["offset"] = page - 1
            return await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                timeout=10.0,
            )
        if self.provider == "firecrawl":
            return await client.post(
                "https://api.firecrawl.dev/v1/search",
                json={"query": query, "limit": n},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=10.0,
            )
        if self.provider == "serply":
            # ``start`` is an offset in results, omitted at page 1 like SerpApi's.
            params = {"q": query, "num": n}
            if page > 1:
                params["start"] = (page - 1) * n
            return await client.get(
                "https://api.serply.io/v1/search",
                params=params,
                headers={"Accept": "application/json", "X-Api-Key": self.api_key},
                timeout=10.0,
            )
        # AnySearch.
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

    def _normalise_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """A provider payload in the Serper shape this tool renders from.

        Only the three keys the render path reads are produced. Anything a
        provider does not carry is absent rather than empty, so a missing
        answer box and a suppressed one stay distinguishable in the shaping row.
        """
        if self.provider == "serper":
            return data
        if self.provider == "serpapi":
            out: dict[str, Any] = {"organic": list(data.get("organic_results") or [])}
            if box := data.get("answer_box"):
                out["answerBox"] = box
            if kg := data.get("knowledge_graph"):
                out["knowledgeGraph"] = kg
            return out
        if self.provider == "tavily":
            out = {"organic": _rows(data.get("results"), url="url", snippet="content")}
            # Tavily returns ``answer`` only when the request opts in with
            # ``include_answer``, which the request above does not send, so this
            # slot is empty today. Kept on the answerBox path so an opt-in later
            # renders through the channel a deep-research profile already
            # switches off.
            if answer := data.get("answer"):
                out["answerBox"] = {"answer": answer}
            return out
        if self.provider == "exa":
            organic = []
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                # Highlights only, never ``text``: a page body in this slot is
                # rendered whole. Whitespace collapsed because a highlight
                # carries newlines and the snippet line is indented once.
                highlights = [h for h in (item.get("highlights") or []) if isinstance(h, str) and h]
                organic.append(
                    {
                        "title": str(item.get("title") or ""),
                        "link": str(item.get("url") or ""),
                        "snippet": " ".join(" ".join(h.split()) for h in highlights),
                    }
                )
            return {"organic": organic}
        if self.provider == "brave":
            web = data.get("web") if isinstance(data.get("web"), dict) else {}
            return {
                "organic": _rows(
                    web.get("results") if isinstance(web, dict) else None, url="url", snippet="description"
                )
            }
        if self.provider == "firecrawl":
            # Errors normally arrive status-coupled and raise in the retry
            # helper. This catches an in-envelope refusal inside a 200 so it
            # renders as an error rather than a dry search: a zero-hit advances
            # the saturation streak, and "the endpoint refused" must not.
            if data.get("success") is False:
                raise ValueError(f"Firecrawl: {data.get('error') or 'search failed'}")
            return {"organic": _rows(data.get("data"), url="url", snippet="description")}
        if self.provider == "serply":
            # Google SERP rows under ``results``; the snippet is ``description``.
            return {"organic": _rows(data.get("results"), url="link", snippet="description")}
        # AnySearch publishes the request shape but not the response. Parse
        # tolerantly: results may sit at the top level or inside the
        # ``{code, message, data}`` envelope its auth endpoint uses, and an item
        # spells the URL ``url`` or ``link`` and the text ``snippet`` or ``content``.
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = body.get("results") if isinstance(body, dict) else None
        organic = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            organic.append(
                {
                    "title": str(item.get("title") or ""),
                    "link": str(item.get("url") or item.get("link") or ""),
                    "snippet": item.get("snippet") or item.get("content") or "",
                }
            )
        return {"organic": organic}


def _rows(items: Any, *, url: str, snippet: str) -> list[dict[str, Any]]:
    """Provider rows in the render path's three-key shape."""
    return [
        {
            "title": str(item.get("title") or ""),
            "link": str(item.get(url) or ""),
            "snippet": item.get(snippet) or "",
        }
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
    ]


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

    ``raw_decode``, not ``loads``: the envelope is recognised even when a
    harness note trails it. ``loads`` demands that the JSON be the WHOLE
    string, and the fetch-gate and sufficiency seams read a message body that
    several observers are entitled to write on - ``BudgetNoteObserver`` appends
    ``[budget: iteration N/M | context ~P%]`` to the newest tool result, the
    fetch floor and the gate append their notes. ``unwrap_untrusted`` now drops
    what follows the fence's close marker, so those seams normally hand this a
    clean envelope; this is the second line, for a body that reaches a caller
    annotated but unfenced. The ledger is unaffected either way - it is handed
    the raw return value, which no observer has touched.
    """
    text = out if isinstance(out, str) else None
    if text is None:
        return False
    try:
        payload, _end = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return "error" not in payload


#: Below this a read brought back a stub rather than a page - a redirect notice, an empty
#: shell, a cookie wall. The same number the appendix calls a thin page, deliberately: a
#: read this tool decided to retry and a read the trail reports as "returned almost
#: nothing" should not be able to disagree.
_THIN_PAGE_CHARS = 400

#: How many rewrites one fetch may try. The chains below are two deep at most, and a
#: budget states the cost in the file rather than leaving it to the length of a table.
_FALLBACK_LIMIT = 2

#: Where a thin read is worth trying again, by the shape of the address rather than by the
#: host alone. Each entry rewrites one URL into the addresses of the same document that
#: carry its body: the landing page of a paper is a stub by design, and the run that
#: prompted this read 36 pages at a 2,358-character median - abstracts and repository
#: front pages - and then wrote "(est.)" into a scored table for the numbers it had not
#: found. A rewrite is not a guess at a different document; each one below is the same
#: work at a URL the publisher also serves.
_FALLBACK_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # arXiv: the abstract page is a stub, the HTML rendering carries the paper, and the
    # PDF carries it when the HTML build failed (which is what a 2026 paper on the
    # earlier run returned - an SVG shell with no text).
    (
        re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/(?P<id>[\w.\-/]+?)(?:v\d+)?/?$", re.I),
        ("https://arxiv.org/html/{id}", "https://arxiv.org/pdf/{id}"),
    ),
    (
        re.compile(r"^https?://(?:www\.)?arxiv\.org/html/(?P<id>[\w.\-/]+?)(?:v\d+)?/?$", re.I),
        ("https://arxiv.org/pdf/{id}", "https://arxiv.org/abs/{id}"),
    ),
    (
        re.compile(r"^https?://(?:www\.)?arxiv\.org/pdf/(?P<id>[\w.\-/]+?)(?:v\d+)?(?:\.pdf)?/?$", re.I),
        ("https://arxiv.org/abs/{id}",),
    ),
    # OpenReview: the forum page is rendered by a client-side app, so a reader gets the
    # shell. Measured at 440 characters on the run that prompted this.
    (
        re.compile(r"^https?://openreview\.net/forum\?id=(?P<id>[\w.\-]+)", re.I),
        ("https://openreview.net/pdf?id={id}",),
    ),
    # ACL Anthology: the landing page carries an abstract, the PDF carries the paper.
    (
        re.compile(r"^https?://aclanthology\.org/(?P<id>[\w.\-]+?)/?$", re.I),
        ("https://aclanthology.org/{id}.pdf",),
    ),
    # Hugging Face: a dataset card can be a one-line README, while the API answers with
    # the split sizes, the licence and the configs - which is what a benchmark table
    # needs and what the run had to estimate.
    (
        re.compile(r"^https?://huggingface\.co/datasets/(?P<id>[\w.\-]+/[\w.\-]+)/?$", re.I),
        ("https://huggingface.co/api/datasets/{id}",),
    ),
    # A repository front page that renders thin still has its README as a file.
    (
        re.compile(r"^https?://github\.com/(?P<id>[\w.\-]+/[\w.\-]+)/?$", re.I),
        (
            "https://raw.githubusercontent.com/{id}/HEAD/README.md",
            "https://api.github.com/repos/{id}",
        ),
    ),
)


def fetch_fallbacks(url: str) -> list[str]:
    """The addresses of the same document to try when a read comes back thin.

    Ordered, deduplicated, and never containing the URL that was asked for: a rewrite that
    resolved to its own input would spend the budget re-reading the stub.
    """
    out: list[str] = []
    for pattern, templates in _FALLBACK_RULES:
        match = pattern.match(url.strip())
        if match is None:
            continue
        for template in templates:
            candidate = template.format(**match.groupdict())
            if candidate != url and candidate not in out:
                out.append(candidate)
        break
    return out


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


_UNRESOLVED_REFUSAL = "Cannot resolve hostname:"
"""The trunk validator's one refusal that says nothing about the target. Pinned
by the tool tests, which fail if the wording moves and takes the tolerance below
with it."""


def _judge_fetch_target(url: str) -> tuple[bool, str]:
    """The trunk validator's verdict, minus the refusal on a resolver failure.

    The reader service opens the connection to ``url`` from its own network;
    this process never does, so a local resolution failure carries no
    information about whether the target is internal, and the address check it
    feeds is vacuous either way. Resolvers fail under load (``EAI_AGAIN``) as
    readily as for a name that does not exist and the caller cannot tell the
    two apart from a ``gaierror``, so refusing here reports load to the model as
    "this URL is bad". The private-address block still applies whenever the
    name resolves; only the refusal-on-failure is dropped, which is what the
    fork got from ``validate_url_target(url, strict_dns=False)``. The trunk
    validator carries no such knob, so the tolerance lives on this side of it.
    """
    is_valid, error_msg = validate_url_target(url)
    if not is_valid and error_msg.startswith(_UNRESOLVED_REFUSAL):
        return True, ""
    return is_valid, error_msg


class _FetchTurnState:
    """One session's mutable ``web_fetch`` state: the retry budget."""

    __slots__ = ("retry_budget",)

    def __init__(self) -> None:
        self.retry_budget = _RetryBudget()


class _ProviderPageError(Exception):
    """A fetch backend answered, but with no page.

    Its own type so "this vendor could not serve the page" stays apart from a
    transport failure, and so neither is confused with a failed URL validation,
    which is a rule and never reads as a vendor fault.
    """


class WebFetchTool(Tool):
    """Fetch and extract content from a URL through the configured provider (Jina by default)."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content."
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
        provider: str = DEFAULT_FETCH_PROVIDER,
        digest_fn: DigestFn | None = None,
        digest_threshold_chars: int = 8000,
        digest_timeout_s: float = 30.0,
    ):
        self._init_api_key = api_key
        self.max_chars = max_chars
        self.proxy = proxy
        self.provider = resolve_fetch_provider(provider)
        # Targeted-extraction path: long pages are distilled by a cheap model
        # instead of blind truncation. The tool never talks to a provider
        # itself - the flow assembly injects ``digest_fn`` so this module
        # stays free of provider knowledge. Digest failures always degrade
        # to the plain truncation path; a broken digest must never make
        # fetch worse than it is today.
        self.digest_fn = digest_fn
        self.digest_threshold_chars = digest_threshold_chars
        self.digest_timeout_s = digest_timeout_s
        self._sessions: dict[str, _FetchTurnState] = {}

    def _state(self) -> _FetchTurnState:
        """The current session's slot, built on first use."""
        key = _SESSION.get()
        state = self._sessions.get(key)
        if state is None:
            state = _FetchTurnState()
            self._sessions[key] = state
        return state

    def forget_session(self, key: str) -> None:
        """Drop a session's slot once the session is gone; a no-op for an unknown key."""
        self._sessions.pop(key, None)

    def start_turn(self) -> None:
        """Refill the current session's retry budget.

        Scoped to the turn like the search tool's repeat memory: a gateway keeps one
        tool instance for the life of the loop, so a per-process budget would silently
        stop retrying partway through a long run.
        """
        self._state().retry_budget = _RetryBudget()

    @classmethod
    def effective_provider(cls, provider: str, api_key: str | None) -> str:
        """The backend to build: the selected one, or the default reader when it cannot run.

        The kernel's own ``web_fetch`` answers this question the same way, and
        this one has to agree with it: this tool REPLACES that one, so a
        research run would otherwise be the single place where a keyless Tavily
        selection is advertised and refuses every call, while a plain raven on
        the same config reads the page through Jina.

        ``web_fetch`` is always offered because Jina reads pages without a key.
        Degraded out loud rather than left to fail per call, and never by
        handing one vendor's credential to another: the branch is only taken
        when no key resolves at all.

        Normalised through :func:`resolve_fetch_provider` first, as the
        constructor does, so a caller handing this an unknown name gets the
        same degradation the tool itself would apply rather than a KeyError.
        """
        provider = resolve_fetch_provider(provider)
        spec = FETCH_PROVIDERS[provider]
        if not spec.needs_key or api_key or os.environ.get(spec.env_var):
            return provider
        logger.warning(
            "WebFetch: {} selected but no key resolves ({}); reading pages through {} instead",
            spec.label,
            spec.env_var,
            FETCH_PROVIDERS[DEFAULT_FETCH_PROVIDER].label,
        )
        return DEFAULT_FETCH_PROVIDER

    @property
    def spec(self) -> FetchProviderSpec:
        return FETCH_PROVIDERS[self.provider]

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get(self.spec.env_var, "")

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
        has several of them (invalid URL, digest, truncation, failure), and an
        instrument that has to be repeated at each is one that will eventually be
        missing from one of them.
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
                self._fetch_record(url, out)
                if out is not None
                else {"ts": time.time(), "op": "fetch", "url": url, "ok": False, "outcome": "aborted", "chars": 0}
            )

    def _fetch_record(self, url: str, out: str) -> dict[str, Any]:
        """Ledger line for one fetch, read off the tool's own structured envelope."""
        record: dict[str, Any] = {
            "ts": time.time(),
            "op": "fetch",
            "url": url,
            "source": "web",
        }
        record["ok"] = fetch_result_ok(out)
        try:
            payload = json.loads(out)
        except (TypeError, ValueError):
            return record
        if not isinstance(payload, dict):
            return record
        # ``extractor`` names the backend that actually served the page. It is a
        # measurement column, not a label, so it stays on the row even though this
        # build has one backend.
        for key, out_key in (
            # ``requested_chars`` is present only on a recovered fetch, and it is what the
            # requested URL itself returned. It wins over ``length`` for this row so the
            # stub is recorded as the stub it was: the recovered text has its own row,
            # written under the address that served it.
            ("requested_chars", "chars"),
            ("length", "chars"),
            ("source_chars", "source_chars"),
            ("digested", "digested"),
            ("docid", "docid"),
            ("truncated", "truncated"),
            ("error", "error"),
            ("encoding_lost", "encoding_lost"),
            ("extractor", "extractor"),
            ("served_url", "served_url"),
            ("fallbacks_tried", "fallbacks_tried"),
        ):
            if key in payload and out_key not in record:
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

        return await _send_with_retry(_send, op="fetch_retry", key=url, budget=self._state().retry_budget)

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
        # A backend with no anonymous tier cannot serve anything without its key.
        # Answered before the URL is judged: the fault is the configuration's, and
        # a well-formed URL would otherwise hide it behind a vendor error.
        if self.spec.needs_key and not self.api_key:
            return json.dumps(
                {
                    "error": (
                        f"{self.spec.label} API key not configured. Set it in ~/.raven/config.json "
                        f'under plugins.config["research-flow"].fetch.apiKey (or export '
                        f"{self.spec.env_var}), then restart the gateway."
                    ),
                    "url": url,
                },
                ensure_ascii=False,
            )
        # Off the event loop: ``socket.getaddrinfo`` is synchronous and can hang for
        # seconds on a slow resolver, and one process serves every session's turn.
        is_valid, error_msg = await asyncio.to_thread(_judge_fetch_target, url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # "no explicit proxy" is not "direct": with trust_env on, httpx
        # still honours HTTP(S)_PROXY from the environment.
        logger.debug(
            "WebFetch: {}",
            "proxy enabled" if self.proxy else "no explicit proxy (environment proxies may apply)",
        )
        try:
            text, status = await self._read_page(url)
        except httpx.HTTPStatusError as e:
            # Same rule as the search half: the reader and the status, not a
            # message that repeats the request URL.
            status = e.response.status_code
            logger.error("WebFetch error for {}: {} answered HTTP {}", url, self.spec.label, status)
            return json.dumps({"error": f"{self.spec.label} answered HTTP {status}", "url": url}, ensure_ascii=False)
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

        # A stub is not an answer. The reader returns 200 with a redirect notice, an empty
        # client-side shell or a licence banner, and the turn then reasons from a page it
        # never really read - on the run that prompted this, 21 of 33 listed pages came
        # back under 3,000 characters and the report estimated what it could not find.
        served_url, fallbacks_tried, requested_chars = url, [], len(text)
        if len(text) < _THIN_PAGE_CHARS:
            text, status, served_url, fallbacks_tried = await self._recover_thin(url, text, status)

        encoding_lost = _encoding_lost(text)
        source_chars = len(text)

        recovery: dict[str, Any] = {}
        if fallbacks_tried:
            recovery["fallbacks_tried"] = fallbacks_tried
            recovery["requested_chars"] = requested_chars
            if served_url != url:
                recovery["served_url"] = served_url

        # A page that lost its encoding is not worth a digest call: the
        # model would distill replacement characters.
        extracted = None if encoding_lost else await self._try_digest(text, info_to_extract, url)
        if extracted is not None:
            return json.dumps(
                {
                    "url": url,
                    "finalUrl": url,
                    "status": status,
                    "extractor": f"{self.spec.extractor}+digest",
                    "extractMode": extractMode,
                    "digested": True,
                    "info_to_extract": info_to_extract,
                    "source_chars": source_chars,
                    "length": len(extracted),
                    "text": extracted,
                    **recovery,
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
            "extractor": self.spec.extractor,
            "extractMode": extractMode,
            "truncated": truncated,
            "length": len(text),
            "text": text,
            **recovery,
        }
        if encoding_lost:
            payload["encoding_lost"] = True
            payload["warning"] = _ENCODING_LOST_WARNING
        return json.dumps(payload, ensure_ascii=False)

    async def _recover_thin(self, url: str, text: str, status: int) -> tuple[str, int, str, list[str]]:
        """Re-read a stub at the addresses that carry its body.

        Returns ``(text, status, served_url, tried)``. The best read wins rather than the
        first: a rewrite that also comes back thin must not replace a stub with a smaller
        stub, and the caller needs to know what was attempted either way.

        Every attempt that produced a page gets its own ledger line here, under the URL it
        actually read. That is what keeps the grounding check honest in both directions:
        the answer may cite the address it asked for or the one that served the text, and
        both were opened, so neither reads as fabricated. It also keeps the trail's page
        lengths true - the stub is recorded as the stub it was.
        """
        best_text, best_status, served = text, status, url
        tried: list[str] = []
        for candidate in fetch_fallbacks(url)[:_FALLBACK_LIMIT]:
            is_valid, _error = await asyncio.to_thread(_judge_fetch_target, candidate)
            if not is_valid:
                continue
            tried.append(candidate)
            try:
                alt_text, alt_status = await self._read_page(candidate)
            except Exception as e:  # noqa: BLE001 - a failed rewrite is not a failed fetch
                logger.debug("WebFetch fallback {} did not answer: {}", candidate, e)
                _ledger_append(
                    {
                        "ts": time.time(),
                        "op": "fetch",
                        "url": candidate,
                        "source": "web",
                        "ok": False,
                        "outcome": "fallback_error",
                        "chars": 0,
                        "fallback_for": url,
                    }
                )
                continue
            _ledger_append(
                {
                    "ts": time.time(),
                    "op": "fetch",
                    "url": candidate,
                    "source": "web",
                    "ok": True,
                    "chars": len(alt_text),
                    "status": alt_status,
                    "fallback_for": url,
                }
            )
            if len(alt_text) > len(best_text):
                best_text, best_status, served = alt_text, alt_status, candidate
            if len(best_text) >= _THIN_PAGE_CHARS:
                break
        return best_text, best_status, served, tried

    async def _read_page(self, url: str) -> tuple[str, int]:
        """One page, read the way the selected backend serves it.

        Raises on anything that did not produce a page. The Jina branch is
        byte-identical to what this tool has always sent - it is the default
        distribution's wire traffic. Do not "tidy" it into the others.

        A backend that can report a title separately does not get a slot here:
        this payload has never carried one, and Jina embeds its own header block
        in the text, so synthesising a partial imitation of that block would
        misreport an undated page.
        """
        if self.provider == "jina":
            headers = {"Accept": "text/plain"}
            if key := self.api_key:
                headers["Authorization"] = f"Bearer {key}"
            r = await self._get_with_retry(url, headers)
            text = r.text
            if _encoding_lost(text):
                # A poisoned cached extraction is the common case, and a fresh
                # render usually comes back clean.
                r = await self._get_with_retry(url, {**headers, "x-no-cache": "true"})
                text = r.text
            return text, r.status_code

        json_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        budget = self._state().retry_budget

        if self.provider == "tavily":
            headers = {**json_headers, "Authorization": f"Bearer {self.api_key}"}

            async def _send_tavily() -> httpx.Response:
                async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                    return await client.post("https://api.tavily.com/extract", json={"urls": [url]}, headers=headers)

            r = await _send_with_retry(_send_tavily, op="fetch_retry", key=url, budget=budget)
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else None
            # A single-URL request has at most one hit; Tavily may normalise the
            # URL (trailing slash, scheme) so this does not match on equality.
            hit = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            if hit is None:
                failed = data.get("failed_results") if isinstance(data, dict) else None
                reason = (
                    failed[0].get("error")
                    if isinstance(failed, list) and failed and isinstance(failed[0], dict)
                    else None
                )
                raise _ProviderPageError(f"Tavily: {reason or 'extract failed'}")
            if not (text := str(hit.get("raw_content") or "")):
                raise _ProviderPageError("Tavily returned no page content")
            return text, r.status_code

        if self.provider == "exa":
            headers = {**json_headers, "x-api-key": self.api_key}

            async def _send_exa() -> httpx.Response:
                async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                    return await client.post(
                        "https://api.exa.ai/contents", json={"urls": [url], "text": True}, headers=headers
                    )

            r = await _send_with_retry(_send_exa, op="fetch_retry", key=url, budget=budget)
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else None
            hit = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
            if hit is None or not (text := str(hit.get("text") or "")):
                raise _ProviderPageError("Exa returned no page content")
            return text, r.status_code

        if self.provider == "firecrawl":
            headers = {**json_headers, "Authorization": f"Bearer {self.api_key}"}

            async def _send_fc() -> httpx.Response:
                async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                    return await client.post(
                        "https://api.firecrawl.dev/v1/scrape",
                        json={"url": url, "formats": ["markdown"]},
                        headers=headers,
                    )

            r = await _send_with_retry(_send_fc, op="fetch_retry", key=url, budget=budget)
            data = r.json()
            if not isinstance(data, dict) or not data.get("success"):
                message = (data.get("error") or "scrape failed") if isinstance(data, dict) else "malformed response"
                raise _ProviderPageError(f"Firecrawl: {message}")
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            if not (text := str(body.get("markdown") or "")):
                raise _ProviderPageError("Firecrawl returned no page content")
            return text, r.status_code

        # AnySearch. No cache-bust header is published, so the garbled-page
        # re-read above has no counterpart here; the encoding check still runs
        # on what comes back.
        headers = dict(json_headers)
        if key := self.api_key:
            headers["Authorization"] = f"Bearer {key}"

        async def _send_any() -> httpx.Response:
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                return await client.post("https://api.anysearch.com/v1/extract", json={"url": url}, headers=headers)

        r = await _send_with_retry(_send_any, op="fetch_retry", key=url, budget=budget)
        data = r.json()
        if not isinstance(data, dict):
            raise _ProviderPageError("AnySearch returned a non-object body")
        # A transport-level failure already raised in ``_send_with_retry``; this
        # catches the other shape, a 200 whose envelope reports the failure.
        if data.get("code") not in (0, None):
            raise _ProviderPageError(f"AnySearch: {data.get('message') or 'extract failed'}")
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        if not (text := str(body.get("content") or "")):
            raise _ProviderPageError("AnySearch returned no page content")
        return text, r.status_code

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


__all__ = [
    "DEFAULT_FETCH_PROVIDER",
    "DEFAULT_SEARCH_PROVIDER",
    "FETCH_PROVIDERS",
    "SEARCH_PROVIDERS",
    "DigestFn",
    "FetchProviderSpec",
    "SearchProviderSpec",
    "WebFetchTool",
    "WebSearchTool",
    "current_session",
    "fetch_result_ok",
    "resolve_fetch_provider",
    "resolve_search_provider",
    "set_current_session",
]
