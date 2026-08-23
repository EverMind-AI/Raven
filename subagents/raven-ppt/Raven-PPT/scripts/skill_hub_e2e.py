"""Skill Hub e2e probe.

Exercises the full skill_hub client surface against a real Hub:
``search → get → install`` plus a SkillsSegmentBuilder dry-run that
hits the rewriter → router → pre-gate body hydrate → gate → post-gate
refs hydrate pipeline end-to-end.

Run from the project root:

    .venv/bin/python scripts/skill_hub_e2e.py \\
        --endpoint https://dev-skillhub.aws.evermind.ai \\
        --query "generate pdf report"

Requires network reach to the Hub endpoint. In containers that route
through a Squid proxy refusing CONNECT to ``*.evermind.ai``, this
script will fail at the first ``search`` call with a network error —
run from a machine with direct internet or whitelist the host.

Skips LLM-driven stages (rewriter / gate) unless ``--provider <id>``
is supplied; without a provider, the segment builder runs in
no-rewriter / no-gate mode (router + hydrate only) so the script is
useful even when no LLM credentials are wired.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap
from pathlib import Path

# Allow `python scripts/skill_hub_e2e.py` without `-m`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raven.memory_engine.skill_forge import (
    HubSkillSource,
    SkillForgeRouter,
)
from raven.skill_hub import SkillHubClient


async def probe_client(endpoint: str, api_key: str | None, query: str, limit: int) -> None:
    """Stage 1 — direct SkillHubClient roundtrip (search → get → install)."""
    print("\n=== Stage 1: SkillHubClient direct ===")
    client = SkillHubClient(endpoint, api_key=api_key, timeout_s=10.0)
    try:
        print(f"GET /openapi/v1/skills?q={query!r}&limit={limit} ...")
        items = await client.search(query, limit=limit)
        print(f"  → {len(items)} item(s)")
        for it in items[:3]:
            print(f"    - {it.get('name')!r:40s}  id={it.get('id')}  q={it.get('quality_score')}")

        if not items:
            print("  (empty result — nothing more to probe)")
            return

        top = items[0]
        print(f"\nGET /openapi/v1/skills/{top['id']} ...")
        meta = await client.get(top["id"])
        body = meta.get("skill_md", "") or ""
        print(f"  → slug={meta.get('slug')}  version={meta.get('version')}  body_chars={len(body)}")
        if body:
            preview = body.splitlines()[:3]
            print("  body head:")
            for line in preview:
                print(f"    {line[:80]}")

        print(f"\ninstall({top['id']}, prefetched_meta=<from get>) ...")
        installed = await client.install(top["id"], prefetched_meta=meta)
        print(f"  → dir={installed.get('dir')}")
        print(f"    scripts_dir={installed.get('scripts_dir')}")
        bundle_dir = Path(installed["dir"]) if installed.get("dir") else None
        if bundle_dir and bundle_dir.is_dir():
            entries = sorted(p.name for p in bundle_dir.iterdir())
            print(f"    files: {entries[:10]}{'...' if len(entries) > 10 else ''}")
    finally:
        await client.aclose()


async def probe_hub_source(endpoint: str, api_key: str | None, query: str, k: int) -> None:
    """Stage 2 — HubSkillSource → RouterHit mapping (Tier 0 catalog)."""
    print("\n=== Stage 2: HubSkillSource → RouterHit ===")
    client = SkillHubClient(endpoint, api_key=api_key, timeout_s=10.0)
    try:
        src = HubSkillSource(client)
        hits = await src.search(query, [], k=k)
        print(f"  → {len(hits)} RouterHit(s); body empty (catalog only):")
        for h in hits[:5]:
            assert h.content == "", "HubSkillSource must emit empty content"
            print(f"    {h.qualified_id}  name={h.name!r}  score={h.score:.3f}")
    finally:
        await client.aclose()


async def probe_segment(
    endpoint: str,
    api_key: str | None,
    query: str,
    k: int,
) -> None:
    """Stage 3 — SkillsSegmentBuilder body hydrate (no rewriter / gate)."""
    print("\n=== Stage 3: SkillsSegmentBuilder hydrate (no LLM) ===")
    from raven.context_engine.base import AssemblyContext
    from raven.context_engine.segments.skills import SkillsSegmentBuilder
    from raven.memory_engine.base import TokenBudget

    client = SkillHubClient(endpoint, api_key=api_key, timeout_s=10.0)
    try:
        hub_source = HubSkillSource(client)
        router = SkillForgeRouter([hub_source])
        builder = SkillsSegmentBuilder(
            router,
            skill_top_k=k,
            hub_client=client,
        )
        ctx = AssemblyContext(
            session_key="e2e",
            current_message=query,
            media=None,
            channel=None,
            chat_id=None,
            session_messages=[],
            budget=TokenBudget(
                context_length=200_000,
                reserved_output=8000,
                reserved_tools=4000,
                reserved_system=4000,
                available_history=184_000,
            ),
        )
        seg = await builder.build(ctx)
        ids = seg.meta.get("injected_skill_ids", [])
        print(f"  → injected_skill_ids: {ids}")
        print(f"    text length: {len(seg.text)} chars")
        print("    text preview:")
        for line in textwrap.indent(seg.text[:1200], "      ").splitlines()[:25]:
            print(line)
    finally:
        await client.aclose()


async def main() -> None:
    p = argparse.ArgumentParser(description="Skill Hub e2e probe")
    p.add_argument("--endpoint", required=True, help="Hub base URL")
    p.add_argument("--api-key", default=None, help="Bearer token (optional)")
    p.add_argument("--query", default="generate pdf report")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--k", type=int, default=3, help="top-K for hub source / segment")
    p.add_argument(
        "--stage",
        choices=["1", "2", "3", "all"],
        default="all",
        help="Which stage to run (1=client, 2=hub_source, 3=segment).",
    )
    args = p.parse_args()

    if args.stage in {"1", "all"}:
        await probe_client(args.endpoint, args.api_key, args.query, args.limit)
    if args.stage in {"2", "all"}:
        await probe_hub_source(args.endpoint, args.api_key, args.query, args.k)
    if args.stage in {"3", "all"}:
        await probe_segment(args.endpoint, args.api_key, args.query, args.k)

    print("\n✓ all stages completed")


if __name__ == "__main__":
    asyncio.run(main())
