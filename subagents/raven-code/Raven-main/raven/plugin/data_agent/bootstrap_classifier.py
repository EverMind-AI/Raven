"""bootstrap_classifier: scale a small labeled sample to a whole corpus without LLM calls.

Per-row LLM labeling of a large text column is the single most expensive thing a
data agent can do: thousands of calls, provider-latency bound, non-reproducible.
The scalable pattern is a funnel -- label a small sample with the LLM (semantic_map),
train a deterministic classifier on that sample, apply it to every row locally,
and escalate only the low-margin rows back to the LLM.

This tool is the middle of that funnel: a multinomial Naive Bayes over word
counts, implemented on numpy so the kernel needs no extra dependencies. It is
deterministic (same inputs, same labels), auditable (per-row margins are
materialized), and runs the whole corpus in seconds.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext
from raven.plugin.data_agent.session import DataAgentSession
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only

_MAX_ROWS = 500_000
_MIN_LABELED = 30
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LOW_MARGIN = 0.55


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


class _NaiveBayes:
    """Multinomial NB with add-one smoothing over unigram counts."""

    def __init__(self) -> None:
        self.labels: list[str] = []
        self._log_prior: dict[str, float] = {}
        self._log_like: dict[str, dict[str, float]] = {}
        self._log_unseen: dict[str, float] = {}

    def fit(self, samples: list[tuple[str, str]]) -> None:
        by_label: dict[str, Counter[str]] = defaultdict(Counter)
        label_rows: Counter[str] = Counter()
        for text, label in samples:
            by_label[label].update(_tokens(text))
            label_rows[label] += 1
        vocabulary = set()
        for counts in by_label.values():
            vocabulary.update(counts)
        vocab_size = max(len(vocabulary), 1)
        total_rows = sum(label_rows.values())
        self.labels = sorted(by_label)
        for label in self.labels:
            counts = by_label[label]
            total = sum(counts.values())
            self._log_prior[label] = math.log(label_rows[label] / total_rows)
            denominator = total + vocab_size
            self._log_like[label] = {
                token: math.log((count + 1) / denominator) for token, count in counts.items()
            }
            self._log_unseen[label] = math.log(1 / denominator)

    def predict(self, text: str) -> tuple[str, float]:
        """Return (label, margin) where margin is the top-1 posterior share."""
        tokens = _tokens(text)
        scores = {}
        for label in self.labels:
            like = self._log_like[label]
            unseen = self._log_unseen[label]
            scores[label] = self._log_prior[label] + sum(
                like.get(token, unseen) for token in tokens
            )
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best_label, best_score = ranked[0]
        # Softmax share of the winner, computed stably against the max.
        total = sum(math.exp(score - best_score) for _, score in ranked)
        return best_label, 1.0 / total


class BootstrapClassifierTool(Tool):
    """Train on an LLM-labeled sample, classify every row deterministically."""

    timeout_seconds = 600.0

    def __init__(self, session: DataAgentSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        return "bootstrap_classifier"

    @property
    def description(self) -> str:
        return (
            "Scale a small labeled sample to a whole text column without further LLM calls: "
            "trains a deterministic word-count classifier (multinomial NB) on a labeled "
            "table -- typically semantic_map's output on a sample -- and classifies every "
            "row, materializing <output_table>(id, label, margin). Use it when a text column "
            "has thousands of rows to label: label a representative sample (500-2000 rows) "
            "with semantic_map first, train on it, then escalate only low-margin rows "
            f"(margin < {_LOW_MARGIN}) back to semantic_map. Deterministic and reproducible; "
            "report the margin distribution honestly rather than trusting it blindly."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Corpus table or read_parquet('<handle>')."},
                "id_column": {"type": "string", "description": "Unique row id column in the corpus."},
                "text_column": {"type": "string", "description": "Text column to classify."},
                "labeled_table": {
                    "type": "string",
                    "description": "Table with the labeled sample, e.g. semantic_map output (id, value).",
                },
                "labeled_id_column": {"type": "string", "description": "Id column in labeled_table (default id)."},
                "label_column": {"type": "string", "description": "Label column in labeled_table (default value)."},
                "output_table": {
                    "type": "string",
                    "description": "Name for the result table (id, label, margin).",
                },
            },
            "required": ["table", "id_column", "text_column", "labeled_table", "output_table"],
        }

    async def execute(
        self,
        table: str,
        id_column: str,
        text_column: str,
        labeled_table: str,
        output_table: str,
        labeled_id_column: str = "id",
        label_column: str = "value",
    ) -> str:
        import asyncio

        train_sql = (
            f'SELECT CAST(t."{text_column}" AS VARCHAR), CAST(s."{label_column}" AS VARCHAR) '
            f'FROM {table} t JOIN {labeled_table} s ON CAST(t."{id_column}" AS VARCHAR) = '
            f'CAST(s."{labeled_id_column}" AS VARCHAR) WHERE s."{label_column}" IS NOT NULL'
        )
        corpus_sql = (
            f'SELECT CAST("{id_column}" AS VARCHAR), CAST("{text_column}" AS VARCHAR) '
            f'FROM {table} WHERE "{text_column}" IS NOT NULL'
        )
        for statement in (train_sql, corpus_sql):
            try:
                ensure_read_only(statement)
            except SqlRejectedError as exc:
                return f"[BOOTSTRAP_CLASSIFIER REJECTED] {exc}"

        train = await asyncio.to_thread(self._session.sql, train_sql, preview=_MAX_ROWS)
        if not train.get("ok"):
            return f"[BOOTSTRAP_CLASSIFIER ERROR] reading labeled sample failed: {train.get('error')}"
        samples = [(str(text), str(label)) for text, label in (train.get("rows") or [])]
        if len(samples) < _MIN_LABELED:
            return (
                f"[BOOTSTRAP_CLASSIFIER REFUSED] only {len(samples)} labeled rows joined; "
                f"need at least {_MIN_LABELED}. Label a bigger sample with semantic_map first, "
                "and check the id columns actually match."
            )
        label_counts = Counter(label for _, label in samples)
        if len(label_counts) < 2:
            return (
                "[BOOTSTRAP_CLASSIFIER REFUSED] the labeled sample contains a single label; "
                "a classifier needs at least two classes."
            )

        corpus = await asyncio.to_thread(self._session.sql, corpus_sql, preview=_MAX_ROWS + 1)
        if not corpus.get("ok"):
            return f"[BOOTSTRAP_CLASSIFIER ERROR] reading corpus failed: {corpus.get('error')}"
        rows = corpus.get("rows") or []
        if len(rows) > _MAX_ROWS:
            return (
                f"[BOOTSTRAP_CLASSIFIER REFUSED] corpus exceeds {_MAX_ROWS} rows; "
                "narrow with a WHERE clause via a view first."
            )

        model = _NaiveBayes()
        model.fit(samples)
        out_rows = []
        predicted: Counter[str] = Counter()
        low_margin = 0
        for row_id, text in rows:
            label, margin = model.predict(str(text))
            predicted[label] += 1
            if margin < _LOW_MARGIN:
                low_margin += 1
            out_rows.append([str(row_id), label, f"{margin:.4f}"])

        put = await asyncio.to_thread(
            self._session.put_rows, output_table, ["id", "label", "margin"], out_rows
        )
        if not put.get("ok"):
            return f"[BOOTSTRAP_CLASSIFIER ERROR] materializing {output_table} failed: {put.get('error')}"

        train_summary = ", ".join(f"{label}: {count}" for label, count in label_counts.most_common())
        predict_summary = ", ".join(f"{label}: {count}" for label, count in predicted.most_common())
        return (
            f"materialized {output_table}(id, label, margin): {len(out_rows)} rows classified "
            f"from {len(samples)} labeled examples ({train_summary}).\n"
            f"predicted distribution: {predict_summary}.\n"
            f"{low_margin} rows have margin < {_LOW_MARGIN} -- they are genuinely ambiguous to "
            "this classifier; escalate them (JOIN on margin) to semantic_map if the answer is "
            "sensitive to them, and sanity-check the predicted distribution against the "
            "labeled sample's before trusting downstream aggregates."
        )


def make_bootstrap_classifier_tool(ctx: PluginContext) -> Tool:
    from pathlib import Path

    from raven.plugin.data_agent.session import get_session, parse_sources

    config = ctx.config or {}
    session = get_session(
        Path(ctx.services.workspace),
        parse_sources(config),
        kernel_python=config.get("kernel_python") or None,
        extension_dir=config.get("duckdb_extension_dir") or None,
    )
    return BootstrapClassifierTool(session)
