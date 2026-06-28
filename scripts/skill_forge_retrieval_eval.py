"""SkillForge retrieval eval — exercises the new pipeline on a fixture
workspace built from a subset of ``benchmarks/skill_evals/queries.jsonl``.

For each query in the fixture set:
  1. Build a temp workspace with SKILL.md files that should match.
  2. Run :class:`SkillForgeRouter` (Local-only — Hub unreachable from
     this container) and capture the top-K ranking.
  3. Optionally run :class:`SkillsSegmentBuilder` end-to-end (rewriter
     disabled, gate disabled — no LLM provider configured) to verify
     that body + ref hydrate produce a valid prompt segment.

Outputs:
  - per-query top-K with names + scores
  - top1_match / top3_match rate against ``must_contain_any``

No LLM calls are issued. No network. Self-contained.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven.context_engine.base import AssemblyContext
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.memory_engine.base import TokenBudget
from raven.memory_engine.skill_forge import (
    LocalSkillSource,
    SkillForgeRouter,
)
from raven.memory_engine.skill_local.local_pool import LocalPool
from raven.memory_engine.skill_local.registry import SkillRegistry


# Mock skills that mirror real ones an OpenSpace skill library would
# contain. Each entry: (name, description-line, body keywords).
_SKILLS = [
    ("pdf-gen", "Generate PDF reports using Python reportlab",
     "Use reportlab to build PDF documents. Supports tables, images, charts."),
    ("pptx-builder", "Create PowerPoint presentations programmatically via python-pptx",
     "python-pptx library for building .pptx slide decks. Add titles, bullets, images."),
    ("docx-md-converter", "Convert .docx documents to Markdown using python-docx",
     "Read Word documents with python-docx and emit markdown text."),
    ("excel-ops", "Manipulate Excel spreadsheets with openpyxl",
     "openpyxl handles xlsx spreadsheet read/write, formulas, formatting."),
    ("react-hooks", "Patterns for React custom hooks for state management",
     "Build reusable hooks: useReducer, useContext, useMemo composition."),
    ("git-resolver", "Resolve git merge conflicts via three-way merge",
     "Parse <<<<<<< / ======= / >>>>>>> markers and reconcile."),
    ("kubernetes-debug", "Debug Kubernetes pods and services using kubectl",
     "kubectl logs / describe / exec for pod-level troubleshooting."),
    ("sql-explain", "Analyze slow SQL queries with EXPLAIN ANALYZE",
     "PostgreSQL / MySQL EXPLAIN output interpretation."),
    ("regex-builder", "Build and test regular expressions",
     "Compose regex patterns step-by-step, test against samples."),
    ("csv-clean", "Clean and normalize CSV data files",
     "pandas-based CSV cleanup: dedup, type coercion, missing value fill."),
]


def _build_fixture_workspace(tmpdir: Path) -> Path:
    """Write each mock skill as an OpenSpace-style SKILL.md tree."""
    skills_root = tmpdir / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for name, desc, body in _SKILLS:
        d = skills_root / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n"
            f"# {name}\n\n{desc}\n\n## Usage\n\n{body}\n",
            encoding="utf-8",
        )
    return tmpdir


def _build_router(workspace: Path) -> tuple[SkillForgeRouter, SkillRegistry]:
    """Local-only router (no hub, no everos)."""
    registry = SkillRegistry(workspace, builtin_skills_dir=workspace / "_unused")
    pool = LocalPool(registry)
    src = LocalSkillSource(pool=pool, registry=registry)
    return SkillForgeRouter([src]), registry


def _matches(name: str, content: str, needles: list[str]) -> bool:
    hay = f"{name} {content}".lower()
    return any(n.lower() in hay for n in needles)


async def _retrieval_test(router: SkillForgeRouter, queries: list[dict], top_k: int) -> dict:
    """Pure-router retrieval — score each query against must_contain_any."""
    print("\n=== Stage 1: Retrieval-only (SkillForgeRouter.select) ===\n")
    top1 = top3 = total = 0
    for q in queries:
        hits = await router.select(
            query=q["query"], history=[], k=top_k,
        )
        names = [h.name for h in hits]
        must = q.get("must_contain_any", [])
        is_top1 = bool(hits) and _matches(hits[0].name, hits[0].content, must)
        first_match = None
        for i, h in enumerate(hits, 1):
            if _matches(h.name, h.content, must):
                first_match = i
                break
        is_top3 = first_match is not None and first_match <= 3
        verdict = "✓ top-1" if is_top1 else ("△ top-3" if is_top3 else "✗ miss")
        print(f"  [{q['id']}] {verdict}  query={q['query']!r:50s}")
        print(f"         top-{top_k}: {names}")
        top1 += int(is_top1)
        top3 += int(is_top3)
        total += 1
    return {"top1": top1, "top3": top3, "total": total}


async def _segment_test(
    workspace: Path, router: SkillForgeRouter, queries: list[dict], top_k: int,
) -> None:
    """Full SkillsSegmentBuilder pipeline (no rewriter / no gate)."""
    print("\n=== Stage 2: SkillsSegmentBuilder full pipeline ===\n")
    builder = SkillsSegmentBuilder(router, skill_top_k=top_k)
    for q in queries[:3]:
        ctx = AssemblyContext(
            session_key=f"eval-{q['id']}",
            current_message=q["query"],
            media=None, channel=None, chat_id=None,
            session_messages=[],
            budget=TokenBudget(
                context_length=200_000, reserved_output=8000,
                reserved_tools=4000, reserved_system=4000,
                available_history=184_000,
            ),
        )
        seg = await builder.build(ctx)
        ids = seg.meta.get("injected_skill_ids", [])
        print(f"  [{q['id']}] injected={ids}")
        # Print first 2 header lines of the rendered segment
        head_lines = "\n".join(seg.text.splitlines()[:6])
        print(textwrap.indent(head_lines, "    "))
        print()


async def main() -> int:
    queries_path = (
        Path(__file__).resolve().parents[1]
        / "benchmarks" / "skill_evals" / "queries.jsonl"
    )
    queries = [
        json.loads(line)
        for line in queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Use a curated subset that matches our fixture skills.
    target_ids = {"q001", "q002", "q003", "q004", "q005"}
    subset = [q for q in queries if q["id"] in target_ids]

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        workspace = _build_fixture_workspace(Path(tmp))
        router, _ = _build_router(workspace)

        stats = await _retrieval_test(router, subset, top_k=5)
        await _segment_test(workspace, router, subset, top_k=3)

    print("\n=== Retrieval rates ===")
    n = stats["total"]
    print(f"  top1: {stats['top1']}/{n} = {stats['top1']/n*100:.0f}%")
    print(f"  top3: {stats['top3']}/{n} = {stats['top3']/n*100:.0f}%")
    return 0 if stats["top1"] == n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
