"""BM25 keyword index over the agent's tool catalog.

Backs ``tool_search``: ranks tools by a query over their name and description,
reusing the shared Okapi BM25 in :mod:`raven.utils.bm25` (CJK-aware, so
Chinese queries match). The name is repeated in the indexed text so a hit on
the tool name outranks an incidental description hit — the same field-weighting
idea as the skill ``LocalPool``.

The index rebuilds only when the catalog's ``(name, description)`` set changes
(startup + each MCP connect is one burst); steady-state ``search`` is one
query-side tokenize plus a BM25 dot product over precomputed ``doc_freqs``.

A single process-level slot caches the most recently built index keyed by that
same signature, so the many short-lived agent loops a resident process spins up
over one fixed tool set reuse one prebuilt BM25 rather than each rebuilding an
identical one. A single slot suffices because only the main loop feeds this
index (subagents run a separate minimal tool set), so one signature dominates;
keying on description (not just name) also forces a rebuild when a tool's
description changes under hot-reload.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from raven.utils.bm25 import BM25Okapi, tokenize

if TYPE_CHECKING:
    from raven.agent.tools.base import Tool

# Catalog signature: the exact inputs to the BM25 build (name + description).
_Signature = frozenset[tuple[str, str]]
# A built index: the names list paired with the BM25 built from it (same
# ordering). Read-only for searchers, so safe to share across loops.
_BuiltIndex = tuple[list[str], BM25Okapi]

# Single process-level slot, guarded by its own lock. The expensive build runs
# outside the lock; the lock only guards the slot read/write. A rare concurrent
# double-build on first miss is harmless — the build is pure, so last-writer-
# wins yields an identical index (same tolerance as ``LocalPool``).
_cache_lock = threading.Lock()
_cached_sig: _Signature = frozenset()
_cached_index: _BuiltIndex | None = None


def _format_tool_text(tool: "Tool") -> str:
    """Indexed text for one tool. name ×3, description ×1 — TF-based field
    weighting so a query term on the name dominates a description hit."""
    name = tool.name
    desc = tool.description or ""
    return f"{name} {name} {name} {desc}"


def _get_or_build(sig: _Signature, tools: "list[Tool]") -> _BuiltIndex:
    """Return the shared prebuilt index for ``sig``, building it once on miss."""
    global _cached_sig, _cached_index
    with _cache_lock:
        if sig == _cached_sig and _cached_index is not None:
            return _cached_index
    names = [t.name for t in tools]
    corpus = [tokenize(_format_tool_text(t)) for t in tools]
    built: _BuiltIndex = (names, BM25Okapi(corpus))
    with _cache_lock:
        _cached_sig, _cached_index = sig, built
    return built


class ToolIndex:
    """Prebuilt BM25 over a tool catalog, rebuilt on (name, description) change.

    Thread-safety: a lock guards the ``(names, BM25Okapi)`` pair. The expensive
    build is delegated to the shared ``_get_or_build`` slot; the lock here is
    held only for the atomic swap and for capturing references in ``search``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: list[str] = []
        self._sig: _Signature = frozenset()
        self._bm25: BM25Okapi | None = None

    def ensure(self, tools: "list[Tool]") -> None:
        """Adopt the shared index iff the (name, description) set changed."""
        sig: _Signature = frozenset((t.name, t.description or "") for t in tools)
        with self._lock:
            if sig == self._sig and self._bm25 is not None:
                return
        names, bm25 = _get_or_build(sig, tools)
        with self._lock:
            self._names, self._bm25, self._sig = names, bm25, sig

    def search(self, query: str, limit: int) -> list[str]:
        """Return up to ``limit`` tool names ranked by BM25, zero-score dropped."""
        tokens = tokenize(query)
        with self._lock:
            bm25, names = self._bm25, self._names
        if bm25 is None or not tokens:
            return []
        scores = bm25.get_scores(tokens)
        ranked = sorted(zip(names, scores), key=lambda x: x[1], reverse=True)
        return [name for name, score in ranked if score > 0.0][:limit]
