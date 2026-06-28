"""Retrieval regression eval — scores SkillForge dense retrieval + reranker
on the 50-query benchmark (``queries.jsonl``).

Adapted to the post-merge SkillForge API:
    - SkillService.select(message) → list[SkillMeta] of top-K hits
      (each has .name, .source, .description, .raw_frontmatter)
    - SkillRegistry.get(name) → SkillMeta for description / frontmatter
      (only needed for legacy callers; select() now returns full metas)

Metrics:
    top1_keyword_rate   fraction of queries where top-1 hit's name+desc
                        contains any of ``must_contain_any``.
    topk_keyword_rate   same but anywhere in top-k.
    top1_category_rate  fraction where top-1's frontmatter.category equals
                        ``expected_category`` (skipped if frontmatter has no
                        ``category`` field — read-only registry can't
                        infer one).
    mrr                 mean reciprocal rank of first keyword match.

Run:
    python -m benchmarks.skill_evals.run_eval \\
        --workspace ~/.raven \\
        --top-k 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from raven.memory_engine.skill_local.registry import SkillRegistry
from raven.memory_engine.skill_local.retrieval import Retrieval, RetrievalConfig
from raven.memory_engine.skill_local.types import SkillMeta


def _load_queries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _enrich(name: str, registry: SkillRegistry) -> dict:
    """name → flat dict {name, description, category} for keyword/category matching."""
    meta: SkillMeta | None = registry.get(name)
    if meta is None:
        return {"name": name, "description": "", "category": ""}
    cat = ""
    fm = meta.raw_frontmatter or {}
    if isinstance(fm, dict):
        cat = str(fm.get("category", "") or "")
    return {"name": meta.name, "description": meta.description or "", "category": cat}


def _matches(hit: dict, must_contain_any: list[str]) -> bool:
    if not must_contain_any:
        return False
    haystack = f"{hit.get('name', '')} {hit.get('description', '')}".lower()
    return any(needle.lower() in haystack for needle in must_contain_any)


def _first_match_rank(hits: list[dict], must_contain_any: list[str]) -> int:
    for i, h in enumerate(hits, 1):
        if _matches(h, must_contain_any):
            return i
    return 0


def run_eval(
    registry: SkillRegistry,
    retrieval: Retrieval,
    queries_path: Path,
    top_k: int,
    verbose: bool = False,
) -> dict:
    queries = _load_queries(queries_path)

    per_query: list[dict] = []
    cat_observed = 0  # how many queries had a frontmatter category to compare

    for q in queries:
        t0 = time.time()
        scored = retrieval.search(q["query"], top_k=top_k)
        latency = time.time() - t0

        hits = [_enrich(s.name, registry) for s in scored]
        must = q.get("must_contain_any", [])
        rank = _first_match_rank(hits, must)
        top1_kw = bool(hits and _matches(hits[0], must))
        topk_kw = rank > 0

        # Category metric only when both expected and observed exist
        expected_cat = q.get("expected_category", "")
        top1_cat: bool | None = None
        if hits and expected_cat:
            obs = hits[0]["category"]
            if obs:
                cat_observed += 1
                top1_cat = obs == expected_cat

        per_query.append({
            "id": q["id"],
            "query": q["query"],
            "rank": rank,
            "top1_kw": top1_kw,
            "topk_kw": topk_kw,
            "top1_cat": top1_cat,
            "latency_ms": int(latency * 1000),
            "top1_name": hits[0]["name"] if hits else None,
        })

    n = len(per_query) or 1
    cat_attempts = sum(1 for r in per_query if r["top1_cat"] is not None)
    cat_hits = sum(1 for r in per_query if r["top1_cat"] is True)

    summary = {
        "n_queries": len(per_query),
        "top_k": top_k,
        "top1_keyword_rate": round(sum(1 for r in per_query if r["top1_kw"]) / n, 3),
        f"top{top_k}_keyword_rate": round(sum(1 for r in per_query if r["topk_kw"]) / n, 3),
        "top1_category_rate": (
            round(cat_hits / cat_attempts, 3) if cat_attempts else None
        ),
        "category_coverage": f"{cat_attempts}/{n}",
        "mrr": round(
            sum(1.0 / r["rank"] for r in per_query if r["rank"] > 0) / n, 3
        ),
        "latency_ms_p50": sorted(r["latency_ms"] for r in per_query)[n // 2],
        "latency_ms_avg": round(
            sum(r["latency_ms"] for r in per_query) / n, 1
        ),
    }

    if verbose:
        # Per-category roll-up
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for r, q in zip(per_query, queries):
            by_cat[q.get("expected_category", "?")].append(r)
        print("\nPer expected_category:")
        for cat, rs in sorted(by_cat.items()):
            n_c = len(rs) or 1
            print(
                f"  {cat:14s}  n={len(rs):2d}  "
                f"top1_kw={sum(1 for r in rs if r['top1_kw'])/n_c:.2f}  "
                f"mrr={sum(1.0/r['rank'] for r in rs if r['rank']>0)/n_c:.2f}"
            )
        print("\nMisses (top1_kw=False):")
        for r in per_query:
            if not r["top1_kw"]:
                print(f"  {r['id']}  '{r['query']}'  → top1={r['top1_name']!r}")

    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", type=Path, required=True,
                    help="Path to raven workspace (contains .cache/skill_index/ "
                         "or .cache/skills.db when --store-path is set)")
    ap.add_argument("--builtin", type=Path, default=None,
                    help="Optional builtin skills dir")
    ap.add_argument("--queries", type=Path,
                    default=Path(__file__).parent / "queries.jsonl",
                    help="Eval queries (default: bundled queries.jsonl)")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--store-path", type=Path, default=None,
                    help="SqliteStore DB path. Defaults to "
                         "<workspace>/.cache/skills.db.")
    args = ap.parse_args(argv)

    # Default store path discovery: if no --store-path was given, look
    # for ``<workspace>/.cache/skills.db``. Either way, retrieval needs
    # a SqliteStore — there's no legacy .pt fallback anymore.
    store_path = args.store_path
    if store_path is None:
        store_path = args.workspace / ".cache" / "skills.db"

    if not store_path.exists():
        print(
            f"[ERROR] No store at {store_path}. "
            "Build it first via `raven skill rebuild-index` "
            "(or run benchmarks/skill_evals/fixtures/build_mock_library.py for a fake DB).",
            file=sys.stderr,
        )
        return 2

    from raven.memory_engine.skill_local.store import SqliteSkillRegistry, SqliteStore
    store = SqliteStore(store_path)
    registry = SqliteSkillRegistry(args.workspace, store, args.builtin)
    retrieval = Retrieval(registry, RetrievalConfig(store=store))
    if not retrieval.load_cache():
        print(
            f"[ERROR] No compatible embeddings in {store_path}. "
            "Run `raven skill rebuild-index` after import-files.",
            file=sys.stderr,
        )
        store.close()
        return 2

    summary = run_eval(
        registry, retrieval, args.queries, args.top_k, verbose=args.verbose,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
