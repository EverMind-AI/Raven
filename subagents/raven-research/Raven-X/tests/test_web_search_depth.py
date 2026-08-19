"""dr@2.8 cross-query dedup: look deeper, render the same width.

The mechanism is one sentence - ask the retrieval service for more than will be
shown, drop documents this turn has already listed, backfill from deeper - and
every way it can be present and do nothing gets an assertion here:

* rendering more lines than before, which trades the measured win for context
  growth on the axis where context is the binding constraint;
* asking deeper without skipping anything, which spends the service's time to
  render the identical five rows;
* skipping without asking deeper, which returns short lists instead of fresh
  documents;
* leaving it on for the flow-off anchor, whose conversion rate from surfaced to
  fetched is measurably different, so the same change is worth a different
  amount on each arm.
"""

from __future__ import annotations

import httpx
import pytest

from raven.agent.tools.web import WebSearchTool


class _RankedCorpus(httpx.AsyncBaseTransport):
    """Corpus stub whose ranking is stable and whose depth is honoured.

    Returns ``d1..dk`` in order, so "which documents came back" and "how deep did
    we ask" are both readable off the response instead of assumed.
    """

    def __init__(self) -> None:
        self.ks: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        k = int(request.url.params.get("k") or 5)
        self.ks.append(k)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "docid": f"d{i}",
                        "title": f"Title {i}",
                        "url": f"https://corpus/{i}",
                        "snippet": f"window {i}",
                    }
                    for i in range(1, k + 1)
                ]
            },
        )


def _patch(monkeypatch, transport):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _docids(rendered: str) -> list[str]:
    return [
        line.split("/")[-1].strip()
        for line in rendered.splitlines()
        if line.strip().startswith("https://corpus/")
    ]


@pytest.mark.asyncio
async def test_off_by_default_the_request_width_is_the_rendered_width(monkeypatch):
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765")

    await tool.execute(query="a")
    await tool.execute(query="b")

    assert transport.ks == [5, 5], "the anchor shares this path and must not go deeper"


def test_both_class_defaults_are_off():
    """Two layers protect the anchor and each needs its own assertion.

    The flow-off arm builds no flow and gets the tool constructor's defaults; a
    treated arm that did not opt in gets the config's. Testing only the first
    leaves the second free to be flipped - which is exactly what happened when
    this was checked by reverse-injection: flipping the config default broke
    nothing, because no test looked at it.
    """
    from raven.config.raven import DRFlowSearchConfig

    assert DRFlowSearchConfig().cross_query_dedup is False
    tool = WebSearchTool()
    assert tool.cross_query_dedup is False


@pytest.mark.asyncio
async def test_a_second_query_gets_documents_the_first_one_did_not_show(monkeypatch):
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=20
    )

    first = await tool.execute(query="a")
    second = await tool.execute(query="b")

    assert transport.ks == [20, 20]
    assert _docids(first) == ["1", "2", "3", "4", "5"]
    # The whole mechanism in one assertion: the second query's top five were all
    # spent already, so the five slots carry ranks 6-10 instead of repeating.
    assert _docids(second) == ["6", "7", "8", "9", "10"]


@pytest.mark.asyncio
async def test_the_rendered_width_never_grows(monkeypatch):
    """Context size is the constraint, not the result count.

    Returning ten rows instead of five was measured at +4.17~6.67pp gold surfaced
    and doubles the result text; deduplicating at unchanged width measured higher
    at no context cost. A change that widened the render would be trading the
    larger win for the smaller one while also feeding the failure mode that
    context overflow prevention exists to stop.
    """
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    plain = WebSearchTool(corpus_endpoint="http://local:8765")
    deep = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=50
    )

    baseline = await plain.execute(query="a")
    widened = await deep.execute(query="a")

    assert len(_docids(baseline)) == len(_docids(widened)) == 5
    assert len(widened) == len(baseline), "byte-for-byte the same size, not merely similar"


@pytest.mark.asyncio
async def test_depth_without_dedup_is_not_reachable(monkeypatch):
    """``search_depth`` is inert on its own.

    Asking deeper while still rendering the head of the list would spend the
    service's time to produce the identical five rows. The two settings are one
    mechanism, and the config default keeps them that way.
    """
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", search_depth=50)

    await tool.execute(query="a")

    assert transport.ks == [5]


@pytest.mark.asyncio
async def test_width_is_preserved_no_matter_how_much_is_deduped(monkeypatch):
    """The invariant, stated once and checked over the whole exhaustion curve.

    Dedup may only ever *replace* result lines, never delete them. Where a deeper
    pool exists the replacements come from depth; where it does not, the slots go
    back to the documents dedup wanted to skip. Without this rule the same config
    key means "same width, better contents" on the corpus path and "half as many
    lines" on the live-web one - one knob with two behaviours, which is how an
    offline price stops applying to the thing that ships.
    """
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=8
    )

    widths = [len(_docids(await tool.execute(query=f"q{i}"))) for i in range(4)]

    assert widths == [5, 5, 5, 5], f"width collapsed as the pool ran dry: {widths}"


@pytest.mark.asyncio
async def test_an_exhausted_query_falls_back_to_its_own_head(monkeypatch):
    """A fully-seen result set renders its head rather than nothing.

    "No results for: q" is a claim about the corpus. Emitting it because the model
    has already seen these documents would be false, and it would push the model
    to re-issue the query it just ran - the exact behaviour the repeat-notice
    machinery exists to suppress.
    """
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=5
    )

    await tool.execute(query="a")
    second = await tool.execute(query="b")

    assert "No results for" not in second
    assert _docids(second) == ["1", "2", "3", "4", "5"]


@pytest.mark.asyncio
async def test_skips_are_recorded_so_a_dead_mechanism_is_visible(monkeypatch, tmp_path):
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=20
    )

    await tool.execute(query="a")
    await tool.execute(query="b")

    import json

    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["k"] for r in rows] == [5, 5], "the shown-width key keeps its old meaning"
    assert [r["k_requested"] for r in rows] == [20, 20]
    assert [r["dedup_skipped"] for r in rows] == [0, 5]


@pytest.mark.asyncio
async def test_dedup_is_scoped_to_the_turn(monkeypatch):
    transport = _RankedCorpus()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765", cross_query_dedup=True, search_depth=20
    )

    await tool.execute(query="a")
    tool.start_turn()
    after = await tool.execute(query="b")

    # The same document a week later is a legitimate re-read, and the tool instance
    # outlives the turn in a gateway.
    assert _docids(after) == ["1", "2", "3", "4", "5"]


@pytest.mark.asyncio
async def test_live_web_dedups_but_never_asks_for_more(monkeypatch):
    """``num`` is inert on the endpoint this build uses.

    Probed directly: 10, 20, 50 and 100 all returned HTTP 200 with 7-8 organic
    results, while ``page=2`` returned the next ten. Sending a larger ``num`` would
    log as a deep request and return the same rows. Dedup still applies to what
    does come back; depth here needs pagination, which is a separate change with a
    separate quota cost.
    """
    sent: list[int] = []

    class _Serper(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            import json as _json

            sent.append(_json.loads(request.content)["num"])
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {"title": f"T{i}", "link": f"https://corpus/{i}"} for i in range(1, 9)
                    ]
                },
            )

    transport = _Serper()
    _patch(monkeypatch, transport)
    tool = WebSearchTool(api_key="k", cross_query_dedup=True, search_depth=50, max_results=5)

    first = await tool.execute(query="a")
    second = await tool.execute(query="b")

    assert sent == [5, 5], "the live-web request width is untouched by search_depth"
    assert _docids(first) == ["1", "2", "3", "4", "5"]
    # Not ["6","7","8"]. There is no deeper pool on this path - ``num`` is inert, so
    # the eight rows are all there is - and dropping the five already-seen ones would
    # delete result lines rather than replace them. Measured on the dr@2.7 web batch
    # 52.89% of the DR arm's slots were repeats, i.e. plain dedup would have cut the
    # list roughly in half. Fresh documents come first; the rest of the width is
    # given back.
    assert len(_docids(second)) == 5
    assert _docids(second)[:3] == ["6", "7", "8"]
