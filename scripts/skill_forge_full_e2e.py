"""SkillForge full pipeline e2e — rewriter + router + gate against a real LLM.

Exercises the complete ``SkillsSegmentBuilder.build`` pipeline:

  1. **QueryRewriter** — judges need_retrieval + rewrites query
  2. **SkillForgeRouter** (LocalSkillSource only — Hub unreachable from container)
  3. **Pre-gate body hydrate** — no-op for local hits (already have body)
  4. **LLMGateFilter** — selects 0..N relevant skills with reasoning + tool-aware
  5. **Post-gate refs hydrate** — resolves {baseDir} refs to absolute paths
  6. Render

Uses OpenRouter via litellm_provider. Reads ``OPENROUTER_API_KEY`` from
env or ``.env`` at the project root.

Run::

    .venv/bin/python scripts/skill_forge_full_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

# Repo root on sys.path so we can run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Load .env if present (simple key=value loader, no python-dotenv dep).
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(_REPO_ROOT / ".env")

from raven.context_engine.base import AssemblyContext
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.memory_engine.base import TokenBudget
from raven.memory_engine.skill_forge import (
    LLMGateFilter,
    LocalSkillSource,
    QueryRewriter,
    SkillForgeRouter,
)
from raven.memory_engine.skill_local.local_pool import LocalPool
from raven.memory_engine.skill_local.registry import SkillRegistry
from raven.providers.litellm_provider import LiteLLMProvider


# Fixture skills — same shape as benchmarks/skill_evals queries, with
# one having a bundled reference file to exercise refs hydrate.
_SKILLS = [
    ("pdf-gen", "Generate PDF reports using Python reportlab",
     "Use reportlab to build PDF documents. Supports tables, images, charts.\n\n"
     "See [Configuration](references/CONFIG.md) for advanced options."),
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


def _build_workspace(tmpdir: Path) -> Path:
    skills_root = tmpdir / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for name, desc, body in _SKILLS:
        d = skills_root / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n"
            f"# {name}\n\n{body}\n",
            encoding="utf-8",
        )
    # For pdf-gen, also create the referenced bundled file so refs hydrate
    # rewrites it to an absolute path in the prompt.
    (skills_root / "pdf-gen" / "references").mkdir()
    (skills_root / "pdf-gen" / "references" / "CONFIG.md").write_text(
        "# PDF Config\n\nFonts, margins, page size.\n", encoding="utf-8",
    )
    return tmpdir


# Tasks designed to exercise three rewriter / gate decision shapes:
_TASKS = [
    {
        "label": "single-skill-needed",
        "query": "I need to programmatically generate a PDF report from CSV data with python",
        "expect": ["pdf-gen"],
    },
    {
        "label": "two-skills-needed",
        "query": "Build a CSV cleanup pipeline and write the cleaned data to an excel spreadsheet",
        "expect_any_of": [{"csv-clean", "excel-ops"}],
    },
    {
        "label": "no-retrieval-needed",
        "query": "Hi! How are you today? Just saying hello.",
        "expect_no_retrieval": True,
    },
]


def _stub_tool_defs() -> list[dict]:
    """A small set of "agent tools" — read_file / exec are typical."""
    return [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "write_file"}},
        {"type": "function", "function": {"name": "exec"}},
    ]


async def run_one(builder: SkillsSegmentBuilder, task: dict) -> None:
    print(f"\n──── {task['label']!r} ────")
    print(f"  Query: {task['query']!r}")

    ctx = AssemblyContext(
        session_key=f"e2e-{task['label']}",
        current_message=task["query"],
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
    print(f"  injected_skill_ids: {ids}")
    print(f"  rewriter_skipped: {seg.meta.get('rewriter_skipped', False)}")
    if seg.text:
        head = "\n".join(seg.text.splitlines()[:8])
        print("  segment text head:")
        print(textwrap.indent(head, "    "))
    else:
        print("  segment text: (empty)")

    # Outcome check
    if task.get("expect_no_retrieval"):
        if seg.meta.get("rewriter_skipped") and not ids:
            print("  ✓ rewriter correctly short-circuited (no retrieval)")
        else:
            print("  ✗ expected no retrieval, but got skills:", ids)
    elif task.get("expect"):
        want = task["expect"]
        names = [i.split("/", 1)[-1] for i in ids]
        if all(w in names for w in want):
            print(f"  ✓ gate picked expected skill(s) {want}")
        else:
            print(f"  ✗ expected {want}, got {names}")
    elif task.get("expect_any_of"):
        names = set(i.split("/", 1)[-1] for i in ids)
        for choice in task["expect_any_of"]:
            if names & choice:
                print(f"  ✓ gate picked some of {choice} → {names & choice}")
                break
        else:
            print(f"  ✗ expected any of {task['expect_any_of']}, got {names}")


async def main() -> int:
    if "OPENROUTER_API_KEY" not in os.environ:
        print("ERROR: OPENROUTER_API_KEY not set. Source .env first.")
        return 1

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        workspace = _build_workspace(Path(tmp))
        registry = SkillRegistry(workspace, builtin_skills_dir=workspace / "_unused")
        pool = LocalPool(registry)
        local_source = LocalSkillSource(pool=pool, registry=registry)
        router = SkillForgeRouter([local_source])

        provider = LiteLLMProvider(
            api_key=os.environ["OPENROUTER_API_KEY"],
            api_base="https://openrouter.ai/api/v1",
            default_model="openrouter/anthropic/claude-haiku-4.5",
        )
        rewriter = QueryRewriter(provider, max_tokens=2048)
        gate = LLMGateFilter(
            provider,
            max_select=2,
            legacy_top_k=5,
            max_tokens=4096,
        )
        builder = SkillsSegmentBuilder(
            router,
            skill_top_k=5,
            rewriter=rewriter,
            gate=gate,
            gate_pool_size=10,
            get_tool_definitions=_stub_tool_defs,
        )

        for task in _TASKS:
            await run_one(builder, task)

    print("\n✓ e2e completed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
