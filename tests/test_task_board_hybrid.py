"""Completed-task recall: D3 documents, BM25 + dense RRF, and the disk cache."""

from __future__ import annotations

import asyncio

import numpy as np

from raven.agent.task_board import hybrid
from raven.agent.task_board.board import Board, ProgressStep, TaskGroup, TaskLink
from raven.agent.task_board.hybrid import (
    bm25_order,
    completed_document,
    dense_order,
    retrieve_completed,
    rrf_order,
)
from raven.agent.task_board.store import EMBEDDINGS_FILENAME, copy_board, reset_board, save_board


def _group(
    group_id: str,
    title: str,
    summary: str = "summary",
    *,
    status: str = "completed",
    steps: list[ProgressStep] | None = None,
) -> TaskGroup:
    return TaskGroup(group_id, title, summary, status, 1, 2, steps or [])


class FakeEmbedder:
    model = "fake-model"

    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[float(len(text)), 1.0] for text in texts]


def test_the_document_is_title_summary_last_five_steps_and_linked_tasks():
    steps = [ProgressStep(f"s{i}", "T0", None, f"Step {i} ancient{i}", "detail", []) for i in range(1, 8)]
    steps[-1].tasks = [TaskLink("spawn", "x", "Linked task summary")]
    document = completed_document(_group("g1", "The title", "The summary", steps=steps))

    assert "The title" in document and "The summary" in document
    assert "Step 3 ancient3" in document and "Step 7 ancient7" in document
    assert "Step 1 ancient1" not in document and "Step 2 ancient2" not in document
    assert "Linked task summary" in document


def test_bm25_ranks_completed_groups_by_the_query():
    alpha = _group("g_alpha", "Alpha report")
    beta = _group("g_beta", "Beta report")

    assert [group.id for group in bm25_order([alpha, beta], "alpha")] == ["g_alpha"]


def test_rrf_weights_the_bm25_ranking_three_times():
    first = _group("g_first", "First")
    second = _group("g_second", "Second")

    order = rrf_order(sparse=[first, second], dense=[second, first], all_groups=[first, second])

    assert [group.id for group in order] == ["g_first", "g_second"]


def test_dense_order_uses_cosine():
    first = _group("g_first", "First")
    second = _group("g_second", "Second")

    order = dense_order(
        [first, second],
        [1.0, 0.0],
        {"g_first": np.array([0.0, 1.0]), "g_second": np.array([1.0, 0.0])},
    )

    assert [group.id for group in order] == ["g_second", "g_first"]


async def test_a_dense_failure_falls_back_to_bm25(tmp_path):
    group = _group("g_alpha", "Alpha report")

    result = await retrieve_completed(
        Board([group], []),
        "alpha",
        session_dir=tmp_path,
        embedder=FakeEmbedder(fail=True),
    )

    assert result == ["g_alpha"]


async def test_an_unconfigured_embedder_falls_back_to_bm25(tmp_path, monkeypatch):
    monkeypatch.setattr(hybrid, "_embedder", lambda: None)
    group = _group("g_alpha", "Alpha report")

    result = await retrieve_completed(Board([group], []), "alpha", session_dir=tmp_path)

    assert result == ["g_alpha"]


async def test_an_embedder_construction_failure_falls_back_to_bm25(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("no embedding endpoint")

    monkeypatch.setattr(hybrid, "_embedder", broken)
    group = _group("g_alpha", "Alpha report")

    result = await retrieve_completed(Board([group], []), "alpha", session_dir=tmp_path)

    assert result == ["g_alpha"]


async def test_a_dense_timeout_falls_back_to_bm25(tmp_path, monkeypatch):
    monkeypatch.setattr(hybrid, "EMBED_TIMEOUT_S", 0.01)
    group = _group("g_alpha", "Alpha report")

    result = await retrieve_completed(
        Board([group], []),
        "alpha",
        session_dir=tmp_path,
        embedder=FakeEmbedder(delay=0.1),
    )

    assert result == ["g_alpha"]


async def test_the_documents_are_cached_on_disk_and_reused(tmp_path):
    first = _group("g_first", "First report")
    second = _group("g_second", "Second report")
    board = Board([first, second], [])
    embedder = FakeEmbedder()

    await retrieve_completed(board, "first query", session_dir=tmp_path, embedder=embedder)
    await retrieve_completed(board, "second query", session_dir=tmp_path, embedder=embedder)

    assert (tmp_path / EMBEDDINGS_FILENAME).is_file()
    assert len(embedder.calls[0]) == 3
    assert embedder.calls[1] == ["second query"]


async def test_a_changed_group_is_embedded_again_but_the_others_are_not(tmp_path):
    first = _group("g_first", "First report")
    second = _group("g_second", "Second report")
    board = Board([first, second], [])
    embedder = FakeEmbedder()

    await retrieve_completed(board, "first query", session_dir=tmp_path, embedder=embedder)
    first.summary = "A changed summary"
    await retrieve_completed(board, "second query", session_dir=tmp_path, embedder=embedder)

    assert len(embedder.calls[1]) == 2
    assert completed_document(first) in embedder.calls[1]
    assert completed_document(second) not in embedder.calls[1]


async def test_an_empty_query_returns_nothing(tmp_path):
    group = _group("g_first", "First report")

    assert await retrieve_completed(Board([group], []), "   ", session_dir=tmp_path) == []


async def test_only_completed_groups_are_recalled(tmp_path):
    open_group = _group("g_open", "Alpha open", status="running")
    done = _group("g_done", "Alpha done")

    result = await retrieve_completed(
        Board([open_group, done], []), "alpha", session_dir=tmp_path, embedder=FakeEmbedder()
    )

    assert result == ["g_done"]


async def test_reset_removes_the_cache_and_copy_does_not_carry_it(tmp_path):
    group = _group("g_first", "First report")
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    save_board(source, Board([group], []))
    await retrieve_completed(Board([group], []), "first query", session_dir=source, embedder=FakeEmbedder())
    assert (source / EMBEDDINGS_FILENAME).is_file()

    copy_board(source, target)
    assert not (target / EMBEDDINGS_FILENAME).exists()

    reset_board(source)
    assert not (source / EMBEDDINGS_FILENAME).exists()
