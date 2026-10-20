# Market scans

A research turn that answers *"who is in this market and what do they
charge"* has already done the hard part: it read the vendors' pages and
wrote the comparison. What it has not done is **check** it -- that every
price came from a page the run opened, that the pages' terms permit the
use, and that the facts the table asserts are the facts the pages say.

A market scan is that check, and the output it produces: a table whose
every fact is traceable to a page the run actually opened.

This page describes the `research_flow/market/` package and the
`research_flow/gates/market_scan.py` gate. What follows is the verified
behaviour.

## What a scan is made of

One row is one company, one cell is one fact, and one page is where that
fact came from:

```
| Company | What it does | Price | Read from |
|---|---|---|---|
| Acme | Observability sold per seat | $49 per seat per month | [acme.example](https://acme.example/pricing) |
```

The field names are normalised (`What it does` becomes `what_it_does`) so
the same table reads the same way to the scan's own criteria, which name
fields like `price` without caring that the report printed "Price".

**A fact the source does not state is absent**, not present and empty. A
present-and-empty field is an unsourced claim, and the scan refuses one. A
fact cell citing **two** pages has no one page, and the scan refuses that
too rather than picking one. A cell with no page of its own falls back to
the row's source column, and is dropped when there is none.

## The refusals

Six, and each is mechanical:

| Refusal | What it means |
| --- | --- |
| A field with no source | The fact has nothing behind it |
| A source whose terms do not permit the use | The registry has not cleared this use for this source |
| A page the run never opened | The claim cites something the run did not read |
| A field that cannot be dated | A sourced fact with no retrieval time cannot be re-checked |
| A field cleared for a different use | The terms were asked about another question |
| A provenance row that names no source | Whose terms govern the field is not known |

The terms are consulted against **the same registry the records store and
the web tool read**, with the operator's real opt-in record -- so a
permission recorded for a source is the same permission everywhere, not a
per-caller copy.

## The three gates

Each is ablatable on its own, so an arm can be attributed to the gate that
changed and not to the ones that did not.

**The mechanical gate** is pass/fail only and costs no model call. It
re-checks the scan, and it looks at the report for the four mechanical
defects the repository's own audit calls out. It mirrors that audit's
severity split: a hard defect fails it, a soft one is recorded and does
not.

**The sufficiency gate** checks the scan against criteria **declared
before retrieval**. The criteria are the operator's configuration, read
when the gate is built -- which is before the turn retrieves anything.
The gate compares that declaration's timestamp against the run's first
fetch and **refuses a criterion written after the pages were read**,
because a criterion written afterwards describes the answer rather than
requiring it.

**The judge** runs last, on a cheap model (`claude-haiku-4-5`), because it
reads a report and answers one question and the expensive model is for the
work. It is measured against a second reading with the companies in the
opposite order: **a judge whose verdict changes when the order changes is
reacting to the presentation, and the gate fails.**

The judge fails open -- a judge with no verdict is a scan the reader still
gets. The other two do not, because a check that could not be performed is
not a pass.

## The stop policy's numbers

The rule that says *enough* is reported in three numbers, kept apart:

| Number | What it is |
| --- | --- |
| Accuracy | The share of stop decisions that were right |
| Precision | Of the times it said *stop*, the share that were right |
| Expected calibration error | How far its confidence is from how often it is right |

**Accuracy is not the one the gate watches.** The cheap way to raise
accuracy is to stop more often, and that trades precision away -- so a
regression in precision or in calibration error fails the gate even when
accuracy is flat or better. A policy that never stops has *no* precision to
report, and saying so is not the same as reporting zero.

## What ships, and what does not

The scan **replaces the answer** when the gates pass. Otherwise the
turn's own answer stands and the note says which gate said no, so "this
arm shipped no market scan" and "this arm's market scan was refused" stay
different claims.

The shipped profile label carries the rung:
`dr@3.7-filetools-askuser-derive-numeric-cite-rank-tiers-plain-high-reader-market`.
The previous label is retired in the product's own supersession table, and
it is the arm with the market rung dropped -- which is where an operator
running the ablation is sent.

**What has not been measured.** The criterion this rung was written against
asks for a paired comparison against a baseline under a 2-sigma gate. There
is no baseline to be paired against, and no live batch has run the new
gate: the chain-level proof is the test harness, not a run. What *is*
verified is the capability -- the refusals hold under test, and the
repository's own audit returns no hard and no soft finding on a scan built
from a real research run.

## Configuration

```json
"market": {
  "enabled": true,
  "use": "competitive_brief",
  "minEntities": 3,
  "minSourceHosts": 2,
  "requirePriceForAll": false,
  "mechanical": true,
  "sufficiency": true,
  "judge": true,
  "positionSwap": true
}
```

`minEntities` and `minSourceHosts` are requirements, not preferences: the
second exists so that a scan of one vendor's own site is not mistaken for
a market. `requirePriceForAll` is off in the shipped slice because a
vendor that publishes no price is a fact about the market and not a
failure to research it.
