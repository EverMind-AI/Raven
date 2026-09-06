#!/usr/bin/env python
"""Host-side launcher for the Raven-PPT ACP server -- the B side, swapped.

The engine this launcher serves is installed raven's own: the deck capability
arrives as the ppt-engine wheel (plugins-dist/ppt-engine), discovered through
the ``raven.plugins`` entry-point group -- eleven deck tools and the
material/deck turn hook, with the twelve templates as sha256-pinned package
data. The vendored fork checkout is no longer on the exec path; what remains
of it here is the A side of the A/B verification, run by its own wrapper.

The launch contract is still the fork launcher's ACP half: refuse without any
LLM key, give an own key to every provider block, honour PPT_MODEL/PPT_API_BASE
on the own-key branch only, recalibrate the context window from the host's
model catalog for whichever model won, render into a 0600 copy whose parent
decides the data dir. Three renders are this hosting's own, each the trunk
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

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
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


def recommended_llm() -> str:
    """What this folder's manifest says this agent is tuned for."""
    try:
        rec = json.loads((HERE / "subagent.json").read_text(encoding="utf-8")).get("recommendedLlm") or {}
    except (OSError, ValueError):
        return "unrecorded"
    return f"{rec.get('model', '?')} via {rec.get('apiBase') or rec.get('provider', '?')}"


def model_context_window(model: str) -> int | None:
    """This model's real ceiling from the host's catalog, or None to leave the pin.

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
        # The pictures are paid for by the key that pays for the words, when the
        # gateway is OpenRouter: GPT Image 2 is an OpenRouter model, and a run
        # without this asked for tools.media.image.apiKey and drew no backdrop it
        # could have generated. An explicit PPT_IMAGE_API_KEY has already landed in
        # the slot above and wins; another gateway gets nothing written, since the
        # image tool would only send that gateway a request it cannot serve.
        if not render.dig(config, IMAGE_KEY_SLOT):
            bases = [
                str(provider.get("apiBase") or "")
                for provider in config.get("providers", {}).values()
                if isinstance(provider, dict)
            ]
            if any("openrouter.ai" in base for base in bases):
                render.put(config, IMAGE_KEY_SLOT, api_key)
                log("[run] images: the LLM key also pays for GPT Image 2 (tools.media.image.apiKey)")
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

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    shipped_window = defaults.get("contextWindowTokens")
    model = defaults.get("model") or ""
    if window := model_context_window(model):
        # The catalog's number is the model's ceiling, and the configured one is
        # allowed to be lower: it is what the deck run is willing to carry. A run
        # that took the catalog's 1.3M as its window let the transcript grow to
        # 450k tokens a call -- 62M input tokens and 77 seconds a turn over 137
        # turns -- because nothing compacted short of a ceiling it never reached.
        if isinstance(shipped_window, int) and 0 < shipped_window < window:
            log(f"[run] window: {shipped_window} from config, under the {window} the host catalog gives {model}")
        else:
            defaults["contextWindowTokens"] = window
            note = "" if window == shipped_window else f", replacing the configured {shipped_window}"
            log(f"[run] window: {window} for {model}, from the host model catalog{note}")
    else:
        log(f"[run] window: {shipped_window} from config; the host catalog has no entry for {model or '(no model)'}")

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
    # vendored row; ACP is this launcher's only hosting, so the flag selects
    # nothing. The fork's one-job CLI mode is retired dead freight, not a
    # pending rebuild (dead-freight ruling 3).
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
