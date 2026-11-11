"""Child entry for rules A4 and A5: build every contribution of the named plugins once.

Usage: ``python -m evolver.validator._probe --config <rendered> --workspace <dir> --plugin <id> ...``.
Mirrors the ``build_plugin_*`` loops in ``raven/core/plugin_stack.py`` per
contribution, so a factory that raises (``failed``) and one that returns ``None``
(``declined``) are told apart. Prints one JSON object::

    {"plugins": {"<id>": {"tools": {name: status}, "hooks": {...}, "services": {...},
                          "tool_gates": {...}, "session_observers": {...}}},
     "activation_failures": [{"plugin": id, "cause": text}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KINDS = ("tools", "hooks", "services", "tool_gates", "session_observers")


def probe(config_path: Path, plugin_ids: list[str], workspace: Path) -> dict:
    from raven.config.raven import load_raven_config
    from raven.contracts.plugin_surface import ServiceLocator
    from raven.core.plugin_stack import build_plugin_registry

    config = load_raven_config(config_path)
    registry = build_plugin_registry(config, notify=lambda _text: None)
    services = ServiceLocator(workspace=workspace, user_id=config.memory.user_id, agent_id=config.memory.agent_id)
    slices = config.plugins.config
    out: dict[str, dict[str, dict[str, str]]] = {pid: {kind: {} for kind in KINDS} for pid in plugin_ids}
    groups = (
        ("tools", registry.tool_names, registry.tool_plugin_id, registry.build_tool),
        ("hooks", registry.hook_names, registry.hook_plugin_id, registry.build_hook),
        ("services", registry.service_names, registry.service_plugin_id, registry.build_service),
        ("tool_gates", registry.tool_gate_names, registry.tool_gate_plugin_id, registry.build_tool_gate),
        (
            "session_observers",
            registry.session_observer_names,
            registry.session_observer_plugin_id,
            registry.build_session_observer,
        ),
    )
    for kind, names, owner, build in groups:
        for name in names():
            plugin_id = owner(name)
            if plugin_id not in out:
                continue
            plugin_slice = slices.get(plugin_id) or slices.get(name) or {}
            try:
                built = build(name, config=plugin_slice, services=services)
            except Exception as exc:  # noqa: BLE001 - the failure is the finding
                out[plugin_id][kind][name] = f"failed: {type(exc).__name__}: {exc}"
                continue
            out[plugin_id][kind][name] = "declined" if built is None else "registered"
    failures = [{"plugin": f.plugin_id, "cause": f.reason} for f in registry.activation_failures()]
    return {"plugins": out, "activation_failures": failures}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evolver.validator._probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--plugin", action="append", default=[])
    args = parser.parse_args(argv)
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    print(json.dumps(probe(Path(args.config), args.plugin, workspace)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
