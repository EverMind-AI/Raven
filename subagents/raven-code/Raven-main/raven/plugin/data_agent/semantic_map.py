"""semantic_map: a batch LLM extraction/classification operator on the plane.

The one tool in the plugin that calls a model. It closes the methodology gap
the DAB paper singles out (agents regex their way through free text and lose
every task that needs reading): take one text column, apply a natural-language
extraction or classification task to every row in batches, and materialize the
results as an ordinary table the next SQL query can join against.

Cost guardrails (design doc decision record #3): a hard row cap that tells the
model to filter first, a cost echo in every reply, ~100-row batches, and one
retry per batch. The model handle comes from ``ServiceLocator.provider`` --
when the host wired no provider the factory declines contribution, so the
tool face stays honest about what it can do.

Consistency layer (R2): batches run concurrently under a semaphore, and
closed-label tasks are annotated by two independent passes with a third
tie-break pass on disagreements -- majority wins, unresolved rows become NULL
and the disagreement rate is surfaced so the caller knows how stable the
labeling was. Open extraction stays single-pass: free-text answers have no
well-defined vote equality.

Adaptive voting (R3): the second pass first runs on a small sample of batches.
Only when the sampled disagreement rate crosses a threshold does the full
second pass run; consistent tasks keep near-single-pass cost, which is what
lets whole-corpus classification finish inside a task timeout. Sampled batches
always keep their tie-breaks either way.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only

_MAX_ROWS = 5_000
# 100-row batches: the L5 ablation showed 150-row batches degrade annotation
# quality (extraction-heavy datasets dropped ~0.03 stratified); throughput
# comes from batch concurrency instead, which leaves quality untouched.
_BATCH_SIZE = 100
_MAX_ITEM_CHARS = 2_000
_MAX_RESPONSE_TOKENS = 8_000
_CONCURRENT_BATCHES = 24
_VOTE_SAMPLE_BATCHES = 3
_VOTE_ESCALATE_RATE = 0.01

_VARIANT_PREAMBLE = {
    0: "You are a batch data annotator.",
    1: (
        "You are a second, independent batch annotator. Read every text afresh "
        "and judge it on its own merits."
    ),
    2: (
        "You are the deciding annotator: two prior annotators disagreed on these "
        "items. Read each text carefully and give your own best judgment."
    ),
}
_VARIANT_TEMPERATURE = {0: 0.0, 1: 0.3, 2: 0.0}


class SemanticMapTool(Tool):
    """Map an LLM task over a text column; results land as a joinable table."""

    timeout_seconds = 1_800.0

    def __init__(self, session: DataAgentSession, provider: Any, model: str | None = None) -> None:
        self._session = session
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "semantic_map"

    @property
    def description(self) -> str:
        return (
            "Apply an extraction or classification task to every row of a text column using "
            "batched LLM calls; results are materialized as table <output_table>(id, value) "
            "that you can JOIN like any other table. Use this instead of regex when the task "
            "needs reading (topics, sentiment, entities, fuzzy dates). Before mapping, check "
            "whether a structured column (category, label, topic, region) already encodes the "
            "answer -- SQL on an existing column beats mapping its text. Filter rows down first "
            f"-- inputs over {_MAX_ROWS} rows are refused. Closed label sets are vote-checked "
            "(sampled double annotation, majority tie-break) and the reply reports the "
            "disagreement rate. Each call reports rows, batches and coverage; spot-check a "
            "sample of the output before trusting it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Source table or read_parquet('<handle>')."},
                "id_column": {"type": "string", "description": "Column that uniquely identifies a row."},
                "text_column": {"type": "string", "description": "The text column the task reads."},
                "task": {
                    "type": "string",
                    "description": "What to extract/decide per row, e.g. 'the publication year as YYYY' .",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional closed label set; exactly one is assigned per row.",
                },
                "output_table": {
                    "type": "string",
                    "description": "Name for the result table (id, value). Overwritten if it exists.",
                },
                "where": {"type": "string", "description": "Optional SQL predicate to filter source rows first."},
            },
            "required": ["table", "id_column", "text_column", "task", "output_table"],
        }

    async def execute(
        self,
        table: str,
        id_column: str,
        text_column: str,
        task: str,
        output_table: str,
        labels: list[str] | None = None,
        where: str | None = None,
    ) -> str:
        import asyncio

        predicate = f" WHERE {where}" if where else ""
        source = (
            f'SELECT CAST("{id_column}" AS VARCHAR) AS id, CAST("{text_column}" AS VARCHAR) AS text '
            f"FROM {table}{predicate}"
        )
        try:
            ensure_read_only(source)
        except SqlRejectedError as exc:
            return f"[SEMANTIC_MAP REJECTED] {exc}"
        resp = await asyncio.to_thread(self._session.sql, source, preview=_MAX_ROWS + 1)
        if not resp.get("ok"):
            return f"[SEMANTIC_MAP ERROR] reading source rows failed: {resp.get('error')}"
        rows = resp.get("rows") or []
        n = int(resp.get("row_count") or 0)
        if n > _MAX_ROWS:
            return (
                f"[SEMANTIC_MAP REFUSED] {n} rows exceed the {_MAX_ROWS} row cap. Narrow the input first "
                "(a WHERE predicate, a deduplicated DISTINCT set, or a coarse SQL pre-filter), then re-run. "
                "Semantic mapping every row of a large table is almost never necessary."
            )
        if n == 0:
            return "[SEMANTIC_MAP] source query returned 0 rows; nothing to map."

        batches = math.ceil(n / _BATCH_SIZE)
        batch_list = [rows[i * _BATCH_SIZE : (i + 1) * _BATCH_SIZE] for i in range(batches)]
        approx_chars = sum(min(len(str(t or "")), _MAX_ITEM_CHARS) for _, t in rows)
        cost_echo = f"{n} rows -> {batches} batch(es) (~{approx_chars // 4 // 1000}k input tokens per pass)"

        semaphore = asyncio.Semaphore(_CONCURRENT_BATCHES)
        vote_stats = {"disagreed": 0, "tie_broken": 0, "unresolved": 0, "second_pass": 0, "escalated": False}

        async def mapped_batch(batch: list[list], variant: int) -> dict[str, str] | None:
            async with semaphore:
                result = await self._map_batch(batch, task, labels, variant=variant)
                if result is None and variant == 0:
                    result = await self._map_batch(batch, task, labels, variant=0)
                return result

        firsts = await asyncio.gather(*(mapped_batch(b, 0) for b in batch_list))

        results: dict[str, str | None] = {}
        failed_batches = 0
        for batch, mapped in zip(batch_list, firsts):
            if mapped is None:
                failed_batches += 1
                for rid, _ in batch:
                    results[str(rid)] = None
            else:
                for rid, _ in batch:
                    results[str(rid)] = mapped.get(str(rid))

        if labels:
            live = [i for i, m in enumerate(firsts) if m is not None]
            step = max(1, len(live) // _VOTE_SAMPLE_BATCHES) if live else 1
            sampled = live[::step][:_VOTE_SAMPLE_BATCHES]
            seconds: dict[int, dict[str, str]] = {}
            for i, second in zip(
                sampled, await asyncio.gather(*(mapped_batch(batch_list[i], 1) for i in sampled))
            ):
                if second is not None:
                    seconds[i] = second
            sampled_rows = sum(len(batch_list[i]) for i in seconds)
            sampled_disagreed = sum(
                1
                for i in seconds
                for rid, _ in batch_list[i]
                if firsts[i].get(str(rid)) != seconds[i].get(str(rid))
            )
            if sampled_rows and sampled_disagreed / sampled_rows > _VOTE_ESCALATE_RATE:
                vote_stats["escalated"] = True
                remaining = [i for i in live if i not in seconds]
                for i, second in zip(
                    remaining,
                    await asyncio.gather(*(mapped_batch(batch_list[i], 1) for i in remaining)),
                ):
                    if second is not None:
                        seconds[i] = second
            vote_stats["second_pass"] = len(seconds)

            tie_batches = []
            for i, second in seconds.items():
                disagreed = [
                    (rid, text)
                    for rid, text in batch_list[i]
                    if firsts[i].get(str(rid)) != second.get(str(rid))
                ]
                if disagreed:
                    tie_batches.append((i, second, disagreed))
            vote_stats["disagreed"] = sum(len(d) for _, _, d in tie_batches)
            thirds = await asyncio.gather(*(mapped_batch(d, 2) for _, _, d in tie_batches))
            for (i, second, disagreed), third in zip(tie_batches, thirds):
                third = third or {}
                for rid, _ in disagreed:
                    sid = str(rid)
                    votes = Counter([firsts[i].get(sid), second.get(sid), third.get(sid)])
                    value, count = votes.most_common(1)[0]
                    if count >= 2:
                        results[sid] = value
                        vote_stats["tie_broken"] += 1
                    else:
                        results[sid] = None
                        vote_stats["unresolved"] += 1

        out_rows = [[rid, value] for rid, value in results.items()]
        put = await asyncio.to_thread(self._session.put_rows, output_table, ["id", "value"], out_rows)
        if not put.get("ok"):
            return f"[SEMANTIC_MAP ERROR] materializing {output_table} failed: {put.get('error')}"

        covered = sum(1 for v in results.values() if v is not None)
        top: dict[str, int] = {}
        for v in results.values():
            if v is not None:
                top[v] = top.get(v, 0) + 1
        distribution = ", ".join(f"{v!r}:{c}" for v, c in sorted(top.items(), key=lambda kv: -kv[1])[:8])
        notes = []
        if failed_batches:
            notes.append(f"{failed_batches} batch(es) failed twice and were written as NULL -- re-run for those ids.")
        if covered < len(results):
            notes.append(f"{len(results) - covered} row(s) have NULL value; check them before aggregating.")
        if labels:
            mode = (
                "escalated to a full second pass" if vote_stats["escalated"] else "sampled check only"
            )
            notes.append(
                f"label voting ({vote_stats['second_pass']} batch(es) double-annotated, {mode}): "
                f"{vote_stats['disagreed']} row(s) disagreed "
                f"({vote_stats['tie_broken']} resolved by majority, {vote_stats['unresolved']} unresolved -> NULL). "
                "High disagreement means the task or label set is ambiguous -- tighten the wording "
                "or spot-check those rows before aggregating."
            )
        notes_txt = ("\nNOTES: " + " | ".join(notes)) if notes else ""
        return (
            f"materialized {output_table}(id, value): {len(out_rows)} rows, {covered} with a value. "
            f"[{cost_echo}]\n"
            f"value distribution (top): {distribution or '(none)'}\n"
            f"Next: spot-check with sql(\"SELECT * FROM {output_table} USING SAMPLE 10\"), then JOIN on id."
            f"{notes_txt}"
        )

    async def _map_batch(
        self,
        batch: list[list],
        task: str,
        labels: list[str] | None,
        variant: int = 0,
    ) -> dict[str, str] | None:
        items = [
            {"id": str(rid), "text": (str(text) if text is not None else "")[:_MAX_ITEM_CHARS]}
            for rid, text in batch
        ]
        label_rule = (
            f"Assign exactly one label from this closed set to each item: {json.dumps(labels)}.\n"
            if labels
            else "Produce the requested value for each item; use null only when the text truly lacks it.\n"
        )
        prompt = (
            f"{_VARIANT_PREAMBLE[variant]} For every item below, perform this task on its text:\n"
            f"TASK: {task}\n"
            f"{label_rule}"
            "Respond with ONLY a JSON array, one object per item, covering every id exactly once: "
            '[{"id": "<id>", "value": <string or null>}, ...]\n\n'
            f"ITEMS:\n{json.dumps(items, ensure_ascii=False)}"
        )
        try:
            response = await self._provider.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                max_tokens=_MAX_RESPONSE_TOKENS,
                temperature=_VARIANT_TEMPERATURE[variant],
            )
        except Exception:
            return None
        return _parse_batch_response(response.content or "")


def _parse_batch_response(content: str) -> dict[str, str] | None:
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    out: dict[str, str] = {}
    for item in parsed:
        if isinstance(item, dict) and "id" in item:
            value = item.get("value")
            out[str(item["id"])] = None if value is None else str(value)
    return out or None


def make_semantic_map_tool(ctx: PluginContext) -> Tool | None:
    from pathlib import Path

    from raven.plugin.data_agent.session import get_session, parse_sources

    provider = ctx.services.provider
    if provider is None:
        # No LLM handle was granted at this assembly point; decline rather
        # than build a second provider from private config (see ServiceLocator).
        return None
    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return SemanticMapTool(session, provider, model=config.get("semantic_map_model") or None)
