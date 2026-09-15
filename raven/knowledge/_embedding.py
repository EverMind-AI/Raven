"""Where a knowledge base gets its vectors.

An OpenAI-compatible base URL, a key and a model, read from raven's own
``embedding`` config section. A knowledge base indexes and answers inside the
gateway process and never speaks to the memory service, so its endpoint is
raven's to hold.

It was EverOS's to hold, read straight out of ``everos.toml``. That made a
feature with nothing to do with memory fail whenever the memory plugin was
absent, uninstalled or simply not the configured backend -- with no message
naming the cause. The reader below still falls back to that file so an
operator who has not moved the values keeps working, and says so once.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
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
    dimensions: int | None = None
    """The vector width, when the operator pinned one.

    ``None`` means ask the model. There is deliberately no default: this config
    was inherited with 1024 hardcoded on the claim that onboarding guarantees
    it, and the model actually configured in the deployment that claim came
    from returns 4096. A wrong width is worse than an unknown one -- it sizes
    the collection to something no vector will fit, and the failure surfaces at
    the first insert with nothing pointing back to here.
    """


def _legacy_everos_config_path() -> Path | None:
    """Where an EverOS config would be, for an install that has not moved yet.

    Three places, in the order the memory plugin itself resolves them, and with
    plain ``tomllib`` rather than by importing the plugin -- or a knowledge base
    would again be unusable exactly where the plugin is not installed:

    1. the root raven recorded in ``plugins.config["everos-memory"]["root"]``;
    2. the machine-wide legacy location, when this is the default installation
       -- that path is not derived from the config directory, so an instance
       running from a moved config must not adopt the default install's root;
    3. the current default under the data directory.

    ``None`` when none of them holds a file. An install that predates root
    recording reaches (2), which is the case this fallback exists for: before
    it, such an install lost its knowledge-base endpoint entirely while a valid
    file sat on disk.
    """
    from raven.config.paths import get_data_dir
    from raven.config.raven import load_raven_config
    from raven.home import get_config_path

    candidates: list[Path] = []
    try:
        slice_ = (load_raven_config().plugins.config or {}).get("everos-memory") or {}
    except Exception as exc:  # noqa: BLE001 - an unreadable config is not this module's to report
        logger.warning("knowledge: cannot read raven config: {}", exc)
        slice_ = {}
    if slice_.get("root"):
        candidates.append(Path(str(slice_["root"])).expanduser())
    if get_config_path() == Path.home() / ".raven" / "config.json":
        candidates.append(Path.home() / ".everos" / "raven")
    candidates.append(get_data_dir() / "everos")

    for root in candidates:
        if (root / "everos.toml").is_file():
            return root / "everos.toml"
    return None


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
    return EmbeddingConfig(model=wire_model, base_url=base_url, api_key=api_key, dimensions=None)


def adopt_legacy_endpoint(raw: dict) -> bool:
    """Copy the memory backend's endpoint into raven's own block, in ``raw``.

    Mutates the caller's already-loaded config dict rather than writing a file:
    ``raven doctor --fix`` applies several corrections in one atomic write, and
    a second writer here would race it.

    The rendering lives with the reader that owns the shape -- which three
    values matter and how they are spelled on disk -- so a caller states the
    intent and nothing else.
    """
    legacy = read_legacy_embedding()
    if legacy is None:
        return False
    block = raw.setdefault("embedding", {})
    block["model"] = legacy.model
    block["baseUrl"] = legacy.base_url
    block["apiKey"] = legacy.api_key
    if legacy.dimensions:
        block["dimensions"] = legacy.dimensions
    return True


def endpoint_is_ravens_own() -> bool:
    """Whether raven's own config already carries a complete endpoint.

    Asked by ``raven doctor`` so it can offer to move one that is still only in
    the memory backend's file. A question rather than a field read: which three
    values make an endpoint usable is this module's rule, and a second copy of
    it elsewhere is how two surfaces come to disagree about the same config.
    """
    from raven.config.raven import load_raven_config

    try:
        section = load_raven_config().embedding
    except Exception as exc:  # noqa: BLE001 - an unreadable config is not this module's to report
        logger.warning("knowledge: cannot read raven config: {}", exc)
        return False
    return bool(section.model and section.base_url and section.api_key)


def read_legacy_embedding() -> "EmbeddingConfig | None":
    """The ``[embedding]`` section of the EverOS config, or ``None``."""
    path = _legacy_everos_config_path()
    if path is None or not path.is_file():
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


def load_embedding_config() -> EmbeddingConfig | None:
    """The configured embedding endpoint, or ``None`` when there is not one.

    Three sources, in the order a deliberate choice outranks an inherited one:
    the knowledge pin, raven's own ``embedding`` section, then the EverOS file
    for an operator who has not moved the values across yet. ``None`` rather
    than a raise: a deployment with no embedding configured is a deployment
    with no knowledge bases, which is an ordinary state. The caller turns it
    into "configure this first", not into a failed start.
    """
    from raven.config.raven import load_raven_config

    pinned = _pinned_embedding_config()
    if pinned is not None:
        return pinned

    try:
        section = load_raven_config().embedding
    except Exception as exc:  # noqa: BLE001 - fall through to the legacy file rather than fail the caller
        logger.warning("knowledge: cannot read raven config: {}", exc)
        section = None

    if section is not None and section.model and section.base_url and section.api_key:
        return EmbeddingConfig(
            model=section.model,
            base_url=section.base_url.rstrip("/"),
            api_key=section.api_key,
            dimensions=section.dimensions if section.dimensions and section.dimensions > 0 else None,
        )

    legacy = read_legacy_embedding()
    if legacy is not None:
        logger.warning(
            "knowledge: embedding endpoint read from the EverOS config; "
            "run `raven doctor --fix` to move it into raven's own `embedding` section"
        )
    return legacy


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
