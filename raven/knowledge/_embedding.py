"""Where a knowledge base gets its vectors.

The endpoint is the one the operator already configured for EverOS memory
(``~/.everos/raven/everos.toml``, ``[embedding]``): an OpenAI-compatible base
URL, a key and a model. Reusing it means a knowledge base needs no second
credential and no picker fed from a provider catalogue -- the earlier attempt
walked one and found it empty, so no base could be created at all.

Reading that file is *not* the same as depending on the EverOS service. Only
the three strings are taken; the request goes straight to the embedding
endpoint. The service is a separate process and has spent whole days
unresponsive, and a knowledge base must not be able to fail for that reason.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger

_TIMEOUT_S = 120.0

# What a probe embeds to learn a model's width. Short on purpose: the answer is
# the vector's length, and nothing about it depends on the text.
_PROBE_TEXT = "probe"


class EmbeddingError(RuntimeError):
    """The endpoint could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """A resolved embedding endpoint."""

    model: str
    base_url: str
    api_key: str
    dimensions: int | None = None
    """The vector width, when the operator pinned one.

    ``None`` means ask the model. There is deliberately no default: this config
    was inherited with 1024 hardcoded on the claim that onboarding guarantees
    it, and the model actually configured in the deployment that claim came
    from returns 4096. A wrong width is worse than an unknown one -- it sizes
    the collection to something no vector will fit, and the failure surfaces at
    the first insert with nothing pointing back to here.
    """


def everos_config_path() -> Path:
    """Where raven keeps the EverOS config: the root raven recorded.

    Through ``everos_root`` rather than off ``EVEROS_ROOT``, which raven
    deliberately treats as an output. It *writes* that variable from
    ``plugins.config["everos-memory"]["root"]`` so the choice is a recorded
    decision, and its own module says why reading it back as an input is not
    safe: a root inherited from an ambient environment and never written down
    is silent data loss, because the memories stay on disk while raven reports
    none.

    Reading it as an input made this module answer two different things for one
    installation -- the recorded root once the memory backend had booted and
    exported the variable, and a hardcoded ``~/.everos/raven`` before that or in
    a process that never boots it. On the deployment this was found on, those
    were two different files with two different endpoints, one of them keyless.

    ``everos_root`` already covers the install that has never written the file:
    it falls back on its own.
    """
    from raven.config.update_everos import everos_root

    return everos_root() / "everos.toml"


def load_embedding_config() -> EmbeddingConfig | None:
    """The configured embedding endpoint, or ``None`` when there is not one.

    ``None`` rather than a raise: a deployment with no embedding configured is
    a deployment with no knowledge bases, which is an ordinary state. The
    caller turns it into "configure this first", not into a failed start.
    """
    path = everos_config_path()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            section = dict(tomllib.load(handle).get("embedding") or {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("knowledge: cannot read {}: {}", path, exc)
        return None

    model, base_url, api_key = section.get("model"), section.get("base_url"), section.get("api_key")
    if not (model and base_url and api_key):
        return None
    dimensions = section.get("dimensions")
    return EmbeddingConfig(
        model=str(model),
        base_url=str(base_url).rstrip("/"),
        api_key=str(api_key),
        dimensions=int(dimensions) if isinstance(dimensions, int) and dimensions > 0 else None,
    )


class EmbeddingClient:
    """Turns text into vectors through an OpenAI-compatible endpoint."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def declared_dimensions(self) -> int | None:
        """The pinned width, or ``None`` when it has to be measured."""
        return self._config.dimensions

    async def probe_dimensions(self) -> int:
        """Measure the model's vector width by embedding one short string."""
        vectors = await self.embed([_PROBE_TEXT])
        return len(vectors[0])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, one vector each, in the order given.

        Order is part of the contract: the caller pairs the result with the
        chunks it sent positionally, so a reordered response would attach every
        vector to the wrong text. The endpoint reports the order it used in
        each item's ``index``, and this sorts by it rather than trusting the
        array to come back as sent.
        """
        if not texts:
            return []

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                response = await client.post(
                    f"{self._config.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self._config.model, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(
                f"embedding endpoint returned {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding endpoint unreachable: {exc}") from exc
        except ValueError as exc:
            raise EmbeddingError(f"embedding endpoint returned a non-JSON body: {exc}") from exc

        items = payload.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingError(
                f"embedding endpoint returned {len(items) if isinstance(items, list) else 'no'} vectors for {len(texts)} inputs"
            )

        ordered = sorted(items, key=lambda item: item.get("index", 0))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("embedding endpoint returned an item with no vector")
            vectors.append([float(value) for value in vector])

        widths = {len(vector) for vector in vectors}
        if len(widths) != 1:
            raise EmbeddingError(f"embedding endpoint returned mixed widths: {sorted(widths)}")
        return vectors
