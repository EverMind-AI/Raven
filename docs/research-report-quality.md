# Research report quality: the protocol

How a Raven-Research report is graded against a competitor's best run, what the audit
script settles mechanically, and what a reader still has to judge. Written after the
2026-09-04 head-to-head, which our report lost 9 dimensions to 4 with 2 split - not on
writing or honesty, where it won, but on four defect classes a parser can find.

The protocol exists so that the next comparison measures the same things in the same way.
A rubric applied twice by hand is two rubrics.

## What is being compared

Two briefs, both real research questions from the compression work, both run against every
vendor so a dimension table has three columns:

- **Brief A - baselines.** Survey the competing methods for query-agnostic, frozen-decoder
  context compression: what exists, which are actually frozen-decoder, at which ratios,
  with what byte accounting, and which are the ones a reviewer will say we ignored.
- **Brief B - benchmark selection.** Choose the long-context benchmarks an ICLR-2027
  submission should report, with per-candidate scoring, a do-not-bother list, and the
  community-convention table that says which published numbers may be quoted beside ours.

Reports are compared as delivered, appendix included. The appendix is part of the product:
a competitor with no trail is not thereby cleaner, it is unmeasured, and the audit says so
rather than crediting the silence.

## Run conditions, recorded with the report

A comparison is void unless these are written down beside the report, because each one
moves the result more than any prompt change in this repo:

| | |
|---|---|
| Mode | `fast` / `deep` / `ultra`. Comparing against a competitor's top tier means `ultra`; the default has been `fast` since 2026-08-27 and bounds a run at 20 flow iterations |
| Flow label | `drFlow.version`, e.g. `dr@3.5-filetools-askuser-derive`. A prompt change moves the suffix, so the label is what makes two runs the same experiment |
| Model | the resolved provider and model, not the pinned one: with no key here the agent inherits the host's LLM, and the launcher prints which on stderr |
| Wall clock | from the trail's own `Nm of research` |
| Brief | verbatim, including any of our own results handed to the model as input |

The 2026-09-04 run has no mode recorded. Its trail shows 46 searches and 36 pages against a
`fast` ceiling of 40 tool iterations, so it was not the default profile - but which of the
two deeper ones it was cannot be recovered from the report. That is the first thing this
protocol fixes: unrecorded conditions make a won or lost dimension unattributable.

## The dimensions

Fifteen, in the order they were scored. The middle column says what settles it: `audit`
means `scripts/research_report_audit.py` decides it from the file, `read` means a person
does, `both` means the audit narrows it and a person judges what is left.

| Dimension | Settled by | What it asks |
|---|---|---|
| Task coverage | both | Every artefact the brief asked for, present and complete. The audit finds missing sections; whether a section answers the brief is a read |
| Factual accuracy | read | Spot-check the numbers against the cited page. The audit cannot know that 2k should be 9,928, only that the cell said "est." |
| Sourcing and verifiability | audit | Every claim traceable to a URL in the report; citations opened rather than merely listed |
| Internal consistency | audit | Ranking agrees with its own totals; no cell contradicts another |
| Mechanism depth | read | Does the report explain why, or only list what |
| Use of our own data | read | Our existing results used as argument, not background |
| Insight | read | Something a reader could not have got from the abstracts |
| Actionability | read | Per-candidate: what to run, what to report, what it costs |
| Scope discipline | read | The brief's envelope respected; out-of-envelope items in a reserve list, not the main table |
| Convention table | both | Complete, quotable, and honest about what may not be compared. The audit counts rows marked not fetched |
| Calibration | read | Uncertainty marked where it exists, and nowhere else |
| Risk awareness | read | Contamination, licence, output-format mismatches named |
| Do-not-bother list | read | Real exclusions with mechanisms, no filler |
| Readability | audit | Table widths, section shape, one screen per table |
| Omissions | read | What a reviewer would attack us for not covering |

## What the audit script decides

```bash
python3 scripts/research_report_audit.py REPORT.md            # human summary
python3 scripts/research_report_audit.py --json REPORT.md     # for a batch
python3 scripts/research_report_audit.py --strict REPORT.md   # exit 1 on a hard finding
```

Three hard findings, one per defect class that lost a dimension on 2026-09-04:

- **`estimate_in_quantitative_column`** - a self-declared estimate inside a column that is
  mostly numbers. This is the one that cost the accuracy dimension: two size cells reading
  `(est.)` were off by an order of magnitude and the candidates were ranked on them. A cell
  that shows its arithmetic (`~3.2 (derived: 5,049/1,585)`) is exempt - deriving what the
  sources leave implicit is what the flow asks for.
- **`rank_contradicts_total`** - a table ordered by its total, except in one row. All
  three parts are found by shape rather than by header word: a rank is an unbroken run of
  integers starting at the top, a total is the column that actually equals the sum of the
  criteria beside it, and the ordering is the number of rows that would have to move for the totals to fall -
  one, or a tenth of a long table. That
  verifies the total instead of trusting its label, keeps the check alive on a report whose
  headers are not in English - which our own are not whenever the host config sets a
  language - and keeps it off the two shapes that are not rankings at all: a column of
  years beside an arithmetic total, and a numbered breakdown whose totals merely vary.
- **`citation_never_opened`** / **`citation_never_surfaced`** - read off the appendix the
  flow already writes.
- **`answer_cites_nothing`** - the appendix's other grounding outcome, the one whose
  sentence carries no count: pages were read and the answer cites no link at all, so the
  grounding check did not run rather than passing. Scanned in a table of runs, an absence
  of citation warnings reads as a clean bill, which is why this one is a finding.

Soft findings, reported and not gated: prose before the first heading (the model answering
its own harness, which ships as the reader's first sentence), a missing template section,
rows the report marks not fetched, shallow fetch depth (median page under 3k characters
means abstracts and READMEs rather than papers and data cards), citations without a URL
scheme, fence-tag citations, and an answer that shipped unreviewed.

One more soft finding settles the readability dimension's measurable half.
`too_many_compared_columns` fires above eight columns that a reader has to compare across,
and names the last two so the finding says what to move rather than only that something is
wrong; a column of bare URLs is held out, because the template asks for full addresses and
the eye goes down a source column rather than along it. Eight is measured rather than
chosen: over the nine reports of the 2026-09-04 corpus - three vendors, 32 tables - every
ordinary comparison table sits at eight or below and everything above is the matrix that
put the rank, the criteria, the total, the venue and the sizes in one grid - ours at ten on
three separate briefs, a competitor's twice at thirteen. Character width is reported beside the count and
deliberately not thresholded: it follows how much prose the cells carry, and a five-column
table of long cells wraps into something still readable while a thirteen-column one does
not.

One finding withholds a verdict rather than giving one. The appendix lists at most forty
pages and counts the rest, so on a longer run the character median is a median over
whichever fetches came first: `fetch_depth_from_a_truncated_list` names the listed share
and the prefix median, and the depth verdict is not issued at all. A prefix that happens
to hold the deepest reads would otherwise buy an all-clear the run did not earn.

The script is stdlib-only and reads any markdown report, so a competitor's file is audited
by the same parser as ours. The few markers that cannot be language-neutral - a cell that
says "approximately", "not obtained", "unverified", "derived" - are written as codepoint
escapes with a glossary constant naming each one in English, because the repo's source
language rule covers string constants and these are input data rather than prose. A test
holds the glossary and the patterns in step, so a marker cannot quietly stop firing. That is the point: without one parser the comparison is two
rubrics again. Vendor-agnostic means the ordinary Markdown shapes too: tables written
without outer pipes, escaped pipes inside a cell, and a table shown inside a fenced block
as an example rather than as data.

Four of its tests build an appendix with the flow's own `process_appendix.render()` and
read it back, so the parser is pinned to the producer rather than to strings copied out of
one report.

## Baseline, 2026-09-04

Our report against the competitor's best tier, on brief B:

| | Ours | Theirs |
|---|---|---|
| Dimensions won | 4 | 9 |
| Hard audit findings | 6 | 1 |
| Fetch depth, median page | 2,358 chars | no appendix |
| Citations cited / unopened / never surfaced | 39 / 10 / 5 | not recorded |

Four root causes, in the order they cost us dimensions:

1. **Reading too shallow.** 36 pages at a 2,358-character median is landing pages, not
   papers: a 5.5K-character README was open when the report wrote "~2k (est.)" for a
   benchmark whose real size is 9,928. Seven convention-table rows never got their page.
2. **No numeric discipline.** Estimates entered scored columns, were summed, and changed
   the ranking. The reviewer passed the draft anyway - its rubric tells it not to reject
   claims it merely cannot verify, which is exactly what an estimate looks like.
3. **The brief's hard constraints had no execution layer.** Length envelope, list types and
   required sections were satisfied by intent rather than checked, so a whole section was
   missing and five out-of-envelope candidates took main-table slots.
4. **Budget mismatched to the comparison.** 19 minutes against a competitor's top tier.

## The sequence this protocol grades

1. **Numeric discipline** - a number that enters a table, a ranking or a cost estimate comes
   from an opened page or is written as not obtained; a reviewer rubric that rejects on it.
2. **Thin-page fallback** - a fetch that returns too little retries down a per-genre chain
   (arXiv abstract to HTML to PDF, OpenReview to PDF, repository to dataset card) instead of
   being counted after the fact.
3. **Brief delivery checklist** - the brief's machine-checkable constraints extracted at the
   start and verified before delivery, bouncing once and naming what is missing.
4. **Citation normalisation** - repository owners case-folded and arXiv forms folded before
   the never-surfaced check, and no identifier written for a source that was never opened.
5. **Ranking and readability** - a ranked table whose order agrees with its total, with
   no exception carried in a row, and a column cap on the main table.

Full re-runs of both briefs are expensive, so they happen after 1, 2 and 3, and once more
at the end for the final scoring. Steps 4 and 5 are checked by the audit and the suite.

### What has landed, and what the scoring still needs

All five steps are implemented as of 2026-09-07. Three things about that are worth
writing down rather than inferring from the git log.

**Step 4's fold is narrower than the sentence above.** "arXiv forms folded" turned out to
be wrong as stated: a pinned `vN` is a different retrievable document, so bare and pinned
forms stay distinct and only the four path prefixes and a trailing `.pdf` fold.

**Step 5's ranking rule lost its escape clause, and the sequence above is corrected to
match.** The in-row reason it first allowed is gone: the audit reads no reason cell and
`rank_contradicts_total` is a hard finding, so a report that followed that escape exactly
would still fail `--strict`, and an instruction no report can satisfy is worse than none.
The template now asks for an order its own total supports and for nothing else - a
constraint from outside the criteria becomes a criterion so the total carries it, or the
candidate leaves the main table with its reason in prose beside it. The rule is asked for
rather than imposed: the audit names the violation after the fact, and neither step
rewrites a table.

**No brief has been re-run since 2026-09-04, and none can be on the machine this work was
done on**: there is no Serper key in `agents/raven-research/.env`, in the host config, or
in the environment. So every claim in this repo about these five changes rests on unit
tests and on the audit script over stored reports - not on a measured run. The head-to-head
in the baseline table above is still the 2026-09-04 one, and the final scoring the sequence
calls for has not happened. Anyone reading a dimension as won or lost should re-run both
briefs first, with the run conditions above recorded beside each report.
