#!/usr/bin/env python
"""Host-side launcher for the Raven-Design ACP server -- the B side, swapped.

The engine this launcher serves is installed raven's own: the visual
capability arrives as the design-engine wheel (plugins-dist/design-engine),
discovered through the ``raven.plugins`` entry-point group -- the Visual
Domain Selector turn hook, the render/preview tools, the resident Task State,
with the fifteen-skill corpus and its reference plates as package data. The
vendored fork checkout is no longer on the exec path; what remains of it is
the A side of the A/B verification, run by its own wrapper.

The launch contract is the fork launcher's render half, kept whole: refuse
without any LLM key, give an own key to every provider block, merge the
optional Serper and Jina keys with per-slot host fallback, resolve the
image-generation key through the fork's waterfall, and pin the engine's
Agent home in the raven data directory (w109). Three renders are this
hosting's own trunk seats, the dw2 debt triple paid: the engine's skill
directory is mounted through ``skillForge.localDirs`` (per-entry, keyed by
path -- an operator's own mounts survive), so the selector cards'
``read_skill local/<name>`` instruction resolves on this host; the resident
Task State gets its ``taskState.stateRoot`` under the product state root, so
that surface stops declining; and the exec target is ``python -m raven acp``
on this interpreter. stdout belongs to the protocol; every diagnostic goes
to stderr.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from raven.config import product_render as render

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
ENGINE_PLUGIN_ID = "design-engine"
ENGINE_PACKAGE = "raven_design"

PRODUCT = "raven-design"

# Where each optional secret belongs in the config the engine loads. The LLM
# key is not among them: it is written to every provider block rather than to
# one path. The image key is not among them either: its value is a waterfall
# over four sources, not one env read (see configure_image_generation).
SECRET_SLOTS = {
    "DESIGN_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "DESIGN_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# The one secret whose absence is fatal: no key, no model, no design.
REQUIRED_SECRETS = ("DESIGN_API_KEY",)


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    return render.product_state_root(PRODUCT, override=env_value("DESIGN_STATE_ROOT"))


def engine_skill_dir() -> Path | None:
    """The domain-Skill corpus directory inside the installed engine wheel.

    ``None`` when the wheel is absent -- serve() has already refused by then,
    so this answers only for the render, and a config mounting a directory
    that does not exist would earn a warning from the catalog rather than the
    clean absence a None caller renders.
    """
    spec = importlib.util.find_spec(ENGINE_PACKAGE)
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "skills"


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


def configure_image_generation(config: dict, host: dict) -> None:
    """Resolve the image-generation key through the fork launcher's waterfall.

    Order kept from the fork: an own DESIGN_IMAGE_API_KEY wins; then the host's
    ``tools.media.image`` key rides along with the base and model it was
    configured for; then the host's OpenRouter provider key; then the own LLM
    key, but only when the configured endpoint is already OpenRouter (the only
    backend trunk's media tools speak). No key resolves to an empty key AND an
    empty model, which is precisely how the trunk registrar withholds the
    ``image_generate`` tool rather than offering one that cannot answer. The
    fork's apiStyle/allowModelOverride companions are not carried: the trunk
    media schema has no such keys (phantom knobs, per the D4 floor).
    """
    own_image_key = env_value("DESIGN_IMAGE_API_KEY")
    own_llm_key = env_value(REQUIRED_SECRETS[0])
    host_media = ((host.get("tools") or {}).get("media") or {}).get("image") or {}
    host_media_key = str(host_media.get("apiKey") or host_media.get("api_key") or "")
    host_media_base = str(host_media.get("apiBase") or host_media.get("api_base") or "")
    host_openrouter = (host.get("providers") or {}).get("openrouter") or {}
    host_openrouter_key = str(host_openrouter.get("apiKey") or host_openrouter.get("api_key") or "")

    image_key = own_image_key
    borrowed_host_media = False
    if not image_key and host_media_key and (not host_media_base or "openrouter.ai" in host_media_base):
        image_key = host_media_key
        borrowed_host_media = True
    if not image_key:
        image_key = host_openrouter_key
    if not image_key and own_llm_key:
        configured_base = str(
            (((config.get("tools") or {}).get("media") or {}).get("image") or {}).get("apiBase") or ""
        )
        if "openrouter.ai" in configured_base:
            image_key = own_llm_key

    image = config.setdefault("tools", {}).setdefault("media", {}).setdefault("image", {})
    if not image_key:
        image["apiKey"] = ""
        image["model"] = ""
        return
    image["apiKey"] = image_key
    if borrowed_host_media:
        if host_media_base:
            image["apiBase"] = host_media_base
        if host_media.get("model"):
            image["model"] = str(host_media["model"])


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root.

    Both branches end with a provider block that can answer, or refuse to
    launch: the runtime makes the key a hard requirement, and a config that
    starts a child which cannot answer surfaces as a generic failure with
    nothing naming the credential.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=(), lookup=env_value)
    configure_image_generation(config, host)

    llm_key = REQUIRED_SECRETS[0]
    if api_key := env_value(llm_key):
        for provider in config.get("providers", {}).values():
            if isinstance(provider, dict) and not provider.get("apiKey"):
                provider["apiKey"] = api_key
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
        log(f"[run] llm: inherited from the host ({taken}); tuned for {recommended_llm()}")

    # The pooled loop reads identity, sessions, transcripts and the skill pool
    # from ONE agent home; unpinned it would be the host's own (the launcher
    # inherits RAVEN_HOME), which this agent must not share -- and it must sit
    # OUTSIDE the host Agent home, which the host hands over as the session
    # cwd (the runtime refuses a cwd that contains the engine's home). The
    # shared placement helper seats it in the raven data directory;
    # DESIGN_ACP_HOME overrides. setdefault, so an operator's explicit
    # workspace wins.
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    defaults.setdefault("workspace", str(render.product_acp_home(PRODUCT, override=env_value("DESIGN_ACP_HOME"))))

    root = state_root()

    # The engine wheel ships the domain-Skill corpus as package data; the
    # catalog mounts configured directories with always_enabled semantics, so
    # the mount is one rendered row -- and with it the selector cards'
    # read_skill local/<name> instruction resolves on this host. Merged per
    # entry, keyed by path (the pw2b lesson): an operator who mounts
    # directories of their own keeps every row they wrote AND the engine row.
    if skill_dir := engine_skill_dir():
        rows = config.setdefault("skillForge", {}).setdefault("localDirs", [])
        if isinstance(rows, list) and not any(
            isinstance(row, dict) and row.get("path") == str(skill_dir) for row in rows
        ):
            rows.append({"path": str(skill_dir), "name": ENGINE_PLUGIN_ID, "alwaysEnabled": True})

    # The resident Task State writes its sidecars under the product state
    # root -- work, never the engine home (the sessions it is keyed by live
    # in neither). setdefault, so an operator's own root wins.
    engine_slice = config.setdefault("plugins", {}).setdefault("config", {}).setdefault(ENGINE_PLUGIN_ID, {})
    if isinstance(engine_slice, dict):
        task_state = engine_slice.setdefault("taskState", {})
        if isinstance(task_state, dict):
            task_state.setdefault("stateRoot", str(root))

    # Still no plugins.dirs: the design-engine wheel arrives by entry point,
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
    and stdio untouched. No chdir: the fork engine resolved its corpus
    relative to its checkout, the wheel resolves it relative to its own
    package. Nothing runs after the exec, so the pid-liveness sweep in
    render_config is the only cleanup this hosting has.
    """
    if importlib.util.find_spec(ENGINE_PACKAGE) is None:
        raise SystemExit(
            f"error: the {ENGINE_PLUGIN_ID} plugin is not installed in this environment "
            f"({sys.executable}). The visual engine ships as the {ENGINE_PLUGIN_ID} wheel "
            f"(plugins-dist/{ENGINE_PLUGIN_ID}); install it where raven is installed."
        )

    rendered = render_config(Path(args.config).resolve())
    log(f"[run] exec {sys.executable} -m raven acp (config {rendered})")
    os.execv(sys.executable, [sys.executable, "-m", "raven", "acp", "--config", str(rendered)])
    raise AssertionError("unreachable: execv does not return")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-Design over ACP on stdio.")
    # ACP is this launcher's only hosting, so the flag selects nothing; the
    # fork's per-turn CLI round-trip (transcript scraping, task preamble, the
    # git-changes reply appendix) stays with the vendored wrapper it belongs
    # to, per the verdict's D3 lane ruling.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
