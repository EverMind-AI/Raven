"""Completed-task recall for the post-turn board snapshot.

A completed group is read as its title, rolling summary and last five steps, then
ranked by BM25 over that text. The same text is embedded through the configured
embedding endpoint and ranked by cosine; the two rankings are fused with RRF,
BM25 weighted three times. Vectors live in the session directory's SQLite file,
keyed by the embedding model and the document text, so a group whose text did
not change is never embedded twice. The file is derived from the board: a clear
removes it, and a read or a write refuses to run once the clear it began before
has landed. Any dense failure leaves the BM25 ranking.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Callable, Protocol, Sequence

import numpy as np
from loguru import logger

from raven.agent.task_board.board import Board, TaskGroup
from raven.agent.task_board.store import EMBEDDINGS_FILENAME
from raven.utils.bm25 import BM25Okapi, tokenize

DOCUMENT_STEPS = 5
RECALL_LIMIT = 5
RRF_K = 60
BM25_WEIGHT = 3.0
DENSE_WEIGHT = 1.0
EMBED_TIMEOUT_S = 30.0
QUERY_CHARS = 8000


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def completed_document(group: TaskGroup) -> str:
    parts = [group.title, group.summary]
    for step in group.steps[-DOCUMENT_STEPS:]:
        parts.append(f"{step.title}: {step.summary}")
        parts.extend(f"{link.kind}:{link.id} {link.summary}" for link in step.tasks)
    return "\n".join(part for part in parts if part)


def bm25_order(groups: Sequence[TaskGroup], query: str) -> list[TaskGroup]:
    if not groups or not query.strip():
        return []
    scores = BM25Okapi([tokenize(completed_document(group)) for group in groups]).get_scores(tokenize(query))
    ranked = sorted(range(len(groups)), key=lambda index: (-scores[index], index))
    return [groups[index] for index in ranked if scores[index] > 0]


def dense_order(
    groups: Sequence[TaskGroup],
    query_vector: Sequence[float],
    vectors: dict[str, np.ndarray],
) -> list[TaskGroup]:
    present = [(index, group) for index, group in enumerate(groups) if group.id in vectors]
    if not present:
        return []
    matrix = np.stack([vectors[group.id] for _, group in present]).astype(np.float32)
    matrix = matrix / np.clip(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12, None)
    query = np.asarray(query_vector, dtype=np.float32)
    query = query / max(float(np.linalg.norm(query)), 1e-12)
    scores = matrix @ query
    order = sorted(range(len(present)), key=lambda index: (-float(scores[index]), present[index][0]))
    return [present[index][1] for index in order]


def rrf_order(
    *,
    sparse: Sequence[TaskGroup],
    dense: Sequence[TaskGroup],
    all_groups: Sequence[TaskGroup],
) -> list[TaskGroup]:
    scores: dict[str, float] = {}
    for rank, group in enumerate(sparse, start=1):
        scores[group.id] = scores.get(group.id, 0.0) + BM25_WEIGHT / (RRF_K + rank)
    for rank, group in enumerate(dense, start=1):
        scores[group.id] = scores.get(group.id, 0.0) + DENSE_WEIGHT / (RRF_K + rank)
    by_id = {group.id: group for group in all_groups}
    position = {group.id: index for index, group in enumerate(all_groups)}
    return [
        by_id[group_id]
        for group_id in sorted(scores, key=lambda group_id: (-scores[group_id], position.get(group_id, len(position))))
    ]


def _cache_path(session_dir: Path) -> Path:
    return Path(session_dir) / EMBEDDINGS_FILENAME


def _key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL)"
    )
    connection.commit()
    return connection


def _read_vectors(path: Path, model: str, items: Sequence[tuple[str, str]]) -> dict[str, np.ndarray]:
    if not items or not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        stored = {
            key: (int(dim), blob)
            for key, dim, blob in connection.execute("SELECT key, dim, vec FROM vec WHERE model = ?", (model,))
        }
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    found: dict[str, np.ndarray] = {}
    for group_id, text in items:
        row = stored.get(_key(model, text))
        if row is None:
            continue
        dim, blob = row
        vector = np.frombuffer(blob, dtype=np.float32)
        if len(vector) == dim:
            found[group_id] = vector
    return found


def _write_vectors(path: Path, model: str, items: Sequence[tuple[str, str, Sequence[float]]]) -> None:
    if not items:
        return
    rows = [
        (_key(model, text), model, len(vector), np.asarray(vector, dtype=np.float32).tobytes())
        for _, text, vector in items
    ]
    connection = _connect(path)
    try:
        connection.executemany("INSERT OR REPLACE INTO vec (key, model, dim, vec) VALUES (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def _embedder() -> Embedder | None:
    try:
        from raven.knowledge._embedding import embedding_client, load_embedding_config
    except Exception as exc:  # noqa: BLE001 - an unavailable embedding stack is a fallback, not a failure
        logger.warning("Task board recall: embedding stack unavailable: {}", exc)
        return None
    try:
        config = load_embedding_config()
        return embedding_client(config) if config is not None else None
    except Exception as exc:  # noqa: BLE001 - a bad config or client is a fallback, not a failure
        logger.warning("Task board recall: embedding endpoint unavailable: {}", exc)
        return None


async def retrieve_completed(
    board: Board,
    query: str,
    *,
    session_dir: Path,
    limit: int = RECALL_LIMIT,
    embedder: Embedder | None = None,
    generation: int | None = None,
    generation_now: Callable[[], int] | None = None,
    write_lock: asyncio.Lock | None = None,
) -> list[str]:
    query = (query or "").strip()[:QUERY_CHARS]
    groups = [group for group in board.groups if group.status == "completed"]
    if not query or not groups:
        return []

    def current() -> bool:
        return generation is None or generation_now is None or generation_now() == generation

    if not current():
        return []
    sparse = bm25_order(groups, query)
    try:
        client = embedder or _embedder()
        if client is None:
            return [group.id for group in sparse[:limit]]
        items = [(group.id, completed_document(group)) for group in groups]
        vectors = _read_vectors(_cache_path(session_dir), client.model, items)
        missing = [(group_id, text) for group_id, text in items if group_id not in vectors]
        texts = [query] + [text for _, text in missing]
        embedded = await asyncio.wait_for(client.embed(texts), timeout=EMBED_TIMEOUT_S)
        if len(embedded) != len(texts):
            raise RuntimeError(f"embedding endpoint returned {len(embedded)} vectors for {len(texts)} inputs")
        if not current():
            return []
        if missing:
            rows = [(group_id, text, vector) for (group_id, text), vector in zip(missing, embedded[1:])]
            if write_lock is None:
                _write_vectors(_cache_path(session_dir), client.model, rows)
            else:
                async with write_lock:
                    if not current():
                        return []
                    _write_vectors(_cache_path(session_dir), client.model, rows)
            for (group_id, _), vector in zip(missing, embedded[1:]):
                vectors[group_id] = np.asarray(vector, dtype=np.float32)
        dense = dense_order(groups, embedded[0], vectors)
        return [group.id for group in rrf_order(sparse=sparse, dense=dense, all_groups=groups)[:limit]]
    except Exception as exc:  # noqa: BLE001 - the update must proceed on BM25 when dense fails
        logger.warning("Task board recall: dense ranking failed ({}); using BM25", exc)
        return [group.id for group in sparse[:limit]]
