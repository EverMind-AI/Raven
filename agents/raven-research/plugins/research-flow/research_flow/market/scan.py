"""The market scan: the ledger, and the report a reader sees.

Three refusals, and they are the capability. A market scan that states
something the sources do not support is worse than no scan, because the
next reader cannot tell which parts to trust.

1. **A field with no source fails.** The ledger keeps the fields and the
   sources apart, and a field with nothing behind it is a claim the
   entity cannot defend.
2. **A source whose terms do not permit the use fails.** The source's
   policy is consulted for the use the scan is making, and a restricted
   source without a recorded opt-in is refused with the reason and the
   remedy, not quietly dropped.
3. **A page the run did not open fails.** The scan may only state what
   was read, so a field citing a page that is not in the run's opened
   set is refused rather than emitted.

The report it writes carries the flow's own **process appendix**, so the
audit can check that every citation was surfaced by a search and opened
by a fetch. That is the property the whole capability exists for: *the
report says only what the sources support* has to be mechanical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from raven.records.types import Provenance
from raven.sources.optins import has_opt_in
from raven.sources.policy import SourceRefusedError, consult

from .ledger import DEFAULT_USE, MarketEntity, entities_payload


class MarketScanError(RuntimeError):
    """A market scan was refused, and the refusal says why."""


@dataclass
class MarketScan:
    """A market scan: the entities, and the report that goes with them."""

    as_of: str
    entities: list[MarketEntity] = field(default_factory=list)
    use: str = DEFAULT_USE
    answer: str = ""
    limitations: list[str] = field(default_factory=list)

    def pages(self) -> list[str]:
        """The distinct pages the scan was read from."""
        seen: list[str] = []
        for entity in self.entities:
            for url in entity.source_urls():
                if url not in seen:
                    seen.append(url)
        return seen

    def to_entities_json(self) -> dict[str, Any]:
        return entities_payload(self.entities, as_of=self.as_of, use=self.use)

    def with_entities(self, entities: Sequence[MarketEntity]) -> "MarketScan":
        """The same scan with a different order of companies.

        The answer is recomputed, because the answer names the companies and
        its order is part of what the position-swap ablation varies.
        """
        other = MarketScan(as_of=self.as_of, entities=list(entities), use=self.use)
        other.answer = _answer_for(other)
        other.limitations = list(self.limitations)
        return other


def _refuse_unsourced(entity: MarketEntity) -> None:
    missing = entity.unsourced_fields()
    if missing:
        raise MarketScanError(
            f"{entity.name} has field(s) with no source: {', '.join(missing)}. "
            "A field with no source is a claim the scan cannot defend, so the scan is refused."
        )


def _refuse_unopened(entity: MarketEntity, opened: set[str]) -> None:
    for url in entity.source_urls():
        if url not in opened:
            raise MarketScanError(
                f"{entity.name} cites {url}, which the run did not open. "
                "A scan may only state what was read."
            )


def _refuse_undated(entity: MarketEntity) -> None:
    """A source with no retrieval time cannot be dated.

    The field is sourced, so the field looks settled - but "this page said it"
    and "this page said it on the day you read it" are different claims, and
    only the second one can be re-checked. A price read in March and a price
    read last week are both true sentences; a price with no date on it cannot
    be weighed against either.
    """
    for item in entity.provenance:
        if not item.retrieved_at:
            raise MarketScanError(
                f"{entity.name} reads {item.field!r} from {item.source_url} with no "
                "retrieval timestamp. A sourced field that cannot be dated cannot be "
                "re-checked, so the scan is refused."
            )


def _refuse_mismatched_use(entity: MarketEntity, use: str) -> None:
    """A field cleared for one use cannot be carried into another.

    The source's terms are consulted for the use the scan is making. An entity
    collected under a different use was cleared under a different question, and
    reusing it here would be a permission that was never asked for.
    """
    if entity.use != use:
        raise MarketScanError(
            f"{entity.name} was collected for {entity.use!r}, and this scan is for "
            f"{use!r}. The source's terms were cleared for the first and not for the "
            "second, so the scan is refused."
        )


def _source_key(item: Provenance) -> str:
    """The key the sources policy is asked about.

    The provenance row names its own source, and that name is the registry
    key - the same key the records store consults when it asks whether a field
    may be kept, and the same one the web tool consults before it calls a
    vendor. A host is not a policy key: one vendor serves many hosts, and a
    host says nothing about the terms, so there is no fallback to one. A row
    that names no source has not said whose terms govern it, and the refusal
    below says exactly that.
    """
    return item.source


def _refuse_permitted_use(entity: MarketEntity, use: str, opt_ins: Mapping[str, bool] | None) -> None:
    for item in entity.provenance:
        source = _source_key(item)
        if not source:
            raise MarketScanError(
                f"{entity.name} reads {item.field!r} from {item.source_url} with no "
                "source named, so it is not known whose terms govern that field. "
                "Name the source in the provenance before using it."
            )
        granted = bool(opt_ins.get(source, False)) if opt_ins is not None else has_opt_in(source, use)
        try:
            consult(source, use=use, has_opt_in=granted)
        except SourceRefusedError as exc:
            raise MarketScanError(
                f"{entity.name} cites {item.source_url}, and {source} may not be used for {use}: {exc.reason}. {exc.remedy}"
            ) from exc


def build_scan(
    entities: Sequence[MarketEntity],
    *,
    as_of: str,
    use: str = DEFAULT_USE,
    opt_ins: Mapping[str, bool] | None = None,
    opened_urls: Iterable[str] | None = None,
    limitations: Sequence[str] | None = None,
) -> MarketScan:
    """Build the scan, or refuse it.

    ``opened_urls`` is what the run actually read. Passing it turns the
    third refusal on; leaving it off builds a scan from the ledger alone,
    which is what a reader of a stored scan gets.

    ``opt_ins`` overrides the operator's opt-in ledger for the sources named
    here, and is what a test uses to say "this deployment has recorded why".
    Left as ``None``, the ledger on disk is consulted - the same ledger the
    records store and the web tool read, so a grant recorded for a source is
    the same grant everywhere rather than a per-caller copy.
    """
    if not entities:
        raise MarketScanError("a scan with no entities is not a scan")
    opened = set(opened_urls) if opened_urls is not None else None

    for entity in entities:
        _refuse_unsourced(entity)
        _refuse_undated(entity)
        _refuse_mismatched_use(entity, use)
        if opened is not None:
            _refuse_unopened(entity, opened)
        _refuse_permitted_use(entity, use, opt_ins)

    pages = MarketScan(as_of=as_of, entities=list(entities), use=use)
    pages.answer = _answer_for(pages)
    pages.limitations = list(limitations or _default_limitations(pages))
    return pages


def _price_field(entity: MarketEntity) -> tuple[str, str] | None:
    """The name and the value of the field that speaks of a price, if there is one.

    A field whose name speaks of price is a *candidate* - and a report that
    headed a column "Pricing model" has not published a price, it has
    published the model. So the candidates are read in order and the first
    one whose value carries a number is the price; a candidate that carries
    none is a statement about the absence, and it is still the field the
    report used to say so, which is why it is the fallback.
    """
    candidates = [
        (name, str(value))
        for name, value in entity.fields.items()
        if "price" in name or "pricing" in name
    ]
    for name, value in candidates:
        if any(character.isdigit() for character in value):
            return name, value
    return candidates[0] if candidates else None


def _price_of(entity: MarketEntity) -> str:
    """The price the sources published, or the empty string when they published none."""
    field = _price_field(entity)
    return field[1] if field else ""


def publishes_price(entity: MarketEntity) -> bool:
    """Whether the source published a price, rather than a statement that it did not.

    **A price is a number.** A field whose value is *"No price published;
    the page carries Contact Us and Contact Sales"* is a well-sourced fact
    about the absence, and counting it as a price would make the market
    look better covered than it is - which is the direction that misleads.

    So the count is made on digits, and a vendor that publishes no price
    is counted as not publishing one even when the report says so at
    length. The count is the finding; it cannot be flattered by a sentence.
    """
    value = _price_of(entity)
    return bool(value) and any(character.isdigit() for character in value)


def _describes(entity: MarketEntity) -> str:
    """The first fact that is not the price, whatever the report called that field.

    The renderer does not know the table's headers in advance, so it takes
    the entity's first non-price fact. A report that headed the column
    "Pricing model" gets that sentence in the description column, which is
    what the reader would have read.
    """
    price_field = _price_field(entity)
    price_name = price_field[0] if price_field else None
    for name, value in entity.fields.items():
        if name == price_name:
            continue
        return str(value)
    return ""


def _answer_for(scan: MarketScan) -> str:
    """The answer, in one paragraph, with the citations in it.

    The citations are in the answer on purpose: a claim with no source in
    the sentence is a claim the audit cannot ground, and the audit is
    right to say so.

    The answer is a **finding**, not a table of contents. "This market holds
    six companies" tells a reader nothing; how many of them publish a price
    at all is the first thing a buyer asks, and it is countable from the
    ledger without a judgement. The count is of *published* prices, because
    the one that is missing is the finding: a vendor with no price on its
    page cannot be compared on price with the others.
    """
    names = ", ".join(
        f"[{entity.name}]({entity.source_urls()[0]})" if entity.source_urls() else entity.name
        for entity in scan.entities
    )
    pages = len(scan.pages())
    total = len(scan.entities)
    priced = sum(1 for entity in scan.entities if publishes_price(entity))
    if priced == 0:
        coverage = f"None of the {total} publishes a price on the page read."
    elif priced == total:
        coverage = f"All {total} publish a price on the page read."
    else:
        coverage = (
            f"{priced} of the {total} publish a price on the page read; "
            f"{total - priced} publishes none."
        )
    return (
        f"As of {scan.as_of}, this market holds {total} tracked compan"
        f"{'y' if total == 1 else 'ies'}, read from {pages} page"
        f"{'' if pages == 1 else 's'}: {names}. {coverage}"
    )


def _default_limitations(scan: MarketScan) -> list[str]:
    """What the scan does not cover, in the scan's own words."""
    return [
        "Every field here is a field the sources supported. A field with no source is refused, "
        "so a gap in the table is a gap in the sources rather than a gap in the reading.",
        "The scan covers the companies listed and nothing else. A company absent from it was not "
        "looked for, and its absence is not evidence that it is not in the market.",
        "Nothing here is a claim about a company beyond what the cited page says about it, and the "
        "pages were read on the date above.",
    ]


def _cell(value: Any) -> str:
    """One table cell, with the pipe escaped and the empty value said plainly."""
    if value is None or value == "":
        return "not stated by the source"
    return str(value).replace("|", "/").replace("\n", " ").strip()


def _readable(field: str) -> str:
    """A field name a reader can read: ``what_it_does`` becomes ``what it does``.

    The ledger keeps the machine's name and the report shows the reader's,
    because the field name is an identifier in one place and a phrase in
    the other, and a reader should not have to know which is which.
    """
    return field.replace("_", " ").strip()


def render_markdown(scan: MarketScan) -> str:
    """The report a reader sees.

    The reader layout: the answer first, as a quote; then the findings; then
    the limitations. The process appendix is appended by :func:`write_scan`,
    which builds it over this body so every link the report carries is one
    the grounding check can see.
    """
    lines: list[str] = []
    lines.append(f"> {scan.answer}")
    lines.append("")
    lines.append("## What is in the market")
    lines.append("")
    lines.append("| Company | What it is | Price | Read from |")
    lines.append("|---|---|---|---|")
    for entity in scan.entities:
        urls = entity.source_urls()
        read_from = (
            f"[{url.split('//', 1)[-1]}]({url})" for url in urls
        )
        lines.append(
            "| {name} | {what} | {price} | {read} |".format(
                name=_cell(entity.name),
                what=_cell(_describes(entity)),
                price=_cell(_price_of(entity)),
                read="<br>".join(read_from) if urls else "not stated by the source",
            )
        )
    lines.append("")
    lines.append("## How each company was read")
    lines.append("")
    for entity in scan.entities:
        lines.append(f"### {entity.name}")
        lines.append("")
        for url in entity.source_urls():
            backed = entity.fields_from(url)
            retrieved = next(
                (p.retrieved_at for p in entity.provenance if p.source_url == url and p.retrieved_at),
                "",
            )
            when = f", read on {retrieved}" if retrieved else ""
            lines.append(f"- **{', '.join(_readable(f) for f in backed) or 'the record'}** — [{url}]({url}){when}")
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for item in scan.limitations:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_scan(scan: MarketScan, out_dir: Path, *, ledger_path: str | None = None) -> dict[str, Path]:
    """Write the scan: ``market_scan.md`` and ``entities.json``.

    The report carries the flow's own process appendix, so the audit can
    check that every citation was opened by a fetch. When there is no ledger
    the appendix says so, rather than rendering "0 searches, 0 pages read"
    over an answer that did plenty of both.

    The appendix is built over the **whole body**, not over the answer
    paragraph. A report whose answer cites two pages and whose table cites
    two more has four citations, and grounding only the answer would leave
    the table's links unchecked - the gap this capability exists to close.
    """
    from ..support.process_appendix import build_appendix

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    body = render_markdown(scan)

    appendix = ""
    if ledger_path:
        appendix, _counters = build_appendix(ledger_path, body)

    report = out_dir / "market_scan.md"
    report.write_text(body + appendix.rstrip("\n") + "\n", encoding="utf-8")

    entities = out_dir / "entities.json"
    entities.write_text(
        json.dumps(scan.to_entities_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"report": report, "entities": entities}


__all__ = [
    "MarketScan",
    "MarketScanError",
    "build_scan",
    "render_markdown",
    "write_scan",
]
