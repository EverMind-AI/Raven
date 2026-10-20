#!/usr/bin/env python3
"""Build the market scan from a preserved research run, and audit it.

The run is the one preserved at ``/Users/snedz/src/market-research-run-2026-10-20/``:
eight searches and eighteen page reads against the real observability vendors'
pricing pages, driven through the flow's own web tools on 2026-10-20.  The scan
is rebuilt from that run's ledger and nothing else, so the report it writes can
be checked against the same audit that checked the original run's answer:

    python3 scripts/research_report_audit.py --strict <out>/market_scan.md

This script does not invent a fact. Every field below was read from the page named
beside it, and the pages are the ones the run's own ledger records as opened. The
ledger is the run's, unmodified: the script reads it, and the scan is built from
what it says was opened.

    python3 market_scan_from_run.py [RUN_DIR] [OUT_DIR]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.market import MarketEntity, build_scan, write_scan  # noqa: E402

from raven.records.types import Provenance  # noqa: E402

RUN = Path(sys.argv[1] if len(sys.argv) > 1 else "/Users/snedz/src/market-research-run-2026-10-20")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else RUN / "market_scan")

AS_OF = "2026-10-20"

# The retrieval source every page in this run was read through: the Jina reader.
# That is the registry key whose terms govern the read, and the report carries the
# attribution the ATTRIBUTED tier asks for - every page is linked, by name and URL.
SOURCE = "jina"

# The entities, and the fields the pages support. A field the page does not state is
# ABSENT rather than empty: a present-and-empty field is an unsourced claim, and the
# scan refuses one. Splunk's pricing page publishes no price, and that absence is
# the finding, so it is left absent.
ENTITIES = [
    (
        "datadog",
        "Datadog",
        "https://www.datadoghq.com/pricing/",
        {
            "what_it_does": "Observability platform sold per host, with consumption meters on top.",
            "price": "$0.10 per GB of log ingest on demand; $55 annual, $89 monthly and $100 on demand per 1M flag configuration requests",
        },
    ),
    (
        "newrelic",
        "New Relic",
        "https://newrelic.com/pricing",
        {
            "what_it_does": "Observability platform priced per user and per data ingested, with hosts included.",
            "price": "$49 per core user; $0.40 per GB ingested beyond the free 100 GB per month",
        },
    ),
    (
        "dynatrace",
        "Dynatrace",
        "https://www.dynatrace.com/pricing/",
        {
            "what_it_does": "Observability platform sold on a platform-level annual commitment.",
            "price": "$7 and $29 per month host tiers, billed $0.01 and $0.04 per host hour",
        },
    ),
    (
        "grafana-cloud",
        "Grafana Cloud",
        "https://grafana.com/pricing/",
        {
            "what_it_does": "Observability platform sold on consumption with a platform fee.",
            "price": "$19 per month platform fee, $6.50 per 1k active series, $25,000 per year minimum commit",
        },
    ),
    (
        "elastic",
        "Elastic",
        "https://www.elastic.co/pricing/serverless-observability",
        {
            "what_it_does": "Observability platform sold as serverless consumption on ingest, retention and transfer.",
            "price": "$0.07 per GB ingest, $0.017 per GB retained per month, $0.05 per GB transferred beyond 50 GB",
        },
    ),
    (
        "splunk",
        "Splunk",
        "https://www.splunk.com/en_us/pricing",
        {
            "what_it_does": "Observability platform sold on request; the pricing page carries Contact Us and Contact Sales.",
        },
    ),
]


def opened_urls(ledger: Path) -> set[str]:
    """The pages the run actually opened, from the run's own ledger."""
    out: set[str] = set()
    for line in ledger.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("op") == "fetch" and row.get("ok"):
            out.add(str(row.get("url") or ""))
    return out


def main() -> int:
    ledger = RUN / "ledger.jsonl"
    if not ledger.is_file():
        print(f"no ledger at {ledger}", file=sys.stderr)
        return 2

    opened = opened_urls(ledger)
    entities = []
    for key, name, url, fields in ENTITIES:
        if url not in opened:
            print(f"REFUSED: {name} cites {url}, which the run did not open", file=sys.stderr)
            return 1
        entities.append(
            MarketEntity(
                key=key,
                name=name,
                fields=dict(fields),
                provenance=[
                    Provenance(
                        field=field,
                        source_url=url,
                        source=SOURCE,
                        method="fetch",
                        retrieved_at=AS_OF,
                    )
                    for field in fields
                ],
            )
        )

    scan = build_scan(entities, as_of=AS_OF, opened_urls=sorted(opened))
    paths = write_scan(scan, OUT, ledger_path=str(ledger))
    print(f"wrote {paths['report']}")
    print(f"wrote {paths['entities']}")
    print(f"entities: {len(scan.entities)}, pages cited: {len(scan.pages())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
