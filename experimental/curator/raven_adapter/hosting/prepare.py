"""Prepare the deployment's existing Raven child profiles through their own rendering entrypoints."""

import json
import shlex
from pathlib import Path

from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.vendored_agents import product_folder

from ..baselines import Baseline
from ..baselines.agents import prepare_agent
from ..deployment import Child, child_directory
from ..materialize import _write


def prepare_children(parent, root, names):
    """Resolve explicitly selected deployment entries; do not create or split business instances."""
    root = Path(root)
    parent_home = root / "deployment" / "parent"
    _write(
        parent_home / "config.json",
        json.dumps(
            {
                **parent.config.model_dump(mode="json", by_alias=True),
                **parent.extensions.model_dump(mode="json", by_alias=True, exclude={"base"}),
            }
        ).encode(),
    )
    rows = {row.name: row for row in parent.config.subagents.agents}
    result = {}
    for name in names:
        directory = child_directory(root, name)
        if name == GENERIC_AGENT:
            home = parent.config.workspace_path / "subagents" / name
            home.mkdir(parents=True, exist_ok=True)
            baseline = Baseline.restore(parent.export())
            baseline.config.agents.defaults.workspace = str(home)
            baseline.inherit_model = True
            baseline.config.tools.mcp_servers = {}
            baseline.config.playbooks.enabled = False
        else:
            row = rows.get(name)
            product = product_folder(name)
            if row is None or row.kind != "acp" or product is None:
                raise ValueError(f"no supported deployed Raven profile for {name}")
            command = shlex.split(row.command)
            source = product / "config.json"
            for index, part in enumerate(command):
                if part == "--config" and index + 1 < len(command):
                    source = Path(command[index + 1]).expanduser()
                elif part.startswith("--config="):
                    source = Path(part.partition("=")[2]).expanduser()
            if not source.is_absolute():
                source = Path(row.cwd or product) / source
            baseline = prepare_agent(
                product,
                root=directory / "prepared",
                workdir=parent.workdir,
                task=parent.task,
                environment={"RAVEN_HOME": str(parent_home), **(row.env or {})},
                config=source,
            )
        result[name] = Child(baseline)
    return result
