"""grain_check: verify the aggregation grain before trusting a grouped result.

The most expensive silent error in aggregation queries is grouping at the wrong
level: hierarchical data (type/subtype, org/repo, state/city) offers several
plausible grouping columns, and picking the child when the question names the
parent splits every parent total across its children. The final query then looks
correct and returns confidently wrong numbers.

This tool makes the grain decision mechanical instead of assumed: it measures
each candidate column's cardinality, detects parent/child relations between
them (functional dependencies), and checks a result export for duplicate grain
keys. Zero LLM calls; read-only probes on the shared query plane.
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only

_MAX_COLUMNS = 4
_TOP_VALUES = 3


class GrainCheckTool(Tool):
    """Cardinality + hierarchy probes over candidate grouping columns."""

    timeout_seconds = 180.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "grain_check"

    @property
    def description(self) -> str:
        return (
            "Before aggregating, verify which column matches the grain the question names: "
            "reports each candidate column's distinct/null counts and top values, detects "
            "parent/child (hierarchy) relations between candidates, and optionally checks a "
            "result table for duplicate grain keys. Grouping by a child column when the "
            "question names the parent level silently splits every total."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table, view, or read_parquet(...) expression to probe.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Candidate grouping columns to compare (1-{_MAX_COLUMNS}).",
                },
                "key_column": {
                    "type": "string",
                    "description": "Optional: expected unique key of a result export; "
                    "reports duplicate rows per key.",
                },
            },
            "required": ["table", "columns"],
        }

    async def _probe(self, sql: str) -> list:
        ensure_read_only(sql)
        resp = await asyncio.to_thread(self._session.sql, sql, preview=16)
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error")))
        return resp.get("rows") or []

    async def execute(
        self,
        table: str,
        columns: list[str],
        key_column: str | None = None,
    ) -> str:
        columns = [c for c in columns if c]
        if not columns:
            return "[GRAIN_CHECK REJECTED] no candidate columns given."
        if len(columns) > _MAX_COLUMNS:
            return f"[GRAIN_CHECK REJECTED] at most {_MAX_COLUMNS} candidate columns per call."

        lines: list[str] = []
        distincts: dict[str, int] = {}
        try:
            total = int((await self._probe(f"SELECT COUNT(*) FROM {table}"))[0][0])
            lines.append(f"rows: {total}")
            for col in columns:
                q = f'"{col}"'
                n_distinct, n_null = (
                    await self._probe(
                        f"SELECT COUNT(DISTINCT {q}), COUNT(*) - COUNT({q}) FROM {table}"
                    )
                )[0]
                top = await self._probe(
                    f"SELECT CAST({q} AS VARCHAR), COUNT(*) FROM {table} WHERE {q} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_TOP_VALUES}"
                )
                distincts[col] = int(n_distinct)
                sample = ", ".join(f"{value} ({count})" for value, count in top)
                lines.append(
                    f'column "{col}": {int(n_distinct)} distinct, {int(n_null)} null; top: {sample}'
                )

            hierarchy: list[str] = []
            cols = list(distincts)
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    a, b = cols[i], cols[j]
                    qa, qb = f'"{a}"', f'"{b}"'
                    a_viol, b_viol = (
                        await self._probe(
                            f"SELECT "
                            f"(SELECT COUNT(*) FROM (SELECT {qa} FROM {table} GROUP BY {qa} "
                            f"HAVING COUNT(DISTINCT {qb}) > 1) x), "
                            f"(SELECT COUNT(*) FROM (SELECT {qb} FROM {table} GROUP BY {qb} "
                            f"HAVING COUNT(DISTINCT {qa}) > 1) y)"
                        )
                    )[0]
                    a_viol, b_viol = int(a_viol), int(b_viol)
                    if a_viol == 0 and b_viol == 0:
                        hierarchy.append(f'"{a}" and "{b}" are 1:1 (aliases of the same grain).')
                    elif b_viol == 0 and distincts[a] < distincts[b]:
                        hierarchy.append(
                            f'"{a}" is a PARENT level of "{b}" '
                            f"({distincts[a]} -> {distincts[b]}): every {b} maps to one {a}."
                        )
                    elif a_viol == 0 and distincts[b] < distincts[a]:
                        hierarchy.append(
                            f'"{b}" is a PARENT level of "{a}" '
                            f"({distincts[b]} -> {distincts[a]}): every {a} maps to one {b}."
                        )
            if hierarchy:
                lines.append("hierarchy: " + " ".join(hierarchy))
                lines.append(
                    "verdict: these candidates sit at DIFFERENT grains. Group by the level the "
                    "question literally names; grouping by the child level splits each parent "
                    "total across its children and every derived number shifts."
                )
            elif len(cols) > 1:
                lines.append(
                    "hierarchy: none detected (no functional dependency between candidates)."
                )

            if key_column:
                qk = f'"{key_column}"'
                dup = int(
                    (
                        await self._probe(
                            f"SELECT COUNT(*) - COUNT(DISTINCT {qk}) FROM {table}"
                        )
                    )[0][0]
                )
                if dup:
                    lines.append(
                        f'key check: {dup} surplus rows for "{key_column}" -- the export is NOT '
                        "unique per grain key; deduplicate or re-aggregate before delivering."
                    )
                else:
                    lines.append(f'key check: "{key_column}" is unique across the export.')
        except SqlRejectedError as exc:
            return f"[GRAIN_CHECK REJECTED] {exc}"
        except RuntimeError as exc:
            return (
                f"[GRAIN_CHECK ERROR] {exc}\n"
                "Next: profile the table to confirm the column names exist."
            )
        return "\n".join(lines)


def make_grain_check_tool(ctx: PluginContext) -> Tool:
    from pathlib import Path

    from raven.plugin.data_agent.session import get_session, parse_sources

    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return GrainCheckTool(session)
