"""Deterministic L1 evidence tools over the kernel-owned DuckDB plane.

Contributed as separate plugin factories that share one DataAgentSession per
workspace. Every tool follows the design-doc tool laws: narrow surface,
output carries the next action, warnings ride back on the result (they are
never a separate tool the model has to remember to call), and large results
pass by reference (a parquet handle) instead of flooding context.

All execution happens in the kernel subprocess (see ``session.py``); this
module validates input (the read-only SQL gate runs host-side, before
anything reaches the kernel), shapes the queries, and formats the replies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession, get_session, parse_sources
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only, wrap_with_cap

_PREVIEW_ROWS = 50
_HARD_ROW_CAP = 100_000
_SENTINELS = {"", "na", "n/a", "nan", "null", "none", "-", "--", "?", "unknown", "-999", "9.99e32"}


class _KernelQueryError(RuntimeError):
    def __init__(self, message: str, session_notes: list[str]):
        super().__init__(message)
        self.session_notes = session_notes


def _session_from_ctx(ctx: PluginContext) -> DataAgentSession:
    config = ctx.config or {}
    return get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )


def _fmt_table(cols: list[str], rows: list[list], limit: int = _PREVIEW_ROWS, total: int | None = None) -> str:
    if not cols:
        return "(no columns)"
    head = rows[:limit]
    widths = [len(c) for c in cols]
    for r in head:
        for i, v in enumerate(r):
            widths[i] = min(60, max(widths[i], len(str(v))))
    def line(vals):
        return " | ".join(str(v)[:60].ljust(widths[i]) for i, v in enumerate(vals))
    out = [line(cols), "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in head]
    remaining = (total if total is not None else len(rows)) - len(head)
    if remaining > 0:
        out.append(f"... ({remaining} more rows not shown)")
    return "\n".join(out)


def _notes_prefix(resp: dict) -> str:
    session_notes = resp.get("session_notes") or []
    return ("".join(f"[SESSION] {n}\n" for n in session_notes) + "\n") if session_notes else ""


async def _query(session: DataAgentSession, sql_text: str, *, preview: int = _PREVIEW_ROWS, **kw) -> dict:
    """Run one internal, tool-authored query through the kernel."""
    resp = await asyncio.to_thread(session.sql, sql_text, preview=preview, **kw)
    if not resp.get("ok"):
        raise _KernelQueryError(str(resp.get("error")), resp.get("session_notes") or [])
    return resp


def _one_row(resp: dict) -> list:
    rows = resp.get("rows") or []
    return rows[0] if rows else []


class SqlTool(Tool):
    """Read-only SQL over every attached store, one DuckDB dialect."""

    timeout_seconds = 180.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "sql"

    @property
    def description(self) -> str:
        return (
            "Run one read-only SQL query over the attached data sources (DuckDB dialect; "
            "cross-source joins are ordinary SQL, e.g. metadata.articles JOIN sales.orders). "
            "Returns a preview plus stats and notes. Large results are written to a parquet "
            "handle you can query again as read_parquet('<handle>'). Writes are rejected."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single read-only SQL query."},
                "save_as": {
                    "type": "string",
                    "description": "Optional handle name; the full result is written to derived/<name>.parquet.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, save_as: str | None = None) -> str:
        try:
            ensure_read_only(query)
        except SqlRejectedError as exc:
            return f"[SQL REJECTED] {exc}"
        capped = wrap_with_cap(query, _HARD_ROW_CAP)
        resp = await asyncio.to_thread(
            self._session.sql,
            capped,
            raw_query=query,
            preview=_PREVIEW_ROWS,
            save_as=save_as,
        )
        prefix = _notes_prefix(resp)
        if not resp.get("ok"):
            return (
                f"{prefix}[SQL ERROR] {resp.get('error')}\n"
                "Next: check column/table names with profile('<source.table>') or "
                "sql(\"SELECT table_schema, table_name FROM information_schema.tables\")."
            )
        cols, rows, n = resp.get("cols") or [], resp.get("rows") or [], int(resp.get("row_count") or 0)
        notes: list[str] = []
        if n == 0:
            notes.append(
                "0 rows. Empty is a valid answer, but confirm it is not a wrong filter: "
                "re-check the predicate against distinct_values on the filtered column."
            )
        if n >= _HARD_ROW_CAP:
            notes.append(
                f"result hit the {_HARD_ROW_CAP} row cap and is truncated; aggregate in SQL instead of pulling rows."
            )
        if resp.get("saved"):
            notes.append(f"full result saved to {resp['saved']} (query it with read_parquet('{resp['saved']}')).")
        body = _fmt_table(cols, rows, total=n)
        notes_txt = ("\nNOTES: " + " | ".join(notes)) if notes else ""
        return f"{prefix}{body}\n\n[rows={n} cols={len(cols)}]{notes_txt}"


class PythonTool(Tool):
    """Stateful Python cells sharing the kernel (and its DuckDB connection)."""

    timeout_seconds = 300.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "python"

    @property
    def description(self) -> str:
        return (
            "Run Python in a persistent kernel: variables survive across calls, and the "
            "DuckDB connection to every attached source is available as `con` "
            "(e.g. df = con.sql('SELECT ...').df()). The cell's last expression is shown "
            "with an automatic shape/dtypes/head inspection for DataFrames, and merge/"
            "groupby/filter results get sanity checks appended. If the kernel dies, "
            "variables are lost and the response says so."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code; the last expression is echoed."},
            },
            "required": ["code"],
        }

    async def execute(self, code: str) -> str:
        resp = await asyncio.to_thread(self._session.exec_code, code, self.timeout_seconds - 10)
        prefix = _notes_prefix(resp)
        if not resp.get("ok"):
            stdout = resp.get("stdout") or ""
            stdout_part = f"{stdout}\n" if stdout else ""
            return f"{prefix}{stdout_part}[PYTHON ERROR] {resp.get('error')}"
        out = (resp.get("stdout") or "").strip()
        for extra in (resp.get("inspect"), *(resp.get("verify") or [])):
            if extra:
                out = f"{out}\n{extra}" if out else extra
        return f"{prefix}{out}" if out else f"{prefix}(cell ran, no output)"


class ProfileTool(Tool):
    """Column-level profile: dtype, null rate, n_distinct, top-k, sentinels, type-mix."""

    timeout_seconds = 180.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "profile"

    @property
    def description(self) -> str:
        return (
            "Profile a table or one column: row count, per-column dtype, null rate, distinct "
            "count, top-k values with frequencies, numeric min/max, and flags for suspected "
            "sentinel values (M, NA, -999, empty, [Not Available]) and mixed-type columns. "
            "Prefer this and distinct_values over pulling sample rows."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Qualified table, e.g. sales.orders."},
                "column": {"type": "string", "description": "Optional single column to profile in depth."},
            },
            "required": ["table"],
        }

    async def execute(self, table: str, column: str | None = None) -> str:
        try:
            total_resp = await _query(self._session, f"SELECT COUNT(*) FROM {table}")
            total = int(_one_row(total_resp)[0])
            cols_resp = await _query(self._session, f"SELECT * FROM {table} LIMIT 0")
            columns = cols_resp.get("cols") or []
        except _KernelQueryError as exc:
            return f"[PROFILE ERROR] cannot read {table}: {exc}"
        prefix = _notes_prefix(total_resp)
        targets = [column] if column else columns
        lines = [f"table {table}: {total} rows, {len(columns)} columns"]
        for col in targets:
            q = f'"{col}"'
            try:
                nn, nd = _one_row(
                    await _query(
                        self._session,
                        f"SELECT COUNT({q}) AS non_null, COUNT(DISTINCT {q}) AS ndistinct FROM {table}",
                    )
                )
                nn, nd = int(nn), int(nd)
            except _KernelQueryError as exc:
                lines.append(f"  {col}: [error {exc}]")
                continue
            null_rate = 0.0 if total == 0 else (total - nn) / total
            info = [f"non_null={nn}", f"n_distinct={nd}", f"null_rate={null_rate:.3f}"]
            # top-k value frequencies (low-card lens; surfaces sentinels like "M")
            try:
                topk_rows = (
                    await _query(
                        self._session,
                        f"SELECT CAST({q} AS VARCHAR) AS v, COUNT(*) c FROM {table} "
                        f"WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 8",
                        preview=8,
                    )
                ).get("rows") or []
                if topk_rows:
                    info.append("top=" + ", ".join(f"{v!r}:{c}" for v, c in topk_rows))
                    suspects = [v for v, _ in topk_rows if str(v).strip().lower() in _SENTINELS or _bracketed(v)]
                    if suspects:
                        info.append(f"SENTINEL? {suspects}")
            except _KernelQueryError:
                pass
            # numeric range + type-mix
            try:
                lo, hi = _one_row(
                    await _query(
                        self._session,
                        f"SELECT MIN(TRY_CAST({q} AS DOUBLE)), MAX(TRY_CAST({q} AS DOUBLE)) FROM {table}",
                    )
                )
                if lo is not None:
                    info.append(f"num_min={lo} num_max={hi}")
                    castable = int(
                        _one_row(
                            await _query(
                                self._session,
                                f"SELECT COUNT(TRY_CAST({q} AS DOUBLE)) FROM {table} WHERE {q} IS NOT NULL",
                            )
                        )[0]
                    )
                    if 0 < castable < nn:
                        info.append(f"MIXED-TYPE? {castable}/{nn} values are numeric")
            except _KernelQueryError:
                pass
            lines.append(f"  {col}: " + " ".join(info))
        return prefix + "\n".join(lines)


class DistinctValuesTool(Tool):
    """Value distribution for one column -- preferred over sample rows."""

    timeout_seconds = 120.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "distinct_values"

    @property
    def description(self) -> str:
        return (
            "List a column's distinct values with frequencies (default 100). Sample rows are "
            "biased and low-diversity; this shows the true value vocabulary, which is what "
            "predicates and joins are usually built on."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "column": {"type": "string"},
                "limit": {"type": "integer", "description": "Max distinct values (default 100)."},
            },
            "required": ["table", "column"],
        }

    async def execute(self, table: str, column: str, limit: int = 100) -> str:
        q = f'"{column}"'
        try:
            values_resp = await _query(
                self._session,
                f"SELECT CAST({q} AS VARCHAR) AS v, COUNT(*) c FROM {table} "
                f"GROUP BY 1 ORDER BY c DESC LIMIT {int(limit)}",
                preview=int(limit),
            )
            nd = int(
                _one_row(await _query(self._session, f"SELECT COUNT(DISTINCT {q}) FROM {table}"))[0]
            )
        except _KernelQueryError as exc:
            return f"[DISTINCT ERROR] {exc}"
        rows = values_resp.get("rows") or []
        body = _fmt_table(["value", "count"], rows, limit=int(limit))
        note = "" if nd <= limit else f"\nNOTES: {nd} distinct total; showing top {limit} by frequency."
        return f"{_notes_prefix(values_resp)}{body}\n\n[n_distinct={nd}]{note}"


class VerifyJoinTool(Tool):
    """COUNT probes for a candidate join -> match rate + fan-out + verdict."""

    timeout_seconds = 180.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "verify_join"

    @property
    def description(self) -> str:
        return (
            "Probe a candidate equi-join before you trust it: reports left/right row counts, "
            "how many left keys match, and the max fan-out. Catches silent join failures "
            "(no match -> empty result) and cardinality blow-ups (wrong grain -> inflated sums). "
            "Optionally transform keys, e.g. left_transform=\"replace(id,'bid_','')\"."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "left": {"type": "string", "description": "Left table."},
                "left_key": {"type": "string"},
                "right": {"type": "string", "description": "Right table."},
                "right_key": {"type": "string"},
                "left_transform": {"type": "string", "description": "Optional SQL expr over left_key."},
                "right_transform": {"type": "string", "description": "Optional SQL expr over right_key."},
            },
            "required": ["left", "left_key", "right", "right_key"],
        }

    async def execute(
        self,
        left: str,
        left_key: str,
        right: str,
        right_key: str,
        left_transform: str | None = None,
        right_transform: str | None = None,
    ) -> str:
        lk = left_transform or f'"{left_key}"'
        rk = right_transform or f'"{right_key}"'
        try:
            lcount = _one_row(
                await _query(self._session, f"SELECT COUNT(*), COUNT(DISTINCT {lk}) FROM {left}")
            )
            rcount = _one_row(
                await _query(self._session, f"SELECT COUNT(*), COUNT(DISTINCT {rk}) FROM {right}")
            )
            matched = int(
                _one_row(
                    await _query(
                        self._session,
                        f"SELECT COUNT(DISTINCT l.k) FROM (SELECT {lk} AS k FROM {left}) l "
                        f"WHERE l.k IN (SELECT {rk} FROM {right})",
                    )
                )[0]
            )
            fanout = _one_row(
                await _query(
                    self._session,
                    f"SELECT MAX(c) FROM (SELECT {rk} AS k, COUNT(*) c FROM {right} GROUP BY 1) t",
                )
            )[0]
        except _KernelQueryError as exc:
            return f"[VERIFY_JOIN ERROR] {exc}\nNext: profile both columns to confirm they exist and share a value shape."
        l_rows, l_keys = int(lcount[0]), int(lcount[1])
        match_rate = 0.0 if not l_keys else matched / l_keys
        verdict = []
        if match_rate == 0:
            verdict.append(
                "NO KEYS MATCH -- the join would drop everything; keys likely need a transform (prefix/case/pad)."
            )
        elif match_rate < 0.5:
            verdict.append(f"low match rate {match_rate:.2f} -- check for a key format mismatch before trusting this join.")
        else:
            verdict.append(f"match rate {match_rate:.2f}.")
        if fanout and int(fanout) > 1:
            verdict.append(
                f"right side is not unique (max fan-out {fanout}); a join will multiply left rows -- aggregate or dedupe first."
            )
        return (
            f"left {left}: {l_rows} rows, {l_keys} distinct keys\n"
            f"right {right}: {rcount[0]} rows, {rcount[1]} distinct keys\n"
            f"matched left keys: {matched}/{l_keys} (match_rate={match_rate:.3f}); max fan-out={fanout}\n"
            f"VERDICT: {' '.join(verdict)}"
        )


def _bracketed(v: Any) -> bool:
    s = str(v).strip()
    return s.startswith("[") and s.endswith("]")


# ---- plugin factories (one per manifest [[plugin.contributes.tools]] entry) ----


def make_sql_tool(ctx: PluginContext) -> Tool:
    return SqlTool(_session_from_ctx(ctx))


def make_python_tool(ctx: PluginContext) -> Tool:
    return PythonTool(_session_from_ctx(ctx))


def make_profile_tool(ctx: PluginContext) -> Tool:
    return ProfileTool(_session_from_ctx(ctx))


def make_distinct_values_tool(ctx: PluginContext) -> Tool:
    return DistinctValuesTool(_session_from_ctx(ctx))


def make_verify_join_tool(ctx: PluginContext) -> Tool:
    return VerifyJoinTool(_session_from_ctx(ctx))
