# -*- coding: utf-8 -*-
"""Re-score retrieved chunks with the reranker Raven already has configured.

A vector search compares two embeddings that were produced without ever seeing
each other: the query was encoded before the document existed, and vice versa.
That is what makes it fast enough to scan a whole collection, and also what
makes it approximate -- it cannot notice that a passage mentions the query's
subject only in passing. A reranker reads the query and one passage *together*
and scores that pair directly. It is far too slow to run over a collection and
exactly right for the few dozen candidates a vector search just produced.

So the standard shape, and the one used here, is recall wide then rerank narrow:
ask the vector store for many more candidates than the caller wants, score each
against the query, and keep the top few. Retrieval quality usually gains more
from this than from any amount of chunking work.

The endpoint is the one the operator already configured for EverOS memory
(``~/.everos/raven/everos.toml``, ``[rerank]``), and the three request shapes
mirror what raven's own onboarding probe sends, so a provider that passes the
wizard's test button works here unchanged.

Never raises and never blocks a turn: no configuration, an unreachable endpoint
or an unexpected payload all leave the candidates in their original order.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_TIMEOUT_SECONDS = 15.0


def _everos_rerank() -> dict[str, Any]:
    """The ``[rerank]`` section of the EverOS config, or ``{}``.

    Read with the standard library for the same reason the embedding side does:
    ``raven.config.update_everos`` imports ``tomli_w`` for its write path, which
    this environment does not carry.
    """
    root = os.environ.get("EVEROS_ROOT") or "~/.everos/raven"
    path = Path(root).expanduser() / "everos.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return dict(tomllib.load(handle).get("rerank") or {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("knowledge: cannot read the rerank config: %s", exc)
        return {}


def _request(section: dict, query: str, documents: list[str]) -> tuple[str, dict]:
    """Build the URL and body for the configured provider's protocol.

    The three shapes are raven's (`_probe_rerank` in its onboarding command);
    keeping them identical means the wizard's test button and this call agree on
    what a working endpoint is.
    """
    base_url = str(section["base_url"]).rstrip("/")
    model = str(section["model"])
    provider = section.get("provider")

    if provider == "deepinfra":
        return f"{base_url}/{model}", {"queries": [query] * len(documents), "documents": documents}
    if provider == "dashscope":
        return (
            f"{base_url}/api/v1/services/rerank/text-rerank/text-rerank",
            {
                "model": model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": len(documents)},
            },
        )
    return f"{base_url}/rerank", {"model": model, "query": query, "documents": documents}


def _order_from(provider: Any, payload: dict, count: int) -> list[int] | None:
    """Read the payload as candidate indices, best first.

    ``None`` when the response does not carry a usable ranking, which the caller
    treats as "leave the order alone" rather than as an error.
    """
    if provider == "deepinfra":
        # Scores positionally, not a ranking: sort the indices ourselves.
        scores = payload.get("scores")
        if not isinstance(scores, list) or len(scores) != count:
            return None
        return sorted(range(count), key=lambda i: scores[i], reverse=True)

    results = payload.get("output", {}).get("results") if provider == "dashscope" else payload.get("results")
    if not isinstance(results, list) or not results:
        return None
    order = [item.get("index") for item in results if isinstance(item, dict)]
    if not all(isinstance(index, int) and 0 <= index < count for index in order):
        return None
    return order


async def rerank(query: str, documents: list[str]) -> list[int] | None:
    """Rank ``documents`` against ``query``, returning indices best-first.

    ``None`` means "no opinion" -- unconfigured, unreachable, or an answer that
    could not be read -- and the caller keeps the order it already had.
    """
    section = _everos_rerank()
    if not (section.get("base_url") and section.get("model")) or len(documents) < 2:
        return None

    import httpx

    url, body = _request(section, query, documents)
    headers = {"Content-Type": "application/json"}
    if section.get("api_key"):
        headers["Authorization"] = f"Bearer {section['api_key']}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            logger.warning("knowledge: rerank HTTP %s: %s", response.status_code, response.text[:200])
            return None
        payload = response.json()
        # Inside the try on purpose. A 200 whose body is not a JSON object -- a
        # top-level array from the endpoint or a proxy, or dashscope's
        # `{"output": null}` -- reaches `payload.get` and raises. Outside, that
        # unwinds past the caller that would have kept the vector order, so a
        # working vector search still cost the turn its context and the log
        # blamed retrieval instead of the reranker.
        order = _order_from(section.get("provider"), payload, len(documents))
    except Exception as exc:  # noqa: BLE001 - retrieval must survive a bad reranker
        logger.warning("knowledge: rerank failed, keeping the vector order: %s", exc)
        return None

    if order is None:
        logger.warning("knowledge: rerank returned no usable ranking, keeping the vector order")
    return order
