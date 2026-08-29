"""The real raven-research ACP chain: launcher -> exec `raven acp` -> frames.

The unit tests prove the discovery row and the config rendering separately;
this proves the chain a real dispatch walks -- the discovered command spawning
`run.py`, the exec into the vendored checkout's own `raven acp`, and a
handshake whose answers are the ones the host's capability verify stands on
(`loadSession`, `sessionCapabilities.resume`). No LLM is dialled: `initialize`
and `session/new` never prompt.

Skips where the vendored venv is unbuilt, which is every fresh clone until
``subagents/install.sh`` runs.
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
_FOLDER = _REPO_ROOT / "subagents" / "raven-research"
_VENV_RAVEN = _FOLDER / "Raven-X" / ".venv" / "bin" / "raven"

_TIMEOUT = 180.0


@pytest.mark.skipif(not os.access(_VENV_RAVEN, os.X_OK), reason="vendored Raven-X venv is not built")
def test_launcher_serves_acp_with_the_capabilities_the_host_verifies(tmp_path: Path) -> None:
    state = tmp_path / "state"
    env = {
        **os.environ,
        # A dummy key satisfies the launcher's required-secret gate; neither
        # request below reaches a provider.
        "RESEARCH_API_KEY": "dummy-never-dialled",
        "RESEARCH_STATE_ROOT": str(state),
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": []}},
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
    assert init["agentInfo"]["name"] == "raven-x-research"
    assert init["agentCapabilities"]["loadSession"] is True
    assert "resume" in init["agentCapabilities"]["sessionCapabilities"]

    assert by_id[2]["result"]["sessionId"].startswith("acp:")

    # The rendered config landed under the state root with the secrets in it
    # and the workspace pinned there -- not in the published config.json.
    rendered = list(state.glob(".config.rendered.*.json"))
    assert len(rendered) == 1
    data = json.loads(rendered[0].read_text(encoding="utf-8"))
    assert data["providers"]["custom"]["apiKey"] == "dummy-never-dialled"
    assert data["agents"]["defaults"]["workspace"] == str(state / "workspace")
    assert "dummy-never-dialled" not in (_FOLDER / "config.json").read_text(encoding="utf-8")


@pytest.mark.skipif(not os.access(_VENV_RAVEN, os.X_OK), reason="vendored Raven-X venv is not built")
async def test_the_startup_backfill_measures_statefulness_by_itself(tmp_path: Path, monkeypatch) -> None:
    """The picker-visibility chain, end to end: a fresh host holds no capability
    snapshot, so the acp row reads stateless and the ``/new-instance`` picker
    hides it -- the startup backfill must repair that with no human in the loop.
    Real handshake against the vendored server; no model tokens are spent
    (``verify_agent`` never prompts).
    """
    from types import SimpleNamespace

    from raven.agent.acp_client import capabilities
    from raven.agent.subagent import probe
    from raven.agent.subagent.backends import agent_meta
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    snap_path = tmp_path / "subagent_acp_capabilities.json"
    monkeypatch.setattr(capabilities, "default_snapshot_path", lambda: snap_path)
    monkeypatch.setattr(probe, "_SCHEDULED", False)

    # The row discovery would build, resolved by hand: the suite's autouse
    # ``no_vendored_subagents`` pin points discovery at nothing, deliberately.
    manifest = json.loads((_FOLDER / "subagent.json").read_text(encoding="utf-8"))
    for field in ("command", "cwd"):
        manifest[field] = manifest[field].replace("{SUBAGENT_DIR}", str(_FOLDER)).replace("{PYTHON}", sys.executable)
    manifest["env"] = {"RESEARCH_API_KEY": "dummy-never-dialled", "RESEARCH_STATE_ROOT": str(tmp_path / "state")}
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
