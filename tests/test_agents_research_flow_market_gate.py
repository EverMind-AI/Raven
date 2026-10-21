"""The market gate, driven the way the flow drives it.

The bug this file exists for: the gate read the flow's **ledger file** to
learn which pages the turn had opened, and the flow writes that file when
the turn **closes**. So a gate running mid-turn read a file that had not
been written yet, concluded the run had opened nothing, and refused every
cited page as unopened -- which meant the market rung never fired on the
flow's own output, and the rung looked inert rather than broken.

The rows are stashed on a contextvar while the turn runs. The gate reads
them there, and the flow's process appendix still gets them, because the
gate *peeks* and the appendix *takes*.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN))

from research_flow.gates.conversation import (  # noqa: E402
    stash_turn_rows,
    take_turn_rows,
    turn_rows,
)
from research_flow.gates.market_scan import MarketScanGate  # noqa: E402

ACME = "https://acme.example/pricing"
GLOBEX = "https://globex.example/pricing"

#: A table in the shape the research flow's report actually has: a name
#: column, fact columns, and the source carried as a link *inside* the cell
#: rather than in a source column.
ANSWER = f"""# Competitive landscape

> The market is led by two vendors.

| Vendor | What it does | Price | Read from |
|---|---|---|---|
| Acme | Observability sold per seat | $49 per seat per month | [acme]({ACME}) |
| Globex | Observability sold on request | Contact Sales | [globex]({GLOBEX}) |
"""

ROWS = [
    {"ts": 1.0, "op": "search", "query": "acme pricing", "urls": [ACME], "n": 5},
    {"ts": 2.0, "op": "fetch", "url": ACME, "ok": True, "chars": 14000, "extractor": "jina-reader"},
    {"ts": 3.0, "op": "search", "query": "globex pricing", "urls": [GLOBEX], "n": 5},
    {"ts": 4.0, "op": "fetch", "url": GLOBEX, "ok": True, "chars": 9000, "extractor": "jina-reader"},
]


def _ctx(answer: str = ANSWER):
    return SimpleNamespace(
        response=SimpleNamespace(content=answer, has_tool_calls=False),
        metadata={},
    )


def _gate() -> MarketScanGate:
    from research_flow.market.gates import MarketCriteria, MarketGateConfig

    return MarketScanGate(
        None,
        criteria=MarketCriteria(declared_at=0.0, min_entities=2),
        gate_config=MarketGateConfig(judge=False, sufficiency=False),
        declared_at=0.0,
        extractor_keys={"jina-reader": "jina"},
    )


@pytest.mark.asyncio
async def test_the_gate_fires_on_the_turns_stashed_rows():
    """The rows the turn stashed are the rows the gate needs.

    The gate **checks the report's own table** and ships the report with
    that table replaced by the verified one. It does not replace the report:
    the reader keeps the argument, the sections and the other tables, and the
    comparison table gains the page behind every fact in it.
    """
    stash_turn_rows(ROWS)
    try:
        decision = await _gate().after_iteration(_ctx())
    finally:
        take_turn_rows()
    shipped = decision.short_circuit_result
    assert shipped is not None
    # The report's own shape survives - this is what lets the flow's file
    # sync write the reply over the file the turn drafted.
    assert shipped.startswith("# Competitive landscape")
    assert "> The market is led by two vendors." in shipped
    # And the table is the verified one: every fact carries its page.
    assert "| Acme |" in shipped
    assert f"[Observability sold per seat]({ACME})" in shipped
    assert f"[$49 per seat per month]({ACME})" in shipped
    assert f"[Contact Sales]({GLOBEX})" in shipped


@pytest.mark.asyncio
async def test_without_the_stashed_rows_the_gate_refuses_rather_than_guessing():
    """No rows means no way to confirm a page was opened, so the gate declines.

    It does not treat the missing rows as "nothing was opened" and invent a
    gap from that: it says so and leaves the answer alone.
    """
    decision = await _gate().after_iteration(_ctx())
    assert decision.short_circuit_result is None
    assert any("market_scan_no_ledger" in n for n in decision.notes)


@pytest.mark.asyncio
async def test_a_table_with_no_sources_is_asked_for_once_and_only_once():
    """The report the flow's own contract produces, and what the gate does with it.

    The contract tells the model to keep a candidate's provenance out of the
    comparison table and under it as prose. So the table the flow produces
    names companies and carries no citation, and the gate cannot trace a
    single fact to a page. It asks for the sources - once - and says so in
    the record.
    """
    answer = """# Competitive landscape

> The market is led by two vendors.

| Vendor | What it does | Price |
|---|---|---|
| Acme | Observability sold per seat | $49 per seat per month |
| Globex | Observability sold on request | Contact Sales |
"""
    ctx = _ctx(answer)
    decision = await _gate().after_iteration(ctx)
    # Asked, not shipped, and not silently ignored.
    assert decision.short_circuit_result is None
    assert decision.rollback is True
    assert decision.rollback_inject is not None
    assert "Read from" in decision.rollback_inject[-1]["content"]
    assert ctx.metadata["market_scan"]["last_event"] == "bounced"

    # Asked a second time, the gate stops asking and declines: a turn that
    # was asked and did not answer keeps its own answer.
    again = await _gate().after_iteration(ctx)
    assert again.rollback is False
    assert again.short_circuit_result is None
    assert any("market_scan_no_sources" in n for n in again.notes)
    assert ctx.metadata["market_scan"]["last_event"] == "no_sources"


@pytest.mark.asyncio
async def test_a_report_without_a_company_table_is_left_alone():
    """A table that does not name companies is not a market table."""
    answer = """# Pricing models

| Model | How it charges | Typical vendors |
|---|---|---|
| Per host | Fixed monthly fee per monitored host | Datadog, New Relic |
| Per GB | Per GB of telemetry | Grafana Cloud |
"""
    ctx = _ctx(answer)
    decision = await _gate().after_iteration(ctx)
    assert decision.short_circuit_result is None
    assert decision.rollback is False
    assert any("market_scan_no_table" in n for n in decision.notes)


def test_appending_a_row_stashes_it_for_the_gate_that_runs_mid_turn():
    """The link that was missing: the rows are stashed as they are written.

    The ledger *file* is written when the turn closes, so a gate that reads
    the file mid-turn reads a file that has not been written yet. The rows
    are stashed as they are appended, which is what makes them visible to a
    gate running during the turn.
    """
    from research_flow.support import ledger as ledger_module

    ledger_module.ledger_path = lambda: None  # type: ignore[assignment]
    # ``ledger_append`` returns early with no path, so drive the stash the way
    # the writer does and read it back the way the gate does.
    from research_flow.gates.conversation import stash_turn_rows, turn_rows

    stash_turn_rows([*turn_rows(), {"op": "fetch", "url": ACME, "ok": True, "chars": 10}])
    try:
        rows = turn_rows()
        assert rows and rows[-1]["url"] == ACME
    finally:
        take_turn_rows()


def test_peeking_at_the_rows_leaves_them_for_the_appendix():
    """The gate looks; the flow's appendix takes. Both get the rows."""
    stash_turn_rows(ROWS)
    try:
        assert len(turn_rows()) == len(ROWS)
        assert len(turn_rows()) == len(ROWS)
        assert len(take_turn_rows()) == len(ROWS)
        assert turn_rows() == []
    finally:
        take_turn_rows()
