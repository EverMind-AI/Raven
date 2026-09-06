"""What a model costs per token and how much context it takes.

Both are facts about a provider's catalogue, so they are decided here and not by
whoever is about to report a number. They used to live in ``token_wise.pricing``
next to the cost formula, which put a provider decision outside
``raven.providers`` -- and a decision outside its module grows a second copy: the
benchmark runner carried its own rate table, and the window resolution grew an
OpenRouter fallback that answered for vendors OpenRouter does not serve.

Two questions, deliberately answered from different places:

* **rates** price a call after it happened. A wrong figure costs an inaccurate
  total, so the ladder can reach for a community-maintained catalogue.
* **the context window** sizes trimming, so it shapes the next request. Only the
  tables that also route may answer it, and an unknown window is answered with
  ``None`` -- the caller keeps its configured default, which is honest, where a
  window borrowed from another vendor is silently wrong.
"""

from __future__ import annotations

import pathlib
import re
import sys
import threading
import time
from functools import lru_cache

import httpx
from loguru import logger

from raven.providers import model_catalog_cache

#: One home for the window ladder's documented fallback: an unknown model gets
#: this many tokens of headroom rather than a number invented at the call site.
DEFAULT_CONTEXT_WINDOW_TOKENS = 65_536

# Output ceiling for a model the catalogue does not know -- self-hosted
# deployments, gateways, models newer than the table. Not a default in the
# sense the surveyed agents use one (theirs applies to every model, mapped or
# not); this only answers where the catalogue cannot, so it is picked for
# breadth rather than for any single model: Claude Code defaults to 16000, the
# Anthropic SDK suggests ~16000 non-streaming, and gpt-4o's real ceiling is
# also 16384. Unmapped backends are OpenAI-compatible servers in practice,
# which clamp an over-large value rather than rejecting it.
DEFAULT_MAX_OUTPUT_TOKENS = 16384

#: Rate pair: (prompt_cost_per_token, completion_cost_per_token) in USD.
#: Keep this table small -- it is a fallback for brand-new models that LiteLLM
#: has not indexed yet. Check LiteLLM first before adding here.
_FALLBACK_PRICING: dict[str, tuple[float, float]] = {
    # OpenRouter model pages (snapshot 2026-03)
    "z-ai/glm-4.5-air": (0.13e-6, 0.85e-6),  # $0.13/$0.85 per 1M
}

# Live OpenRouter price table, fetched lazily and cached 1h in-process.
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_CACHE_TTL = 3600
_OPENROUTER_CACHE: dict[str, dict] = {}
_OPENROUTER_CACHE_TIME: float = 0.0
# Monotonic stamp of the last background warm attempt (0 = never), and how
# long a failed one waits before another is allowed. See
# warm_catalog_in_background.
_WARM_AT: float = 0.0
_WARM_RETRY_SECONDS = 300.0


def _litellm_price_table() -> dict:
    """LiteLLM's static price table, or an empty dict if it cannot be imported."""
    try:
        from raven.providers.litellm_setup import import_litellm

        return getattr(import_litellm(), "model_cost", None) or {}
    except Exception:
        return {}


def _table_entry(model: str) -> dict | None:
    """The table row keyed exactly by this model id, or None.

    Deliberately no prefix-stripping fallback. It looked like a free replacement
    for asking LiteLLM, but the ask does more than key normalization: for an
    "openrouter/<vendor>/<model>" candidate it derives OpenRouter's own numbers,
    which are in no table row -- and stripping to the direct row answered three
    MiniMax models with the direct figure where LiteLLM had reported OpenRouter's.
    The table is read here to skip the ask where the ask cannot be made; it does
    not replace it.
    """
    entry = _litellm_price_table().get(model)
    return entry if isinstance(entry, dict) else None


@lru_cache(maxsize=1)
def _drivers_dir() -> pathlib.Path | None:
    """Where the installed LiteLLM keeps its per-provider drivers."""
    try:
        from raven.providers.litellm_setup import import_litellm

        return pathlib.Path(import_litellm().__file__).parent / "llms"
    except Exception:
        return None


def _may_prompt(model: str) -> bool:
    """Would handing this model to LiteLLM start an interactive login?

    Three of its drivers ship a device-flow authenticator, and every entry point
    that resolves a model reaches it -- ``get_model_info``, ``cost_per_token`` and
    ``validate_environment`` alike. With no token file the call prints a device
    code to stdout and blocks. Any segment counts, not just the first: a bare id
    and its ``openrouter/`` alias reach the same driver, so checking only the
    head lets the second candidate hang the lookup anyway.

    Asked of the installed package rather than a snapshot of it -- a driver is one
    that ships ``authenticator.py``. A frozen list would have to be regenerated on
    every LiteLLM bump, and a stale one brings the hang back for the vendor it
    missed; this cannot go stale. The check is a stat, and the callers have already
    paid for the import.
    """
    drivers = _drivers_dir()
    if drivers is None:
        return False
    return any((drivers / part / "authenticator.py").exists() for part in model.split("/") if part)


def _numeric(entry: dict | None, *fields: str) -> float | None:
    """First numeric value among ``fields``, or None.

    The table ships a self-documenting sample row whose numeric-looking fields
    hold prose, so the type check is load-bearing rather than defensive.
    """
    if not isinstance(entry, dict):
        return None
    for field in fields:
        value = entry.get(field)
        if isinstance(value, (int, float)) and value:
            return float(value)
    return None


def is_plan_billed(model: str) -> bool:
    """Is this model's provider billed by subscription rather than per token?

    Asked wherever a dollar figure is about to be reported, because on a
    subscription there is no per-token figure to report -- not even zero, which
    reads as free.
    """
    from raven.providers.registry import find_by_model

    spec = find_by_model(model)
    return bool(spec and spec.billing == "plan")


def _candidates(model: str) -> list[str]:
    """Every id LiteLLM's table might file this model under. See ``providers.wire``."""
    from raven.providers.wire import metadata_candidates

    return metadata_candidates(model)


def _try_litellm_rates(model: str, input_tokens: int, output_tokens: int) -> tuple[float, float] | None:
    """Ask LiteLLM for per-token rates. Returns (prompt_rate, completion_rate) or None."""
    try:
        from raven.providers.litellm_setup import import_litellm

        litellm = import_litellm()
    except Exception:
        return None

    # litellm.cost_per_token expects *at least* 1 non-zero token to compute.
    # We pass synthetic tokens to recover the per-token rate.
    probe_in = input_tokens if input_tokens else 1
    probe_out = output_tokens if output_tokens else 1

    for candidate in _candidates(model):
        if _may_prompt(candidate):
            # Skipped, not read from the table: the rows these families have are
            # priced at zero, which this function already treats as unknown, so
            # reading them would add a branch that cannot fire. The caller falls
            # through to the remaining tiers, which is what any model LiteLLM does
            # not price already does.
            continue
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=candidate, prompt_tokens=probe_in, completion_tokens=probe_out
            )
        except Exception:
            continue
        if prompt_cost is None or completion_cost is None:
            continue
        if prompt_cost == 0 and completion_cost == 0:
            # LiteLLM returns (0, 0) when the model is unknown -- treat as miss.
            continue
        return prompt_cost / probe_in, completion_cost / probe_out

    return None


def _fresh_openrouter_models() -> dict[str, dict]:
    """The gateway's own table, but only while it is still current.

    Deliberately not :func:`_cache_only_openrouter_models`. That one answers with
    a copy of any age, which is right for sizing a window -- a stale window is
    still this model's window, roughly. It is wrong for the price tier that runs
    ahead of LiteLLM: a copy old enough to have expired is not better evidence
    than the router's table, and letting it answer would suppress the refetch
    that the expiry exists to trigger.

    Never fetches, for the same reason the any-age reader does not: this is read
    after every completion, on the event loop.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME

    if not _OPENROUTER_CACHE:
        disk = model_catalog_cache.load()
        if disk is None:
            return {}
        _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME = disk
    if _OPENROUTER_CACHE and (time.time() - _OPENROUTER_CACHE_TIME) < _OPENROUTER_CACHE_TTL:
        return _OPENROUTER_CACHE
    return {}


def _cache_only_openrouter_models() -> dict[str, dict]:
    """Whatever OpenRouter table is already on hand, without a network call.

    For a caller inside object construction or an asyncio event loop --
    ``AgentLoop.__init__`` and a ``/model`` switch, both of which resolve a
    context window before there is a request to size. Those callers want
    whatever is already on hand: an in-process cache of any age answers, then
    an on-disk cache of any age, then an empty table -- the network is never
    touched, because a synchronous ``httpx.Client`` there would block startup
    or freeze the running event loop for up to 10s. A stale answer only costs
    a stale window; a blocked event loop costs the whole turn.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME

    if _OPENROUTER_CACHE:
        return _OPENROUTER_CACHE
    disk = model_catalog_cache.load()
    if disk is not None:
        _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME = disk
        return _OPENROUTER_CACHE
    return {}


def _fetch_openrouter_models(*, allow_fetch: bool = True) -> dict[str, dict]:
    """Return OpenRouter's model table, fetched live and cached 1h in-process.

    Each entry is ``{"pricing": ..., "context_length": ...}``, double-keyed by
    the full id and the bare alias. On any network failure, returns the stale
    cache (or an empty dict) -- pricing must never raise into the cost path.

    ``allow_fetch=False`` delegates to ``_cache_only_openrouter_models``; see
    there for what it changes. The per-call usage path is the place that still
    refreshes normally -- it already runs inside an ``await``, and is where a
    stale price or window is supposed to catch up.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME

    if not allow_fetch:
        return _cache_only_openrouter_models()

    now = time.time()
    if _OPENROUTER_CACHE and (now - _OPENROUTER_CACHE_TIME) < _OPENROUTER_CACHE_TTL:
        return _OPENROUTER_CACHE

    # Disk tier: warm-start (or pick up a sibling process's fresher fetch)
    # from a fresh on-disk cache without touching the network.
    disk = model_catalog_cache.load()
    if disk is not None and (now - disk[1]) < _OPENROUTER_CACHE_TTL:
        _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME = disk
        return _OPENROUTER_CACHE

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("rates: OpenRouter models fetch failed ({}), degrading", exc)
        if _OPENROUTER_CACHE:
            return _OPENROUTER_CACHE
        if disk is not None:
            _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME = disk
            return _OPENROUTER_CACHE
        return {}

    cache: dict[str, dict] = {}
    for model in data.get("data", []):
        model_id = model.get("id", "")
        if not model_id:
            continue
        arch = model.get("architecture") or {}
        mods = arch.get("input_modalities")
        entry = {
            "pricing": model.get("pricing") or {},
            "context_length": model.get("context_length"),
            # What the model accepts as input ("text" / "image" / "audio" /
            # "file" / "video"). The catalog is fetched for prices, and it is
            # also the only published answer to "can this model see" that
            # states itself for every model it lists -- see
            # ``capabilities.supports_vision``.
            "input_modalities": list(mods) if isinstance(mods, list) and mods else None,
        }
        cache[model_id] = entry
        if "/" in model_id:
            cache.setdefault(model_id.split("/", 1)[1], entry)

    _OPENROUTER_CACHE = cache
    _OPENROUTER_CACHE_TIME = time.time()
    model_catalog_cache.save(cache)
    return cache


def warm_catalog_in_background() -> None:
    """Start filling the catalog off the request path, without blocking a turn.

    The pricing path cannot be relied on to do it. It asks LiteLLM's static
    table first and only reaches this catalog when that table *misses*, so for
    every model LiteLLM does carry -- which is every model Raven ships a default
    for -- the catalog is never fetched and a reader like
    :func:`openrouter_input_modalities` has nothing to read, forever.

    Called instead of fetching inline because the fetch is synchronous with a
    10s timeout: on a machine that cannot reach the host, doing it in the turn
    would stall the turn. A cold caller therefore degrades until the fetch lands.

    Retried on a cooldown rather than attempted once. An attempt that fails
    proves nothing about the next one -- the first turn of a session routinely
    runs before a VPN is up or a proxy has authenticated -- and a single latched
    attempt would leave the reader answering from an empty catalog for the whole
    process. A success needs no cooldown: a *fresh* cache is itself the guard --
    fresh, not merely non-empty, because ``_cached_catalog_only`` adopts a disk
    table of any age (leaving its timestamp at zero), and a days-old table must
    not suppress the warm for the life of the process.
    """
    global _WARM_AT

    if _OPENROUTER_CACHE and _OPENROUTER_CACHE_TIME and time.time() - _OPENROUTER_CACHE_TIME < _OPENROUTER_CACHE_TTL:
        return
    now = time.monotonic()
    if _WARM_AT and now - _WARM_AT < _WARM_RETRY_SECONDS:
        return
    _WARM_AT = now

    # Resolved here rather than inside the thread. A thread body that looks the
    # name up on entry can lose a race with whoever patched it -- a test seam
    # restored between ``start()`` and the thread's first bytecode would send a
    # real request from inside the suite and write the real cache file.
    fetch = _fetch_openrouter_models

    def _run() -> None:
        try:
            fetch()
        except Exception as exc:  # the fetch degrades internally; a thread must not die loudly
            logger.debug("rates: background catalog warm failed ({})", exc)

    threading.Thread(target=_run, name="raven-model-catalog-warm", daemon=True).start()


def _cached_catalog_only() -> dict[str, dict]:
    """Whatever catalog is already in hand, at any age, without fetching.

    ``_fetch_openrouter_models`` is synchronous with a one-hour TTL, so calling
    it from a request path would hand one turn a stall whenever the hour rolls
    over. Prices are why that TTL is short; a model's input modalities are not,
    so this reader takes a stale table happily and an absent one as "no answer".
    Filling an absent one is :func:`warm_catalog_in_background`'s job.
    """
    global _OPENROUTER_CACHE

    if _OPENROUTER_CACHE:
        return _OPENROUTER_CACHE
    disk = model_catalog_cache.load()
    if disk is None:
        return {}
    # Re-checked after the read, not just before it: ``load()`` touches the
    # filesystem and releases the GIL, so a background warm can land in that
    # window with both a fresher table and a fresh ``_OPENROUTER_CACHE_TIME``.
    # Overwriting it with this stale copy would leave that timestamp vouching
    # for the wrong table. Narrows the race without closing it -- there is no
    # lock, so a warm landing between this read and the assignment still
    # loses -- accepted because the stale table is only ever one fetch TTL
    # from correcting itself.
    if _OPENROUTER_CACHE:
        return _OPENROUTER_CACHE
    # Kept so the next lookup does not re-read and re-parse the file.
    # ``_OPENROUTER_CACHE_TIME`` is deliberately left alone: the fetch reads it
    # to decide freshness, and this table is of unknown age -- good enough for a
    # modality question, not to be mistaken for fresh pricing.
    _OPENROUTER_CACHE = disk[0]
    return _OPENROUTER_CACHE


def openrouter_input_modalities(model: str) -> tuple[str, ...] | None:
    """What the catalog says ``model`` accepts as input, or ``None``.

    ``None`` means the catalog has no entry (or one written before this field
    was kept), never "text only": this source states itself for every model it
    lists, so silence is absence rather than a denial.

    Matched on the full id and then the bare alias, case-folded -- the catalog
    spells every id it publishes in lower case, while a routed id need not
    (``minimax/MiniMax-M2``). Punctuation is *not* normalized away, and that
    restraint is the point: an id that survives only a fuzzier match is an id
    this catalog does not actually list, and the only thing a wrong match can do
    here is deny vision to a model that has it. ``azure/`` and the local
    runtimes take a user-chosen deployment or tag name where every other
    provider takes a model id, so ``azure/gpt4`` and ``ollama/phi4`` would join
    against ``openai/gpt-4`` and ``microsoft/phi-4`` on a punctuation-stripping
    key and lose every picture, silently, on a deployment that may well serve a
    vision model. Losing the fuzzy tier costs nothing measurable: on the live
    catalog every model it additionally matched either already answers "can see"
    (the default when there is no answer at all) or is one of these false
    denials.

    Reads only what is already cached -- see :func:`_cached_catalog_only`.
    """
    key = model.removeprefix("openrouter/").lower()
    table = _cached_catalog_only()
    entry = table.get(key)
    if entry is None and "/" in key:
        entry = table.get(key.split("/", 1)[1])
    if not entry:
        return None
    mods = entry.get("input_modalities")
    return tuple(mods) if isinstance(mods, list) and mods else None


_DIGIT_HYPHEN_DIGIT = re.compile(r"(?<=\d)-(?=\d)")


def _dotted_version_variants(key: str) -> list[str]:
    """Dotted spellings of a hyphenated version number, most likely first.

    Vendors and OpenRouter disagree on the separator: the id Raven routes with
    is Anthropic's ``claude-sonnet-4-5``, while OpenRouter files the same model
    as ``claude-sonnet-4.5``. Exact-key lookup therefore missed the default
    model Raven itself recommends, and every turn reported a cost of None.

    One variant per digit-hyphen-digit boundary (``llama-3-3-70b`` must become
    ``llama-3.3-70b``, not ``llama-3.3.70b``), then the all-boundaries form for
    ids that really do carry two dots. Only ever consulted after the exact key
    misses, so a wrong guess degrades to the same None it replaces.
    """
    spots = [m.start() for m in _DIGIT_HYPHEN_DIGIT.finditer(key)]
    variants = [key[:i] + "." + key[i + 1 :] for i in spots]
    if len(spots) > 1:
        variants.append(_DIGIT_HYPHEN_DIGIT.sub(".", key))
    return variants


def _lookup_openrouter_entry(model: str, *, allow_fetch: bool = True, table: dict | None = None) -> dict | None:
    """This model's row in OpenRouter's catalogue, or None.

    Only for ids that name OpenRouter. The table was once consulted for every id,
    which reads across vendors: a self-hosted ``hosted_vllm/qwen3-32b`` matched
    OpenRouter's ``qwen/qwen3-32b`` and was reported at a price and a context
    window belonging to somebody else's deployment. What made it wrong was asking
    this table about a request that does not go to OpenRouter -- not the bare
    alias, which stays because within OpenRouter's own namespace a bare id names
    the same model the full one does.

    ``allow_fetch=False`` reaches ``_cache_only_openrouter_models`` directly
    rather than ``_fetch_openrouter_models(allow_fetch=False)`` -- the latter is
    the name a test double stands in for with the fetch's old zero-argument
    signature, and that double does not declare ``allow_fetch``.
    """
    if not model.startswith("openrouter/"):
        return None
    key = model.removeprefix("openrouter/")
    if table is None:
        table = _fetch_openrouter_models() if allow_fetch else _cache_only_openrouter_models()
    for candidate in (key, *_dotted_version_variants(key)):
        entry = table.get(candidate)
        if entry is None and "/" in candidate:
            entry = table.get(candidate.split("/", 1)[1])
        if entry is not None:
            return entry
    return None


def _try_openrouter_rates(model: str, *, table: dict | None = None) -> tuple[float, float] | None:
    """Look up live OpenRouter per-token rates. Returns rates or None.

    ``table`` supplies an already-resolved catalogue, which is what the ladder's
    first tier passes: pricing runs after every completion, on the event loop, and
    must not be the thing that blocks a turn on an HTTP round-trip. Omitted, this
    fetches as before.
    """
    entry = _lookup_openrouter_entry(model, table=table)
    if not entry:
        return None
    pricing = entry.get("pricing") or {}
    try:
        return float(pricing["prompt"]), float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None


def _try_snapshot_rates(model: str) -> tuple[float, float] | None:
    """The vendor's own published price, from the bundled models.dev snapshot.

    Reaches vendors LiteLLM has not indexed without reading another vendor's
    row: the snapshot is keyed by provider, so a direct ``zai/glm-5.2`` is
    answered by Z.ai's figure rather than by whatever OpenRouter charges for a
    model with a similar name. Costs are published per million tokens.
    """
    from raven.providers.catalog import model_cost

    cost = model_cost(model)
    if not cost:
        return None
    prompt = _numeric(cost, "input")
    completion = _numeric(cost, "output")
    if prompt is None or completion is None:
        return None
    return prompt / 1e6, completion / 1e6


def token_rates(model: str, input_tokens: int = 0, output_tokens: int = 0) -> tuple[float, float] | None:
    """This model's (prompt, completion) cost per token in USD, or None.

    Ladder, most authoritative first:

    0. OpenRouter's own catalogue while it is still current, and only for ids
       that name OpenRouter. Ahead of LiteLLM because the two questions come
       apart here: LiteLLM routes the request, but OpenRouter is the party
       *billing* it, and who sends is not who charges. Measured, they disagree --
       LiteLLM files ``openrouter/z-ai/glm-4.6`` at 0.40/1.75 per million where
       OpenRouter's own current table says 0.50/2.00, so the ladder under-reported
       a real bill by a fifth. Fresh-only and never fetching: an expired copy is
       not better evidence than the router's table, and answering from one would
       suppress the refetch the expiry exists to trigger -- while a blocking fetch
       here would be paid by the turn;
    1. LiteLLM's own table -- it also routes the request, so its answer and the
       call agree by construction;
    2. OpenRouter's live catalogue, fetching if needed, and still only for ids
       that name OpenRouter. Ahead of the snapshot for the tier-0 reason, behind
       LiteLLM because reaching the network is worse than a slightly stale row;
    3. the bundled models.dev snapshot, keyed by provider, which reaches vendors
       LiteLLM has not indexed without reading another vendor's row;
    4. the manual table above, for a model too new for all three.

    Tier 1 carries a deliberate exception to "only an id naming OpenRouter reads
    OpenRouter": ``wire.metadata_candidates`` offers LiteLLM the ``openrouter/``
    alias of a direct id as a *second* candidate, after the vendor's own row.
    That is safe where tiers 2 and 3 are not, because LiteLLM is the thing doing
    the sending -- it is answering about a request it would make, not reading a
    stranger's catalogue. The direct row is asked first for the same reason:
    asked alias-first, one model reported half its window at half its price.

    A bare id -- no prefix -- reaches tier 1 and tier 4 only. Tiers 2 and 3 are
    both keyed by vendor, and matching a bare name across either is what priced a
    self-hosted deployment at a hosted model's rate. A bare id left by an older
    version is priced as unknown until its model is picked again, which stores it
    qualified.

    Token counts are passed through because a vendor may price by size, so the
    rate for a 200k-token prompt is not always the rate for a short one.
    """
    return (
        _try_openrouter_rates(model, table=_fresh_openrouter_models())
        or _try_litellm_rates(model, input_tokens, output_tokens)
        or _try_openrouter_rates(model)
        or _try_snapshot_rates(model)
        or _FALLBACK_PRICING.get(model.removeprefix("openrouter/"))
    )


def _trustworthy_ceiling(entry: dict | None) -> int | None:
    """A row's output ceiling, unless the row is filing a window as one.

    984 of the 3040 rows in the pinned LiteLLM carry ``max_output_tokens >=
    max_input_tokens``; measured, ``openrouter/anthropic/claude-sonnet-4.5``
    reports 1000000 for both where Anthropic's real ceiling is 64000. A request
    carrying that number is refused, and the refusal classifies as
    ``invalid_request`` -- not retryable, not fallback-worthy, not compressible
    -- so the turn dies rather than degrades.

    The second condition is what makes this safe rather than merely suspicious.
    Rejecting only rows at or above the fallback guarantees the replacement is
    never larger than what the row claimed; without it, 252 small rows
    (``4096/4096`` shapes) are raised past their real ceiling, trading one
    refused request for another.
    """
    ceiling = _numeric(entry, "max_output_tokens", "max_tokens")
    if not ceiling:
        return None
    window = _numeric(entry, "max_input_tokens")
    if window and ceiling >= window and ceiling >= DEFAULT_MAX_OUTPUT_TOKENS:
        return None
    return int(ceiling)


def _try_litellm_max_output(model: str, *, allow_import: bool = True) -> int | None:
    """The model's own output ceiling, from the same metadata as the window.

    ``max_tokens`` is a legitimate fallback here in a way it is not on the
    input side: in LiteLLM's table it *is* the output ceiling, and the two
    agree wherever both are present.

    Both tiers are filtered through ``_trustworthy_ceiling``: the rows that get
    this wrong answer the table and ``get_model_info`` alike, so guarding one
    would hand back exactly what the other just rejected.
    """
    if not allow_import and "litellm" not in sys.modules:
        return None
    try:
        from raven.providers.litellm_setup import import_litellm

        litellm = import_litellm()
    except Exception:
        return None

    for candidate in _candidates(model):
        ceiling = _trustworthy_ceiling(_table_entry(candidate))
        if ceiling:
            return ceiling
        if _may_prompt(candidate):
            continue
        try:
            info = litellm.get_model_info(candidate)
        except Exception:
            continue
        ceiling = _trustworthy_ceiling(info)
        if ceiling:
            return ceiling
    return None


def resolve_max_output_tokens(model: str | None, *, allow_fetch: bool = True) -> int:
    """How many output tokens to ask for. Never ``None`` -- the caller is about
    to build a request with the result.

    Table first, fixed fallback second, which is the shape LiteLLM's own
    Anthropic path uses and for the same reason: one constant cannot fit every
    model. Too large for a small model is a 400; too small for a large one
    truncates silently, which is the failure this whole module's callers exist
    to avoid. See ``DEFAULT_MAX_OUTPUT_TOKENS`` for how that fallback is
    chosen; it only ever answers for a model the catalogue has no row for.
    """
    if not model:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return _try_litellm_max_output(model, allow_import=allow_fetch) or DEFAULT_MAX_OUTPUT_TOKENS


def _try_litellm_context_window(model: str, *, allow_import: bool = True) -> int | None:
    """LiteLLM's static model metadata -- offline, covers most mapped providers.

    Falls back to ``max_tokens`` (the output ceiling) for the handful of rows
    that carry no ``max_input_tokens``. Not because the two mean the same
    thing, but because a model's window is never smaller than what it is
    allowed to emit, so the output ceiling is a safe lower bound -- and a lower
    bound only over-trims, where this module's documented default (65536) would
    over-estimate an 8k model by a factor of eight.

    ``allow_import=False`` answers only from a LiteLLM already sitting in
    ``sys.modules``: importing it costs ~2-7s, and a caller passing this
    (``AgentLoop`` construction, before the lazy provider's prewarm thread has
    had a chance to import it) wants the cheap tiers only, not to trigger the
    same import it is trying to defer. Once LiteLLM is imported the check is
    free and the lookup proceeds exactly as with ``allow_import=True``.
    """
    if not allow_import and "litellm" not in sys.modules:
        return None
    try:
        from raven.providers.litellm_setup import import_litellm

        litellm = import_litellm()
    except Exception:
        return None

    for candidate in _candidates(model):
        # The table before the ask: it holds every model the interactive-login
        # drivers are asked about in practice, and reading it cannot prompt.
        window = _numeric(_table_entry(candidate), "max_input_tokens", "max_tokens")
        if window:
            return int(window)
        if _may_prompt(candidate):
            continue
        try:
            info = litellm.get_model_info(candidate)
        except Exception:
            continue
        window = _numeric(info, "max_input_tokens", "max_tokens")
        if window:
            return int(window)
    return None


def _registry_context_window(model: str) -> int | None:
    """A current vendor window declared by the provider registry, or None."""
    from raven.providers.registry import find_by_model, find_by_name, split_model_id

    prefix, vendor_model = split_model_id(model)
    if not prefix:
        return None
    spec = find_by_model(model)
    if spec is None:
        return None
    source = find_by_name(spec.metadata_prefix) if spec.metadata_prefix else spec
    if source is None:
        return None
    wanted = vendor_model.casefold()
    return next((window for model_id, window in source.model_context_windows if model_id.casefold() == wanted), None)


def resolve_context_window(model: str, *, allow_fetch: bool = True) -> int | None:
    """Return a model's real context window in tokens, or None.

    Current model facts declared by the routing registry come first, then
    LiteLLM's static metadata, then OpenRouter's catalogue for ids that name
    OpenRouter. The snapshot is deliberately not a source: a window sizes
    trimming, so a community-maintained file that goes stale or wrong would
    shape the next request rather than cost a label. Unknown models return None
    so the caller keeps its configured default.

    ``allow_fetch=False`` means "answer from what is already on hand": it
    passes through to the OpenRouter tier (see ``_fetch_openrouter_models``)
    and also tells the LiteLLM tier not to import LiteLLM on this caller's
    behalf (see ``_try_litellm_context_window``) -- a caller cheap enough to
    pass this is cheap enough not to pay a fresh import either.
    """
    window = _registry_context_window(model)
    if window:
        return window

    window = _try_litellm_context_window(model, allow_import=allow_fetch)
    if window:
        return window

    entry = _lookup_openrouter_entry(model, allow_fetch=allow_fetch)
    if entry:
        try:
            length = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length:
            return length
    return None


def effective_context_window(model: str, configured: int | None, *, allow_fetch: bool = True) -> int:
    """The context window to size trimming with -- the decision ladder's front door.

    Explicit configuration wins outright: a user or caller who pinned a number
    meant it as an override, not a hint. Absent that, the model's real window
    from ``resolve_context_window`` answers; absent *that* too (an unmapped
    model, or every source down), this module's documented default does --
    never ``None``, since the caller is about to size a request with the
    result.

    ``resolve_context_window`` already folds every LiteLLM and network failure
    into ``None`` rather than raising (see its tiers), so there is no
    exception left here to catch.

    ``allow_fetch=False`` passes straight through to ``resolve_context_window``;
    see ``_fetch_openrouter_models`` for what it changes.
    """
    if configured:
        return configured
    return resolve_context_window(model, allow_fetch=allow_fetch) or DEFAULT_CONTEXT_WINDOW_TOKENS


def reset_openrouter_cache() -> None:
    """Clear the in-process OpenRouter catalog cache.

    Only useful for tests -- pair it with the ``model_catalog_cache._CACHE_PATH``
    seam to exercise the disk tiers without touching the real ~/.raven/cache/.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME, _WARM_AT
    _OPENROUTER_CACHE = {}
    _OPENROUTER_CACHE_TIME = 0.0
    # Reset too, or a warm attempt from an earlier test leaves this one on a
    # cooldown it never asked for.
    _WARM_AT = 0.0
