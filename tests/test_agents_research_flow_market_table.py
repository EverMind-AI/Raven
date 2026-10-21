"""The market table, and the gate that turns one into a shipped scan.

Two things are tested here, and they are the two halves of the rung.

**The parser.** A deep-research answer's comparison table is read as a set
of claims: a company, the facts said about it, and the page each fact came
from. The parser's refusals are the point - a fact that cannot be traced
to one page is a fact the scan must not carry, and the two ways that
happens (two pages in one cell, no page at all) are tested separately
because they are different mistakes.

**The gate.** It reads the turn's answer, builds the scan, runs the three
gates over it, and replaces the answer only when they pass. The tests
cover the three outcomes that matter: the scan ships, the scan is refused
before it is built, and the scan is built but a gate says no. In the last
two the turn's own answer stands - the gate is fail-open, and the note it
leaves says which gate said no, so "this arm shipped no market scan" and
"this arm's market scan was refused" stay different claims.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.base import GateCtx  # noqa: E402
from research_flow.gates.market_scan import MarketScanGate  # noqa: E402
from research_flow.market import (  # noqa: E402
    MarketCriteria,
    MarketGateConfig,
    MarketScanError,
    entities_from_table,
    find_market_table,
    normalise_field,
    opened_pages,
)

ACME = "https://acme.example/pricing"
GLOBEX = "https://globex.example/pricing"

TABLE = f"""| Company | What it does | Price | Read from |
|---|---|---|---|
| Acme | Observability sold per seat | $49 per seat per month | [acme.example]({ACME}) |
| Globex | Observability sold on request | Contact Sales | [globex.example]({GLOBEX}) |
"""

INLINE_TABLE = f"""| Company | Price |
|---|---|
| Acme | $49 per seat [acme.example]({ACME}) |
| Globex | Contact Sales [globex.example]({GLOBEX}) |
"""


def _ledger(tmp_path: Path, rows: list[dict]) -> str:
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def _rows() -> list[dict]:
    return [
        {"ts": 1_000.0, "op": "fetch", "url": ACME, "ok": True, "chars": 9000, "extractor": "jina-reader"},
        {"ts": 1_010.0, "op": "fetch", "url": GLOBEX, "ok": True, "chars": 8000, "extractor": "jina-reader"},
    ]


def _pages() -> dict:
    return opened_pages(_rows(), extractor_keys={"jina-reader": "jina"})


# ── the parser ──────────────────────────────────────────────────────────────


def test_a_market_table_is_found_and_read():
    table = find_market_table(TABLE)
    assert table is not None
    assert table.name_index == 0
    assert table.source_index == 3
    assert table.field_indices == [1, 2]


def test_a_table_with_links_in_the_fact_cells_is_a_market_table():
    table = find_market_table(INLINE_TABLE)
    assert table is not None
    assert table.name_index == 0
    assert table.field_indices == [1]


def test_a_table_with_no_citation_in_it_is_not_a_market_table():
    """A table of numbers and labels is not a market, and reading it as one
    would invent companies out of row headers."""
    assert find_market_table("| Metric | Value |\n|---|---|\n| Users | 10 |\n| Pages | 3 |\n") is None
    assert find_market_table("| Company | Price |\n|---|---|\n| Acme | $49 |\n") is None


def test_the_facts_and_their_pages_come_out_of_the_table():
    table = find_market_table(TABLE)
    entities = entities_from_table(table, _pages())
    assert [e.name for e in entities] == ["Acme", "Globex"]
    acme = entities[0]
    assert acme.fields["price"] == "$49 per seat per month"
    assert acme.fields["what_it_does"] == "Observability sold per seat"
    assert acme.source_urls() == [ACME]
    assert acme.fields_from(ACME) == ["price", "what_it_does"]


def test_a_fact_in_a_cell_with_a_link_takes_that_link_as_its_source():
    table = find_market_table(INLINE_TABLE)
    entities = entities_from_table(table, _pages())
    assert entities[0].source_urls() == [ACME]
    assert entities[0].fields["price"] == "$49 per seat"


def test_a_fact_citing_a_page_the_run_never_opened_is_refused():
    table = find_market_table(TABLE)
    with pytest.raises(MarketScanError) as exc:
        entities_from_table(table, opened_pages(_rows()[:1], extractor_keys={"jina-reader": "jina"}))
    assert "did not open" in str(exc.value)


def test_a_fact_citing_two_pages_is_refused():
    """Two pages is not one page, and picking either would be a guess."""
    table = find_market_table(
        f"| Company | Price |\n|---|---|\n"
        f"| Acme | $49 [a]({ACME}) [b]({GLOBEX}) |\n"
    )
    with pytest.raises(MarketScanError) as exc:
        entities_from_table(table, _pages())
    assert "cannot be traced to one" in str(exc.value)


def test_a_fact_with_no_page_anywhere_is_left_out():
    """It is not carried with a borrowed source; it is simply not there.

    A fact cell with no link of its own falls back to the row's source
    column - which is how a report that cites once per row is read. But a
    row with no source at all has no page for its facts, so those facts
    are dropped and the row is skipped rather than attributed to a page
    that never supported it.
    """
    table = find_market_table(
        f"| Company | What it does | Price | Read from |\n|---|---|---|---|\n"
        f"| Acme | A platform | $49 | [acme]({ACME}) |\n"
        f"| Globex | A platform | $9 |  |\n"
    )
    assert table is not None
    entities = entities_from_table(table, _pages())
    assert [e.name for e in entities] == ["Acme"]
    assert entities[0].source_urls() == [ACME]


def test_the_reader_that_served_a_page_names_the_source():
    pages = _pages()
    assert pages[ACME].source == "jina"
    assert pages[ACME].retrieved_at == 1_000.0


def test_a_reader_with_no_registry_key_keeps_its_own_name():
    """A reader whose terms have not been looked at cannot back a scan."""
    pages = opened_pages(_rows())
    assert pages[ACME].source == "jina-reader"


def test_a_field_name_is_normalised_for_the_criteria():
    assert normalise_field("What it does") == "what_it_does"
    assert normalise_field("Published price") == "published_price"


# ── the gate ────────────────────────────────────────────────────────────────


def _ctx(answer: str) -> GateCtx:
    return GateCtx(
        session_key="s1",
        response=SimpleNamespace(content=answer, has_tool_calls=False),
        metadata={},
    )


def _gate(tmp_path: Path, **kwargs) -> MarketScanGate:
    path = _ledger(tmp_path, _rows())
    params = {
        "criteria": MarketCriteria(declared_at=900.0, min_entities=2),
        "gate_config": MarketGateConfig(judge=False),
        "declared_at": 900.0,
        "ledger_path": lambda: path,
        "extractor_keys": {"jina-reader": "jina"},
    }
    params.update(kwargs)
    return MarketScanGate(**params)


@pytest.mark.asyncio
async def test_the_gate_ships_the_scan_when_the_gates_pass(tmp_path):
    gate = _gate(tmp_path)
    decision = await gate.after_iteration(_ctx(f"Here is the market.\n\n{TABLE}"))
    assert decision.short_circuit_result is not None
    assert "Acme" in decision.short_circuit_result
    assert "$49 per seat per month" in decision.short_circuit_result
    assert "market_scan_shipped" in " ".join(decision.notes)


@pytest.mark.asyncio
async def test_the_gate_leaves_the_answer_alone_when_there_is_no_table(tmp_path):
    gate = _gate(tmp_path)
    decision = await gate.after_iteration(_ctx("There is no market here."))
    assert decision.short_circuit_result is None
    assert any("market_scan_no_table" in n for n in decision.notes)


@pytest.mark.asyncio
async def test_the_gate_leaves_the_answer_alone_when_a_fact_cannot_be_traced(tmp_path):
    """A table that cites a page the run never opened is not shipped as a scan."""
    table = f"| Company | Price |\n|---|---|\n| Acme | $49 [acme]({ACME}) |\n| Nope | $9 [nope](https://nope.example/p) |\n"
    gate = _gate(tmp_path)
    decision = await gate.after_iteration(_ctx(table))
    assert decision.short_circuit_result is None
    assert any("market_scan_refused" in n for n in decision.notes)


@pytest.mark.asyncio
async def test_the_gate_leaves_the_answer_alone_when_a_gate_says_no(tmp_path):
    """The criteria are not met, so the scan does not ship and the answer stands."""
    gate = _gate(
        tmp_path,
        criteria=MarketCriteria(declared_at=900.0, min_entities=5),
    )
    decision = await gate.after_iteration(_ctx(TABLE))
    assert decision.short_circuit_result is None
    assert any("market_scan_gated" in n for n in decision.notes)
    assert any("sufficiency" in n for n in decision.notes)


@pytest.mark.asyncio
async def test_the_gate_leaves_the_answer_alone_when_the_run_opened_nothing(tmp_path):
    path = _ledger(tmp_path, [])
    gate = MarketScanGate(
        criteria=MarketCriteria(declared_at=900.0),
        gate_config=MarketGateConfig(judge=False),
        declared_at=900.0,
        ledger_path=lambda: path,
    )
    decision = await gate.after_iteration(_ctx(TABLE))
    assert decision.short_circuit_result is None
    assert any("market_scan_no_ledger" in n for n in decision.notes)


@pytest.mark.asyncio
async def test_the_gate_does_not_run_on_a_turn_that_is_still_working(tmp_path):
    gate = _gate(tmp_path)
    ctx = _ctx(TABLE)
    ctx.response = SimpleNamespace(content=TABLE, has_tool_calls=True)
    decision = await gate.after_iteration(ctx)
    assert decision.short_circuit_result is None
    # It says why it stood aside. A gate that declines in silence is
    # indistinguishable from a gate that was never wired.
    assert any("market_scan_skipped_tool_calls" in n for n in decision.notes)


# -- the table the flow's own contract produces -------------------------------
#
# The report clause asks for the URL of the page a finding came from in the
# **prose**, and says a candidate's provenance belongs under the comparison
# table rather than in it. So the comparison table a flow run writes names
# companies and carries no citation, and the strict reader refuses it.

CONTRACT_TABLE = """| Vendor | 2025 Gartner MQ | Pricing model | Key risk |
|---|---|---|---|
| Datadog | Leader (5th yr) | Per host + per-GB ingest | TCO spikes in k8s |
| Dynatrace | Leader (15th yr) | DPS (consumption commit) | Opaque enterprise pricing |
"""

PRICING_TABLE = """| Model | How it charges | Typical vendors |
|---|---|---|
| Per host | Fixed monthly fee per monitored host | Datadog, New Relic |
| Per GB | Per GB of telemetry | Grafana Cloud |
"""


def test_the_contract_table_is_found_by_its_shape_even_with_no_citations():
    """The shape finder sees the table the flow's report clause produces."""
    from research_flow.market.table import find_vendor_table

    table = find_vendor_table(CONTRACT_TABLE)
    assert table is not None
    assert table.header[table.name_index] == "Vendor"
    assert table.rows[0][0] == "Datadog"
    # No citation anywhere, so the strict reader refuses it.
    assert find_market_table(CONTRACT_TABLE) is None


def test_a_table_that_does_not_name_companies_is_not_a_vendor_table():
    """A pricing table lists vendors in a cell; it does not compare them."""
    from research_flow.market.table import find_vendor_table

    assert find_vendor_table(PRICING_TABLE) is None


def test_a_table_carrying_no_source_cannot_be_traced():
    """Whether any **fact** can be traced - a linked company name is not a fact."""
    from research_flow.market.table import carries_provenance, find_vendor_table

    table = find_vendor_table(CONTRACT_TABLE)
    assert table is not None
    assert carries_provenance(table) is False

    sourced = find_vendor_table(
        f"""| Vendor | Price | Read from |
|---|---|---|
| Datadog | $15/host | [acme]({ACME}) |
| Dynatrace | $2k/mo | [globex]({GLOBEX}) |
"""
    )
    assert sourced is not None
    assert carries_provenance(sourced) is True


def test_the_swap_keeps_the_report_and_checks_the_table():
    """The report is kept; the table inside it is the verified one."""
    from research_flow.market.table import find_vendor_table, swap_table_rows

    # The table the turn writes **after** it was asked for the sources.
    report = f"""# Competitive landscape

> The market is led by two vendors.

| Vendor | Pricing model | Read from |
|---|---|---|
| Datadog | Per host + per-GB ingest | [acme]({ACME}) |
| Dynatrace | DPS (consumption commit) | [globex]({GLOBEX}) |
## Trends

OpenTelemetry is the default now.
"""
    table = find_vendor_table(report)
    assert table is not None
    pages = {
        ACME: SimpleNamespace(url=ACME, retrieved_at=1_700_000_000.0, source="jina"),
        GLOBEX: SimpleNamespace(url=GLOBEX, retrieved_at=1_700_000_000.0, source="jina"),
    }
    entities = entities_from_table(find_market_table(report), pages)
    swapped = swap_table_rows(report, table, entities)

    # The report's own shape survives - that is what lets the flow's file
    # sync write the reply over the file the turn drafted.
    assert swapped.startswith("# Competitive landscape")
    assert "> The market is led by two vendors." in swapped
    assert "## Trends" in swapped
    assert "OpenTelemetry is the default now." in swapped

    # And the table is the verified one: every fact carries its page.
    assert "| Datadog |" in swapped
    assert f"[Per host + per-GB ingest]({ACME})" in swapped
    assert f"[DPS (consumption commit)]({GLOBEX})" in swapped
    # The source column still names the pages.
    assert f"[acme.example/pricing]({ACME})" in swapped
    assert f"[globex.example/pricing]({GLOBEX})" in swapped


def test_a_row_citing_a_page_the_run_never_opened_refuses_the_whole_scan():
    """One untraceable row refuses the scan - it does not drop the row.

    A scan that states something the sources do not support is worse than no
    scan, because the next reader cannot tell which parts to trust. So the
    refusal is the whole scan's, and the turn's own answer stands.
    """
    from research_flow.market.table import find_market_table

    report = f"""| Vendor | Price | Read from |
|---|---|---|
| Datadog | $15 per host | [acme]({ACME}) |
| Dynatrace | Contact Sales | [globex]({GLOBEX}) |
"""
    pages = {
        ACME: SimpleNamespace(url=ACME, retrieved_at=1_700_000_000.0, source="jina"),
    }
    with pytest.raises(MarketScanError) as excinfo:
        entities_from_table(find_market_table(report), pages)
    assert "which the run did not open" in str(excinfo.value)
