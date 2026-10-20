"""The market scan: a ledger of entities, and the report a reader sees.

The capability the plan's W4 asks for: a market scan that produces
``market_scan.md`` and ``entities.json``, and that cannot state anything
the sources do not support.

Three gates sit over it - mechanical, sufficiency and judge - and each is
switchable on its own so an arm can be attributed to the gate that was
changed and not to the ones that were not.
"""

from .defects import report_defects, table_defects
from .gates import (
    CHEAP_JUDGE_MODEL,
    GateResult,
    MarketCriteria,
    MarketGateConfig,
    judge_gate,
    mechanical_gate,
    run_market_gates,
    sufficiency_gate,
)
from .ledger import DEFAULT_USE, MarketEntity, entities_payload, read_entities, write_entities
from .metrics import StopDecision, StopMetrics, compare_stop_metrics, stop_metrics
from .scan import MarketScan, MarketScanError, build_scan, render_markdown, write_scan
from .table import (
    MarketTable,
    OpenedPage,
    entities_from_table,
    find_market_table,
    normalise_field,
    opened_pages,
)

__all__ = [
    "CHEAP_JUDGE_MODEL",
    "DEFAULT_USE",
    "GateResult",
    "MarketCriteria",
    "MarketEntity",
    "MarketGateConfig",
    "MarketScan",
    "MarketScanError",
    "MarketTable",
    "OpenedPage",
    "StopDecision",
    "StopMetrics",
    "build_scan",
    "compare_stop_metrics",
    "entities_from_table",
    "entities_payload",
    "find_market_table",
    "judge_gate",
    "mechanical_gate",
    "normalise_field",
    "opened_pages",
    "read_entities",
    "render_markdown",
    "report_defects",
    "run_market_gates",
    "stop_metrics",
    "sufficiency_gate",
    "table_defects",
    "write_entities",
    "write_scan",
]
