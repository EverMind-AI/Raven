"""resolve_entities: deterministic fuzzy clustering of a value column.

Real warehouses spell the same entity many ways (typos, casing, diacritics,
word order). Aggregating over the raw column silently splits every such
entity's weight across its variants -- totals stay plausible while rankings
and counts go wrong. This tool clusters variants of one column and
materializes a (value, canonical, cluster_size) mapping table the next SQL
query can JOIN through, so aggregation happens on resolved entities.

The pipeline is classic entity resolution, kept deterministic and offline:
normalize (casefold, strip accents and punctuation, token-sort) -> block on
the normalized key -> sorted-neighborhood fuzzy merge between nearby keys
(difflib ratio, union-find) -> canonical spelling = the most frequent
original variant. No LLM calls; byte-stable across runs.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only

_MAX_DISTINCT = 20_000
_NEIGHBOR_WINDOW = 10
_DEFAULT_THRESHOLD = 0.88


class ResolveEntitiesTool(Tool):
    """Cluster spelling variants of a column into canonical entities."""

    timeout_seconds = 600.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "resolve_entities"

    @property
    def description(self) -> str:
        return (
            "Cluster spelling variants of one column (typos, casing, accents, word order, "
            "decorations such as numeric prefixes or an appended album/author) and "
            "materialize table <output_table>(value, canonical, cluster_size). Use it "
            "before any GROUP BY / ranking over names of people, titles, companies or "
            "products that may be spelled inconsistently -- aggregating raw variants splits "
            "an entity's weight and corrupts rankings. Null-equivalent placeholders "
            "(unknown/untitled/n.a./digits-only) are never merged: they are missing data, "
            "not entities. JOIN through the mapping (ON t.col = m.value, GROUP BY "
            "m.canonical) and sanity-check the reported cluster count before trusting "
            f"downstream numbers. Inputs over {_MAX_DISTINCT} distinct values are refused; "
            "narrow with WHERE first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Source table or read_parquet('<handle>')."},
                "column": {"type": "string", "description": "The text column whose variants to cluster."},
                "output_table": {
                    "type": "string",
                    "description": "Name for the mapping table (value, canonical, cluster_size).",
                },
                "where": {"type": "string", "description": "Optional SQL predicate to filter source rows first."},
                "threshold": {
                    "type": "number",
                    "description": f"Fuzzy-merge similarity in [0.8, 1.0]; default {_DEFAULT_THRESHOLD}. "
                    "Higher is stricter (fewer merges).",
                },
            },
            "required": ["table", "column", "output_table"],
        }

    async def execute(
        self,
        table: str,
        column: str,
        output_table: str,
        where: str | None = None,
        threshold: float | None = None,
    ) -> str:
        import asyncio

        ratio = _DEFAULT_THRESHOLD if threshold is None else max(0.8, min(1.0, float(threshold)))
        predicate = f" WHERE {where}" if where else ""
        source = (
            f'SELECT CAST("{column}" AS VARCHAR) AS v, COUNT(*) AS n FROM {table}{predicate} '
            f'WHERE "{column}" IS NOT NULL GROUP BY 1'
        )
        if where:
            source = (
                f'SELECT CAST("{column}" AS VARCHAR) AS v, COUNT(*) AS n FROM {table} '
                f'WHERE ({where}) AND "{column}" IS NOT NULL GROUP BY 1'
            )
        try:
            ensure_read_only(source)
        except SqlRejectedError as exc:
            return f"[RESOLVE_ENTITIES REJECTED] {exc}"
        resp = await asyncio.to_thread(self._session.sql, source, preview=_MAX_DISTINCT + 1)
        if not resp.get("ok"):
            return f"[RESOLVE_ENTITIES ERROR] reading distinct values failed: {resp.get('error')}"
        pairs = [(str(v), int(n)) for v, n in (resp.get("rows") or [])]
        if len(pairs) > _MAX_DISTINCT:
            return (
                f"[RESOLVE_ENTITIES REFUSED] {len(pairs)} distinct values exceed the {_MAX_DISTINCT} cap. "
                "Filter the source down first (WHERE on a relevant slice), then re-run."
            )
        if not pairs:
            return "[RESOLVE_ENTITIES] source query returned 0 values; nothing to resolve."

        clusters = _cluster(pairs, ratio)
        out_rows = []
        merged_clusters = 0
        examples: list[str] = []
        for canonical, members in clusters:
            size = len(members)
            if size > 1:
                merged_clusters += 1
                if len(examples) < 5:
                    variants = ", ".join(repr(m) for m, _ in members[:4] if m != canonical)
                    if variants:
                        examples.append(f"{canonical!r} <- {variants}")
            for value, _count in members:
                out_rows.append([value, canonical, str(size)])

        put = await asyncio.to_thread(
            self._session.put_rows, output_table, ["value", "canonical", "cluster_size"], out_rows
        )
        if not put.get("ok"):
            return f"[RESOLVE_ENTITIES ERROR] materializing {output_table} failed: {put.get('error')}"

        placeholders = sum(1 for v, _ in pairs if _is_placeholder(_normalize(v)) or not _normalize(v))
        placeholder_note = (
            f"\n{placeholders} value(s) look like null placeholders (unknown/untitled/n.a./digits-only); "
            "they were never merged and are missing data, not entities -- exclude them from rankings."
            if placeholders
            else ""
        )
        merge_preview = ("\nexample merges: " + "; ".join(examples)) if examples else ""
        return (
            f"materialized {output_table}(value, canonical, cluster_size): "
            f"{len(pairs)} distinct values -> {len(clusters)} entities "
            f"({merged_clusters} clusters merged 2+ variants, threshold {ratio})."
            f"{placeholder_note}{merge_preview}\n"
            f"Next: JOIN ON t.\"{column}\" = {output_table}.value and GROUP BY {output_table}.canonical. "
            "Sanity-check the entity count and the top clusters before aggregating: placeholder-looking "
            "winners are missing data, and if unrelated values merged, raise threshold and re-run."
        )


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = text.split()
    return " ".join(sorted(tokens))


# Null-equivalent placeholder spellings (pandas na_values plus the catalog staples).
# A placeholder repeated across records is missing data, not one entity: merging
# every 'unknown' would fabricate a giant top-ranked pseudo-entity out of thin air.
_NA_VALUES = (
    "", "n/a", "na", "n.a.", "null", "none", "nil", "nan", "n.d.", "nd", "tbd", "tba",
    "unknown", "untitled", "[untitled]", "unnamed", "no title", "not available",
    "missing", "misc", "other", "others", "various", "unk", "unk.", "[silence]", "-",
)
_NA_KEYS = frozenset(_normalize(v) for v in _NA_VALUES)

_CONTAIN_MIN_TOKENS = 4
_CONTAIN_BUCKET_CAP = 50


def _is_placeholder(key: str) -> bool:
    """Normalized keys that denote missing data (or are digits-only labels)."""
    if key in _NA_KEYS:
        return True
    tokens = key.split()
    return bool(tokens) and all(t.isdigit() for t in tokens)


def _cluster(pairs: list[tuple[str, int]], threshold: float) -> list[tuple[str, list[tuple[str, int]]]]:
    """Union-find clustering: exact normalized blocks + sorted-neighborhood fuzzy merge."""
    values = [v for v, _ in pairs]
    parent = list(range(len(values)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    # Values whose normalization collapses to nothing (pure punctuation, whitespace)
    # or to a null-equivalent placeholder must not share a block: an empty key would
    # merge '***' with '...', and merging every 'unknown' fabricates one giant
    # pseudo-entity out of missing data. Keep them distinct via a sentinel key that
    # the merge passes below also skip.
    raw_keys = [_normalize(v) for v in values]
    normalized = [
        key if key and not _is_placeholder(key) else f"\x00{v}"
        for key, v in zip(raw_keys, values)
    ]
    blocks: dict[str, int] = {}
    for i, key in enumerate(normalized):
        if key in blocks:
            union(blocks[key], i)
        else:
            blocks[key] = i

    # Sorted-neighborhood pass over the distinct normalized keys: near-identical
    # keys sort next to each other, so a small window catches typo-level variants
    # without an O(n^2) sweep.
    keys = sorted(k for k in blocks if not k.startswith("\x00"))
    for idx, key in enumerate(keys):
        for other in keys[idx + 1 : idx + 1 + _NEIGHBOR_WINDOW]:
            if abs(len(key) - len(other)) / max(len(key), len(other)) > (1 - threshold):
                continue
            if difflib.SequenceMatcher(None, key, other).ratio() >= threshold:
                union(blocks[key], blocks[other])

    # Containment pass for decorated variants: the same entity often reappears with
    # extra tokens bolted on (an album after the title, a numeric prefix, the author
    # in front of the name). Edit distance never reaches those, and decoration
    # changes the sort position, so neighborhood misses them too. Block on shared
    # non-numeric tokens and merge when one token set contains the other, guarded so
    # short names ('love' in 'love story') and heavy decoration cannot merge: the
    # contained set needs >= 4 tokens and may at most double.
    token_sets = {key: frozenset(t for t in key.split() if not t.isdigit()) for key in keys}
    token_buckets: dict[str, list[str]] = {}
    for key, tokens in token_sets.items():
        for token in tokens:
            if len(token) >= 4:
                token_buckets.setdefault(token, []).append(key)
    for bucket in token_buckets.values():
        if len(bucket) > _CONTAIN_BUCKET_CAP:
            continue
        for i, key in enumerate(bucket):
            for other in bucket[i + 1 :]:
                a, b = token_sets[key], token_sets[other]
                small, big = (a, b) if len(a) <= len(b) else (b, a)
                if len(small) >= _CONTAIN_MIN_TOKENS and small <= big and len(big) - len(small) <= len(small):
                    union(blocks[key], blocks[other])

    groups: dict[int, list[tuple[str, int]]] = {}
    for i, pair in enumerate(pairs):
        groups.setdefault(find(i), []).append(pair)
    clusters = []
    for members in groups.values():
        # Most frequent spelling wins; on ties prefer the least decorated one
        # (fewest tokens), so the canonical is the bare title, not '006-Title'.
        members.sort(key=lambda m: (-m[1], len(_normalize(m[0]).split()), m[0]))
        canonical = members[0][0]
        clusters.append((canonical, members))
    clusters.sort(key=lambda c: c[0])
    return clusters


def make_resolve_entities_tool(ctx: PluginContext) -> Tool:
    from pathlib import Path

    from raven.plugin.data_agent.session import get_session, parse_sources

    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return ResolveEntitiesTool(session)
