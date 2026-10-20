"""The market scan, as a rung of the research flow.

A deep-research turn that answers "who is in this market and what do they
charge" has already done the hard part: it read the vendors' pages and
wrote the comparison. What it has not done is **check** it - that every
price came from a page the run opened, that the pages' terms permit the
use, and that the facts the table asserts are the facts the pages say.

This gate is the check, and it is what makes the market scan a rung of the
ladder rather than a shape a report happens to have:

1. The turn's answer is read as a **market table** - companies, facts, and
   the pages behind them (``research_flow.market.table``).
2. The scan is built from it. The refusals apply: a fact with no page
   behind it, a page the run never opened, a source whose terms do not
   permit the use, a fact that cannot be dated.
3. The **three gates** run over the result - mechanical, sufficiency,
   judge - each switchable on its own.
4. If they pass, the scan **replaces** the answer: the reader gets the
   table with its trace, not the prose that surrounded it.

**Fail-open, and say so.** A scan that cannot be built, or whose gates
fail, leaves the turn's own answer standing. The turn is a research turn
and it still has one. What the gate does *not* do is let a failed scan
ship as a scan: the note on the decision and the ledger row say which
gate said no, and the answer the reader gets is the model's own.

**The criteria are the operator's configuration**, read at the moment the
gate is built - which is before the turn retrieves anything. That is what
makes the sufficiency gate's "declared before retrieval" check mean
something: the criteria were written before the pages were read, so they
are a requirement on the search rather than a description of its result.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

from raven.contracts.loop_hooks import HookDecision
from research_flow.gates.base import Gate, GateCtx
from research_flow.market.gates import MarketCriteria, MarketGateConfig, run_market_gates
from research_flow.market.scan import MarketScanError, build_scan, render_markdown
from research_flow.market.table import entities_from_table, find_market_table, opened_pages
from research_flow.support.answer_text import visible_answer
from research_flow.support.ledger import ledger_append
from research_flow.support.process_appendix import build_appendix, read_ledger

logger = logging.getLogger(__name__)


def _extractor_keys() -> dict[str, str]:
    """Extractor name to sources-registry key, from the flow's own providers.

    The ledger names the backend that served a page (``jina-reader``); the
    registry is keyed by the vendor whose terms that backend carries
    (``jina``). The flow's provider table is the only place both are
    written down, so it is where the mapping is read from - and a backend
    the table does not know keeps its own name, which the scan refuses,
    because a reader whose terms have not been looked at cannot back a
    market scan.
    """
    try:
        from research_flow.tools.web import FETCH_PROVIDERS
    except Exception:  # noqa: BLE001 - a missing provider table is not a turn's failure
        return {}
    return {spec.extractor: spec.vendor for spec in FETCH_PROVIDERS.values()}


class MarketScanGate(Gate):
    """Read the turn's market table, gate it, and ship it only if it stands."""

    def __init__(
        self,
        provider: Any = None,
        *,
        criteria: MarketCriteria,
        gate_config: MarketGateConfig | None = None,
        declared_at: float | None = None,
        use: str | None = None,
        ledger_path: Callable[[], str | None] | None = None,
        extractor_keys: Mapping[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._criteria = criteria
        self._gate_config = gate_config or MarketGateConfig()
        self._declared_at = declared_at if declared_at is not None else time.time()
        self._use = use or criteria.require_use
        self._ledger_path = ledger_path
        self._extractor_keys = dict(extractor_keys) if extractor_keys is not None else None

    @property
    def name(self) -> str:
        return "MarketScanGate"

    def _path(self) -> str | None:
        if self._ledger_path is not None:
            return self._ledger_path()
        from research_flow.support.ledger import ledger_path as default_path

        return default_path()

    def _turn_rows(self, path: str | None) -> list[dict[str, Any]]:
        """The turn's ledger rows.

        **The stashed rows, not the ledger file.** The flow writes the
        ledger file when the turn closes, so a gate that runs mid-turn and
        reads the file reads a file that has not been written yet -- and
        concludes that the run opened nothing, which is false. The stashed
        rows are the same rows, live.

        The file is still read as a fallback, because a caller that hands
        this gate a ledger path means it.
        """
        from research_flow.gates.conversation import turn_rows

        rows = turn_rows()
        if rows:
            return rows
        return read_ledger(path) if path else []

    async def after_iteration(self, ctx: GateCtx) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        content = getattr(ctx.response, "content", None) or ""
        answer = visible_answer(content)
        if not answer:
            return HookDecision()

        table = find_market_table(answer)
        if table is None:
            return HookDecision(notes=["market_scan_no_table"])

        path = self._path()
        rows = self._turn_rows(path)
        pages = opened_pages(rows, extractor_keys=self._extractor_keys or _extractor_keys())
        if not pages:
            return HookDecision(notes=["market_scan_no_ledger"])

        try:
            entities = entities_from_table(table, pages, use=self._use)
            scan = build_scan(
                entities,
                as_of=self._as_of(pages),
                use=self._use,
                opened_urls=list(pages),
            )
        except MarketScanError as exc:
            self._record(ctx, "refused", str(exc))
            logger.warning("market-scan: refused (%s)", exc)
            return HookDecision(notes=[f"market_scan_refused: {exc}"])

        report = render_markdown(scan)
        results = await run_market_gates(
            scan,
            self._criteria,
            config=self._gate_config,
            provider=self._provider,
            report_text=report,
            retrieved_at=self._first_fetch(rows),
        )

        failed = [r for r in results if r.enabled and not r.passed]
        if failed:
            detail = "; ".join(f"{r.name}: {', '.join(r.findings)}" for r in failed)
            self._record(ctx, "gated_out", detail)
            logger.warning("market-scan: gates refused the scan (%s)", detail)
            return HookDecision(notes=[f"market_scan_gated: {detail}"])

        if path:
            appendix, _counters = build_appendix(path, report)
            report = report.rstrip("\n") + "\n" + appendix.rstrip("\n") + "\n"

        self._record(ctx, "shipped", f"{len(scan.entities)} entities, {len(scan.pages())} pages")
        return HookDecision(
            short_circuit_result=report,
            notes=[f"market_scan_shipped: {len(scan.entities)} entities"],
        )

    @staticmethod
    def _as_of(pages: Mapping[str, Any]) -> str:
        stamps = [p.retrieved_at for p in pages.values()]
        return time.strftime("%Y-%m-%d", time.localtime(min(stamps))) if stamps else time.strftime("%Y-%m-%d")

    @staticmethod
    def _first_fetch(rows: list[dict[str, Any]]) -> float | None:
        stamps = [
            float(r.get("ts") or 0)
            for r in rows
            if r.get("op") == "fetch" and r.get("ok") and r.get("ts")
        ]
        return min(stamps) if stamps else None

    @staticmethod
    def _record(ctx: GateCtx, event: str, detail: str) -> None:
        """What the turn did, on the turn's own record.

        Write-only, like every other ledger line: a note nobody reads back
        is still the difference between "this arm shipped no market scan"
        and "this arm's market scan was refused and by which gate".
        """
        state = ctx.metadata.setdefault("market_scan", {})
        state["events"] = state.get("events", 0) + 1
        state["last_event"] = event
        state["last_detail"] = detail
        ledger_append({"ts": time.time(), "op": "market_scan", "event": event, "detail": detail})


__all__ = ["MarketScanGate"]
