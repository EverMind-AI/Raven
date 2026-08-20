"""Per-turn search saturation: notice when searching has stopped finding documents.

A deep-research turn can spend most of its budget re-asking a question the corpus
has already answered as far as it is going to. Measured on one 100-question batch,
1,120 of 5,586 searches were trailing dry spin - 20.1% - and of the 14 questions
that ended with six or more consecutive dry searches, **zero were judged correct**.
One question issued 295 searches, the last 234 of which returned nothing the turn
had not already been shown.

The rule: ``k`` consecutive searches that return no identity new to this turn means
the query family is exhausted. What happens then is a ladder, cheapest rung first -
widen the rendered result list to what the endpoint was already returning (free: the
same one call), then ask for the next page (one extra call), then stop searching -
never "advise the model".

The order is the point. On the live-web axis ``num`` saturates upward but is honoured
downward, and the default rendered width is five, so 64.6% of one batch's DR searches
displayed five rows out of a response that had already returned nine. Buying a second
call before displaying the rows of the first spends quota to get what was on the table.

**Why the action must be control flow.** The model is already being told. With
snippet dedup on, a replay of the live-web batch shows 41.9% of the DR arm's result
slots carry a "you have already seen this" marker, and the model keeps searching
anyway. Adding a sentence would be rerunning an experiment that has already failed
once at scale. So a stopped turn does not receive a recommendation - the call does
not happen. ``web_fetch`` is untouched: every URL already surfaced stays openable,
which is what keeps the priced loss upper bound at zero.

**Why this is its own object.** The mechanism it replaces, dr@2.8's cross-query
dedup, shipped with a counter (``dedup_skipped``) that reads 0 on every search row
of every batch since - so there is no on-disk evidence it ever fired, and its
effect has to be inferred from an unrelated field. That happened because the
counter lived inside a branch that a second, independent config knob decided
whether to enter. Here the state, the decision and the counters are one testable
unit that does not depend on any other knob's setting, and ``counters()`` is
written on every search row including the ones where nothing happened - because a
counter that only appears when the mechanism fires cannot distinguish "did not
fire" from "was not installed".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class SearchSaturation:
    """Decides, per turn, whether searching should deepen or stop.

    One instance per tool, reset at ``start_turn``. Holding it here rather than in
    ``WebSearchTool`` keeps its identity set separate from the two that already
    exist there - ``_snippet_seen`` ("has this been previewed") and ``_result_seen``
    ("has this been listed") - which are themselves deliberately separate. Sharing
    a set with either would make one knob silently move another's trigger point.
    """

    k: int = 10
    identity_key: str = "url"
    on_saturate: str = "paginate"
    max_pages: int = 2
    widen_to: int = 10
    """Rendered width the ``widen`` step escalates to.

    10 because the endpoint saturates there: probed at num 10/20/50/100, every value
    returned 8-10 organic rows, so asking for more is a request that logs as though it
    went deep and does not. Below it, ``num`` IS honoured - num=5 returns exactly five -
    which is what makes this step free: the wider response costs the same one call."""
    paginates: bool = True
    """Whether the caller's endpoint can actually serve a page beyond the first.

    False on the fixed-corpus path, whose service takes a width but no offset. A
    corpus arm configured to paginate would otherwise behave exactly like a
    stopping one while the config said otherwise - the failure mode where a
    capability is on, its activation site is unreachable, and there is no symptom.
    """

    _seen: set[str] = field(default_factory=set, repr=False)
    _query_pages: dict[str, int] = field(default_factory=dict, repr=False)
    """Deepest page actually REQUESTED per normalized query, this turn.

    The ladder's escalation level (``_page``) is turn-global by design - the dry
    streak that drives it deliberately spans queries. But a page is only deeper
    for a query that has already been shown the pages before it: serving a
    brand-new query at page 2 skips ranks 1-10 for terms nobody has seen ranked,
    and its near-certain empty page then scores as dry, feeding the ``stop``
    rung. ``page_for`` therefore caps the global escalation at one past the
    deepest page this query family has actually been sent to."""
    _dry_streak: int = 0
    _width: int | None = None
    _page: int = 1
    _stopped: bool = False
    _pages_opened: int = 0
    _suppressed: int = 0
    _degraded: bool = False
    _fired: bool = False
    """Whether the rung this row is being logged for fired on this row.

    dr@3.1. ``sat_action`` is a sticky state label - once a turn latches ``stopped``
    every later row of that turn repeats it - and the first reading of a batch that
    carried it counted rows as firings, reporting stops as outnumbering page-turns by
    67x where the events are 25 against 27. The state label is the right field for
    "what state was this search served under" and the wrong one for "how often did
    the rule act", so the second question gets its own field rather than a convention
    about how to read the first.
    """

    def reset(self, keep_seen: bool = False) -> None:
        """Clear turn state. ``keep_seen`` retains only the identity set.

        dr@3.0 product surface (``identityScope="topic"``). Everything that is a
        decision or a budget always clears, including with ``keep_seen``: a
        carried ``_stopped`` would open the next turn with search already closed
        for a question nobody had asked, and a carried ``_dry_streak`` would
        condemn the new turn on the previous one's evidence.

        ⚠️ What ``keep_seen`` does keep still has a cost, and it is the reason the
        scope is not the default: a follow-up's first searches legitimately
        re-find the previous turn's pages, and each of those now counts as dry,
        so the rule can escalate earlier than it would have on turn one. The
        streak reset bounds this to ``k`` searches of head start, not more.
        """
        if not keep_seen:
            self._seen.clear()
        self._query_pages.clear()
        self._dry_streak = 0
        self._width = None
        self._page = 1
        self._stopped = False
        self._pages_opened = 0
        self._suppressed = 0
        self._degraded = False
        self._fired = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def page(self) -> int:
        """The rung the ladder has escalated to. 1 means the default page.

        This is the turn-global escalation LEVEL, not what any given request
        should send - requests go through :meth:`page_for`, which keys the
        depth on the query family so a fresh query still starts at page 1.
        """
        return self._page

    def page_for(self, query_key: str) -> int:
        """Page the next request for this (normalized) query should ask for.

        Caps the global escalation at one past the deepest page this query has
        actually been requested at, so pagination deepens a repeated query
        instead of beheading a new one. Pure - recording an issued request is
        :meth:`note_page`'s job, so reading this cannot move state.
        """
        if self._page <= 1:
            return 1
        return min(self._page, self._query_pages.get(query_key, 0) + 1)

    def note_page(self, query_key: str, page: int) -> None:
        """Record that a real request for this query was served at ``page``.

        Called by the tool after the request came back - never on a replay
        hit, a suppressed call, or a transport error, none of which served
        a page anyone saw.
        """
        if page > self._query_pages.get(query_key, 0):
            self._query_pages[query_key] = page

    def width(self, requested: int) -> int:
        """Rendered width for the next search, given what the caller asked for.

        Returns ``requested`` unchanged until the ``widen`` step has fired, and never
        narrows: a rule whose job is to find more documents must not be able to return
        fewer, and ``max`` here is what makes that structural rather than a promise.
        """
        if self._width is None:
            return requested
        return max(requested, self._width)

    def suppress(self) -> None:
        """Count a search the rule refused to issue. Called by the tool, not here."""
        self._suppressed += 1

    def observe(self, identities: Iterable[str]) -> None:
        """Record one completed search's result identities and update the streak.

        A replayed (cached) search is deliberately passed through here too: an exact
        repeat is the archetypal dry search, and exempting it would let a turn stall
        forever at zero cost to the streak. A zero-hit search is dry for the same
        reason - it returned no document this turn had not seen, which is the
        definition, not an approximation of it.
        """
        fresh = False
        for identity in identities:
            if not identity:
                continue
            if identity not in self._seen:
                self._seen.add(identity)
                fresh = True
        if fresh:
            self._dry_streak = 0
            return
        self._dry_streak += 1
        if self._dry_streak < self.k:
            return
        self._escalate()

    def end_row(self) -> None:
        """Close the ledger row this rule was just reported on. Called by the tool.

        Kept separate from ``counters()`` so that reading the counters stays free of
        side effects - a reader that advances state is the shape that makes a field
        depend on who looked at it and how often. This is the only place a row ends,
        and every logged search reaches it, including the ones where the rule did
        nothing and the ones a transport error cut short.
        """
        self._fired = False

    def _escalate(self) -> None:
        # Every path out of here changes state - widen, page, or stop - so this row is
        # a firing whichever rung it lands on, and the label for it comes from the one
        # place that names rungs, ``counters()``.
        self._fired = True
        # Cheapest step first. Widening costs nothing - the endpoint already returned
        # those rows and the arm paid for them - while paginating buys a second call,
        # and the extra call lands disproportionately on the arm that searches most.
        # Spending quota before spending what is already on the table gets the order
        # backwards.
        if self.on_saturate == "widen" and self._width is None:
            self._width = self.widen_to
            self._dry_streak = 0
            return
        can_page = (
            self.on_saturate in ("paginate", "widen")
            and self.paginates
            and self._page < self.max_pages
        )
        if can_page:
            self._page += 1
            self._pages_opened += 1
            # The streak restarts because a new page is a genuinely different pool:
            # judging it on the evidence that condemned the previous one would stop
            # the turn on the first search of a page it has not read yet.
            self._dry_streak = 0
            return
        if self.on_saturate in ("paginate", "widen") and not self.paginates:
            self._degraded = True
        self._stopped = True

    def counters(self) -> dict[str, object]:
        """Ledger payload. Written on every search row, firing or not.

        ``action`` reports what the rule DID, not what it was configured to do, so a
        paginate-configured arm on an endpoint that cannot page is readable as such
        instead of appearing to have paged.
        """
        if self._stopped:
            action = "stopped_degraded" if self._degraded else "stopped"
        elif self._pages_opened:
            action = "paginated"
        elif self._width is not None:
            action = "widened"
        else:
            action = "none"
        return {
            "sat_action": action,
            # dr@3.1. Non-null ONLY on the row whose ``observe`` escalated a rung, so
            # counting firings is counting non-null cells rather than applying a
            # convention to ``sat_action``. Deliberately the SAME string as
            # ``sat_action`` rather than a name of its own: two fields that can
            # disagree about which rung fired would need a rule for which one wins,
            # and there is no honest rule. Null therefore means "no rung fired on this
            # row", which on an arm with the rule installed is the overwhelmingly
            # common case - and null-on-every-row is still distinguishable from an arm
            # with no rule at all, because that one has ``sat_action`` null too.
            "sat_event": action if self._fired else None,
            "sat_dry_streak": self._dry_streak,
            "sat_page": self._page,
            "sat_width": self._width,
            "sat_pages_opened": self._pages_opened,
            "sat_suppressed": self._suppressed,
            "sat_seen": len(self._seen),
        }


__all__ = ["SearchSaturation"]
