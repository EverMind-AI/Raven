# Data Analysis Methodology

General discipline for answering questions against databases you did not build.
Every rule here is dataset-agnostic: each one holds because of how relational
data and human questions work, not because of any particular schema.

## 1. Read the question like a contract

- Map every qualifier in the question to a concrete data operation before
  writing any query. "Latest release", "per year", "in the World category",
  "excluding refunds" — each phrase either becomes a filter, a grouping, a
  window, or an explicit decision to ignore it. Unmapped qualifiers are how
  right-looking answers end up wrong, on any dataset, because the reference
  answer was computed with the qualifier applied.
- When a phrase admits more than one reading ("top products by sales": revenue
  or units?), enumerate the 2-4 candidate readings explicitly, then run one
  cheap read-only probe per reading to see which is supported by the data
  (column names, units, value ranges). Commit to one and say why. Guessing
  silently costs a full wrong answer; probing costs one query.
- When an interval metric (turnaround, cycle time, lead time, duration) meets a
  time window ("in April", "during 2023"), the window binds to the interval's
  END event (the closing/completion side) unless the question explicitly says
  opened-in or created-in. Compute both readings when in doubt, and commit to
  one with a stated reason at delivery.
- Rounding, units, and output format specified in the question are part of the
  contract. Compute at full precision, round only at the end, exactly as asked
  — intermediate rounding compounds, and a correct value in the wrong unit is
  a wrong answer everywhere.

## 2. Decide shape and grain before computing

- Before the first aggregate: is the answer one value, one row per group, or a
  ranked list? Which column defines the grain? Write the expected shape down,
  and after computing, check the result against it — a per-group question
  answered with a collapsed total (or vice versa) type-checks fine and is
  still wrong, in every engine.
- After any GROUP BY, compare the row count with the count of distinct group
  keys you expected. A mismatch means the grain is not what you thought
  (duplicated keys, NULL groups, a join that widened the table) and every
  downstream number inherits the error.

## 3. Choose columns deliberately

- When a question asks for a code, an ID, or an abbreviation, look for the
  dedicated column next to the human-readable name before grouping by the
  name. Names get reused and restyled across rows; grouping by them silently
  merges or splits entities. This holds in any database because names are for
  humans and keys are for machines.
- Never trust a column by its name alone. Profile it first: distinct values,
  NULL share, a few sample rows. A column called `year` that stores strings,
  a `price` mixing currencies, a `region` with three spellings of the same
  region — these are the norm in real data, not the exception.
- Prefer an existing structured column over re-deriving the same fact from
  free text. If a table already carries a category, label, or region column,
  classifying the text again is slower and strictly less consistent than the
  curated column — reach for text analysis only for what no column encodes.

## 4. Grouping, ranking, and ties

- Any "top N" or "largest/smallest" needs an explicit tie rule. Decide before
  querying what happens when two groups share the boundary value — include
  both, or break the tie by a stated secondary key. Engines return an
  arbitrary winner otherwise, and arbitrary is unreproducible.
- The set of groups comes from the data, not from what looks plausible: after
  applying the question's explicit exclusions, every remaining distinct value
  of the grouping column is its own group — including values that look like
  codes, typos, or outliers. World knowledge may explain a value, never delete
  it. Before delivering a per-group answer, list the grouping column's distinct
  values once and account for every one of them: a group in your answer set,
  or an exclusion the question's own words justify.
- "For each X" means every X in the population, including the ones with no
  matching rows. If a group can legitimately be empty, aggregate over the full
  list of groups (left join from the group list, or reindex) so empty groups
  appear as zeros instead of vanishing. Vanished zeros shift every average and
  every rank computed on top.
- ORDER BY on the final projection only. Sorting intermediate results proves
  nothing and can hide nondeterminism in what you actually return.

## 5. Analysis conventions that survive any schema

- Time series: after grouping by period, reindex over the full period range
  and fill genuine absence with zero before averaging — an average over only
  the periods that happen to have rows answers a different question. Confirm
  whether the question's range is inclusive on both ends and whether the data
  actually covers it.
- Ratios and shares: pin the denominator explicitly. "What share of orders…"
  — share of all orders, or of orders that survived your filters? Compute the
  denominator as its own query so it cannot drift when the numerator's
  filters change.
- Mixed scales: before comparing or summing across rows, confirm the rows are
  in the same unit and currency; normalize first if not. Sums across mixed
  units are meaningless in a way no downstream step can repair.
- Text matching: substring containment is not category membership — "male"
  is contained in "female"; use exact matches against the profiled value set,
  word boundaries, or a proper classification, never bare LIKE '%…%' on
  categorical intent.

## 6. Verify before you submit

- When the task is to single out one hit from a candidate set (which rule is
  violated, which cause, which anomaly), test each candidate's base rate over
  the WHOLE population before committing: a candidate that flags nearly every
  record (or none) has no discriminating power and is background, not the
  answer. The right answer is rare population-wide and specific to the target.
- Gate every significant filter: after applying it, check the surviving row
  count and eyeball a few surviving rows. A filter that removed 99.8% of rows
  is either exactly right or catastrophically wrong, and the count tells you
  which faster than the final answer will.
- Re-derive the headline number once by a different route — a different
  grouping order, a direct COUNT instead of a SUM of flags, SQL instead of
  dataframe code. Two implementations agreeing is strong evidence; re-running
  the same query twice is none. Probe to contradict your answer, not to
  confirm it.
- When a verification fails, fix the failing step only and re-verify it. Do
  not rewrite passing steps on a hunch, and stop when everything passes —
  churning a correct pipeline introduces regressions with no signal to catch
  them.
- An empty result is a finding, not an error. Diagnose which clause emptied it
  (drop clauses one at a time) before concluding the answer is zero or that
  the data is missing.

## 7. Only the provided data

- The answer must be derivable from the stores you were given. World knowledge
  may guide where to look and what to name things, but never substitutes for
  a value the data should provide — if the data lacks it, say so rather than
  fill the gap from memory. Reference answers are computed from the provided
  data, so imported facts diverge from them even when true.
- Read the provided documentation and schema descriptions fully before the
  first query. Ten minutes of reading routinely replaces an hour of schema
  archaeology, and the documentation is part of the provided data.

## 8. Deliver exactly what was asked

- The final answer contains the concrete values requested — names, numbers,
  lists — in the exact format and rounding the question specifies, with no
  narration of the journey. Formatting requirements are validation
  requirements.
- Before writing the final answer, re-read the question one last time against
  the value in hand: does it answer every part (some questions ask for two
  things), at the right grain, in the right unit and format? This last read
  is the cheapest verification step you have; it catches the qualifier you
  mapped correctly six steps ago and then lost.
