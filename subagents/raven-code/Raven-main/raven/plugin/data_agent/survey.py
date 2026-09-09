"""survey_sources: one structured deep-scan of every attached store, up front.

The recurring upstream failure in data tasks is starting to query before the
data system is understood: the wrong one of two synonym columns gets grouped,
a policy threshold gets matched to the wrong unit of measure, a join fans out
silently. All of these are visible in the data before the first real query --
if someone looks. This tool is that look: pure SQL probes, no LLM calls, run
once at the start; a compact map comes back and the full notes land in the
workspace for later reference.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession

_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "temp", "system"}
_MAX_TABLES = 24
_MAX_COLUMNS_PER_TABLE = 40
_TOP_VALUES = 4
_SOURCE_BUDGET_SEC = 60.0
_SUMMARY_CHARS = 4000
_NOTES_PATH = "/workspace/survey.md"
_SYNONYM_TOKEN_MIN = 5
_FD_MAX_PAIRS_PER_TABLE = 6
_FD_MAX_DISTINCT = 2000
_PLACEHOLDERS = {"na", "n/a", "none", "null", "unknown", "-", "--", "#", ""}
_BRACKET_RE = re.compile(r"^\s*\[.*\]\s*$")


def _tokens(name: str) -> set[str]:
    parts = re.split(r"[_\W]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name))
    out = set()
    for p in parts:
        p = p.lower()
        if len(p) < _SYNONYM_TOKEN_MIN:
            continue
        # An 8-char stem folds inflections of one concept ("histology" /
        # "histological") into the same group.
        out.add(p[:8] if len(p) >= 8 else p)
    return out


class _TimeoutError(Exception):
    pass


class SurveySourcesTool(Tool):
    """Deterministic schema/value/relationship survey over all sources."""

    timeout_seconds = 600.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session
        self._session_notes: list[str] = []

    @property
    def name(self) -> str:
        return "survey_sources"

    @property
    def description(self) -> str:
        return (
            "Run once, first: a deterministic survey of every attached store -- per-column "
            "types, distincts, nulls and top values; groups of columns whose names share a "
            "root (old/new or name/code variants of the same concept, with which one carries "
            "which values); dirty-value signatures (bracketed annotations, placeholders, "
            "mixed types); numeric columns classified as count-like, money-like or "
            "ratio-like; parent/child column pairs; and same-name join candidates across "
            "tables. Returns a compact map and writes full notes to survey.md. Reading the "
            "map before the first real query is how column-choice and unit-of-measure "
            "mistakes get caught while they are still free."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _q(self, sql: str, deadline: float, preview: int = 32) -> list:
        if time.monotonic() > deadline:
            raise _TimeoutError()
        resp = await asyncio.to_thread(self._session.sql, sql, preview=preview)
        # Attach failures and other kernel bootstrap notes ride on responses;
        # a survey that silently sees 0 tables is worse than one that says why.
        for note in resp.get("session_notes") or []:
            if note not in self._session_notes:
                self._session_notes.append(str(note))
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error")))
        return resp.get("rows") or []

    async def execute(self) -> str:
        start = time.monotonic()
        hard_deadline = start + self.timeout_seconds - 30
        # SHOW ALL TABLES is the one enumeration that spans every attached
        # catalog including scanner-backed ones (postgres/sqlite), where the
        # cross-catalog information_schema union can come back empty. It also
        # carries each table's column names and types, saving a query per table.
        try:
            raw = await self._q(
                "SELECT database, schema, name, column_names, column_types "
                "FROM (SHOW ALL TABLES) ORDER BY database, schema, name",
                hard_deadline,
                preview=200,
            )
        except (RuntimeError, _TimeoutError) as exc:
            return f"[SURVEY ERROR] cannot enumerate tables: {exc}"
        import ast

        def _aslist(v: Any) -> list[str]:
            if isinstance(v, list):
                return [str(x) for x in v]
            try:
                return [str(x) for x in ast.literal_eval(str(v))]
            except (ValueError, SyntaxError):
                return []

        tables = [
            (str(c), str(s), str(t), _aslist(cn), _aslist(ct))
            for c, s, t, cn, ct in raw
            if str(s).lower() not in _SYSTEM_SCHEMAS and str(c).lower() not in _SYSTEM_SCHEMAS
        ]
        skipped_tables = max(0, len(tables) - _MAX_TABLES)
        tables = tables[:_MAX_TABLES]

        notes: list[str] = ["# Source survey (auto-generated, this attempt only)", ""]
        summary: list[str] = []
        col_index: dict[str, list[tuple[str, int]]] = defaultdict(list)

        for catalog, schema, table, col_names, col_types in tables:
            fq = f'"{catalog}"."{schema}"."{table}"'
            label = f"{catalog}.{table}" if schema == "main" else f"{catalog}.{schema}.{table}"
            deadline = min(hard_deadline, time.monotonic() + _SOURCE_BUDGET_SEC)
            try:
                total = int((await self._q(f"SELECT COUNT(*) FROM {fq}", deadline))[0][0])
            except (RuntimeError, _TimeoutError) as exc:
                notes.append(f"## {label}: [unreadable: {exc}]")
                continue
            cols = list(zip(col_names, col_types))[:_MAX_COLUMNS_PER_TABLE]
            notes.append(f"## {label} -- {total} rows, {len(cols)} columns")
            summary.append(f"{label}: {total} rows, {len(cols)} cols")
            facts: dict[str, dict] = {}

            for name, dtype in ((str(c), str(d)) for c, d in cols):
                q = f'"{name}"'
                fact: dict[str, Any] = {"dtype": dtype}
                try:
                    nn, nd = (
                        await self._q(
                            f"SELECT COUNT({q}), COUNT(DISTINCT {q}) FROM {fq}", deadline
                        )
                    )[0]
                    fact["non_null"], fact["distinct"] = int(nn), int(nd)
                    lower = dtype.lower()
                    if any(k in lower for k in ("char", "text", "string", "uuid")):
                        await self._text_probe(fq, q, fact, deadline)
                    elif any(
                        k in lower for k in ("int", "decimal", "double", "float", "numeric", "hugeint")
                    ):
                        await self._numeric_probe(fq, q, fact, deadline)
                except _TimeoutError:
                    fact["note"] = "budget hit"
                except RuntimeError as exc:
                    fact["note"] = f"error {str(exc)[:60]}"
                facts[name] = fact
                col_index[name.lower()].append((label, fact.get("distinct", 0)))

            for name, fact in facts.items():
                bits = [
                    fact["dtype"],
                    f"distinct={fact.get('distinct', '?')}",
                    f"null={total - fact.get('non_null', total)}",
                ]
                for key in ("top", "shape", "bracketed", "placeholders", "list_valued", "note"):
                    if fact.get(key):
                        bits.append(f"{key}={fact[key]}")
                notes.append(f"- {name}: " + ", ".join(str(b) for b in bits))

            listy = [n for n, f in facts.items() if f.get("list_valued")]
            if listy:
                line = (
                    f"LIST-VALUED columns in {label}: {', '.join(listy)} -- values hold "
                    "several comma-separated items; deliver the stored string whole "
                    "(never one item of it) and match with LIKE/contains, not equality."
                )
                notes.append(line)
                summary.append(line)

            groups = self._synonym_groups(facts)
            for token, members in groups:
                detail = "; ".join(
                    f'"{m}" ({facts[m].get("distinct", "?")} distinct'
                    + (f", {facts[m]['bracketed']} bracketed" if facts[m].get("bracketed") else "")
                    + ")"
                    for m in members
                )
                line = (
                    f"SYNONYM GROUP '{token}' in {label}: {detail} -- decide which "
                    "column the question's wording actually applies to before grouping."
                )
                notes.append(line)
                summary.append(line)

            try:
                for line in await self._fd_pairs(fq, facts, deadline):
                    notes.append(line)
                    summary.append(f"{label}: {line}")
            except (_TimeoutError, RuntimeError):
                pass
            notes.append("")

        join_lines = [
            f"JOIN CANDIDATE column '{name}' appears in: "
            + ", ".join(f"{t} ({d} distinct)" for t, d in refs)
            for name, refs in sorted(col_index.items())
            if len(refs) > 1 and not name.startswith("_")
        ][:12]
        notes.extend(join_lines)
        if skipped_tables:
            notes.append(f"({skipped_tables} further tables not surveyed -- cap)")

        try:
            Path(_NOTES_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(_NOTES_PATH).write_text("\n".join(notes) + "\n", encoding="utf-8")
            wrote = f"full notes in {_NOTES_PATH}"
        except OSError:
            wrote = "notes file not writable; summary only"

        summary.extend(join_lines[:6])
        head = (
            f"surveyed {len(tables)} tables in {time.monotonic() - start:.0f}s; {wrote}\n"
        )
        boot = list(getattr(self._session, "_bootstrap_notes", []) or [])
        for note in boot:
            if note not in self._session_notes:
                self._session_notes.append(str(note))
        if self._session_notes:
            head += "".join(f"[SESSION] {n}\n" for n in self._session_notes[:8])
        if not tables:
            configured = ", ".join(f"{s.alias}({s.kind})" for s in self._session.sources) or "none"
            head += (
                f"No tables visible on the query plane (configured sources: {configured}) -- "
                "the stores may not have attached (see notes above); fall back to connecting "
                "to each store directly.\n"
            )
            try:
                dbs = await self._q(
                    "SELECT database_name FROM duckdb_databases()", hard_deadline
                )
                head += "attached catalogs: " + ", ".join(str(r[0]) for r in dbs) + "\n"
            except (RuntimeError, _TimeoutError) as exc:
                head += f"duckdb_databases() probe failed: {exc}\n"
        return head + "\n".join(summary)[: _SUMMARY_CHARS]

    async def _text_probe(self, fq: str, q: str, fact: dict, deadline: float) -> None:
        rows = await self._q(
            f"SELECT CAST({q} AS VARCHAR), COUNT(*) FROM {fq} WHERE {q} IS NOT NULL "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {_TOP_VALUES}",
            deadline,
        )
        fact["top"] = "/".join(str(v)[:24] for v, _ in rows)
        stats = (
            await self._q(
                f"SELECT SUM(CASE WHEN regexp_matches(CAST({q} AS VARCHAR), '^\\s*\\[.*\\]\\s*$') "
                "THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN lower(trim(CAST({q} AS VARCHAR))) IN "
                "('na','n/a','none','null','unknown','-','--','#','') THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN regexp_matches(CAST({q} AS VARCHAR), ',\\s\\S') "
                "THEN 1 ELSE 0 END), COUNT(*) "
                f"FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if stats and int(stats[0] or 0):
            fact["bracketed"] = int(stats[0])
        if stats and int(stats[1] or 0):
            fact["placeholders"] = int(stats[1])
        # A column where a third of the values hold comma-separated items is a
        # list-valued field: answers drawn from it must carry the stored string
        # whole, and equality filters on it silently miss list members.
        if stats and int(stats[3] or 0) and int(stats[2] or 0) >= 0.3 * int(stats[3]):
            fact["list_valued"] = f"{int(stats[2])}/{int(stats[3])} multi-item"
        numericish = (
            await self._q(
                f"SELECT COUNT(*), SUM(CASE WHEN regexp_matches(trim(CAST({q} AS VARCHAR)), "
                f"'^-?[0-9]+(\\.[0-9]+)?$') THEN 1 ELSE 0 END) FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if numericish and int(numericish[0]) and int(numericish[1] or 0) >= 0.95 * int(numericish[0]):
            await self._numeric_probe(fq, f"TRY_CAST({q} AS DOUBLE)", fact, deadline)
            if fact.get("shape"):
                fact["shape"] = fact["shape"] + ", stored as text"

    async def _numeric_probe(self, fq: str, q: str, fact: dict, deadline: float) -> None:
        row = (
            await self._q(
                f"SELECT MIN({q}), MAX({q}), AVG(CASE WHEN {q} = FLOOR({q}) THEN 1.0 ELSE 0.0 END) "
                f"FROM {fq} WHERE {q} IS NOT NULL",
                deadline,
            )
        )[0]
        if not row or row[0] is None:
            return
        lo, hi, int_share = float(row[0]), float(row[1]), float(row[2] or 0)
        if 0.0 <= lo and hi <= 1.0:
            fact["shape"] = "ratio-like (0..1)"
        elif int_share > 0.99 and hi <= 10_000:
            fact["shape"] = f"count-like (integers {lo:.0f}..{hi:.0f})"
        elif int_share < 0.9:
            fact["shape"] = f"money/measure-like (decimals {lo:.4g}..{hi:.4g})"
        else:
            fact["shape"] = f"integers {lo:.0f}..{hi:.0f}"

    def _synonym_groups(self, facts: dict[str, dict]) -> list[tuple[str, list[str]]]:
        by_token: dict[str, list[str]] = defaultdict(list)
        for name in facts:
            for token in _tokens(name):
                by_token[token].append(name)
        groups = []
        seen: set[frozenset] = set()
        for token, members in by_token.items():
            if len(members) < 2:
                continue
            key = frozenset(members)
            if key in seen:
                continue
            seen.add(key)
            groups.append((token, sorted(members)))
        return groups

    async def _fd_pairs(self, fq: str, facts: dict[str, dict], deadline: float) -> list[str]:
        candidates = [
            n
            for n, f in facts.items()
            if 1 < int(f.get("distinct") or 0) <= _FD_MAX_DISTINCT
            # Hierarchy is a property of categorical levels; measures (floats,
            # money) produce accidental dependencies on small data.
            and not any(k in str(f.get("dtype", "")).lower() for k in ("double", "float", "decimal", "real"))
            and "money" not in str(f.get("shape", ""))
        ]
        lines: list[str] = []
        pairs = 0
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                if pairs >= _FD_MAX_PAIRS_PER_TABLE:
                    return lines
                a, b = candidates[i], candidates[j]
                da, db = int(facts[a]["distinct"]), int(facts[b]["distinct"])
                if da == db or max(da, db) // max(min(da, db), 1) > 500:
                    continue
                pairs += 1
                parent, child = (a, b) if da < db else (b, a)
                qp, qc = f'"{parent}"', f'"{child}"'
                viol = int(
                    (
                        await self._q(
                            f"SELECT COUNT(*) FROM (SELECT {qc} FROM {fq} GROUP BY {qc} "
                            f"HAVING COUNT(DISTINCT {qp}) > 1) v",
                            deadline,
                        )
                    )[0][0]
                )
                if viol == 0:
                    lines.append(
                        f'HIERARCHY: "{parent}" is a parent level of "{child}" '
                        f"({min(da, db)} -> {max(da, db)}); group by the level the question names."
                    )
        return lines


def make_survey_sources_tool(ctx: PluginContext) -> Tool:
    from raven.plugin.data_agent.session import get_session, parse_sources

    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return SurveySourcesTool(session)
