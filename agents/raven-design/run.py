#!/usr/bin/env python
"""Launch Raven-Design with host-owned model and tool credentials.

The rendered config only adds engine tools, storage placement and iteration
limits. Model selection, reasoning effort and image settings come from the host.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

from raven.config import product_render as render

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
ENGINE_PLUGIN_ID = "design-engine"
ENGINE_PACKAGE = "raven_design"

PRODUCT = "raven-design"

MODES_DIR = HERE / "modes"
MODE_LABELS = {
    "medium": (
        "Medium",
        "Bounded: 60 tool iterations. A quick draft or a small revision.",
    ),
    "high": (
        "High",
        "The default: 150 tool iterations.",
    ),
    "max": (
        "Max",
        "300 tool iterations. A full deliverable where the ceiling matters more than the bill.",
    ),
}
BASELINE_MODE = "high"
OVERLAY_KEYS = frozenset({"agents"})


def agent_default(overlay: dict, key: str):
    """One ``agents.defaults`` knob from a mode overlay, or ``None``."""
    return ((overlay.get("agents") or {}).get("defaults") or {}).get(key)


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


def configure_image_generation(config: dict, host: dict) -> None:
    """Use the host image section and keep subsequent settings edits live."""
    render.inherit_media_image(config, host)


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root.

    Both branches end with a provider block that can answer, or refuse to
    launch: the runtime makes the key a hard requirement, and a config that
    starts a child which cannot answer surfaces as a generic failure with
    nothing naming the credential.
    """
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    config.setdefault("tools", {})["web"] = deepcopy((host.get("tools") or {}).get("web") or {})
    configure_image_generation(config, host)

    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    for key in ("model", "provider", "reasoningEffort"):
        defaults.pop(key, None)
    config.pop("providers", None)
    config.pop("routing", None)
    taken = render.inherit_llm(config, deepcopy(host))
    if not taken:
        raise SystemExit("error: configure a model provider in the host Raven settings before starting Design")
    log(f"[run] llm: inherited from the host ({taken})")

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

    # Every entry declares its own cap and effort, the baseline included, so
    # the loop enforces them per session without reading the overlay -- which
    # the engine does not read either. The overlay stays on the entry as the
    # record of what the mode changed.
    catalogue = render.mode_catalogue(
        MODES_DIR,
        MODE_LABELS,
        baseline=BASELINE_MODE,
        overlay_keys=OVERLAY_KEYS,
        resolve=lambda overlay: (
            agent_default(overlay, "maxToolIterations") or defaults.get("maxToolIterations"),
            {"agents": overlay.get("agents", {})},
        ),
    )
    if catalogue:
        for entry in catalogue.values():
            overlay_defaults = ((entry.get("overlay") or {}).get("agents") or {}).get("defaults") or {}
            overlay_defaults.pop("reasoningEffort", None)
            entry["reasoningEffort"] = defaults.get("reasoningEffort")
        acp = config.get("acp")
        if not isinstance(acp, dict):
            acp = config["acp"] = {}
        acp["modes"] = catalogue
        acp["defaultMode"] = BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {BASELINE_MODE})")

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
    # git-changes reply appendix) stayed with the retired fork wrapper it
    # belonged to, per the verdict's D3 lane ruling.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
