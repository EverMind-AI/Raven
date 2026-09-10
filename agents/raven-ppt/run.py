#!/usr/bin/env python
"""Host-side launcher for the Raven-PPT ACP server -- the B side, swapped.

The engine this launcher serves is installed raven's own: the deck capability
arrives as the ppt-engine wheel (plugins-dist/ppt-engine), discovered through
the ``raven.plugins`` entry-point group -- eleven deck tools and the
material/deck turn hook, with the eight templates as sha256-pinned package
data. The vendored fork checkout is retired; the A side of the A/B verification
survives as byte snapshots under ``tests/fixtures/vendored_fork/``.

The launch contract is still the fork launcher's ACP half: refuse without any
LLM key, give an own key to every provider block, honour PPT_MODEL/PPT_API_BASE
on the own-key branch only, size the context window from the endpoint that
will serve whichever model won, render into a 0600 copy whose parent decides
the data dir. Three renders are this hosting's own, each the trunk
runtime's seat for something the fork engine did per checkout: the agent home
is pinned under the state root (the fork fenced per-session workspaces inside
its own process; a pooled loop reads identity, sessions and skills from ONE
home, and that home must not be the host's), the engine's skill directory is
mounted through ``skillForge.localDirs`` (the fork shipped the skill inside
its checkout; the wheel ships it inside the package), and the retired
``tools.ppt`` block is dropped from an operator's carried config with a
one-line hint at its successor (the plugin slice renders the same knobs).
stdout belongs to the protocol; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent

# The host's tier ladder (medium / high / max), as this product spends it. The
# source config IS the max profile -- every reading, every build, until the gates
# and the reader are satisfied -- so max needs no overlay file. High keeps the
# author's effort and caps the run: six measured decks had every useful fix in
# by the tenth whole-deck build and the third reading, and what followed was
# churn on taste findings and misread pages. Medium is high at low effort: one
# run at low effort finished a deck in 32 minutes where medium took 165.
MODES_DIR = HERE / "modes"
BASELINE_MODE = "max"
DEFAULT_MODE = "high"
MODE_LABELS = {
    "medium": (
        "Medium",
        "Low reasoning effort and a capped run: ten whole-deck builds, three readings, then the deck "
        "goes out as it stands. The fastest tier; a first draft or a small deck.",
    ),
    "high": (
        "High",
        "Standard reasoning effort with the same caps: ten whole-deck builds and three readings, then the "
        "deck is delivered as it stands. The default.",
    ),
    "max": (
        "Max",
        "No caps: the run builds and reads until the gates and the second reader are satisfied. "
        "The slowest tier, for a deck that has to be right.",
    ),
}
OVERLAY_KEYS = frozenset({"agents", "plugins"})
DEFAULT_CONFIG = HERE / "config.json"
ENGINE_PLUGIN_ID = "ppt-engine"
ENGINE_PACKAGE = "raven_ppt"

PRODUCT = "raven-ppt"

# Where each optional secret belongs in the config the engine loads. The LLM
# key is not among them: it is written to every provider block rather than to
# one path, and its own branch below carries the model and base the same key
# pays for (the fork launcher's shape).
SECRET_SLOTS = {
    "PPT_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "PPT_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
    "PPT_IMAGE_API_KEY": ("tools", "media", "image", "apiKey"),
}
IMAGE_KEY_SLOT = SECRET_SLOTS["PPT_IMAGE_API_KEY"]
# The rest of the image section a product may pin against the inherited one:
# a different base or model than the host's, for a deck account of its own.
IMAGE_SETTING_SLOTS = {
    "PPT_IMAGE_API_BASE": ("tools", "media", "image", "apiBase"),
    "PPT_IMAGE_MODEL": ("tools", "media", "image", "model"),
}

# Where the web tools take their proxy from, and the environment names that
# stand in for it. The engine's fetch builds its client with `trust_env=False` on
# purpose -- a deck's downloads must not silently follow whatever proxy happens
# to be exported -- so nothing under the runtime reads HTTPS_PROXY. On a host
# that reaches the internet through one, that leaves every fetch failing as
# "host unreachable": a live run spent three rounds on it (a thumbnail host was
# refused as unreachable, and the same proxy answered it in 0.33s), and settled
# for a worse picture. The translation belongs here, once, in the open, rather
# than in a client that would then be following an environment nobody declared.
PROXY_SLOT = ("tools", "web", "proxy")
PROXY_ENV = ("PPT_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")

# The one secret whose absence is fatal: no pictures makes a poorer deck, no
# model makes no deck at all.
REQUIRED_SECRETS = ("PPT_API_KEY",)


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    return render.product_state_root(PRODUCT, override=env_value("PPT_STATE_ROOT"))


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def engine_skill_dir() -> Path | None:
    """The deck-authoring skill directory inside the installed engine wheel.

    ``None`` when the wheel is absent -- serve() has already refused by then,
    so this answers only for the render, and a config mounting a directory
    that does not exist would earn a warning from the catalog rather than the
    clean absence a None caller renders.
    """
    spec = importlib.util.find_spec(ENGINE_PACKAGE)
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "skill"


_IMAGE_GATEWAY = "openrouter.ai"


def openrouter_key_in_force(config: dict) -> str:
    """The OpenRouter key this render will actually call with, or ``""``.

    Picture generation has one backend, so the key that can draw a backdrop is
    whichever provider block in the rendered config addresses OpenRouter. A
    block says which gateway it is two ways and both have to be read: an
    explicit ``apiBase``, which is what PPT_API_BASE writes into a block named
    ``ppt`` for no spec to claim, and -- when that is empty -- the address the
    registry ships for the block's own name, which is the shape an inherited
    ``openrouter`` block has. Reading only the first is what left the inherit
    branch with no key to draw with.
    """
    from raven.providers.registry import find_by_name

    for name, provider in (config.get("providers") or {}).items():
        if not isinstance(provider, dict):
            continue
        base = str(provider.get("apiBase") or "")
        if not base:
            spec = find_by_name(name)
            base = spec.usable_default_api_base if spec else ""
        if _IMAGE_GATEWAY in base and (key := provider.get("apiKey")):
            return str(key)
    return ""


def configure_image_generation(config: dict, host: dict) -> None:
    """Give the deck a picture generator, on either LLM branch.

    Called after the branch for the reason the window recalibration is: which
    key can draw is a fact about the gateway that won, not about whose key paid
    for the words.

    The host's own ``tools.media.image`` carries the operator's model and
    quality choice, and ``selectionConfig`` keeps a later Settings edit live
    rather than frozen at launch. The key keeps the precedence
    ``apply_secret_slots`` set -- an explicit PPT_IMAGE_API_KEY, else the host's
    own image key -- and only with both absent does the key paying for the
    words pay for the pictures too. That last step is what the inherit branch
    could not reach: it has no PPT_API_KEY to offer, and a host that configured
    its OpenRouter key for chat alone deliberately surfaces no media tool, so
    the deck asked for a key nobody had written and drew nothing.

    The product's own ``PPT_IMAGE_API_BASE`` and ``PPT_IMAGE_MODEL`` pin a deck
    endpoint or model over the inherited one, and any product pin (key, base or
    model) also pins the section as rendered: a live host selection would
    replace it whole on the next call. The host's media proxy rides along.

    The engine gets no copy: the deck's ``ppt_generate_image`` rides the host's
    ``image_generate`` and reads ``tools.media.image`` through the locator's
    ``media_config`` grant, live, exactly as the host tool does. The slice's own
    ``image`` key stays an operator override for a host that grants nothing.
    """
    from raven.config.schema import live_media_tool_config

    host_tools = host.get("tools") or {}
    host_image = (host_tools.get("media") or {}).get("image")
    section = live_media_tool_config(host_image, (host.get("providers") or {}).get("openrouter"))
    image = section.model_dump(by_alias=True, exclude_unset=True) if section is not None else {}
    paid_by = "the host image section"
    pinned: list[str] = []
    for name, path in IMAGE_SETTING_SLOTS.items():
        if value := env_value(name):
            image[path[-1]] = value
            pinned.append(name)
    if resolved := render.dig(config, IMAGE_KEY_SLOT):
        image["apiKey"], paid_by = resolved, "PPT_IMAGE_API_KEY or the host image key"
    elif not image.get("apiKey") and _IMAGE_GATEWAY in (image.get("apiBase") or _IMAGE_GATEWAY):
        # Borrowed only towards OpenRouter. The host's image section may name
        # another endpoint while leaving its key empty -- a state the host's own
        # borrow declines to fill, because a section holding neither key nor
        # model is not configured -- and lending the chat credential to an
        # address its owner never nominated for pictures is the one thing this
        # backfill must not do.
        if borrowed := openrouter_key_in_force(config):
            image["apiKey"], paid_by = borrowed, "the key that pays for the words"
    # selectionConfig hands the section back to the host file, which the
    # generator re-reads per call -- so it is written only where that file is
    # what answers. A section holding neither key nor model answers "no key",
    # an empty key in a present section being a revocation here, so writing it
    # beside a borrowed key would erase the borrow on every call (measured);
    # and an explicit PPT_IMAGE_API_KEY is this deployment's choice, not a
    # host preference to be overridden by a later Settings edit.
    host_selects = isinstance(host_image, dict) and bool(host_image.get("apiKey") or host_image.get("model"))
    if env_value("PPT_IMAGE_API_KEY"):
        pinned.insert(0, "PPT_IMAGE_API_KEY")
    if host_selects and not pinned:
        image["selectionConfig"] = str(render.raven_home() / render.CONFIG_FILENAME)
    media = config.setdefault("tools", {}).setdefault("media", {})
    media["image"] = image
    if (proxy := (host_tools.get("media") or {}).get("proxy")) and not media.get("proxy"):
        media["proxy"] = proxy
    if not image.get("apiKey"):
        log("[run] images: no OpenRouter key to draw with; the deck keeps only the pictures it can find")
        return
    pin_note = f", pinned by {', '.join(pinned)}" if pinned else ", following the host" if host_selects else ""
    log(f"[run] images: {image.get('model') or 'the shipped default'}, paid by {paid_by}{pin_note}")


def recommended_llm() -> str:
    """What this folder's manifest says this agent is tuned for."""
    try:
        rec = json.loads((HERE / "subagent.json").read_text(encoding="utf-8")).get("recommendedLlm") or {}
    except (OSError, ValueError):
        return "unrecorded"
    return f"{rec.get('model', '?')} via {rec.get('apiBase') or rec.get('provider', '?')}"


PROBE_TIMEOUT_S = 8.0


def _get_json(url: str, api_key: str) -> Any:
    """One GET with the run's own credential, JSON back, None on any failure.

    Environment proxies apply on purpose: this runs on the host machine before
    any raven client exists, and asks the very gateway the run will talk to.
    """
    import urllib.request

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=PROBE_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _length(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def served_providers(defaults: dict, model: str) -> set[str]:
    """The OpenRouter providers this config lets ``model`` reach, lower-cased;
    empty when any may serve it. Read off the same ``modelOverrides`` rows the
    runtime sends as ``extra_body.provider``. An ``only`` list is the whole set.
    An ``order`` is a priority, not a fence: with fallbacks left on, OpenRouter
    hands the call to any other provider once the ordered ones are unavailable,
    so ``order`` narrows the set only when ``allow_fallbacks`` is false. The
    window is sized for every provider the request may land on. The shipped
    glm-5.3-flash row is a fence (its order with fallbacks off), so the run is
    sized by those three providers and never routed to the fp8 host OpenRouter
    also lists; the price is that a call finding all three unavailable fails
    over the runtime's retry ladder rather than to a smaller-window provider."""
    names: set[str] = set()
    for key, override in (defaults.get("modelOverrides") or {}).items():
        if key not in model or not isinstance(override, dict):
            continue
        provider = (override.get("extra_body") or {}).get("provider") or {}
        if provider.get("only"):
            names.update(provider["only"])
        elif provider.get("allow_fallbacks") is False:
            names.update(provider.get("order") or ())
    return {str(name).lower() for name in names}


def ignored_providers(defaults: dict, model: str) -> set[str]:
    """The providers the same rows tell OpenRouter never to use, lower-cased."""
    names: set[str] = set()
    for key, override in (defaults.get("modelOverrides") or {}).items():
        if key in model and isinstance(override, dict):
            names.update(((override.get("extra_body") or {}).get("provider") or {}).get("ignore") or ())
    return {str(name).lower() for name in names}


def probe_context_window(
    model: str, api_base: str, api_key: str, served: set[str], ignored: set[str] = frozenset()
) -> tuple[int, str] | None:
    """The window the serving endpoint reports for ``model``, and where it came from.

    OpenRouter lists every provider it routes a model to with that provider's
    own context length, which is often below the model's headline number
    (glm-5.3-flash: 1,310,720 on the card, 1,048,576 at Z.AI, DeepInfra and
    Novita, 262,144 at an fp8 host the shipped row's fence keeps the run away
    from); the smallest of the providers the request may reach is the one
    number a run can rely on. On OpenRouter that listing is the only endpoint
    answer: its generic ``/models`` row carries the model card's number, not a
    serving endpoint's, so it is not consulted there and a failed endpoints
    call answers None. An OpenAI-compatible server (vLLM and its kind) states
    ``max_model_len`` in its model listing. None means the endpoint did not
    answer, or answered without a number, and the caller then holds any
    catalog figure under the configured number.
    """
    base = (api_base or "").rstrip("/")
    if not base or not model:
        return None
    if "openrouter.ai" in base:
        data = _get_json(f"{base}/models/{model}/endpoints", api_key)
        endpoints = ((data or {}).get("data") or {}).get("endpoints") if isinstance(data, dict) else None
        lengths = []
        for endpoint in endpoints or []:
            if not isinstance(endpoint, dict):
                continue
            name = str(endpoint.get("provider_name") or "").lower()
            if (served and name not in served) or name in ignored:
                continue
            if endpoint.get("status") not in (None, 0):
                continue
            if length := _length(endpoint.get("context_length")):
                lengths.append(length)
        if lengths:
            return min(lengths), f"the smallest of the {len(lengths)} OpenRouter endpoints serving it"
        return None
    data = _get_json(f"{base}/models", api_key)
    rows = data.get("data") if isinstance(data, dict) else data
    short = model.rsplit("/", 1)[-1]
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        if row_id not in (model, short) and not row_id.endswith("/" + model):
            continue
        for field in ("max_model_len", "context_length", "context_window", "max_context_length"):
            if length := _length(row.get(field)):
                return length, f"{field} in the endpoint's model listing"
    return None


def litellm_context_window(model: str, api_base: str) -> tuple[int, str] | None:
    """LiteLLM's table for the model, or None: the number a model is sold with,
    which the endpoint may serve less of, so it is asked only after the endpoint."""
    try:
        import litellm
    except Exception:
        return None
    routed = f"openrouter/{model}"
    candidates = (routed,) if "openrouter.ai" in (api_base or "") else (model, routed)
    for candidate in candidates:
        try:
            info = litellm.get_model_info(candidate)
        except Exception:
            continue
        if length := _length(info.get("max_input_tokens") or info.get("max_tokens")):
            return length, f"litellm's table for {candidate}"
    return None


def resolve_context_window(model: str, defaults: dict, provider_block: dict) -> tuple[int, str] | None:
    """The window to render, and its source, or None to keep the shipped number.

    ``PPT_CONTEXT_WINDOW`` pins it by hand. Otherwise the serving endpoint is
    asked first, and its answer is the window outright: the exact number for
    this gateway and the providers the request may reach, in either direction.
    When the endpoint does not answer, LiteLLM's table and then the host's
    catalog are asked, but a catalog knows the number a model is sold with,
    not what the endpoint serves, so a catalog answer may only lower the
    configured number, never raise it: a probe that timed out must not leave a
    long-lived run trimming against 1,310,720 when its endpoints take 1,048,576.
    The runtime sizes its pre-send trimming against this number, so an
    invented margin in either direction would be a number nobody reading the
    rendered config could account for.
    """
    if pinned := env_value("PPT_CONTEXT_WINDOW"):
        if length := _length(int(pinned) if pinned.strip().isdigit() else None):
            return length, "PPT_CONTEXT_WINDOW"
        log(f"[run] window: ignoring PPT_CONTEXT_WINDOW={pinned!r}, not a positive integer")
    api_base = str(provider_block.get("apiBase") or "")
    api_key = str(provider_block.get("apiKey") or "")
    served = served_providers(defaults, model)
    if found := probe_context_window(model, api_base, api_key, served, ignored_providers(defaults, model)):
        return found
    found = litellm_context_window(model, api_base)
    if found is None and (length := model_context_window(model)):
        found = length, "the host model catalog"
    if found is None:
        return None
    shipped = _length(defaults.get("contextWindowTokens"))
    if shipped and found[0] > shipped:
        return shipped, f"the configured number, kept under {found[1]} ({found[0]}) since the endpoint did not answer"
    return found


def model_context_window(model: str) -> int | None:
    """This model's ceiling from the host's catalog, or None to leave the pin.

    None covers every way the catalog can decline to answer -- never fetched,
    unreadable, or holding no row for this id -- and the caller then keeps
    whatever ``config.json`` shipped. The number is taken verbatim: the
    runtime sizes its pre-send trimming against it, and an invented margin in
    either direction is a number nobody reading the rendered config could
    account for (the fork launcher's reasoning, kept whole).
    """
    try:
        catalog = json.loads((raven_home() / "cache" / "model-catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    entry = models.get(model) if isinstance(models, dict) else None
    length = entry.get("context_length") if isinstance(entry, dict) else None
    return length if isinstance(length, int) and not isinstance(length, bool) and length > 0 else None


def resolve_mode(overlay: dict) -> tuple[None, dict]:
    """The product's half of the mode catalogue: no iteration cap of its own, and the
    diff the plugin's hook reads is the ppt-engine slice (buildCap / readingCap)."""
    return None, dict(((overlay.get("plugins") or {}).get("config") or {}).get(ENGINE_PLUGIN_ID) or {})


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root.

    Both branches end with a provider block that can answer, or refuse to
    launch: the runtime makes the key a hard requirement, and a config that
    starts a child which cannot answer surfaces as a generic failure with
    nothing naming the credential. The window recalibration runs after both
    branches because it is a fact about whichever model won, not about whose
    key is paying for it.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=(), lookup=env_value)
    if not render.dig(config, PROXY_SLOT):
        from_host = render.dig(host, PROXY_SLOT)
        if proxy := (from_host or next((value for name in PROXY_ENV if (value := env_value(name))), "")):
            render.put(config, PROXY_SLOT, proxy)
            log(f"[run] web: proxy={'host' if from_host else 'own'}")

    llm_key = REQUIRED_SECRETS[0]
    if api_key := env_value(llm_key):
        for provider in config.get("providers", {}).values():
            if isinstance(provider, dict) and not provider.get("apiKey"):
                provider["apiKey"] = api_key
        # Read on this branch only: both describe the endpoint this key pays
        # for. Applied on top of an inherited block they would point the
        # host's gateway at a model it may not serve, which surfaces as a bad
        # answer rather than an error -- the same trap inherit_llm copies
        # wholesale to avoid.
        if model := env_value("PPT_MODEL"):
            config.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
            for provider in config.get("providers", {}).values():
                if isinstance(provider, dict):
                    provider["models"] = [model]
        if api_base := env_value("PPT_API_BASE"):
            for provider in config.get("providers", {}).values():
                if isinstance(provider, dict):
                    provider["apiBase"] = api_base
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = render.inherit_llm(config, host)
        if not taken:
            raise SystemExit(
                f"error: {llm_key} is not set and the host config has no provider key to "
                f"inherit from; put the key in {HERE / '.env'} (see .env.example), export "
                f"it, or configure a provider in the host raven"
            )
        ignored = [name for name in ("PPT_MODEL", "PPT_API_BASE") if env_value(name)]
        log(
            f"[run] llm: inherited from the host ({taken}); tuned for {recommended_llm()}"
            + (f"; ignored {', '.join(ignored)}, which need {llm_key}" if ignored else "")
        )

    configure_image_generation(config, host)

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    shipped_window = defaults.get("contextWindowTokens")
    model = defaults.get("model") or ""
    provider_block = config.get("providers", {}).get(defaults.get("provider") or "")
    if found := resolve_context_window(model, defaults, provider_block if isinstance(provider_block, dict) else {}):
        window, source = found
        defaults["contextWindowTokens"] = window
        note = "" if window == shipped_window else f", replacing the configured {shipped_window}"
        log(f"[run] window: {window} for {model}, from {source}{note}")
    else:
        log(
            f"[run] window: {shipped_window} from config; no endpoint, table or catalog answered for {model or '(no model)'}"
        )

    # The picture-search key reaches both consumers from ONE source of truth:
    # the tools.web slot AFTER apply_secret_slots, which is the env key when
    # one is set and the host config's own tools.web.search.apiKey when not
    # (the per-slot fallback this family pins). trunk's web_search reads that
    # slot; the engine's ppt_image_search reads its slice key, so the merged
    # value is copied across -- rendered from the env var alone, a host-keyed
    # deploy would register web_search while the deck's own image search
    # silently declined. setdefault twice: a slice that shipped a key keeps it.
    serper_key = (((config.get("tools") or {}).get("web") or {}).get("search") or {}).get("apiKey")
    if serper_key:
        engine_slice = config.setdefault("plugins", {}).setdefault("config", {}).setdefault(ENGINE_PLUGIN_ID, {})
        engine_slice.setdefault("imageSearch", {}).setdefault("apiKey", serper_key)

    # The proxy walks the same bridge (G3, the Serper key's shape again): the
    # fork's one tools.web.proxy fed the web tools AND every deck tool, and
    # ppt_fetch is trust_env=False on purpose, so an environment proxy cannot
    # stand in -- a config that proxies web_search while the deck tools dial
    # bare would split the face without a sound.
    web_proxy = ((config.get("tools") or {}).get("web") or {}).get("proxy")
    if web_proxy:
        engine_slice = config.setdefault("plugins", {}).setdefault("config", {}).setdefault(ENGINE_PLUGIN_ID, {})
        engine_slice.setdefault("webProxy", web_proxy)

    # The migration floor for the retired fork key: the shipped config no
    # longer carries tools.ppt, but an operator's carried copy might, and the
    # trunk loader would ignore it without a word -- the knobs look honoured
    # and are not. Dropped here, once, with the successor named.
    tools = config.get("tools")
    if isinstance(tools, dict) and tools.pop("ppt", None) is not None:
        log(f'[run] config: tools.ppt retired; the engine reads plugins.config["{ENGINE_PLUGIN_ID}"] (same knobs)')

    root = state_root()

    # The pooled loop reads identity, sessions, transcripts and the skill pool
    # from ONE agent home; unpinned it would be the host's own (the launcher
    # inherits RAVEN_HOME), which this agent must not share -- and it must sit
    # OUTSIDE the host Agent home, which the host hands over as the session
    # cwd (the runtime refuses a cwd that contains the engine's home). The
    # shared placement helper seats it in the raven data directory;
    # PPT_ACP_HOME overrides. The state root keeps the work (rendered
    # configs, sweep) exactly as before. setdefault, so an operator's
    # explicit workspace wins.
    defaults.setdefault("workspace", str(render.product_acp_home(PRODUCT, override=env_value("PPT_ACP_HOME"))))

    # The engine wheel ships the deck-authoring skill as package data; the
    # catalog mounts configured directories with always_enabled semantics
    # (the verdict's feature-14 collapse), so the mount is one rendered row.
    # Merged per entry, keyed by path: an operator who mounts directories of
    # their own keeps every row they wrote AND the engine row -- a whole-list
    # default would silently unmount the deck skill the moment they added one,
    # and the wheel's site-packages path is nothing they could re-spell by
    # hand. The fork shipped this skill unconditionally with its checkout.
    if skill_dir := engine_skill_dir():
        rows = config.setdefault("skillForge", {}).setdefault("localDirs", [])
        if isinstance(rows, list) and not any(
            isinstance(row, dict) and row.get("path") == str(skill_dir) for row in rows
        ):
            rows.append({"path": str(skill_dir), "name": ENGINE_PLUGIN_ID, "alwaysEnabled": True})

    # Still no plugins.dirs: the ppt-engine wheel arrives by entry point,
    # never by directory (the everos-memory shape).
    root.mkdir(parents=True, exist_ok=True)
    render.sweep_stale_renders(root)
    catalogue = render.mode_catalogue(
        MODES_DIR, MODE_LABELS, baseline=BASELINE_MODE, overlay_keys=OVERLAY_KEYS, resolve=resolve_mode
    )
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = DEFAULT_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {DEFAULT_MODE})")
    return render.write_rendered(config, root)


def serve(args: argparse.Namespace) -> int:
    """Render the config, then become installed raven's ``raven acp`` on stdio.

    The engine precheck comes before the render, the fork launcher's order: a
    missing engine is the answer whoever installed this needs first, and no
    file holding merged secrets should exist for a run that cannot start.
    After rendering, this process execs ``python -m raven acp`` on its own
    interpreter (the roster row's ``{PYTHON}`` resolves at install time to
    one that imports raven), so the server inherits this pid, process group
    and stdio untouched. No chdir: the fork engine resolved its templates and
    skills relative to its checkout, the wheel resolves them relative to its
    own package. Nothing runs after the exec, so the pid-liveness sweep in
    render_config is the only cleanup this hosting has.
    """
    if importlib.util.find_spec(ENGINE_PACKAGE) is None:
        raise SystemExit(
            f"error: the {ENGINE_PLUGIN_ID} plugin is not installed in this environment "
            f"({sys.executable}). The deck engine ships as the {ENGINE_PLUGIN_ID} wheel "
            f"(plugins-dist/{ENGINE_PLUGIN_ID}); install it where raven is installed."
        )

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-PPT over ACP on stdio.")
    # The roster row's command carries --acp, kept byte-identical to the
    # fork-era row; ACP is this launcher's only hosting, so the flag selects
    # nothing. The fork's one-job CLI mode is retired dead freight, not a
    # pending rebuild (dead-freight ruling 3).
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
