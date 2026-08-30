"""Web tools: ``web_search`` and ``web_fetch`` for the research flow.

These replace the kernel's built-in tools of the same names. Search goes to Serper
(``POST https://google.serper.dev/search``), fetch goes through the Jina reader
(``GET https://r.jina.ai/{url}``), exactly as the built-ins do; what this pair adds
is the research harness around the call - the per-turn replay cache and repeat
notice, snippet and cross-query dedup, the saturation rule, the evidence round,
the digest path, a bounded retry policy, and a ledger row for every call.

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
import time
import zlib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
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
                "error": str(last),
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
    """Search the web through Serper."""

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
        repeat_notice: bool = False,
        evidence_round_factory: Callable[[], EvidenceRound] | None = None,
        saturation_factory: Callable[[], SearchSaturation] | None = None,
    ):
        self._init_api_key = api_key
        self.max_results = max_results
        self.proxy = proxy
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
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("SERPER_API_KEY", "")

    def _state(self) -> _SearchTurnState:
        """The current session's slot, built on first use from inside that session's context."""
        key = _SESSION.get()
        state = self._sessions.get(key)
        if state is None:
            state = _SearchTurnState(
                self._evidence_round_factory() if self._evidence_round_factory is not None else None,
                self._saturation_factory() if self._saturation_factory is not None else None,
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
                "Error: Serper API key not configured. Set it in "
                '~/.raven/config.json under plugins.config["research-flow"].search.apiKey '
                "(or export SERPER_API_KEY), then restart the gateway.",
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

            r = await _send_with_retry(_send, op="search_retry", key=query, budget=state.retry_budget)
            data = r.json()
            if not isinstance(data, dict):
                data = {}
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
        except httpx.ProxyError as e:
            logger.error("WebSearch proxy error: {}", e)
            return f"Proxy error: {e}", [], _error_shaping(e)
        except Exception as e:
            logger.error("WebSearch error: {}", e)
            return f"Error: {e}", [], _error_shaping(e)


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


class WebFetchTool(Tool):
    """Fetch and extract content from a URL through the Jina reader."""

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
    ):
        self._init_api_key = api_key
        self.max_chars = max_chars
        self.proxy = proxy
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

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("JINA_API_KEY", "")

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
            ("length", "chars"),
            ("source_chars", "source_chars"),
            ("digested", "digested"),
            ("docid", "docid"),
            ("truncated", "truncated"),
            ("error", "error"),
            ("encoding_lost", "encoding_lost"),
            ("extractor", "extractor"),
        ):
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
        except httpx.ProxyError as e:
            logger.error("WebFetch proxy error for {}: {}", url, e)
            return json.dumps({"error": f"Proxy error: {e}", "url": url}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

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
                    "extractor": "jina-reader+digest",
                    "extractMode": extractMode,
                    "digested": True,
                    "info_to_extract": info_to_extract,
                    "source_chars": source_chars,
                    "length": len(extracted),
                    "text": extracted,
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
            "extractor": "jina-reader",
            "extractMode": extractMode,
            "truncated": truncated,
            "length": len(text),
            "text": text,
        }
        if encoding_lost:
            payload["encoding_lost"] = True
            payload["warning"] = _ENCODING_LOST_WARNING
        return json.dumps(payload, ensure_ascii=False)

    async def _read_page(self, url: str) -> tuple[str, int]:
        """One page through the reader: the text and the status to report.

        Raises on anything that did not produce a page. The request is byte-identical
        to what this tool has always sent - it is the anchor's wire traffic.
        """
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
    "DigestFn",
    "WebFetchTool",
    "WebSearchTool",
    "current_session",
    "fetch_result_ok",
    "set_current_session",
]
