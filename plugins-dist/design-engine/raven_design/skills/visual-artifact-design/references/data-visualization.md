# Data visualization

Apply this card to charts, maps, dashboards, metrics, infographics, and any
visual whose geometry or claims encode quantitative data. Also apply the
artifact's implementation-format card, such as HTML or SVG.

## Data integrity

- Store data once. Derive marks, labels, summaries, legends, tooltips, and
  annotations from the same source.
- Reconcile totals, percentages, units, dates, rankings, and prose claims
  against that source. Never eyeball geometry and type a separate value beside
  it.
- Preserve truthful magnitude. Start quantitative axes at zero when required,
  or make a truncated range unmistakable. Do not encode magnitude with an area
  or volume that exaggerates the underlying ratio.
- Distinguish missing, zero, estimated, and not-applicable values rather than
  collapsing them into one visual state.

## Visual encoding and reading experience

- Choose the encoding for the question: comparison, trend, distribution,
  relationship, geography, or part-to-whole.
- Keep the overview readable without interaction. Use hover or drill-down for
  precision and detail, not to reveal the basic story.
- Label values directly when space permits. Otherwise shorten, wrap, rotate, or
  change the layout; do not silently truncate meaningful labels.
- State scope, units, time period, and source. Use a conclusion-led title or a
  concise takeaway when the task calls for analysis rather than neutral
  exploration.
- Use color consistently across views and ensure critical distinctions remain
  legible without color alone.

## Validation matrix

- Recompute representative visible values, totals, percentages, scales, and
  prose claims from the source.
- Inspect the densest labels, extreme values, empty/missing states, and every
  interactive filter or series toggle.
- Verify axes, legends, tooltips, map boundaries, ordering, units, and numeric
  precision in the rendered output.
- Confirm responsive layouts preserve the data story rather than hiding series
  or labels without disclosure.
