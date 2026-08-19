"""The saturation rule, as the tool actually executes it.

``test_agent_search_saturation.py`` covers the decision logic in isolation. This
file covers the three things only the wiring can get wrong, and each of them is a
failure that produces no error and no log line:

* a stopped turn that still issues the request (the rule becomes advice),
* a paginating turn that never puts ``page`` on the wire (the rule becomes a
  no-op that reports itself as working),
* an arm without the rule whose request body is no longer byte-identical (the
  flow-off anchor moves, and every published delta is measured against it).

The last one is why the request body is captured and compared rather than merely
inspected for a key.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from raven.agent.search_saturation import SearchSaturation
from raven.agent.tools.web import WebSearchTool

_DATA = {
    "organic": [
        {"title": "One", "link": "https://one.example", "snippet": "s1"},
        {"title": "Two", "link": "https://two.example", "snippet": "s2"},
    ]
}


class _Recorder:
    """Captures every request body the tool sends, and how many it sent."""

    def __init__(self, data=None):
        self.bodies: list[dict] = []
        self._data = _DATA if data is None else data

    def client(self, **_kw):
        recorder = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, _url, json=None, **_kw):
                recorder.bodies.append(json)
                return _R(recorder._data)

        return _C()


class _R:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def _patched(rec):
    return patch("raven.agent.tools.web.httpx.AsyncClient", rec.client)


@pytest.mark.asyncio
async def test_an_arm_without_the_rule_sends_the_body_it_always_sent():
    """The anchor's wire shape is the reference frame for every published delta."""
    rec = _Recorder()
    tool = WebSearchTool(api_key="k", max_results=5)
    with _patched(rec):
        await tool.execute(query="q")
    assert rec.bodies == [{"q": "q", "num": 5}]


@pytest.mark.asyncio
async def test_page_one_still_omits_the_page_key_entirely():
    """An un-saturated turn on a rule-carrying arm must also be unchanged.

    Sending ``page=1`` explicitly would be semantically identical and behaviourally
    untested - the endpoint's treatment of an explicit page 1 was never probed, so
    the safe shape is the one three batches already ran.
    """
    rec = _Recorder()
    tool = WebSearchTool(api_key="k", max_results=5,
                         saturation=SearchSaturation(k=2, on_saturate="paginate"))
    with _patched(rec):
        await tool.execute(query="q")
    assert rec.bodies == [{"q": "q", "num": 5}]


@pytest.mark.asyncio
async def test_a_saturated_turn_puts_page_on_the_wire():
    rec = _Recorder(data={"organic": []})
    sat = SearchSaturation(k=2, on_saturate="paginate", max_pages=2)
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with _patched(rec):
        await tool.execute(query="a")
        await tool.execute(query="b")
        assert sat.page == 2
        await tool.execute(query="c")
    assert rec.bodies[-1] == {"q": "c", "num": 5, "page": 2}


@pytest.mark.asyncio
async def test_a_stopped_turn_issues_no_request_at_all():
    """Control flow, not advice. The measured reason is in the module docstring:
    the model already receives a 'you have seen this' marker on 41.9% of its result
    slots and searches on regardless, so a rule that merely returned a warning
    alongside real results would be the intervention that has already failed."""
    rec = _Recorder(data={"organic": []})
    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with _patched(rec):
        await tool.execute(query="a")
        assert sat.stopped
        sent_before = len(rec.bodies)
        out = await tool.execute(query="b")
    assert len(rec.bodies) == sent_before
    assert "Search is closed" in out
    assert sat.counters()["sat_suppressed"] == 1


@pytest.mark.asyncio
async def test_a_stopped_turn_does_not_serve_the_replay_cache_either():
    """The stop check runs ahead of the replay lookup on purpose.

    A cached page handed back after the rule fired would be results plus a warning -
    i.e. exactly the advice shape - and it would keep the turn's context growing
    while reporting the rule as active.
    """
    rec = _Recorder()
    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, repeat_notice=True, saturation=sat)
    with _patched(rec):
        await tool.execute(query="q")   # populates the replay cache
        await tool.execute(query="q")   # replay: dry, trips the rule
        assert sat.stopped
        out = await tool.execute(query="q")
    assert "Search is closed" in out
    assert "one.example" not in out


@pytest.mark.asyncio
async def test_a_page_two_request_is_not_answered_from_the_page_one_cache():
    """dr@3.1. The replay key must carry the page, or paginating cannot work at all.

    dr@3.0 keyed on the two widths only. A turn that escalated to page 2 and then
    re-asked a query it had already run on page 1 got the page-1 entry back, so the
    rung reported itself as working while returning exactly what it was meant to move
    past. Measured on that batch's 302 live-web ledgers: 206 such replays against 83
    real page-2 requests, i.e. 71.3% of paginated searches.

    The second-order effect is why this is a test and not a note. A replay is scored
    as a dry search, so each false page-turn also pushed the turn one step closer to
    ``stopped`` - the broken rung fed the rung after it.
    """
    rec = _Recorder()
    sat = SearchSaturation(k=2, on_saturate="paginate", max_pages=2)
    tool = WebSearchTool(api_key="k", max_results=5, repeat_notice=True, saturation=sat)
    with _patched(rec):
        await tool.execute(query="q")          # real call, caches page 1
        await tool.execute(query="q")          # byte-identical repeat: replay, dry
        await tool.execute(query="q")          # second dry search trips the rule
        assert sat.page == 2
        sent_before = len(rec.bodies)
        await tool.execute(query="q")          # same terms, different page
    assert len(rec.bodies) == sent_before + 1, "page 2 was served from the page-1 cache"
    assert rec.bodies[-1] == {"q": "q", "num": 5, "page": 2}


@pytest.mark.asyncio
async def test_the_replay_cache_is_untouched_where_pagination_cannot_happen():
    """The other half of the same change: adding ``page`` to the key must not make an
    un-paginating arm miss a hit it used to get. A tuple element that is constant
    across every key moves no key relative to any other, and that is what keeps this
    repair off the anchor and off the corpus axis.
    """
    rec = _Recorder()
    tool = WebSearchTool(api_key="k", max_results=5, repeat_notice=True)
    with _patched(rec):
        await tool.execute(query="q")
        out = await tool.execute(query="q")
    assert len(rec.bodies) == 1, "an arm with no saturation rule lost its replay hit"
    assert "one.example" in out


@pytest.mark.asyncio
async def test_sat_event_marks_only_the_row_that_fired(tmp_path, monkeypatch):
    """dr@3.1. The field that makes firings countable without a reading convention.

    ``sat_action`` is sticky: the first batch carrying it was read row-wise and
    reported stops as outnumbering page-turns 67x, where the events are 25 to 27.
    ``sat_event`` is non-null on the firing row only, so the two questions - what
    state was this search served under, and did a rung fire here - stop sharing a
    field. Both directions are asserted: the firing row carries it, and the rows
    around it (including a suppressed one, which never reaches ``observe``) do not.
    """
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(tmp_path / "led.jsonl"))
    rec = _Recorder(data={"organic": []})
    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with _patched(rec):
        await tool.execute(query="a")   # zero-hit -> dry -> fires 'stopped'
        await tool.execute(query="b")   # suppressed: no observe, must not re-fire

    rows = [json.loads(x) for x in (tmp_path / "led.jsonl").read_text().splitlines() if x]
    searches = [r for r in rows if r.get("op") == "search"]
    assert [r["sat_action"] for r in searches] == ["stopped", "stopped"]
    assert [r["sat_event"] for r in searches] == ["stopped", None]
    assert [r["suppressed"] for r in searches] == [False, True]


@pytest.mark.asyncio
async def test_an_arm_without_the_rule_writes_sat_event_as_null_not_absent(tmp_path, monkeypatch):
    """A key that appears only where the mechanism exists cannot say which of
    'did not fire' and 'was never installed' a batch is showing - the whole lesson of
    ``dedup_skipped``, which read 0 on every row of every landed batch for two
    independent reasons. ``sat_event`` is present and null on an arm with no rule.
    """
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(tmp_path / "led.jsonl"))
    rec = _Recorder()
    tool = WebSearchTool(api_key="k", max_results=5)
    with _patched(rec):
        await tool.execute(query="q")

    rows = [json.loads(x) for x in (tmp_path / "led.jsonl").read_text().splitlines() if x]
    row = next(r for r in rows if r.get("op") == "search")
    assert "sat_event" in row and row["sat_event"] is None
    assert "sat_action" in row and row["sat_action"] is None


@pytest.mark.asyncio
async def test_the_corpus_path_turns_pagination_off_by_itself():
    """The config may ask to paginate; the corpus service cannot, so the tool says so.

    Asserted on construction rather than on behaviour because the point is that the
    tool does not wait for a saturated turn to discover it: a rule that believed it
    could page would report ``paginated`` in the ledger of an arm that never did.
    """
    sat = SearchSaturation(k=2, on_saturate="paginate")
    WebSearchTool(corpus_endpoint="http://local:8765", saturation=sat)
    assert sat.paginates is False


@pytest.mark.asyncio
async def test_a_zero_hit_search_counts_as_dry():
    """The shape that dominates the measured tail spin, and the one the first
    implementation missed: an empty result set returns before the rendering loop,
    so an observation placed only at the end of that loop never sees it."""
    rec = _Recorder(data={"organic": []})
    sat = SearchSaturation(k=2, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with _patched(rec):
        await tool.execute(query="a")
        await tool.execute(query="b")
    assert sat.stopped


@pytest.mark.asyncio
async def test_a_transport_failure_does_not_count_as_exhaustion():
    """An outage means the pool was never consulted.

    Counting it as dry would let a network fault close search on a turn whose
    queries were fine - a rule that converts infrastructure failure into an
    early answer, on the arm that searches most. Asserted rather than commented,
    because a distinction nothing tests is a distinction the next edit removes.
    """
    class _Boom:
        def client(self, **_kw):
            class _C:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def post(self, *a, **kw):
                    raise RuntimeError("connection reset")
            return _C()

    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with patch("raven.agent.tools.web.httpx.AsyncClient", _Boom().client):
        out = await tool.execute(query="a")
    assert out.startswith("Error:")
    assert not sat.stopped
    assert sat.counters()["sat_dry_streak"] == 0


@pytest.mark.asyncio
async def test_start_turn_reopens_search_for_the_next_question():
    rec = _Recorder(data={"organic": []})
    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = WebSearchTool(api_key="k", max_results=5, saturation=sat)
    with _patched(rec):
        await tool.execute(query="a")
        assert sat.stopped
        tool.start_turn()
        assert not sat.stopped
        await tool.execute(query="b")
    assert rec.bodies[-1] == {"q": "b", "num": 5}
