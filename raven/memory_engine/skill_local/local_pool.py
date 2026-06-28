"""LocalPool — BM25 keyword retrieval over file-based skills.

The "local" pool covers everything that lives as a SKILL.md file on disk:

  - workspace/skills/ (user-authored)
  - packaged builtin/ (the 9 shipped skills)
  - workspace/skills/everos/ (EverOS-extracted, optional)

These pools are small (tens to a few hundred skills) and frequently edited,
so BM25 over the in-memory corpus is the right shape:
  - no embedding model loaded → starts in milliseconds
  - relevance is keyword-driven → user intent on a specific tool
    ("pdf" / "weather") matches better than dense semantic
  - re-tokenize per ``select`` is cheap at this scale

Index is built eagerly in ``__init__`` and refreshed via the public
``rebuild_index()`` — :class:`SkillService` calls it from
``invalidate_skill_cache`` so every file-watcher / evolver invalidation
flows through to the BM25 state. Steady-state ``search`` therefore
costs one query-side tokenize + one BM25 dot-product over precomputed
``doc_freqs``; the per-doc tokenize and IDF accumulation only run when
files actually changed.

A self-contained BM25Okapi implementation (no ``rank_bm25`` / ``jieba`` /
``nltk`` dependency) keeps the install footprint small. Tokenization is
regex-based, splitting on word boundaries and treating Chinese characters
as single tokens — sufficient for skill names + descriptions, which are
mostly short English domain terms.
"""

from __future__ import annotations

import math
import re
import threading
from typing import TYPE_CHECKING

from raven.memory_engine.skill_local.types import ScoredSkill, SkillMeta

if TYPE_CHECKING:
    from raven.memory_engine.skill_local.registry import SkillRegistry


# Match length-≥2 alphanumeric runs OR a single CJK ideograph.
# ``re`` precompile is module-level to dodge per-call regex setup.
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class _BM25Okapi:
    """Minimal Okapi BM25 — same formula as rank_bm25 / Lucene defaults.

    ``score(D, Q) = Σ idf(q_i) * f(q_i, D) * (k1 + 1)
                          / (f(q_i, D) + k1 * (1 - b + b * |D| / avgdl))``
    """

    def __init__(
        self,
        tokenized_corpus: list[list[str]],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = k1
        self.b = b
        self.corpus_size = len(tokenized_corpus)
        self.doc_lens = [len(d) for d in tokenized_corpus]
        self.avgdl = (
            sum(self.doc_lens) / self.corpus_size if self.corpus_size else 0.0
        )

        self.doc_freqs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in tokenized_corpus:
            freqs: dict[str, int] = {}
            for tok in doc:
                freqs[tok] = freqs.get(tok, 0) + 1
            self.doc_freqs.append(freqs)
            for tok in freqs:
                df[tok] = df.get(tok, 0) + 1

        n = self.corpus_size
        # ``log(1 + (N - n + 0.5) / (n + 0.5))`` — Robertson-Spärck-Jones
        # weighting; the ``1 +`` guard keeps it non-negative when n ≈ N.
        self.idf = {
            term: math.log(1 + (n - count + 0.5) / (count + 0.5))
            for term, count in df.items()
        }

    def get_scores(self, query_tokens: list[str]) -> list[float]:
        scores = [0.0] * self.corpus_size
        if not query_tokens or self.corpus_size == 0:
            return scores
        for term in query_tokens:
            idf = self.idf.get(term, 0.0)
            if idf <= 0.0:
                continue
            for i, freqs in enumerate(self.doc_freqs):
                f = freqs.get(term, 0)
                if f == 0:
                    continue
                dl = self.doc_lens[i]
                norm = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
                scores[i] += idf * f * (self.k1 + 1) / (f + norm)
        return scores


def _format_skill_text(meta: SkillMeta, body_max: int = 4000) -> str:
    """One-line representation fed into the BM25 index. Heavier on signal
    fields (name, description) than body — ``"weather"`` should fire on
    the weather skill even when the body talks about HTTP and caching."""
    body = (meta.content or "")[:body_max]
    # Repeat name + description so they outweigh a long body in BM25 TF.
    return f"{meta.name} {meta.name} {meta.description or ''} {body}"


class LocalPool:
    """BM25 retrieval wrapper around a file-based ``SkillRegistry``.

    Holds a prebuilt ``_BM25Okapi`` over the current registry contents.
    :class:`SkillService` calls :meth:`rebuild_index` from its
    ``invalidate_skill_cache`` hook so file-watcher events and evolver
    writes refresh the index directly, leaving ``search`` to a single
    query-side tokenize + BM25 dot product.

    Thread-safety: an internal :class:`threading.Lock` guards the
    ``(metas, _BM25Okapi)`` pair. ``rebuild_index`` does the expensive
    tokenize + BM25 construction *outside* the lock and only takes it
    for the atomic swap; ``search`` holds the lock only long enough
    to capture the two references, then scores + sorts outside.
    """

    def __init__(self, registry: "SkillRegistry") -> None:
        self._registry = registry
        self._metas: list[SkillMeta] = []
        self._bm25: _BM25Okapi | None = None
        # Plain Lock (not RLock): no method re-enters another.
        self._lock = threading.Lock()
        # Eager initial build — matches the rest of the service which
        # pays disk-walk cost up front rather than at first user query.
        self.rebuild_index()

    def rebuild_index(self) -> None:
        """Re-read the registry and rebuild the BM25 index in place.

        Called from :meth:`SkillService.invalidate_skill_cache` on every
        watcher event / evolver write, and once from ``__init__`` for the
        initial build. Idempotent and safe to call concurrently — the
        last writer's index wins; in-flight searches retain their
        previously captured references and finish against a consistent
        snapshot.
        """
        metas = self._registry.list_all()
        if not metas:
            with self._lock:
                self._metas = []
                self._bm25 = None
            return
        tokenized_corpus = [_tokenize(_format_skill_text(m)) for m in metas]
        bm25 = _BM25Okapi(tokenized_corpus)
        # Defensive copy: registry's ``list_all`` hands out its cached
        # list by reference, and a future rebuild replaces (not mutates)
        # it — copying decouples us so the snapshot we serve to readers
        # cannot diverge from the BM25 we paired it with.
        metas_snapshot = list(metas)
        with self._lock:
            self._metas = metas_snapshot
            self._bm25 = bm25

    def search(self, query: str, top_k: int = 50) -> list[ScoredSkill]:
        """Return top-K matches by BM25 over the prebuilt index."""
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        with self._lock:
            bm25 = self._bm25
            metas = self._metas
        if bm25 is None or not metas:
            return []
        scores = bm25.get_scores(query_tokens)
        # Drop zero-score docs and order by descending score, then take top_k.
        # Exclude ``m.always`` skills: they're already injected by
        # ``ContextBuilder`` via ``get_always_skills()`` → ``# Active Skills``
        # block. Letting them ALSO surface here would duplicate the same
        # body in the system prompt (once as Active, once as top-K).
        # Mass-pool entries can't double-inject — ``get_always_skills``
        # only reads ``_registry``, never ``_mass_registry`` — so the
        # filter is only meaningful on the local pool side.
        ranked = sorted(
            ((s, m) for s, m in zip(scores, metas) if s > 0.0 and not m.always),
            key=lambda x: x[0],
            reverse=True,
        )[:top_k]
        return [
            ScoredSkill(name=m.name, score=float(s), source=m.source)
            for s, m in ranked
        ]
