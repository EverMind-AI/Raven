"""Deterministic read-only enforcement and row-capping for the SQL tool.

"Safety is deterministic, not instructional" (design doc law 14): the plane
never trusts a prompt rule like "only SELECT". Every statement is parsed with
sqlglot and rejected unless it is a single read-only query; the row cap wraps
the query rather than trusting a model-supplied LIMIT.

Two layers, fail-closed: a cheap lexical guard that errs toward rejection, then
the sqlglot parse. A parse failure is a rejection, not a pass.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

# Statement types that read without mutating. Anything else -- INSERT, UPDATE,
# DELETE, CREATE, DROP, ATTACH, COPY, CALL, PRAGMA with side effects -- is out.
_READONLY_TOP = (exp.Select, exp.Union, exp.Subquery, exp.With)

# Lexical fast-guard: tokens that must never appear at statement scope. The
# guard is intentionally trigger-happy (it runs before the parser); the parser
# is the precise arbiter for anything it lets through.
_LEXICAL_DENY = (
    "insert ", "update ", "delete ", "drop ", "create ", "alter ", "truncate ",
    "attach ", "detach ", "copy ", "install ", "load ", "pragma ", "call ",
    "export ", "import ", "grant ", "revoke ", "vacuum ", "checkpoint ",
)


class SqlRejectedError(ValueError):
    """Raised when a statement is not a single read-only query."""


def _strip_comments_and_strings(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i : i + 2]
        if two == "--":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        c = sql[i]
        if c in ("'", '"'):
            i += 1
            while i < n and sql[i] != c:
                i += 1
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def ensure_read_only(sql: str) -> None:
    """Raise SqlRejectedError unless ``sql`` is exactly one read-only statement."""
    bare = _strip_comments_and_strings(sql).strip().rstrip(";")
    if not bare:
        raise SqlRejectedError("empty statement")
    # Statement stacking: a bare ';' between two statements survives the strip.
    if ";" in bare:
        raise SqlRejectedError("multiple statements are not allowed; send one query")
    lowered = f"{bare.lower()} "
    for tok in _LEXICAL_DENY:
        if lowered.startswith(tok) or f" {tok}" in lowered or f"({tok}" in lowered:
            raise SqlRejectedError(f"disallowed keyword near {tok.strip()!r}; only read-only queries are allowed")
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except Exception as exc:  # fail-closed: unparseable -> rejected
        raise SqlRejectedError(f"could not parse SQL: {exc}") from exc
    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise SqlRejectedError("send exactly one statement")
    top = statements[0]
    if not isinstance(top, _READONLY_TOP):
        raise SqlRejectedError(f"only read-only queries are allowed, got {type(top).__name__.upper()}")


def wrap_with_cap(sql: str, cap: int) -> str:
    """Wrap a validated query so at most ``cap`` rows come back.

    CTE-aware: sqlglot renders the whole query (WITH prefix included) as one
    expression, so wrapping it in an outer SELECT ... LIMIT keeps the CTEs in
    scope instead of stranding a dangling WITH.
    """
    bare = sql.strip().rstrip(";")
    return f"SELECT * FROM (\n{bare}\n) AS _dab_capped LIMIT {int(cap)}"
