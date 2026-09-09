"""The real raven-research ACP chain: launcher -> exec `raven acp` -> frames.

The unit tests prove the discovery row and the config rendering separately;
this proves the chain a real dispatch walks -- the discovered command spawning
`run.py`, the exec into the installed raven's own `raven acp`, and a
handshake whose answers are the ones the host's capability verify stands on
(`loadSession`, `sessionCapabilities.resume`). No LLM is dialled: `initialize`
and `session/new` never prompt.

The launcher execs the interpreter that runs this test, so the only
requirement is the dev environment itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FOLDER = _REPO_ROOT / "agents" / "raven-research"

_TIMEOUT = 180.0


def test_launcher_serves_acp_with_the_capabilities_the_host_verifies(tmp_path: Path) -> None:
    state = tmp_path / "state"
    # The session cwd must not contain the agent home (the workdir guard
    # refuses that layout), so the two live in sibling directories.
    workdir = tmp_path / "ws"
    workdir.mkdir()
    env = {
        **os.environ,
        # A dummy key satisfies the launcher's required-secret gate; neither
        # request below reaches a provider.
        "RESEARCH_API_KEY": "dummy-never-dialled",
        "RESEARCH_SERPER_API_KEY": "dummy-never-dialled",
        "RESEARCH_NG_STATE_ROOT": str(state),
        # Sandbox the subprocess's raven home: the launcher derives the
        # product workspace from it, and a test must not write the real one.
        "RAVEN_HOME": str(tmp_path / "home"),
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(workdir), "mcpServers": []}},
    ]
    payload = b"".join((json.dumps(r) + "\n").encode("utf-8") for r in requests)

    proc = subprocess.run(
        [sys.executable, str(_FOLDER / "run.py")],
        input=payload,
        capture_output=True,
        timeout=_TIMEOUT,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]

    # Frame purity: the launcher execs before the protocol starts, so its own
    # diagnostics must all be on stderr and every stdout byte a frame.
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {f["id"]: f for f in frames if "id" in f and "method" not in f}

    init = by_id[1]["result"]
    # The fork announced itself as raven-x-research; the product serves on the
    # installed raven, whose ACP identity is its own name. Adjudicated as a
    # trunk-additive difference in the D7 transport-parity ledger.
    assert init["agentInfo"]["name"] == "raven"
    assert init["agentCapabilities"]["loadSession"] is True
    assert "resume" in init["agentCapabilities"]["sessionCapabilities"]

    assert by_id[2]["result"]["sessionId"].startswith("acp:")

    # The rendered config landed under the state root with the secrets in it
    # and the workspace pinned there -- not in the published config.json.
    # The config-floor watermark sidecar (.migrations.json) lands beside the
    # render and matches the same glob; the render itself is the one file left.
    rendered = [f for f in state.glob(".config.rendered.*.json") if not f.name.endswith(".migrations.json")]
    assert len(rendered) == 1
    data = json.loads(rendered[0].read_text(encoding="utf-8"))
    assert data["providers"]["openrouter"]["apiKey"] == "dummy-never-dialled"
    workspace = data["agents"]["defaults"]["workspace"]
    assert workspace.startswith(str(tmp_path / "home")), workspace
    assert "raven-research-ng" in workspace, workspace
    assert "dummy-never-dialled" not in (_FOLDER / "config.json").read_text(encoding="utf-8")


async def test_the_startup_backfill_measures_statefulness_by_itself(tmp_path: Path, monkeypatch) -> None:
    """The picker-visibility chain, end to end: a fresh host holds no capability
    snapshot, so the acp row reads stateless and the ``/new-instance`` picker
    hides it -- the startup backfill must repair that with no human in the loop.
    Real handshake against the vendored server; no model tokens are spent
    (``verify_agent`` never prompts).
    """
    from types import SimpleNamespace

    from raven.acp_client import capabilities
    from raven.agent.subagent import probe
    from raven.agent.subagent.backends import agent_meta
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    snap_path = tmp_path / "subagent_acp_capabilities.json"
    monkeypatch.setattr(capabilities, "default_snapshot_path", lambda: snap_path)
    monkeypatch.setattr(probe, "_SCHEDULED", False)

    # The row discovery would build, resolved by hand: the suite's autouse
    # ``no_discovered_products`` pin points discovery at nothing, deliberately.
    manifest = json.loads((_FOLDER / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        manifest[field] = manifest[field].replace("{SUBAGENT_DIR}", str(_FOLDER)).replace("{PYTHON}", sys.executable)
    manifest["env"] = {
        "RESEARCH_API_KEY": "dummy-never-dialled",
        "RESEARCH_SERPER_API_KEY": "dummy-never-dialled",
        "RESEARCH_NG_STATE_ROOT": str(tmp_path / "state"),
    }
    cfg = ThirdPartyAcpSubagentConfig.model_validate(manifest)
    assert agent_meta(cfg).stateful is False, "no snapshot yet, so the picker would hide the row"

    refreshed: list[int] = []
    manager = SimpleNamespace(
        registry=SimpleNamespace(rows=lambda: [SimpleNamespace(name=cfg.name, kind="acp", enabled=True, config=cfg)]),
        refresh_agents=lambda: refreshed.append(1),
    )
    task = probe.schedule_snapshot_verification(manager)
    assert task is not None
    await task

    assert snap_path.exists()
    assert agent_meta(cfg).stateful is True
    assert refreshed == [1], "the table must be rebuilt so the roster picks the snapshot up"
