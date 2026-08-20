"""BM25 keyword index over the agent's tool catalog.

Backs ``tool_search``: ranks tools by a query over each tool's name,
description and parameter schema, reusing the shared Okapi BM25 in
:mod:`raven.utils.bm25` (CJK-aware, so Chinese queries match). The name is
repeated in the indexed text so a hit on the tool name outranks an incidental
body hit — the same field-weighting idea as the skill ``LocalPool``. Parameter
names, their descriptions and enum values are folded into the indexed body:
many tools carry the discriminating keywords (a repo owner, a channel id, an
image size) in the schema rather than the one-line description, so indexing the
schema lifts recall without touching the schema the model sees.

The index rebuilds only when the catalog's ``(name, description, parameters)``
set changes (startup + each MCP connect is one burst); steady-state ``search``
is one query-side tokenize plus a BM25 dot product over precomputed
``doc_freqs``. Each ``ensure`` flattens every tool's text to compute the
signature (cheap string work); the expensive BM25 rebuild is skipped on a hit.

A single process-level slot caches the most recently built index keyed by that
same signature, so the many short-lived agent loops a resident process spins up
over one fixed tool set reuse one prebuilt BM25 rather than each rebuilding an
identical one. A single slot suffices because only the main loop feeds this
index (subagents run a separate minimal tool set), so one signature dominates;
keying on the full indexed text (not just the name) also forces a rebuild when
a tool's description or parameters change under hot-reload.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from raven.utils.bm25 import BM25Okapi, tokenize

if TYPE_CHECKING:
    from raven.agent.tools.base import Tool

# Catalog signature: one (name, indexed-text) pair per tool. The indexed text
# embeds description + parameter schema, so the signature changes whenever any
# indexed field does — the exact trigger for a rebuild.
_Signature = frozenset[tuple[str, str]]

# Cap schema-walk recursion so a pathologically nested MCP schema can't blow the
# stack or balloon the indexed text; tool schemas are shallow in practice.
_MAX_SCHEMA_DEPTH = 6
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


def _schema_text(parameters: "dict[str, Any] | None") -> str:
    """Natural-language keywords drawn from a JSON-Schema parameter block.

    Collects property names, their ``description`` strings and enum values,
    recursing through nested objects and array items. Types and structural
    keywords are skipped — they carry no query signal.
    """
    if not isinstance(parameters, dict):
        return ""
    parts: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_SCHEMA_DEPTH or not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for key, sub in props.items():
                parts.append(str(key))
                walk(sub, depth + 1)
        desc = node.get("description")
        if isinstance(desc, str):
            parts.append(desc)
        enum = node.get("enum")
        if isinstance(enum, list):
            parts.extend(str(v) for v in enum if isinstance(v, (str, int, float)))
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, depth + 1)

    walk(parameters, 0)
    return " ".join(parts)


def _format_tool_text(tool: "Tool") -> str:
    """Indexed text for one tool: name ×3, then description + parameter-schema
    keywords ×1 — TF-based field weighting so a query term on the name
    dominates a hit in the body."""
    name = tool.name
    body = f"{tool.description or ''} {_schema_text(tool.parameters)}".strip()
    return f"{name} {name} {name} {body}".rstrip()


def _get_or_build(sig: _Signature, items: list[tuple[str, str]]) -> _BuiltIndex:
    """Return the shared prebuilt index for ``sig``, building it once on miss.

    ``items`` are ``(name, indexed-text)`` pairs already flattened by the
    caller, so the text is computed once per ``ensure`` (for the signature) and
    reused here rather than recomputed.
    """
    global _cached_sig, _cached_index
    with _cache_lock:
        if sig == _cached_sig and _cached_index is not None:
            return _cached_index
    names = [name for name, _ in items]
    corpus = [tokenize(text) for _, text in items]
    built: _BuiltIndex = (names, BM25Okapi(corpus))
    with _cache_lock:
        _cached_sig, _cached_index = sig, built
    return built


class ToolIndex:
    """Prebuilt BM25 over a tool catalog, rebuilt on (name, description, parameters) change.

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
        """Adopt the shared index iff the (name, description, parameters) set changed."""
        items = [(t.name, _format_tool_text(t)) for t in tools]
        sig: _Signature = frozenset(items)
        with self._lock:
            if sig == self._sig and self._bm25 is not None:
                return
        names, bm25 = _get_or_build(sig, items)
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
