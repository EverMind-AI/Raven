"""The three gates of a market scan, each ablatable on its own.

A scan that produced a report and nothing else is a scan whose quality rests
on the reader. Three gates replace that reader, and the order is the point:

1. **Mechanical** - pass/fail only, no model. Every field sourced, every
   source permitted for the use, every page opened, every field dated, and
   the report free of the four mechanical defects. This gate cannot be
   argued with and it never costs a model call.
2. **Sufficiency** - the **mandatory criteria, declared before retrieval**.
   The criteria are written down *before* the run opens a page, and the gate
   checks the retrieved set against them. Declaring them after the fact
   would make them a description of what was found rather than a
   requirement, so the gate checks the declaration's timestamp against the
   run's first fetch and refuses a retrofit.
3. **Judge** - last, and a model. It asks whether the report meets the
   criteria and whether its claims are traced, and it is measured two ways:
   a **position-swap ablation** (the same scan judged in two orders, because
   a judge whose verdict depends on the order the companies happen to be
   listed in is not judging the scan) and a **cheap-judge default** (the
   judge runs on a small model, because it reads a report and answers one
   question, and the expensive model is for the work).

**Ablation is per gate.** Each has its own switch, and turning one off
changes nothing about the others. That is what makes them separately
measurable: a judge gate can be compared against a run without it and the
difference attributed to the judge, because the mechanical and sufficiency
gates ran identically in both arms.

**The judge fails open, the other two do not.** A judge that cannot be
reached has no verdict, and a scan with no verdict is a scan the reader
still gets - the house rule everywhere a model judges. The mechanical and
sufficiency gates are checks on facts, and a check that could not be
performed is not a pass.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from raven.records.types import Provenance
from raven.sources.optins import has_opt_in
from raven.sources.policy import SourceRefusedError, consult
from research_flow.support._verdict import parse_bool_verdict
from research_flow.support.answer_text import visible_answer

from .defects import report_defects
from .ledger import DEFAULT_USE
from .scan import MarketEntity, MarketScan, publishes_price, render_markdown

logger = logging.getLogger(__name__)

#: The cheap judge. The same default the repo's own eval judge uses
#: (``raven/eval_engine/judge/judge.py``): the task is one report and one
#: question, and the expensive model belongs to the work, not to the check.
CHEAP_JUDGE_MODEL = "claude-haiku-4-5"

_JUDGE_SYSTEM = (
    "You judge one market scan. You are given the criteria the scan was declared "
    "to meet, and the report it produced. Decide whether the report meets those "
    "criteria and whether every claim in it is traced to a page the run read. "
    "Answer with a single JSON object and nothing else: "
    '{"meets_criteria": true or false, "reason": "one sentence"}. '
    "Judge only what the report and the criteria show."
)


@dataclass(frozen=True)
class MarketCriteria:
    """The criteria a scan was declared to meet, and when they were declared.

    ``declared_at`` is epoch seconds and it is not decoration: the gate
    compares it against the run's first fetch, because a criterion written
    after the pages were read is a description of the answer rather than a
    requirement on the search. A scan with no criteria declared is a scan
    with no sufficiency gate, and the gate says so rather than passing a
    check that was never asked for.
    """

    declared_at: float
    min_entities: int = 0
    required_fields: tuple[str, ...] = ()
    require_price_for_all: bool = False
    min_source_hosts: int = 0
    require_use: str = DEFAULT_USE

    def describe(self) -> str:
        """The criteria as a reader - and a judge - can be shown."""
        lines = [f"- at least {self.min_entities} compan{'y' if self.min_entities == 1 else 'ies'}"]
        if self.required_fields:
            lines.append("- every company carries: " + ", ".join(self.required_fields))
        if self.require_price_for_all:
            lines.append("- every company publishes a price on the page read")
        if self.min_source_hosts:
            lines.append(f"- at least {self.min_source_hosts} distinct source hosts")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_at": self.declared_at,
            "min_entities": self.min_entities,
            "required_fields": list(self.required_fields),
            "require_price_for_all": self.require_price_for_all,
            "min_source_hosts": self.min_source_hosts,
            "require_use": self.require_use,
        }


@dataclass
class GateResult:
    """One gate's verdict, and what it measured.

    ``findings`` are fatal: the gate failed because of them. ``notes`` are
    not - they are the things a reader should know and the gate can carry.
    The split is the audit's own: a hard defect fails ``--strict`` and a
    soft one is reported, and a gate that failed on both would be stricter
    than the report format the house has already settled.
    """

    name: str
    passed: bool
    findings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "enabled": self.enabled,
            "findings": list(self.findings),
            "notes": list(self.notes),
            "measurements": dict(self.measurements),
        }

    @classmethod
    def skipped(cls, name: str) -> "GateResult":
        """A gate that was switched off.

        It passes, because it did not run - and it says it did not run, so
        a reader of the results cannot mistake an ablated arm for an arm
        whose gate was satisfied.
        """
        return cls(name=name, passed=True, enabled=False, measurements={"skipped": True})


@dataclass
class MarketGateConfig:
    """The three switches, and the judge's two settings.

    Each gate has its own switch so the arms are separately attributable,
    and the defaults are all on: a capability whose gates are off by
    default is a capability with no gates.
    """

    mechanical: bool = True
    sufficiency: bool = True
    judge: bool = True
    judge_model: str = CHEAP_JUDGE_MODEL
    position_swap: bool = True
    judge_timeout_seconds: float = 30.0
    judge_max_tokens: int = 600


# ── the mechanical gate ─────────────────────────────────────────────────────


def _source_of(item: Provenance) -> str:
    return item.source


def _check_entity(entity: MarketEntity, use: str, opt_ins: Mapping[str, bool] | None) -> list[str]:
    """The scan-side defects of one entity: unsourced, undated, unnamed, refused."""
    findings: list[str] = []
    missing = entity.unsourced_fields()
    if missing:
        findings.append(
            f"{entity.name} has field(s) with no source: {', '.join(missing)}"
        )
    for item in entity.provenance:
        if not item.retrieved_at:
            findings.append(
                f"{entity.name} reads {item.field!r} from {item.source_url} with no retrieval timestamp"
            )
        source = _source_of(item)
        if not source:
            findings.append(
                f"{entity.name} reads {item.field!r} from {item.source_url} with no source named"
            )
            continue
        granted = bool(opt_ins.get(source, False)) if opt_ins is not None else has_opt_in(source, use)
        try:
            consult(source, use=use, has_opt_in=granted)
        except SourceRefusedError as exc:
            findings.append(f"{entity.name} cites {item.source_url} and {source} may not be used for {use}: {exc.reason}")
    return findings


def mechanical_gate(
    scan: MarketScan,
    *,
    report_text: str = "",
    opt_ins: Mapping[str, bool] | None = None,
) -> GateResult:
    """The pass/fail gate: no model, no judgement, no argument.

    It answers two questions mechanically. Does the scan say only what its
    sources support - every field sourced, every source permitted for this
    use, every field dated, every page opened? And does the report it wrote
    carry one of the four mechanical defects?
    """
    findings: list[str] = []
    for entity in scan.entities:
        findings.extend(_check_entity(entity, scan.use, opt_ins))

    defects = report_defects(report_text) if report_text else []
    soft: list[str] = []
    for defect in defects:
        line = f"{defect['kind']}: {defect['detail']}"
        if defect.get("hard", True):
            findings.append(line)
        else:
            soft.append(line)

    return GateResult(
        name="mechanical",
        passed=not findings,
        findings=findings,
        notes=soft,
        measurements={
            "entities": len(scan.entities),
            "pages": len(scan.pages()),
            "report_defects": len(defects),
            "soft_defects": len(soft),
            "checked_report": bool(report_text),
        },
    )


# ── the sufficiency gate ────────────────────────────────────────────────────


def sufficiency_gate(
    scan: MarketScan,
    criteria: MarketCriteria | None,
    *,
    retrieved_at: float | None = None,
) -> GateResult:
    """The gate that asks whether the scan is enough, against declared criteria.

    Two failures are possible and they are different in kind.

    The first is a **retrofit**: the criteria were declared after the run
    opened its first page, so they were written with the answer in view and
    are a description rather than a requirement. The gate refuses, because
    a sufficiency gate that accepts criteria written afterwards measures
    nothing.

    The second is a **shortfall**: the criteria are honest and the scan
    does not meet them. The gate fails, and the missing criteria are named
    so the run knows what to go and get.
    """
    findings: list[str] = []
    measurements: dict[str, Any] = {}

    if criteria is None:
        return GateResult(
            name="sufficiency",
            passed=False,
            findings=["no criteria were declared, so sufficiency was never checked against anything"],
            measurements={"declared": False},
        )

    measurements["declared"] = True
    measurements["criteria"] = criteria.to_dict()

    if retrieved_at is None:
        findings.append(
            "the run recorded no retrieval, so the criteria declared before it could not be checked"
        )
        measurements["retrofit"] = None
    elif criteria.declared_at > retrieved_at:
        findings.append(
            f"the criteria were declared at {criteria.declared_at:.3f}, after the run opened "
            f"its first page at {retrieved_at:.3f}, so they are a description of the result "
            "rather than a requirement on the search"
        )
        measurements["retrofit"] = True
    else:
        measurements["retrofit"] = False
        measurements["declared_before_retrieval"] = True

    met: list[str] = []
    missed: list[str] = []

    if len(scan.entities) < criteria.min_entities:
        missed.append(f"the scan holds {len(scan.entities)} compan{'y' if len(scan.entities) == 1 else 'ies'}, fewer than the {criteria.min_entities} declared")
    else:
        met.append(f"the scan holds at least {criteria.min_entities} compan{'y' if criteria.min_entities == 1 else 'ies'}")

    for field_name in criteria.required_fields:
        absent = sorted(e.name for e in scan.entities if field_name not in e.fields)
        if absent:
            missed.append(f"{', '.join(absent)} do not carry {field_name!r}")
        else:
            met.append(f"every company carries {field_name!r}")

    if criteria.require_price_for_all:
        # A price is a number, so a vendor whose page says "no price published"
        # is a vendor that publishes no price. Counting the sentence as the
        # price would let the criterion be satisfied by the absence it was
        # written to catch.
        absent = sorted(e.name for e in scan.entities if not publishes_price(e))
        if absent:
            missed.append(f"{', '.join(absent)} publish no price on the page read")
        else:
            met.append("every company publishes a price on the page read")

    if criteria.min_source_hosts:
        hosts = {url.split("//", 1)[-1].split("/", 1)[0] for url in scan.pages()}
        if len(hosts) < criteria.min_source_hosts:
            missed.append(f"the scan reads {len(hosts)} source host(s), fewer than the {criteria.min_source_hosts} declared")
        else:
            met.append(f"the scan reads at least {criteria.min_source_hosts} source host(s)")

    measurements["met"] = met
    measurements["missed"] = missed
    findings.extend(missed)

    return GateResult(
        name="sufficiency",
        passed=not findings,
        findings=findings,
        measurements=measurements,
    )


# ── the judge gate ──────────────────────────────────────────────────────────


async def _judge_once(
    provider: Any,
    report: str,
    criteria: MarketCriteria | None,
    *,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
) -> tuple[bool | None, str | None]:
    """One judge call. ``(verdict, reason)``; ``(None, None)`` when it could not be had."""
    user = (
        (f"Criteria:\n{criteria.describe()}\n\n" if criteria else "")
        + f"Report:\n{report}"
    )
    try:
        response = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": user},
                ],
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - a judge that cannot be reached has no verdict
        logger.warning("market judge: call failed (%s: %s); fail-open", type(exc).__name__, exc)
        return None, None

    text = visible_answer(getattr(response, "content", None) or "")
    parsed = parse_bool_verdict(text, "meets_criteria")
    if parsed is None:
        logger.warning("market judge: output missing a boolean 'meets_criteria'; fail-open")
        return None, None
    data, verdict = parsed
    reason = data.get("reason")
    return bool(verdict), reason if isinstance(reason, str) else None


async def judge_gate(
    scan: MarketScan,
    provider: Any,
    criteria: MarketCriteria | None = None,
    *,
    model: str = CHEAP_JUDGE_MODEL,
    position_swap: bool = True,
    timeout_seconds: float = 30.0,
    max_tokens: int = 600,
) -> GateResult:
    """The last gate: a model reads the report and says whether it stands up.

    **The position-swap ablation.** The same scan is judged twice - once as
    it is, once with its companies in the opposite order - and the two
    verdicts are compared. A judge that flips when the order flips is
    reacting to the presentation rather than to the scan, and the gate says
    so and fails: a gate that passed on a verdict the judge would not
    repeat has certified nothing.

    **The cheap-judge default.** The judge runs on a small model by
    default, because it reads a report and answers one question. The
    expensive model is for the work, and a gate that spends it re-reads what
    the mechanical gate has already settled.
    """
    report = render_markdown(scan)
    verdict, reason = await _judge_once(
        provider,
        report,
        criteria,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
    )

    measurements: dict[str, Any] = {"model": model, "verdict": verdict, "reason": reason}
    findings: list[str] = []

    if verdict is None:
        # Fail-open, and it says which way it fell: a scan with no verdict
        # is a scan the reader still gets.
        measurements["judged"] = False
        return GateResult(name="judge", passed=True, findings=[], measurements=measurements)

    measurements["judged"] = True

    if position_swap:
        swapped = scan.with_entities(list(reversed(scan.entities)))
        other, other_reason = await _judge_once(
            provider,
            render_markdown(swapped),
            criteria,
            model=model,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        measurements["swapped_verdict"] = other
        measurements["swapped_reason"] = other_reason
        if other is None:
            measurements["order_stable"] = None
        else:
            stable = other == verdict
            measurements["order_stable"] = stable
            if not stable:
                findings.append(
                    "the judge's verdict depends on the order the companies are presented in: "
                    f"{verdict} in one order and {other} in the other"
                )

    if not verdict:
        findings.append(f"the judge did not accept the report: {reason or 'no reason given'}")

    return GateResult(
        name="judge",
        passed=not findings,
        findings=findings,
        measurements=measurements,
    )


# ── the runner ──────────────────────────────────────────────────────────────


async def run_market_gates(
    scan: MarketScan,
    criteria: MarketCriteria | None = None,
    *,
    config: MarketGateConfig | None = None,
    provider: Any = None,
    report_text: str = "",
    retrieved_at: float | None = None,
    opt_ins: Mapping[str, bool] | None = None,
) -> list[GateResult]:
    """The three gates, in order, each ablatable on its own.

    The order is fixed - mechanical, then sufficiency, then judge - because
    the judge reads a report the other two have already settled, and a judge
    asked about a report with an unsourced field in it is a model call spent
    on something a parser could have said.

    A gate that is switched off is skipped and recorded as skipped. The
    judge needs a provider; without one it is skipped too, and says so,
    rather than failing for a reason that has nothing to do with the scan.
    """
    config = config or MarketGateConfig()
    results: list[GateResult] = []

    if config.mechanical:
        results.append(mechanical_gate(scan, report_text=report_text, opt_ins=opt_ins))
    else:
        results.append(GateResult.skipped("mechanical"))

    if config.sufficiency:
        results.append(sufficiency_gate(scan, criteria, retrieved_at=retrieved_at))
    else:
        results.append(GateResult.skipped("sufficiency"))

    if not config.judge:
        results.append(GateResult.skipped("judge"))
    elif provider is None:
        results.append(GateResult.skipped("judge"))
        results[-1].findings.append("no provider was supplied, so the judge did not run")
    else:
        results.append(
            await judge_gate(
                scan,
                provider,
                criteria,
                model=config.judge_model,
                position_swap=config.position_swap,
                timeout_seconds=config.judge_timeout_seconds,
                max_tokens=config.judge_max_tokens,
            )
        )

    return results


__all__ = [
    "CHEAP_JUDGE_MODEL",
    "GateResult",
    "MarketCriteria",
    "MarketGateConfig",
    "judge_gate",
    "mechanical_gate",
    "run_market_gates",
    "sufficiency_gate",
]
