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
4. If they pass, the **table** is replaced by the verified one: the reader
   gets the report they were given, with a comparison table whose every
   fact carries the page it came from.

**The report is kept; the table is checked.** Replacing the whole report
with the scan's own rendering would ship a different document - a shorter
one, with different headings - and the flow's file sync would then leave
the report file the turn drafted untouched. So the gate swaps the table
inside the report and leaves the argument, the sections and the other
tables where they are.

**The table is found by its shape, and the sources are asked for.** The
flow's report contract tells the model to keep a candidate's provenance
out of the comparison table and under it as prose, so a report written to
that contract has a vendor table whose cells carry no citation. A gate
that cannot see the table cannot ask for the sources it is missing, so
the table is found by shape first, and a table whose facts cannot be
traced is **asked about once**: the turn is bounced with the request to
add a ``Read from`` column. A turn that was asked and did not answer
keeps its own answer, and the record says it was asked.

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
from research_flow.market.scan import MarketScanError, build_scan
from research_flow.market.table import (
    carries_provenance,
    entities_from_table,
    find_vendor_table,
    opened_pages,
    swap_table_rows,
)
from research_flow.support.answer_text import visible_answer
from research_flow.support.ledger import ledger_append
from research_flow.support.process_appendix import read_ledger

logger = logging.getLogger(__name__)

_SOURCE_COLUMN_PROMPT = """The comparison table in this report does not say where its
facts came from, so none of it can be checked against what was actually read.

Add a `Read from` column to that table: one URL per row, written out in full
and starting with `https://`, naming the page you opened that the row's facts
come from. A row whose facts came from two pages names both.

Keep everything else in the report as it is - the sections, the argument, the
other tables, and the wording of the comparison table's own columns. The table
keeps the columns that decide it; the sources go in the new column beside
them."""


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
            # Say so. A gate that declines in silence is a gate whose absence
            # is invisible, and an invisible gate is indistinguishable from a
            # gate that was never wired - which is exactly what two batches of
            # measurements cost.
            return HookDecision(notes=["market_scan_skipped_tool_calls"])
        content = getattr(ctx.response, "content", None) or ""
        answer = visible_answer(content)
        if not answer:
            return HookDecision(notes=["market_scan_skipped_empty_answer"])

        # Found by its shape, not by its citations. The flow's report contract
        # keeps a candidate's provenance out of the comparison table and under
        # it as prose, so a report written to that contract has a vendor table
        # whose cells carry no link - and a gate that cannot see the table
        # cannot ask the turn for the sources it is missing.
        table = find_vendor_table(answer)
        if table is None:
            return self._decline(ctx, "no_table", "the answer carries no table that compares companies")
        if not carries_provenance(table):
            return self._ask_for_sources(ctx, answer)

        path = self._path()
        rows = self._turn_rows(path)
        pages = opened_pages(rows, extractor_keys=self._extractor_keys or _extractor_keys())
        if not pages:
            return self._decline(ctx, "no_ledger", "the turn opened no page the scan could read")

        try:
            entities = entities_from_table(table, pages, use=self._use)
            scan = build_scan(
                entities,
                as_of=self._as_of(pages),
                use=self._use,
                opened_urls=list(pages),
            )
        except MarketScanError as exc:
            return self._decline(ctx, "refused", str(exc))

        # The report is kept and the table inside it is checked: the reader
        # gets the argument, the sections and the other tables, and a
        # comparison table whose every fact carries the page it came from.
        # Replacing the whole report with the scan's own rendering would ship
        # a different document - a shorter one, with different headings - and
        # the flow's file sync would then leave the report file the turn
        # drafted untouched, so the check would never reach the reader.
        report = swap_table_rows(answer, table, entities)
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
            return self._decline(ctx, "gated", detail)

        self._record(ctx, "shipped", f"{len(scan.entities)} entities, {len(scan.pages())} pages")
        logger.warning(
            "market-scan: shipped %d entities from %d pages", len(scan.entities), len(scan.pages())
        )
        return HookDecision(
            short_circuit_result=report,
            notes=[f"market_scan_shipped: {len(scan.entities)} entities"],
        )

    def _ask_for_sources(self, ctx: GateCtx, answer: str) -> HookDecision:
        """Ask the turn once for the sources its comparison table does not carry.

        The table is found and it names companies, but none of its facts can
        be traced to a page: the run opened nine pages and the table says
        nothing about which of them backs which row. That is the one gap the
        gate can close, and it closes it by asking - the same way the report
        shape gate asks for a report in three sections.

        **Once.** A turn that was asked and did not answer keeps its own
        answer, and the record says it was asked: "asked and still had no
        source" and "never asked" are different states, and only the first is
        evidence that the prompt-side fix has run out of road.
        """
        state = ctx.metadata.setdefault("market_scan", {})
        if state.get("asked_for_sources"):
            return self._decline(
                ctx, "no_sources", "the comparison table still carries no source for its facts"
            )
        state["asked_for_sources"] = True
        self._record(
            ctx, "bounced", "the comparison table carries no source for its facts; asked for one"
        )
        logger.warning(
            "market-scan: the comparison table carries no source; asking for a Read from column"
        )
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": answer, "_recovery_synthetic": True},
                {
                    "role": "user",
                    "content": _SOURCE_COLUMN_PROMPT,
                    "_recovery_synthetic": True,
                },
            ],
            notes=["market_scan_bounced: asked for a Read from column"],
        )

    def _decline(self, ctx: GateCtx, event: str, detail: str) -> HookDecision:
        """Leave the turn's own answer standing, and say why in the record.

        A decline that is only a note leaves no trace in the turn's own
        record, so "the gate refused" and "the gate never ran" look the
        same from the outside - which is what two batches of measurement
        paid for.
        """
        self._record(ctx, event, detail)
        logger.warning("market-scan: declined (%s: %s)", event, detail)
        return HookDecision(notes=[f"market_scan_{event}: {detail}"])

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
