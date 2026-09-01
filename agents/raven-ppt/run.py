#!/usr/bin/env python
"""Host-side launcher for the Raven-PPT ACP server -- the B side.

The vendored Raven-PPT (subagents/raven-ppt) carries a whole fork checkout;
this product carries its assets only. It renders its config the way every
agents/ product does -- secrets merged into a 0600 copy whose parent decides
the data dir -- but the process it starts is still the fork checkout's own
engine. The trunk runtime cannot serve this product yet for one reason with
no gate-shaped second: installed raven has no deck engine at all -- no
ppt_* tools, no template catalogue, no render loop -- until the engine lands
as the ppt-engine plugin wheel (verdict C3, ruled (ii): a standalone
distribution under plugins-dist/, its templates as sha256-pinned package
data). Swapping the exec target today would install a deck agent that
cannot build decks. The swap to ``python -m raven acp`` is the engine
wave's own step; the rendered config already loads on both engines so that
swap changes one exec line, not this file's shape.

The launch contract is the fork launcher's ACP half, and only that half:
the one-turn CLI job hosting (material staging, deck verification, the 3h
watchdog, MEDIA delivery) is dead freight -- its only callers were the
fork's own benchmarks (dead-freight ruling 3) -- and is not rebuilt.
What remains is the fork's render: refuse without any LLM key, give an
own key to every provider block, honour PPT_MODEL/PPT_API_BASE on the
own-key branch only, recalibrate the context window from the host's model
catalog for whichever model won, and exec the checkout's own ``raven acp``
from the checkout (its bundled templates and skills resolve from there).
stdout belongs to the protocol; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
# The fork engine this launcher still hosts; retired at the exec-target swap,
# which the ppt-engine wheel (plugins-dist/ppt-engine) must land first.
DEFAULT_CHECKOUT = HERE.parent.parent / "subagents" / "raven-ppt" / "Raven-PPT"
ENGINE_PLUGIN_ID = "ppt-engine"

PRODUCT = "raven-ppt"

# Where each optional secret belongs in the config the engine loads. The LLM
# key is not among them: it is written to every provider block rather than to
# one path, and its own branch below carries the model and base the same key
# pays for (the fork launcher's shape).
SECRET_SLOTS = {
    "PPT_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "PPT_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

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

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    shipped_window = defaults.get("contextWindowTokens")
    model = defaults.get("model") or ""
    if window := model_context_window(model):
        defaults["contextWindowTokens"] = window
        note = "" if window == shipped_window else f", replacing the configured {shipped_window}"
        log(f"[run] window: {window} for {model}, from the host model catalog{note}")
    else:
        log(f"[run] window: {shipped_window} from config; the host catalog has no entry for {model or '(no model)'}")

    # No plugins.dirs and no workspace pin, both deliberate. The fork engine
    # consuming this render forbids plugins fields it does not know, and the
    # ppt-engine wheel will arrive by entry point, never by directory; the
    # fork's ACP hosting fences per-session deck projects itself and its
    # launcher pinned no workspace, so pinning one here would be a new claim
    # the A side never made.
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root)


def serve(args: argparse.Namespace) -> int:
    """Render the config, then become the fork engine's ``raven acp`` on stdio.

    The venv precheck comes before the render, the fork launcher's order: a
    missing engine is the answer whoever installed this needs first, and no
    file holding merged secrets should exist for a run that cannot start.
    ``execv``, not a subprocess -- stdin and stdout are the protocol, and a
    middleman is one more buffer to flush. The chdir is load-bearing: the
    fork runtime resolves its bundled templates and skills relative to the
    checkout. Nothing runs after the exec, so the pid-liveness sweep in
    render_config is the only cleanup this hosting has.
    """
    checkout = Path(args.checkout).expanduser().resolve()
    raven_bin = checkout / ".venv" / "bin" / "raven"
    if not (raven_bin.is_file() and os.access(raven_bin, os.X_OK)):
        raise SystemExit(
            f"error: {raven_bin} is not an executable. Build the checkout's venv first:\n"
            f"  cd {checkout} && uv sync --extra ppt"
        )

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] acp: serving from {checkout} with {rendered}")
    os.chdir(checkout)
    os.execv(str(raven_bin), [str(raven_bin), "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-PPT over ACP on stdio.")
    # The roster row's command carries --acp, kept byte-identical to the
    # vendored row; ACP is this launcher's only hosting, so the flag selects
    # nothing. The fork's one-job CLI mode is retired dead freight, not a
    # pending rebuild (dead-freight ruling 3).
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--checkout", default=str(DEFAULT_CHECKOUT), help="fork checkout hosting the engine")
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
