"""One-time script to pre-compute task category embeddings.

Run once before enabling routing:
    python -m raven.routing.generate_embeddings --api-key sk-or-...

Writes embedding_data.json alongside this file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from raven.routing.classifier import EMBEDDING_MODEL, fetch_embedding

# Human-readable descriptions for each of the 23 PinchBench task categories.
# These match the intent of each task — the same descriptions EcoClaw uses
# as reference points for cosine similarity matching.
TASK_DESCRIPTIONS: dict[str, str] = {
    "task_00_sanity": "Basic sanity check, simple greeting, hello world, or trivial question",
    "task_01_calendar": "Calendar management, scheduling meetings, checking availability, date and time planning",
    "task_02_stock": "Stock market data, financial prices, trading, portfolio management, investment analysis",
    "task_03_blog": "Writing blog posts, content creation, articles, long-form writing, publishing",
    "task_04_weather": "Weather forecast, current conditions, tool use, API calling, external data retrieval",
    "task_05_summary": "Summarizing documents, text summarization, condensing information, TL;DR",
    "task_06_events": "Event planning, event management, organizing activities, social events",
    "task_07_email": "Composing emails, email management, drafting messages, professional communication",
    "task_08_memory": "Memory retrieval, recalling past information, remembering context, second brain queries",
    "task_09_files": "File management, reading files, writing files, directory operations, file search",
    "task_10_workflow": "Workflow automation, multi-step processes, task orchestration, pipeline execution",
    "task_11_clawdhub": "OpenClaw/ClawdHub specific features, agent marketplace, finding and using agents",
    "task_12_skill_search": "Searching for skills, finding capabilities, discovering tools and plugins",
    "task_13_image_gen": "Image generation, creating pictures, visual content, DALL-E, Stable Diffusion",
    "task_14_humanizer": "Rewriting AI-generated text to sound human, paraphrasing, tone adjustment",
    "task_15_daily_summary": "Daily briefing, morning summary, news digest, daily report generation",
    "task_16_email_triage": "Email triage, inbox management, prioritizing emails, flagging important messages",
    "task_17_email_search": "Searching emails, finding specific messages, email query, inbox search",
    "task_18_market_research": "Market research, competitor analysis, industry trends, business intelligence",
    "task_19_spreadsheet_summary": "Spreadsheet analysis, data tables, Excel/CSV processing, numerical summaries",
    "task_20_eli5_pdf_summary": "Explaining PDF documents simply, ELI5, document comprehension, simplifying complex text",
    "task_21_openclaw_comprehension": "Understanding OpenClaw documentation, system comprehension, feature questions",
    "task_22_second_brain": "Personal knowledge management, second brain, note-taking, knowledge retrieval, Obsidian",
}

OUTPUT_PATH = Path(__file__).parent / "embedding_data.json"


async def generate(api_key: str) -> None:
    print(f"Generating embeddings for {len(TASK_DESCRIPTIONS)} task categories...")
    print(f"Model: {EMBEDDING_MODEL}")

    tasks = []
    for task_id, description in TASK_DESCRIPTIONS.items():
        print(f"  Embedding {task_id}...", end=" ", flush=True)
        vec = await fetch_embedding(description, api_key)
        tasks.append(
            {
                "task_id": task_id,
                "description": description,
                "embedding": vec,
            }
        )
        print(f"done ({len(vec)}d)")

    output = {
        "model": EMBEDDING_MODEL,
        "dimensions": len(tasks[0]["embedding"]) if tasks else 0,
        "tasks": tasks,
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved to {OUTPUT_PATH}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate task category embeddings")
    parser.add_argument("--api-key", required=True, help="OpenRouter API key")
    args = parser.parse_args()

    asyncio.run(generate(args.api_key))
