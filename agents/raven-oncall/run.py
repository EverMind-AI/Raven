#!/usr/bin/env python
"""Host-side launcher for the Raven-Oncall ACP server -- the B side.

The retired vendored Raven-Oncall carried a whole fork checkout; this
product carries none. It renders its config and serves the
installed raven's own ``raven acp``, so every turn runs through the same
assembly door (build_runtime) as the host's TUI and gateway. The machinery
of rendering lives in the launcher library
(``raven.config.product_render``); what remains here is this product's own
half -- its tables (which secrets go where) and its judgement (refusing to
launch without any LLM key, pointing the machine registry at the owner's
file, seeding the on-call guide into the workspace).

The rendered copy lands under the state root and the location is
load-bearing: raven derives its data dir from the config file's own parent,
so transcripts, cache and logs follow the rendered file. The workspace is
pinned under the same root -- the schema default is the host raven's own
workspace, which this agent must not share.

What the vendored launcher also hosted does not come back here: the
resident wake shell's duties pass to resident trunk hosts and the product
plugin's contributions, and the one-turn CLI adjudication path retires with
its only callers (the wake shell and the fork benchmarks). This launcher
serves ACP, nothing else.

After rendering, the launcher serves ``python -m raven acp`` on this
interpreter with inherited stdio. POSIX replaces the launcher; Windows
keeps it alive until the child exits, preserving the rendered config's owner.
stdout belongs to the protocol; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from raven.config import product_render as render
from raven.home import raven_home

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
PLUGINS_DIR = HERE / "plugins"
FLOW_PLUGIN_ID = "oncall-flow"
GUIDE_SECTION = PLUGINS_DIR / FLOW_PLUGIN_ID / "prompts" / "TOOLS_ONCALL.md"
MODES_DIR = HERE / "modes"
# The shipped config.json IS the high profile; modes/ holds the other two as
# diffs. Measured 2026-09-04 on deepseek-v4-flash over openrouter: the model
# does not tell low, medium and high apart (200-340 thinking tokens, 6-17 s),
# so the felt steps are thinking off, thinking on, and thinking at max
# (2300-3300 tokens, about 70 s a call). Three profiles, one real knob.
BASELINE_MODE = "high"
MODE_LABELS = {
    "medium": (
        "Medium",
        "Thinking off: answers in seconds. For routine looks -- reading a status, polling a queue.",
    ),
    "high": (
        "High",
        "Thinking on, the shipped default: a few seconds to a quarter minute a call.",
    ),
    "max": (
        "Max",
        "Thinking at maximum effort: about a minute a call, ten times the reasoning. For a judgement that "
        "decides a round -- what to submit, whether to kill.",
    ),
}
# An overlay may touch the agent's own generation defaults and the flow
# plugin's slice; anything else is a different file's business.
OVERLAY_KEYS = frozenset({"agents", "plugins"})

PRODUCT = "raven-oncall"

# Where each secret belongs in the config raven loads. The paths are trunk
# raven's own config surface; keys stay out of config.json because that file
# is published.
SECRET_SLOTS = {
    "ONCALL_API_KEY": ("providers", "custom", "apiKey"),
    "ONCALL_SERPER_API_KEY": ("tools", "web", "search", "apiKey"),
    "ONCALL_JINA_API_KEY": ("tools", "web", "jinaApiKey"),
}

# These two land in the pre-vendor leaves, which trunk reads only after an
# empty vendor slot; the host's slot is not inherited over them.
OWN_WEB_KEYS = {"serper": "ONCALL_SERPER_API_KEY", "jina": "ONCALL_JINA_API_KEY"}

# The LLM key never inherits per-slot: its absence switches the whole LLM
# block to host inheritance (see render_config), so it stays out of the
# optional fallback loop.
REQUIRED_SECRETS = ("ONCALL_API_KEY",)

# The env var trunk's raven.ops.connections honours as its store override; a
# literal so the launcher imports nothing beyond the launcher library and the
# path paper. Pinned against raven.ops.connections.CONNECTIONS_ENV by
# tests/test_agents_oncall_launcher.py.
CONNECTIONS_ENV = "RAVEN_CONNECTIONS"


def env_value(name: str) -> str | None:
    """This product's settings lookup: the process environment, then ``.env``."""
    return render.env_value(name, env_file=HERE / ".env")


def state_root() -> Path:
    """Everything this product persists lands here, never in this folder."""
    return render.product_state_root(PRODUCT, override=env_value("ONCALL_STATE_ROOT"))


def log(message: str) -> None:
    """Record a diagnostic without contaminating the protocol stream."""
    print(message, file=sys.stderr, flush=True)


def connections_registry() -> Path:
    """The machine registry this install reads: the owner's, not a copy of it.

    Without an answer, ``raven.ops.connections`` resolves its path beside the
    rendered config -- which is the state root here -- so the on-call tools
    would see no machines at all, and a task naming a remote path would be
    read as naming a local one. A pointer rather than a copy: a copy is wrong
    from the first machine the owner adds, with nothing to say so.

    The owner's home when it holds a registry; a list of this install's own
    only when the home has none. The home comes first because a list in the
    state root is a copy -- nothing writes there since 2026-09-01 -- and read
    first it hid every machine added to the home since, including the coding
    agent's own adds, which land in the home (reviewed 2026-09-24). An install
    that predates the home registry and kept its own list still reads it.
    """
    host = raven_home() / "connections.json"
    if host.is_file():
        return host
    own = state_root() / "connections.json"
    return own if own.is_file() else host


def seed_guide(workspace: Path) -> None:
    """Write the workspace TOOLS.md with the on-call section already in place.

    The vendored twin appended TOOLS_ONCALL.md to the workspace TOOLS.md on
    every sync while its gate was on; here the composed file is written before
    the engine starts, so trunk's own template sync finds it existing and
    leaves it alone. Regenerated on every launch (``write_generated``): the
    template half changes with the installed raven, and an operator's own
    lines go in ``TOOLS.local.md``. The composition mirrors the twin's append
    -- template body, blank line, section -- so the model-visible text is
    byte-equal to what the fork produced on a fresh workspace.

    A raven older than ``write_generated`` -- a home tree copied out by a newer
    wheel serves an older checkout too -- seeds the guide once instead, and
    says that it will not be refreshed.
    """
    target = workspace / "TOOLS.md"
    generate = getattr(render, "write_generated", None)
    if generate is None:
        render.seed_once(target, _guide_text)
        log(f"[run] {target.name}: this raven predates regenerated prompts; seeded once, never refreshed")
        return
    try:
        generate(target, _guide_text(), owner=PRODUCT, notify=lambda message: log(f"[run] {message}"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"error: cannot regenerate {target}: {exc}") from exc


def _guide_text() -> str:
    from raven import templates

    base = (Path(templates.__file__).resolve().parent / "TOOLS.md").read_text(encoding="utf-8")
    return base.rstrip("\n") + "\n\n" + GUIDE_SECTION.read_text(encoding="utf-8")


APPROVAL_MODES = ("full", "smart", "ask")


def _plugin_path() -> str:
    """The plugin package on this launcher's path, so its own code can be asked."""
    path = str(PLUGINS_DIR / FLOW_PLUGIN_ID)
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def _flow_enabled(flow_slice: dict) -> bool:
    """Whether the on-call flow this product's policy belongs to is on."""
    try:
        _plugin_path()
        from oncall_flow.config import FlowConfig

        return bool(FlowConfig.from_slice(flow_slice).enabled)
    except Exception as exc:  # noqa: BLE001 -- an unreadable slice is a flow that is not on
        log(f"[run] approval: the oncall-flow slice could not be read ({exc})")
        return False


def _gate_ready(config: dict, flow_slice: dict, workspace: Path) -> bool:
    """Whether the plugin gate that carries this product's policy can be built.

    The launcher renders the trunk gate at ``full`` and hands the policy to
    ``oncall_flow.approval``. A gate that does not build -- an older plugin
    dir, a factory that raises -- would leave every call unreviewed, with one
    log line as the only trace, so the product asks before it runs instead.
    """
    if not _flow_enabled(flow_slice):
        return False
    try:
        _plugin_path()
        from oncall_flow.approval import make_approval_gate

        from raven.contracts.plugin_surface import ServiceLocator
        from raven.plugins.context import PluginContext
    except Exception as exc:  # noqa: BLE001 -- no gate is a gate that is not ready
        log(f"[run] approval: the oncall_approval gate is not available ({exc})")
        return False
    memory = config.get("memory") or {}
    try:
        services = ServiceLocator(
            workspace=workspace,
            user_id=str(memory.get("userId") or "raven-oncall"),
            agent_id=str(memory.get("agentId") or "raven-oncall"),
        )
        gate = make_approval_gate(PluginContext(config=flow_slice, services=services))
    except Exception as exc:  # noqa: BLE001 -- a factory that raises is a gate that is not ready
        log(f"[run] approval: the oncall_approval gate did not build ({exc})")
        return False
    if gate is None:
        log("[run] approval: the oncall_approval gate declined")
        return False
    return True


def apply_approval_policy(config: dict, flow_slice: dict, host: dict, workspace: Path | None = None) -> str:
    """Hand the approval decision to the plugin's gate, at the owner's mode.

    Most of this product's turns are wakes it scheduled itself, where the trunk
    gate has no one to ask and refuses -- which once refused the call that
    schedules the next wake. So the trunk gate is rendered at ``full``, where
    only builtin denials and the host's deny rules refuse (the host's rules
    arrive with ``inherit_host_permissions``, which keeps this mode), and
    ``oncall_flow.approval`` applies the
    mode per call. The mode is the first one set of: this slice's own
    ``approval.mode``, this product config's ``permissions.mode``, the host's
    ``permissions.mode``, then the trunk default ``smart``. It is read once,
    here: a later change on the host reaches the on-call when it next starts.

    The trunk is at ``full`` only while the plugin gate that carries the
    policy is there to carry it. A flow that is on and a gate that does not
    build would run every call unreviewed, so the on-call does not start; a
    flow that is off has no policy to delegate to, and the trunk keeps the
    owner's mode -- asking, which is the safe side.
    """
    approval = flow_slice.setdefault("approval", {})
    timeout = approval.get("reviewTimeoutSeconds")
    if timeout is not None:
        try:
            valid = float(timeout) > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SystemExit(
                f"error: oncall-flow approval.reviewTimeoutSeconds must be a positive number of "
                f"seconds, got {timeout!r}; the on-call does not start with its approval gate unsure"
            )
    own = (config.get("permissions") or {}).get("mode")
    host_mode = (host.get("permissions") or {}).get("mode")
    mode = next(
        (m for m in (approval.get("mode"), own, host_mode) if m in APPROVAL_MODES),
        "smart",
    )
    approval["mode"] = mode
    gate = workspace is not None and _gate_ready(config, flow_slice, workspace)
    if gate:
        config.setdefault("permissions", {})["mode"] = "full"
    else:
        config.setdefault("permissions", {})["mode"] = next(
            (m for m in (own, host_mode) if m in APPROVAL_MODES), "smart"
        )
    if _flow_enabled(flow_slice) and not gate:
        raise SystemExit(
            "error: the oncall_approval gate did not build, so the on-call would run with no "
            "approval policy; it does not start"
        )
    return mode


def render_config(source: Path) -> Path:
    """Write a copy of ``source`` with the secrets merged in, under the state root."""
    config = json.loads(source.read_text(encoding="utf-8"))
    host = render.host_config()

    render.apply_secret_slots(config, host, slots=SECRET_SLOTS, required=REQUIRED_SECRETS, lookup=env_value)

    if (inherit_web := getattr(render, "inherit_web_vendors", None)) is not None:
        if carried := inherit_web(config, host, own_keys=OWN_WEB_KEYS, lookup=env_value):
            log(f"[run] web: vendor keys from the host ({', '.join(carried)})")

    llm_key = REQUIRED_SECRETS[0]
    if env_value(llm_key):
        defaults = config.get("agents", {}).get("defaults", {})
        log(f"[run] llm: own key (provider={defaults.get('provider')} model={defaults.get('model')})")
    else:
        taken = render.inherit_llm(config, host)
        if not taken:
            # A home tree copied out by a newer wheel also serves an older checkout, so this
            # can run on a raven that predates inherit_refusal; that one refuses without the reason.
            explain = getattr(render, "inherit_refusal", None)
            reason = f" ({explain(config, host)})" if explain else ""
            raise SystemExit(
                f"error: {llm_key} is not set and the host's model cannot be inherited{reason}; "
                f"put the key in {HERE / '.env'} (see .env.example), export it, or configure a "
                f"provider in the host raven"
            )
        log(f"[run] llm: inherited from the host ({taken})")

    root = state_root()
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    # The engine's Agent home must sit OUTSIDE the host Agent home (the host
    # hands its home over as the session cwd, and the runtime refuses a cwd
    # that contains the engine's home), so the default comes from the shared
    # placement helper -- the raven data directory, ONCALL_ACP_HOME overrides.
    # The shipped config spells the default as the literal "workspace" (the
    # fork's own spelling); that sentinel means "the default", wherever it
    # should live today. An operator's own value is honoured as written
    # (relative paths keep resolving under the state root, their documented
    # shape); the state root itself still holds the work (oncall_flow store,
    # rendered configs).
    configured = defaults.get("workspace")
    if configured and configured != "workspace":
        workspace = Path(configured)
        if not workspace.is_absolute():
            workspace = (root / workspace).resolve()
    else:
        workspace = render.product_acp_home(PRODUCT, override=env_value("ONCALL_ACP_HOME"))
    defaults["workspace"] = str(workspace)

    plugins = config.setdefault("plugins", {})
    plugins["dirs"] = [str(PLUGINS_DIR)]
    flow_slice = plugins.setdefault("config", {}).setdefault(FLOW_PLUGIN_ID, {})
    flow_slice.setdefault("stateRoot", str(root / "oncall_flow"))
    mode = apply_approval_policy(config, flow_slice, host, workspace)
    trunk_mode = (config.get("permissions") or {}).get("mode")
    log(f"[run] approval: {mode} (trunk gate {trunk_mode}; the oncall_approval gate applies it)")

    catalogue = render.mode_catalogue(
        MODES_DIR,
        MODE_LABELS,
        baseline=BASELINE_MODE,
        overlay_keys=OVERLAY_KEYS,
        # The loop reads agents.defaults off the overlay itself and the plugin's
        # hooks read their slice, so the entry carries the whole diff; the cap
        # is the overlay's when it names one, else the shipped default.
        resolve=lambda overlay: (
            ((overlay.get("agents") or {}).get("defaults") or {}).get(
                "maxToolIterations", defaults.get("maxToolIterations")
            ),
            overlay,
        ),
    )
    if catalogue:
        acp = config.setdefault("acp", {})
        acp["modes"] = catalogue
        acp["defaultMode"] = BASELINE_MODE
        log(f"[run] modes: {', '.join(catalogue)} (default {BASELINE_MODE})")

    root.mkdir(parents=True, exist_ok=True)
    seed_guide(workspace)
    render.sweep_stale_renders(root)
    return render.write_rendered(config, root, own_plugins=(FLOW_PLUGIN_ID,))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Raven-Oncall over ACP on stdio.")
    # The roster row's command carries --acp, kept byte-identical to the
    # fork-era row; ACP is this launcher's only hosting, so the flag selects
    # nothing.
    parser.add_argument("--acp", action="store_true", help="serve ACP on stdio (the only hosting)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    rendered = render_config(Path(args.config).resolve())
    os.environ.setdefault(CONNECTIONS_ENV, str(connections_registry()))
    log(f"[run] serve {sys.executable} -m raven acp (config {rendered})")
    argv = [sys.executable, "-m", "raven", "acp", "--config", str(rendered)]
    if sys.platform == "win32":
        raise SystemExit(subprocess.call(argv))
    os.execv(sys.executable, argv)
    raise AssertionError("unreachable: execv does not return")


if __name__ == "__main__":
    sys.exit(main())
