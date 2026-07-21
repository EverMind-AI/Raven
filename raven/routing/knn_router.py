"""Task-level KNN model router.

For each incoming task: embed it, retrieve the K nearest training tasks from a
prebuilt memory, and pick the model with the best expected value
(``reward - lambda_cost * cost``) averaged over those neighbours. Exposes the
same ``select_model_chain`` interface as the EcoClaw ``ModelRouter`` so the
agent loop can use either interchangeably.

Memory schema (JSON list), one entry per training task::

    {"task_name": str,
     "text": str,               # task description; embedded at load when
                                # "embedding" is absent (keeps the file small
                                # and the vectors consistent with live queries)
     "embedding": [float, ...], # optional precomputed vector; used as-is if set
     "rewards": {model_name: float, ...},
     "costs":   {model_name: float, ...}}

Routing candidates are the intersection of configured models and models that
appear in the memory. Any failure (missing memory, embedding error, no
candidates) yields ``(None, [])`` so the caller falls back to the default model.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import httpx
import numpy as np
from loguru import logger


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    return mat / np.maximum(norms, 1e-8)


class KNNModelRouter:
    """Route each task to the best-value model via KNN over per-model rewards."""

    def __init__(self, routing_cfg, default_model: str | None = None):
        self._k = max(1, int(routing_cfg.k))
        self._lambda = float(routing_cfg.lambda_cost)
        self._embed_url = routing_cfg.embedding_endpoint
        self._config_models = [m.model for m in routing_cfg.models if m.model]
        # Agent's configured default model: the safe home the router only leaves
        # with enough evidence. None -> the "already on default" / margin gates
        # are inert (caller still falls back to its own model on a None return).
        self._default_model = default_model
        self._min_similarity = float(getattr(routing_cfg, "min_similarity", 0.6))
        self._min_similar = max(1, int(getattr(routing_cfg, "min_similar_neighbors", 4)))
        self._min_memory_size = max(1, int(getattr(routing_cfg, "min_memory_size", 10)))
        self._min_margin = float(getattr(routing_cfg, "min_margin", 0.0))

        self._task_names: list[str] = []
        self._embeddings = np.empty((0, 0))
        self._rewards: list[dict[str, float]] = []
        self._costs: list[dict[str, float]] = []
        self._candidates: list[str] = []
        self._load_memory(routing_cfg.memory_path)

    def _load_memory(self, path: str) -> None:
        if not path:
            logger.warning("KNNModelRouter: no memory_path configured; routing disabled")
            return
        try:
            entries = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("KNNModelRouter: failed to load memory {}: {}", path, e)
            return
        if not entries:
            logger.warning("KNNModelRouter: empty memory at {}", path)
            return

        self._task_names = [e.get("task_name", "") for e in entries]
        self._rewards = [e.get("rewards", {}) for e in entries]
        self._costs = [e.get("costs", {}) for e in entries]

        mat = self._resolve_embeddings(entries, path)
        if mat is None:
            return
        self._embeddings = _normalize(mat)

        mem_models = {m for r in self._rewards for m in r}
        self._candidates = [m for m in self._config_models if m in mem_models]
        missing_reward = [m for m in self._config_models if m not in mem_models]
        if missing_reward:
            logger.warning("KNNModelRouter: configured models absent from memory (skipped): {}", missing_reward)
        logger.info(
            "KNNModelRouter: loaded {} tasks, candidates={}, k={}, lambda={}",
            len(entries),
            self._candidates,
            self._k,
            self._lambda,
        )

    def _resolve_embeddings(self, entries: list[dict], path: str) -> "np.ndarray | None":
        """Use precomputed ``embedding`` vectors if present; otherwise embed each
        entry's ``text`` one at a time (batch=1, to match how live queries are
        embedded) via the configured endpoint, cached next to the memory file."""
        if all("embedding" in e for e in entries):
            return np.array([e["embedding"] for e in entries], dtype=np.float32)

        texts = [e.get("text") or e.get("task_name", "") for e in entries]
        if not self._embed_url:
            logger.warning("KNNModelRouter: memory has no embeddings and no embedding_endpoint; routing disabled")
            return None

        cache = self._read_emb_cache(path)
        missing = [t for t in dict.fromkeys(texts) if t not in cache]
        try:
            for t in missing:
                payload = json.dumps({"texts": [t]}).encode()
                req = urllib.request.Request(
                    self._embed_url, data=payload, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    cache[t] = json.loads(resp.read())["embeddings"][0]
        except Exception as e:
            logger.warning("KNNModelRouter: failed to embed memory texts: {}", e)
            return None
        if missing:
            self._write_emb_cache(path, cache)
            logger.info("KNNModelRouter: embedded {} memory texts at load", len(missing))
        return np.array([cache[t] for t in texts], dtype=np.float32)

    def _emb_cache_path(self, path: str) -> Path:
        # Cache in a user dir (not next to the memory file, which may be a
        # read-only repo asset). Keyed by endpoint + memory path so a different
        # embedder or memory file gets its own cache.
        key = hashlib.sha1(f"{self._embed_url}|{Path(path).resolve()}".encode()).hexdigest()[:16]
        d = Path.home() / ".raven" / "knn_embcache"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d / f"{key}.json"

    def _read_emb_cache(self, path: str) -> dict:
        try:
            return json.loads(self._emb_cache_path(path).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_emb_cache(self, path: str, cache: dict) -> None:
        try:
            self._emb_cache_path(path).write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass  # read-only location: skip caching, re-embed next load

    async def _embed(self, prompt: str) -> np.ndarray | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self._embed_url, json={"texts": [prompt]})
                resp.raise_for_status()
                vec = np.array(resp.json()["embeddings"][0], dtype=np.float32)
            return vec / max(float(np.linalg.norm(vec)), 1e-8)
        except Exception as e:
            logger.warning("KNNModelRouter: embedding failed: {}", e)
            return None

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        """Return ``(primary_model, [fallback_models])``; ``(None, [])`` to use default."""
        # Cold-start / structural gate: too few candidates or too little memory
        # to make a trustworthy decision -> keep the caller's default model.
        if len(self._candidates) < 2 or self._embeddings.shape[0] < self._min_memory_size:
            return None, []

        q = await self._embed(prompt)
        if q is None:
            return None, []

        # Routing is an optional enhancement: never let a KNN/data error crash
        # the turn -> any failure degrades to the caller's default model.
        try:
            sims = self._embeddings @ q
            top = np.argsort(-sims)[: self._k]

            # Similar-support gate: the pick is trusted only when enough
            # retrieved neighbours are actually similar (cosine >=
            # min_similarity). An off-distribution query (e.g. casual chat) has
            # few similar neighbours and stays on the default. Scoring uses only
            # these similar neighbours so far-away tasks do not dilute rewards.
            similar = [int(i) for i in top if float(sims[i]) >= self._min_similarity]
            if len(similar) < self._min_similar:
                return None, []

            scores: dict[str, float] = {}
            for m in self._candidates:
                # Average reward and cost over the SAME neighbours (those that
                # carry this model's reward), so a neighbour missing the model
                # does not dilute the cost with a zero.
                pairs = [(self._rewards[i][m], self._costs[i].get(m, 0.0)) for i in similar if m in self._rewards[i]]
                if not pairs:
                    continue
                reward = float(np.mean([r for r, _ in pairs]))
                cost = float(np.mean([c for _, c in pairs]))
                scores[m] = reward - self._lambda * cost

            if not scores:
                return None, []

            ranked = sorted(scores, key=lambda m: scores[m], reverse=True)
            primary = ranked[0]

            # Already on the default model -> no switch needed.
            if primary == self._default_model:
                return None, []

            # Margin gate: only leave the default if the pick beats it clearly.
            baseline = scores.get(self._default_model)
            if baseline is not None and scores[primary] - baseline < self._min_margin:
                return None, []

            fallbacks = ranked[1:]
            logger.info("KNNModelRouter: routed to {} (scores={})", primary, scores)
            return primary, fallbacks
        except Exception as e:
            logger.warning("KNNModelRouter: routing failed, using default: {}", e)
            return None, []
