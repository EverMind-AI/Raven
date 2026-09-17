"""Where a knowledge base gets its vectors.

The endpoint is the one the operator already configured for EverOS memory
(``~/.everos/raven/everos.toml``, ``[embedding]``): an OpenAI-compatible base
URL, a key and a model. Reusing it means a knowledge base needs no second
credential and no picker fed from a provider catalogue.

Reading that file is *not* the same as depending on the EverOS service. Only
the three strings are taken; the request goes straight to the embedding
endpoint. The service is a separate process and has spent whole days
unresponsive, and a knowledge base must not be able to fail for that reason.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

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
    provider: str = ""
    """Who serves the model, when that is known.

    Recorded so a base can be re-embedded later through the same provider: a
    model id does not name a credential, and the base URL a query has to go out
    on is the provider's, not something derivable from the model. Empty for the
    endpoint inherited from the EverOS file, which names no provider."""

    batch_size: int | None = None
    """How many inputs the endpoint accepts in one call, when it is known.

    Stated by the operator (the EverOS file carries ``batch_size``) rather than
    discovered, because the discovery costs a failed request. ``None`` means
    use the default and let the endpoint correct it -- see
    :data:`_DEFAULT_BATCH`."""

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


def _pinned_embedding_config() -> EmbeddingConfig | None:
    """The embedding pair configured in raven's own config, resolved.

    ``knowledge.embeddingModel`` names the model and
    ``knowledge.embeddingProvider`` names who serves it, and the provider's own
    address and key are what the call goes out on -- the same pair every other
    subsystem pin states, for the same reason: a model id does not name a
    credential.

    ``None`` whenever the pair cannot be completed, which hands the caller back
    to the EverOS endpoint rather than failing. An install that never sets this
    behaves exactly as it did before the block existed.
    """
    try:
        from raven.config.raven import load_raven_config
        from raven.config.update_providers import resolve_provider_credentials
    except Exception:
        return None
    try:
        pin = load_raven_config().knowledge
    except Exception as exc:
        logger.debug("knowledge: cannot read the embedding pin ({}); using the everos endpoint", exc)
        return None
    model, provider = pin.embedding_model, pin.embedding_provider
    if not model or not provider:
        return None
    resolved = resolve_provider_credentials(provider)
    if resolved is None:
        logger.warning(
            "knowledge: embedding provider {!r} has no usable credentials; using the everos endpoint instead",
            provider,
        )
        return None
    base_url, api_key = resolved
    # The provider half already said whose credential this is, so the stored
    # `provider/model` spelling has served its purpose: what goes on the wire is
    # the vendor's own id, which is the tail.
    wire_model = model.split("/", 1)[1] if "/" in model else model
    return EmbeddingConfig(model=wire_model, base_url=base_url, api_key=api_key, provider=provider, dimensions=None)


def asking_for(config: EmbeddingConfig, model: str, dimensions: int | None = None) -> EmbeddingConfig:
    """The same endpoint, asked for a different model.

    For a base built before the configured pin moved: the address and the
    credential are still the ones in hand, and only the model has to follow
    the base. Lives here rather than at the call site because an endpoint's
    credential is this module's business -- ``providers.auth`` owns who is
    configured, and no other module should be handling a key to restate one.
    """
    return replace(config, model=model, dimensions=dimensions)


def embedding_config_for(provider: str, model: str, dimensions: int | None = None) -> EmbeddingConfig | None:
    """The endpoint a named provider serves a named model on.

    What a knowledge base needs to be searched with the model it was built
    with, once today's configured pin has moved on: the base recorded who
    served it, and the credential for that provider is still resolvable from
    the same place the pin resolves from.

    ``None`` when the provider has no usable credentials any more -- the
    caller decides whether that means skipping one base or failing.

    The model is used exactly as given. Unlike the configured pin, which is
    stored as ``provider/model`` and has its first segment stripped on the way
    out, this one is already the id that went on the wire -- and stripping a
    namespace that belongs to the model would rewrite
    ``BAAI/bge-large-zh-v1.5`` into a model no endpoint serves.
    """
    if not provider or not model:
        return None
    try:
        from raven.config.update_providers import resolve_provider_credentials
    except Exception:
        return None
    try:
        resolved = resolve_provider_credentials(provider)
    except Exception as exc:
        logger.debug("knowledge: cannot resolve provider {!r} ({})", provider, exc)
        return None
    if resolved is None:
        return None
    base_url, api_key = resolved
    return EmbeddingConfig(model=model, base_url=base_url, api_key=api_key, provider=provider, dimensions=dimensions)


def everos_embedding_config() -> EmbeddingConfig | None:
    """The endpoint the memory backend configured, ignoring raven's own pin.

    Separate from :func:`load_embedding_config` because it answers a different
    question. That one asks what to build a new base with; this one asks where
    a base built before the pin existed came from -- and for a base carrying a
    model the pin does not name, this file is the only record of the endpoint
    that produced its vectors.
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
    batch_size = section.get("batch_size")
    return EmbeddingConfig(
        model=str(model),
        base_url=str(base_url).rstrip("/"),
        api_key=str(api_key),
        batch_size=int(batch_size) if isinstance(batch_size, int) and batch_size > 0 else None,
        dimensions=int(dimensions) if isinstance(dimensions, int) and dimensions > 0 else None,
    )


def load_embedding_config() -> EmbeddingConfig | None:
    """The configured embedding endpoint, or ``None`` when there is not one.

    ``None`` rather than a raise: a deployment with no embedding configured is
    a deployment with no knowledge bases, which is an ordinary state. The
    caller turns it into "configure this first", not into a failed start.

    Raven's own pin wins when it is complete. It is the choice somebody made in
    settings, against a provider they can see; the EverOS file is the endpoint
    inherited from the memory backend, and staying on it after a deliberate
    pick would make the picker a control that changes nothing.
    """
    pinned = _pinned_embedding_config()
    if pinned is not None:
        return pinned
    return everos_embedding_config()


#: Inputs per request when nothing says otherwise. Endpoints disagree by two
#: orders of magnitude -- OpenAI takes 2048, DashScope 20 -- and most do not
#: publish it anywhere a client can read. Low enough to clear the common caps,
#: high enough that a long document is not hundreds of round trips.
_DEFAULT_BATCH = 16

#: Caps learned the only way an endpoint that states none will teach them: by
#: refusing a request. Keyed by endpoint and model and kept for the life of the
#: process, so one document pays the lesson and the rest of the session does
#: not repeat it.
_LEARNED_BATCH: dict[tuple[str, str], int] = {}

#: What a cap being exceeded sounds like. Every one of these is a 400 naming
#: the batch rather than the content, which is what separates "send fewer at a
#: time" from "this input is wrong" -- retrying the second would loop.
_BATCH_COMPLAINT = re.compile(
    r"batch[ _-]?size|too many inputs|number of inputs|exceeds? the maximum number|at most \d+ (inputs|items|texts)",
    re.IGNORECASE,
)


class EmbeddingClient:
    """Turns text into vectors through an OpenAI-compatible endpoint."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def provider(self) -> str:
        """Who serves this endpoint, or ``""`` when the config names nobody."""
        return self._config.provider

    @property
    def declared_dimensions(self) -> int | None:
        """The pinned width, or ``None`` when it has to be measured."""
        return self._config.dimensions

    async def probe_dimensions(self) -> int:
        """Measure the model's vector width by embedding one short string."""
        vectors = await self.embed([_PROBE_TEXT])
        return len(vectors[0])

    @property
    def batch_size(self) -> int:
        """How many inputs to send at once.

        What the config states, narrowed by anything this endpoint has already
        refused. A document is embedded in batches either way: one call with
        every chunk in it is what the endpoints that cap this reject, and the
        rejection takes the whole document down rather than the excess.
        """
        learned = _LEARNED_BATCH.get((self._config.base_url, self._config.model))
        stated = self._config.batch_size or _DEFAULT_BATCH
        return min(stated, learned) if learned else stated

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, one vector each, in the order given.

        Order is part of the contract: the caller pairs the result with the
        chunks it sent positionally, so a reordered response would attach every
        vector to the wrong text. Batches are sent in order and concatenated in
        order, and within a batch the endpoint reports the order it used in
        each item's ``index``, which is sorted on rather than trusted.
        """
        if not texts:
            return []

        vectors: list[list[float]] = []
        start = 0
        while start < len(texts):
            size = self.batch_size
            batch = texts[start : start + size]
            try:
                vectors.extend(await self._embed_batch(batch))
            except EmbeddingError as exc:
                if len(batch) == 1 or not _BATCH_COMPLAINT.search(str(exc)):
                    raise
                # The endpoint has just stated its cap, in the only way it
                # offers: halved rather than parsed out of the message, because
                # the number in it is not reliably there and the retry finds
                # the same answer in at most a few steps.
                learned = max(1, len(batch) // 2)
                _LEARNED_BATCH[(self._config.base_url, self._config.model)] = learned
                logger.warning(
                    "knowledge: {} refused a batch of {}; retrying in batches of {}",
                    self._config.model,
                    len(batch),
                    learned,
                )
                continue
            start += len(batch)

        widths = {len(vector) for vector in vectors}
        if len(widths) != 1:
            raise EmbeddingError(f"embedding endpoint returned mixed widths: {sorted(widths)}")
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One request. Width is checked by the caller, across every batch."""
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
        return vectors


# ── SiliconFlow ───────────────────────────────────────────────────────

SILICONFLOW_HOSTS = ("siliconflow.cn", "siliconflow.com")

#: Input limits for the embedding models SiliconFlow serves, in tokens, from
#: each model card. The default is the smaller number rather than the larger:
#: a model whose real limit is higher only loses some of one input's tail,
#: while a model whose real limit is 512 loses the whole document.
SILICONFLOW_MAX_TOKENS: dict[str, int] = {
    "BAAI/bge-m3": 8192,
    "Pro/BAAI/bge-m3": 8192,
}
SILICONFLOW_DEFAULT_MAX_TOKENS = 512

#: Kept under the model's own limit, because the count below is an estimate and
#: the endpoint refuses the entire request rather than the one input it objects
#: to. The margin is what a wrong estimate costs instead of a failed document.
_TOKEN_HEADROOM = 0.94

#: Non-CJK characters per token. Measured against this endpoint rather than
#: assumed: binary-searching the longest accepted prefix of real documentation
#: pages put it at 2.1 to 2.3, where the usual English-prose rule of four would
#: have said 512 tokens was 2048 characters. Markdown, URLs and code all
#: tokenize far denser than prose, and a knowledge base is mostly those.
_CHARS_PER_TOKEN = 2.0


def _is_cjk(ch: str) -> bool:
    """Whether a character is one a CJK tokenizer spends a whole token on."""
    return "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" or "\uff00" <= ch <= "\uffef"


def estimate_tokens(text: str) -> int:
    """How many tokens ``text`` is likely to cost, erring high.

    Erring high on purpose. The chunker's own estimate is ``utf-8 bytes // 4``,
    which reads a CJK character as three quarters of a token when it is one,
    and reads a URL as a quarter of what it costs. Both make a chunk that looks
    within budget and is not, and the endpoint answers that by refusing the
    request the chunk arrived in.
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return int(cjk + (len(text) - cjk) / _CHARS_PER_TOKEN)


def fit_to_tokens(text: str, budget: int) -> str:
    """``text`` cut to the longest prefix that fits ``budget`` tokens.

    By bisection on the estimate rather than by a characters-per-token
    multiplication, because the cost per character is not uniform across a
    string: a paragraph of prose followed by a code block is cheap then
    expensive, and cutting at a flat ratio lands in the wrong place on both.
    """
    if budget <= 0 or estimate_tokens(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


class SiliconFlowEmbeddingClient(EmbeddingClient):
    """SiliconFlow, where one over-long input refuses the whole request.

    The endpoint answers an input past its model's limit with HTTP 400 and
    ``{"code":20015,"message":"The parameter is invalid"}`` -- naming neither
    which input nor what was wrong with it, and failing every other input in
    the same call. A document of eighteen chunks where two are long indexes
    none of the other sixteen.

    So each input is cut to fit before it is sent. What that costs is the tail
    of an over-long chunk: the chunk is still stored and still shown in full
    when it is retrieved, but its vector speaks for its beginning. That is a
    poor second to chunking to the model's real limit in the first place --
    which is where this belongs -- and a good first to indexing nothing.
    """

    @property
    def max_input_tokens(self) -> int:
        """The configured model's input limit, in tokens."""
        return SILICONFLOW_MAX_TOKENS.get(self._config.model, SILICONFLOW_DEFAULT_MAX_TOKENS)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        budget = int(self.max_input_tokens * _TOKEN_HEADROOM)
        fitted = [fit_to_tokens(text, budget) for text in texts]
        cut = sum(1 for before, after in zip(texts, fitted, strict=True) if before != after)
        if cut:
            logger.warning(
                "knowledge: cut {} of {} inputs to {}'s {}-token limit; their vectors speak for "
                "the start of the text only",
                cut,
                len(texts),
                self._config.model,
                self.max_input_tokens,
            )
        return await super().embed(fitted)


def embedding_client(config: EmbeddingConfig) -> EmbeddingClient:
    """The client for an endpoint: the plain one, or a vendor's own.

    Chosen by host rather than configured, because which vendor is being
    spoken to is a fact about the base URL and not a second thing for an
    operator to get right. Everything else stays on the plain client -- an
    OpenAI-compatible endpoint is what this package targets, and a vendor
    subclass exists only where the vendor departs from it.
    """
    host = (urlparse(config.base_url).hostname or "").lower()
    if any(host == name or host.endswith("." + name) for name in SILICONFLOW_HOSTS):
        return SiliconFlowEmbeddingClient(config)
    return EmbeddingClient(config)
