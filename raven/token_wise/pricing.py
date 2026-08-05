"""Single source of truth for LLM call cost estimation.

Used by ``UsageTracker`` and ``BudgetAlerter``. Returning a consistent cost
from one place prevents drift between "what we tracked" and "what we
budgeted".

Pricing sources (in order):
    1. ``litellm.cost_per_token`` — covers most public models. Tries the
       ``openrouter/<model>`` alias first, then the bare model id.
    2. OpenRouter ``/api/v1/models`` — live per-token prices for any model
       LiteLLM lags on, used as a cross-provider catalog (cached 1h in-process).
    3. ``_FALLBACK_PRICING`` — manual rate table for models missing from
       both.
    4. ``None`` — model unknown to all. Caller should degrade gracefully.

Anthropic ephemeral cache pricing is applied on top of the base rate:
    cache read  → 10% of prompt rate
    cache write → 125% of prompt rate (ephemeral 5-min TTL)

Non-Anthropic providers (no cache support) pass ``cache_read_tokens=0``,
``cache_write_tokens=0`` and the function collapses to the standard formula.
"""

from __future__ import annotations

import pathlib
import time
from functools import lru_cache

import httpx
from loguru import logger

from raven.token_wise import model_catalog_cache

# Rate pair: (prompt_cost_per_token, completion_cost_per_token) in USD.
# Keep this table small — it is a fallback for brand-new models that
# LiteLLM hasn't indexed yet. Check LiteLLM first before adding here.
_FALLBACK_PRICING: dict[str, tuple[float, float]] = {
    # OpenRouter model pages (snapshot 2026-03)
    "z-ai/glm-4.5-air": (0.13e-6, 0.85e-6),  # $0.13/$0.85 per 1M
}

# Track which unknown models we've already warned about so we log once each.
_WARNED_UNKNOWN: set[str] = set()

# Live OpenRouter price table, fetched lazily and cached in-process for 1h.
# Maps both the full id (``deepseek/deepseek-v4-pro``) and the bare alias
# (``deepseek-v4-pro``) to OpenRouter's per-token ``pricing`` dict.
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_CACHE_TTL = 3600
_OPENROUTER_CACHE: dict[str, dict] = {}
_OPENROUTER_CACHE_TIME: float = 0.0


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
    code to stdout and blocks about a minute per attempt, three attempts per
    candidate: one window lookup cost 410 seconds and six codes, because the bare
    and the openrouter-prefixed candidate reach the same driver. That is why any
    segment counts, not just the first.

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


def _is_plan_billed(model: str) -> bool:
    """Is this model's provider billed by subscription rather than per token?"""
    from raven.providers.registry import find_by_model

    spec = find_by_model(model)
    return bool(spec and spec.billing == "plan")


def _candidates(model: str) -> list[str]:
    """Which ids to ask LiteLLM's table about, best first.

    A provider reached by region or by subscription is filed in that table under
    the vendor's own spelling, and the registry says which
    ("minimax-global/MiniMax-M3" -> "minimax/MiniMax-M3"). Asking with the
    routing id instead missed every time, and each miss cost a second guess and
    the live catalogue fetch behind it.

    Everything else is asked about as it routes, and only then under an
    ``openrouter/`` alias: OpenRouter fronts other vendors and prices them under
    its own prefix, so the alias covers models LiteLLM lists nowhere else -- but
    it answers with OpenRouter's numbers, which are not what a user routing
    directly to the vendor pays. Asked alias-first, ``deepseek/deepseek-chat``
    reported a 65,536-token window at $0.14/M where the vendor's own row says
    131,072 at $0.28/M.
    """
    from raven.providers.registry import metadata_model_id

    filed_as = metadata_model_id(model)
    if filed_as:
        return [filed_as]

    if model.startswith("openrouter/"):
        return [model]

    return [model, f"openrouter/{model}"]


def _try_litellm_rates(model: str, input_tokens: int, output_tokens: int) -> tuple[float, float] | None:
    """Ask LiteLLM for per-token rates. Returns (prompt_rate, completion_rate) or None."""
    try:
        from raven.providers.litellm_setup import import_litellm

        litellm = import_litellm()
    except Exception:
        return None

    candidates = _candidates(model)

    # litellm.cost_per_token expects *at least* 1 non-zero token to compute.
    # We pass synthetic tokens to recover the per-token rate.
    probe_in = input_tokens if input_tokens else 1
    probe_out = output_tokens if output_tokens else 1

    for candidate in candidates:
        if _may_prompt(candidate):
            # Skipped, not read from the table: the rows these families have are
            # priced at zero, which this function already treats as unknown, so
            # reading them would add a branch that cannot fire. The caller falls
            # through to the live OpenRouter catalogue and the manual table, which
            # is what any model LiteLLM does not price already does.
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
            # LiteLLM returns (0, 0) when the model is unknown — treat as miss.
            continue
        return prompt_cost / probe_in, completion_cost / probe_out

    return None


def _fetch_openrouter_models() -> dict[str, dict]:
    """Return OpenRouter's model table, fetched live and cached 1h in-process.

    Each entry is ``{"pricing": ..., "context_length": ...}``, double-keyed by
    full id and bare alias. On any network failure, returns the stale cache
    (or an empty dict) — pricing must never raise into the cost path.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME

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
        logger.debug("pricing: OpenRouter models fetch failed ({}), degrading", exc)
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
        entry = {
            "pricing": model.get("pricing") or {},
            "context_length": model.get("context_length"),
        }
        cache[model_id] = entry
        if "/" in model_id:
            cache.setdefault(model_id.split("/", 1)[1], entry)

    _OPENROUTER_CACHE = cache
    _OPENROUTER_CACHE_TIME = time.time()
    model_catalog_cache.save(cache)
    return cache


def _lookup_openrouter_entry(model: str) -> dict | None:
    """Resolve a model to its OpenRouter catalog entry.

    Strips a leading ``openrouter/`` then tries the remaining id and its bare
    alias. Used as a cross-provider fallback for any model LiteLLM doesn't map,
    so the catalog also covers e.g. a direct ``deepseek/...`` route.
    """
    key = model.removeprefix("openrouter/")
    table = _fetch_openrouter_models()
    entry = table.get(key)
    if entry is None and "/" in key:
        entry = table.get(key.split("/", 1)[1])
    return entry


def _try_openrouter_rates(model: str) -> tuple[float, float] | None:
    """Look up live OpenRouter per-token rates. Returns rates or None."""
    entry = _lookup_openrouter_entry(model)
    if not entry:
        return None
    pricing = entry.get("pricing") or {}
    try:
        return float(pricing["prompt"]), float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None


def _try_litellm_context_window(model: str) -> int | None:
    """LiteLLM's static model metadata — offline, covers most mapped providers."""
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


def resolve_context_window(model: str) -> int | None:
    """Return a model's real context window in tokens, or None.

    Sources, in order: LiteLLM's static model metadata (offline, covers every
    provider it maps), then OpenRouter's live ``/models`` table
    (``context_length``) for any model LiteLLM lags on. Unknown models return
    None so the caller keeps its configured default.
    """
    window = _try_litellm_context_window(model)
    if window:
        return window

    entry = _lookup_openrouter_entry(model)
    if entry:
        try:
            length = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length:
            return length
    return None


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimate USD cost for a single LLM call. Returns None for unknown models.

    ``input_tokens`` is fresh (non-cache) prompt tokens. Anthropic's
    ``usage.input_tokens`` already excludes cache tokens, so pass it
    through untouched.

    A plan-billed provider returns None as well. The subscription is the price,
    so no per-token figure describes this call: LiteLLM files those models at
    zero, which the tiers below read as "unknown" and answered with the
    pay-as-you-go rate the user is not paying -- $2.50 per million for a Copilot
    seat. Callers already degrade on None; the tokens are still counted.
    """
    if _is_plan_billed(model):
        return None

    rates = _try_litellm_rates(model, input_tokens, output_tokens)
    if rates is None:
        rates = _try_openrouter_rates(model)
    if rates is None:
        key = model.removeprefix("openrouter/")
        if key in _FALLBACK_PRICING:
            rates = _FALLBACK_PRICING[key]
        else:
            if model not in _WARNED_UNKNOWN:
                logger.warning("pricing: unknown model '{}', cost estimate = None", model)
                _WARNED_UNKNOWN.add(model)
            return None

    prompt_rate, completion_rate = rates
    cost = (
        input_tokens * prompt_rate
        + output_tokens * completion_rate
        + cache_read_tokens * prompt_rate * 0.1
        + cache_write_tokens * prompt_rate * 1.25
    )
    return cost


def reset_warning_cache() -> None:
    """Clear the set of models we've already logged an 'unknown' warning for.

    Only useful for tests — production code should let warnings land once.
    """
    _WARNED_UNKNOWN.clear()


def reset_openrouter_cache() -> None:
    """Clear the in-process OpenRouter catalog cache.

    Only useful for tests — pair it with the ``model_catalog_cache._CACHE_PATH``
    seam to exercise the disk tiers without touching the real ~/.raven/cache/.
    """
    global _OPENROUTER_CACHE, _OPENROUTER_CACHE_TIME
    _OPENROUTER_CACHE = {}
    _OPENROUTER_CACHE_TIME = 0.0
