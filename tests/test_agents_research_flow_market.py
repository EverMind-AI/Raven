"""The market scan: what it refuses, what it writes, and what the audit says about it.

The capability exists for one property - *the report says only what the sources support* -
and that property has to be mechanical. So the tests below are written as the failures they
prevent: a field nobody sourced, a source whose terms do not permit the use, a page the run
never opened, a field nobody can date, a field cleared for a different use, and a link the
run never fetched.

The last test is the one that matters most. It builds a scan, writes it, and runs the
repository's own ``research_report_audit.py`` over the file it produced, and asserts that
the audit finds **nothing** - not "no hard findings". A scan that emits a report the audit
can find a defect in is a scan whose gate does not hold, and the audit is the only thing
here that can say so without a judge in the loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.market import (  # noqa: E402
    MarketEntity,
    MarketScanError,
    build_scan,
    read_entities,
    render_markdown,
    write_entities,
    write_scan,
)
from research_flow.market.ledger import entities_payload  # noqa: E402

from raven.records.types import Provenance  # noqa: E402
from scripts.research_report_audit import ESTIMATE_MARKERS, audit, parse_tables  # noqa: E402

AS_OF = "2026-10-20"
ACME = "https://acme.example/pricing"
GLOBEX = "https://globex.example/pricing"
INITECH = "https://initech.example/pricing"

#: A run's ledger, in the shape the flow writes it: one row per search, one per fetch.
LEDGER_ROWS = [
    {
        "ts": 1.0,
        "op": "search",
        "query": "acme pricing per seat",
        "k": 5,
        "urls": [ACME, "https://acme.example/docs"],
    },
    {
        "ts": 2.0,
        "op": "search",
        "query": "globex pricing",
        "k": 5,
        "urls": [GLOBEX],
    },
    {
        "ts": 3.0,
        "op": "search",
        "query": "initech pricing per gb",
        "k": 5,
        "urls": [INITECH],
    },
    {"ts": 4.0, "op": "fetch", "url": ACME, "ok": True, "chars": 14000},
    {"ts": 5.0, "op": "fetch", "url": GLOBEX, "ok": True, "chars": 12000},
    {"ts": 6.0, "op": "fetch", "url": INITECH, "ok": True, "chars": 9000},
]

OPENED = [ACME, GLOBEX, INITECH]


def _ledger(tmp_path: Path) -> str:
    """The run's ledger on disk, for the appendix to read."""
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in LEDGER_ROWS),
        encoding="utf-8",
    )
    return str(path)


def _provenance(field: str, url: str, *, source: str = "jina", retrieved_at: str = AS_OF) -> Provenance:
    return Provenance(
        field=field,
        source_url=url,
        source=source,
        method="fetch",
        retrieved_at=retrieved_at,
    )


def _acme() -> MarketEntity:
    return MarketEntity(
        key="acme",
        name="Acme",
        fields={
            "what_it_does": "Observability platform sold per seat.",
            "price": "$49 per seat per month",
        },
        provenance=[
            _provenance("what_it_does", ACME),
            _provenance("price", ACME),
        ],
    )


def _globex() -> MarketEntity:
    """A vendor that publishes no price.

    The price is **absent** from the fields rather than present and empty: a field the
    source does not state is not a field the scan has to defend, and a present-and-empty
    field is exactly the unsourced claim the scan refuses.
    """
    return MarketEntity(
        key="globex",
        name="Globex",
        fields={"what_it_does": "Observability platform sold on request."},
        provenance=[_provenance("what_it_does", GLOBEX)],
    )


def _initech() -> MarketEntity:
    return MarketEntity(
        key="initech",
        name="Initech",
        fields={
            "what_it_does": "Log ingest billed by the gigabyte.",
            "price": "$0.50 per GB ingest",
        },
        provenance=[
            _provenance("what_it_does", INITECH),
            _provenance("price", INITECH),
        ],
    )


def _entities() -> list[MarketEntity]:
    return [_acme(), _globex(), _initech()]


def _scan(**kwargs):
    params = {
        "as_of": AS_OF,
        "opened_urls": OPENED,
        "opt_ins": {},
    }
    params.update(kwargs)
    return build_scan(_entities(), **params)


# ── the ledger ──────────────────────────────────────────────────────────────


def test_a_field_with_no_source_is_a_claim_the_entity_cannot_defend():
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform.", "price": "$49"},
        provenance=[_provenance("what_it_does", ACME)],
    )
    assert entity.unsourced_fields() == ["price"]


def test_the_ledger_round_trips_through_its_file(tmp_path):
    path = write_entities(_entities(), tmp_path / "entities.json", as_of=AS_OF)
    back = read_entities(path)
    assert [e.key for e in back] == ["acme", "globex", "initech"]
    assert back[0].fields["price"] == "$49 per seat per month"
    assert back[0].source_urls() == [ACME]
    assert back[0].fields_from(ACME) == ["price", "what_it_does"]


def test_the_ledger_file_carries_the_date_and_the_use_it_was_collected_for(tmp_path):
    path = write_entities(_entities(), tmp_path / "entities.json", as_of=AS_OF, use="competitive_brief")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "raven_market_entities"
    assert data["as_of"] == AS_OF
    assert data["use"] == "competitive_brief"
    assert len(data["entities"]) == 3


def test_the_two_shapes_of_the_entities_file_are_one_shape():
    """The scan's own writer and the ledger's writer must not drift apart."""
    scan = _scan()
    payload = scan.to_entities_json()
    assert payload["schema"] == "raven_market_entities"
    assert payload["entities"] == entities_payload(
        scan.entities, as_of=scan.as_of, use=scan.use
    )["entities"]


# ── the refusals ────────────────────────────────────────────────────────────


def test_a_fabricated_field_fails_the_scan():
    """A field with nothing behind it is a claim the entity cannot defend."""
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform.", "price": "$49"},
        provenance=[_provenance("what_it_does", ACME)],
    )
    with pytest.raises(MarketScanError) as exc:
        build_scan([entity], as_of=AS_OF, opened_urls=OPENED, opt_ins={})
    assert "no source" in str(exc.value)
    assert "price" in str(exc.value)


def test_a_restricted_source_without_an_opt_in_fails():
    """A source whose terms do not permit the use is refused, with the remedy."""
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform."},
        provenance=[_provenance("what_it_does", ACME, source="linkedin")],
    )
    with pytest.raises(MarketScanError) as exc:
        build_scan([entity], as_of=AS_OF, opened_urls=OPENED, opt_ins={})
    message = str(exc.value)
    assert "linkedin" in message
    assert "may not be used" in message
    # The refusal has to say what would change the answer, or it is a scold.
    assert "optin" in message


def test_a_restricted_source_with_a_recorded_opt_in_is_allowed():
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform."},
        provenance=[_provenance("what_it_does", ACME, source="linkedin")],
    )
    scan = build_scan(
        [entity],
        as_of=AS_OF,
        opened_urls=OPENED,
        opt_ins={"linkedin": True},
    )
    assert scan.entities[0].name == "Acme"


def test_a_page_the_run_did_not_open_fails():
    with pytest.raises(MarketScanError) as exc:
        build_scan(_entities(), as_of=AS_OF, opened_urls=[ACME], opt_ins={})
    assert "did not open" in str(exc.value)


def test_a_field_that_cannot_be_dated_fails():
    """A sourced field with no retrieval time cannot be re-checked."""
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform."},
        provenance=[_provenance("what_it_does", ACME, retrieved_at="")],
    )
    with pytest.raises(MarketScanError) as exc:
        build_scan([entity], as_of=AS_OF, opened_urls=OPENED, opt_ins={})
    assert "retrieval timestamp" in str(exc.value)


def test_a_field_cleared_for_another_use_cannot_be_carried_over():
    """The terms were cleared for one question; this scan is asking another."""
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform."},
        provenance=[_provenance("what_it_does", ACME)],
        use="due_diligence",
    )
    with pytest.raises(MarketScanError) as exc:
        build_scan([entity], as_of=AS_OF, use="competitive_brief", opened_urls=OPENED, opt_ins={})
    assert "due_diligence" in str(exc.value)
    assert "competitive_brief" in str(exc.value)


def test_a_provenance_row_that_names_no_source_fails():
    """Whose terms govern the field is not known, so the scan cannot proceed."""
    entity = MarketEntity(
        key="acme",
        name="Acme",
        fields={"what_it_does": "A platform."},
        provenance=[_provenance("what_it_does", ACME, source="")],
    )
    with pytest.raises(MarketScanError) as exc:
        build_scan([entity], as_of=AS_OF, opened_urls=OPENED, opt_ins={})
    assert "no source named" in str(exc.value)


def test_a_scan_with_no_entities_is_not_a_scan():
    with pytest.raises(MarketScanError) as exc:
        build_scan([], as_of=AS_OF, opened_urls=OPENED, opt_ins={})
    assert "no entities" in str(exc.value)


# ── the report ──────────────────────────────────────────────────────────────


def test_the_report_is_in_the_reader_layout_the_audit_reads():
    body = render_markdown(_scan())
    assert body.startswith("> ")
    assert "## What is in the market" in body
    assert "## Limitations" in body


def test_a_price_the_source_does_not_state_is_said_to_be_unstated():
    """The cell says the source did not state it - not that the scan failed to fetch."""
    body = render_markdown(_scan())
    assert "not stated by the source" in body
    assert "not fetched" not in body


def test_every_link_the_report_carries_is_one_the_run_opened():
    body = render_markdown(_scan())
    import re

    links = set(re.findall(r"https?://[^\s)\]]+", body))
    assert links
    assert links <= set(OPENED)


def test_the_table_carries_no_rank_column_and_no_total_column():
    """The audit calls a ranking that contradicts its totals a hard defect.

    A market scan has no ordering to defend and no score to add up, so the
    table must not have a column that looks like either.
    """
    body = render_markdown(_scan())
    tables = parse_tables(body.split("\n"))
    assert len(tables) == 1
    table = tables[0]
    header = [h.lower() for h in table.header]
    assert not any("rank" in h or h in ("#", "no") for h in header)
    assert not any("total" in h or "score" in h for h in header)


def test_no_cell_in_the_report_carries_an_estimate_marker():
    """An estimate marker inside a quantitative column is a hard finding."""
    body = render_markdown(_scan())
    for table in parse_tables(body.split("\n")):
        for row in table.rows:
            for cell in row:
                for name, pattern, _hard in ESTIMATE_MARKERS:
                    assert not pattern.search(cell), f"{name} in {cell!r}"


# ── the audit ───────────────────────────────────────────────────────────────


def test_the_emitted_report_passes_the_strict_audit_with_no_findings(tmp_path):
    """The gate holds: the repository's own audit finds nothing in the report."""
    ledger = _ledger(tmp_path)
    scan = _scan()
    paths = write_scan(scan, tmp_path / "out", ledger_path=ledger)

    report = paths["report"]
    assert report.name == "market_scan.md"
    assert paths["entities"].name == "entities.json"

    result = audit(report)
    hard = [f.as_dict() for f in result.findings if f.hard]
    soft = [f.as_dict() for f in result.findings if not f.hard]
    assert hard == [], hard
    assert soft == [], soft


def test_the_report_carries_the_run_trail_and_the_entities_file_the_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    paths = write_scan(_scan(), tmp_path / "out", ledger_path=ledger)

    body = paths["report"].read_text(encoding="utf-8")
    assert "**Research trail**" in body
    assert "3 searches" in body
    assert "3 pages read" in body

    data = json.loads(paths["entities"].read_text(encoding="utf-8"))
    assert data["as_of"] == AS_OF
    assert [e["name"] for e in data["entities"]] == ["Acme", "Globex", "Initech"]
    # Every field in the file carries a source and a moment it was read.
    for entity in data["entities"]:
        sourced = {p["field"] for p in entity["provenance"]}
        assert set(entity["fields"]) <= sourced
        assert all(p.get("retrieved_at") for p in entity["provenance"])


def test_a_report_written_without_a_ledger_says_so_rather_than_claiming_zero(tmp_path):
    """No ledger is not the same as a run that searched for nothing."""
    paths = write_scan(_scan(), tmp_path / "out")
    body = paths["report"].read_text(encoding="utf-8")
    assert "**Research trail**" not in body
    assert "0 searches" not in body
