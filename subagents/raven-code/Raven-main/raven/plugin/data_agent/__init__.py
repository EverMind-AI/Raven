"""Data-agent plugin: L1 evidence layer over heterogeneous stores.

A bundled Raven plugin that turns the generic agent into a data agent by
contributing a narrow set of tools over a single DuckDB query plane:

  sql              read-only SQL over every attached store, one dialect
  profile          row/dtype/null/n_distinct + sentinel + type-mix detection
  distinct_values  value distribution for a column (preferred over sample rows)
  verify_join      COUNT probes for a candidate join -> match_rate + fan-out

Design rationale and the layer model live in
``docs/specs/data-agent-design.md`` (sections 3-4). The plane is deliberately
one engine: sqlite/duckdb attach natively, postgres via the postgres scanner,
mongo materialized to a temp table at session start, so cross-store joins are
ordinary SQL and the model only ever writes one dialect.
"""

from __future__ import annotations
