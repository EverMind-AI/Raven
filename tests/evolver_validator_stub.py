"""Test helpers for the agent package validator; not a test module (pytest collects ``test_*``).

``scaffold_template`` writes a real ``raven agents new`` folder with its TODO prompts
filled, the baseline every rule mutant derives from. ``make_stub_agent`` writes an
agent folder whose launcher serves ``tests/acp_stub_server.py``, so the activation
and end-to-end layers can be exercised without a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent


def scaffold_template(root: Path, name: str = "demo-agent") -> Path:
    """Scaffold ``name`` under ``root/home/agents`` and fill its description and owns."""
    from raven.cli.commands import app

    home = root / "home"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RAVEN_HOME", str(home))
        result = CliRunner().invoke(app, ["agents", "new", name, "--no-smoke"])
    assert result.exit_code == 0, result.output
    folder = home / "agents" / name
    manifest_path = folder / "subagent.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["description"] = "Says hello. Use it when the user asks for a greeting."
    manifest["owns"] = "owns greetings."
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


_STUB_RUN = '''"""Launcher of a stub agent: render like an agent's launcher, then serve the ACP stub."""

import json
import os
import sys
from pathlib import Path

settings = json.loads(SETTINGS)
if settings.get("refuse"):
    print(settings["refuse"], file=sys.stderr)
    sys.exit(1)
os.environ.update(settings["env"])
if settings["render"]:
    from raven.config import product_render as render

    home = Path(os.environ["RAVEN_HOME"])
    host = json.loads((home / "config.json").read_text(encoding="utf-8"))
    host.pop("providers", None)
    root = render.product_state_root("stub-agent")
    root.mkdir(parents=True, exist_ok=True)
    (root / f".config.rendered.{os.getpid()}.json").write_text(json.dumps(host), encoding="utf-8")
server = settings["server"]
os.execv(sys.executable, [sys.executable, server])
'''


def make_stub_agent(
    root: Path,
    *,
    mode: str,
    reply: str = "pong",
    extra_env: dict[str, str] | None = None,
    render: bool = True,
    name: str = "Stub-Agent",
    refuse: str | None = None,
    routes: list[dict] | None = None,
) -> Path:
    """Write ``root/stub-agent``: a folder that passes the static layers and serves the ACP stub."""
    folder = root / "stub-agent"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "kind": "acp",
        "description": "A stub agent that answers with a scripted reply, for validator tests.",
        "owns": "owns scripted test replies.",
        "enabled": True,
        "command": "{PYTHON} {SUBAGENT_DIR}/run.py",
        "cwd": "{SUBAGENT_DIR}",
        "readyTimeoutMs": 30000,
        "timeout": None,
        "maxOutputChars": 60000,
        "memory": {"userId": "stub-agent", "agentId": "stub-agent"},
    }
    if routes:
        manifest["routes"] = routes
    (folder / "subagent.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (folder / "config.json").write_text("{}\n", encoding="utf-8")
    env = {"ACP_STUB_MODE": mode, "ACP_STUB_REPLY": reply, **(extra_env or {})}
    settings = {"env": env, "render": render, "refuse": refuse, "server": str(TESTS / "acp_stub_server.py")}
    run = _STUB_RUN.replace(
        "settings = json.loads(SETTINGS)", f"settings = json.loads({json.dumps(json.dumps(settings))})"
    )
    (folder / "run.py").write_text(run, encoding="utf-8")
    os.chmod(folder / "run.py", 0o755)
    return folder
