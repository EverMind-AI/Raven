"""A steer through the whole host, the way the TUI sends one.

Real RPC server, real ``AgentLoop`` and ``SubagentManager``, a real ACP
connection to the stub agent. The unit suites prove each layer alone; this is
the one place the order of events across all of them is asserted: the steer is
accepted while the turn runs, the agent announces it, and the settled record
reads prompt, what was said before, the steer, then what followed -- with the
words before it written once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from raven.config.schema import ThirdPartyAcpSubagentConfig

from .tui_rpc_harness import TuiRpcHarness, make_loop

pytestmark = pytest.mark.integration

_STUB = Path(__file__).parents[1] / "acp_stub_server.py"
SESSION = "tui:harness"


def _stub_agent(mode: str) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(
        name="stub",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": mode},
        ready_timeout_ms=15000,
    )


@pytest.fixture
async def rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    loop = await make_loop(tmp_path, monkeypatch, agents=[_stub_agent("steerable")])
    async with TuiRpcHarness(loop, monkeypatch) as harness:
        yield harness


def _history(rpc: TuiRpcHarness, handle: str):
    return lambda: rpc.call("subagents.instance.history", {"session_key": SESSION, "agent": "stub", "handle": handle})


def _rows(result) -> list[tuple[str, str, bool]]:
    return [(t["role"], t["content"], bool(t.get("steer"))) for t in result["turns"]]


async def test_a_steer_lands_in_the_running_turn_and_the_record_reads_in_order(rpc: TuiRpcHarness) -> None:
    await rpc.call("turn.subscribe", {"session_key": SESSION})
    sent = await rpc.call(
        "turn.send",
        {"session_key": SESSION, "content": "go", "target": {"agent": "stub", "handle": "h1"}},
    )
    assert sent["accepted"] is True

    # The turn is live: the stub said "on it" and is holding the prompt open
    # for a steer. The history read is what the pane polls while it works.
    live = await rpc.poll(
        _history(rpc, "h1"),
        lambda r: any(t.get("live") and t["content"] == "on it" for t in r["turns"]),
    )
    assert [t["role"] for t in live["turns"]][:2] == ["user", "assistant"]

    steer = await rpc.call(
        "subagents.instance.steer",
        {"session_key": SESSION, "agent": "stub", "handle": "h1", "text": "the docs first"},
    )
    assert steer == {"status": "injected"}

    done = await rpc.wait_event(
        lambda e: e.get("type") == "message.complete" and (e.get("payload") or {}).get("target") is not None
    )
    assert done["payload"]["target"] == {"agent": "stub", "handle": "h1"}

    settled = await rpc.poll(_history(rpc, "h1"), lambda r: not any(t.get("live") for t in r["turns"]))
    rows = _rows(settled)
    assert rows == [
        ("user", "go", False),
        ("assistant", "on it", False),
        ("user", "the docs first", True),
        ("assistant", "steered: the docs first", False),
    ], rows


async def test_a_steer_after_the_turn_ended_reports_no_turn(rpc: TuiRpcHarness) -> None:
    await rpc.call("turn.subscribe", {"session_key": SESSION})
    await rpc.call(
        "turn.send",
        {"session_key": SESSION, "content": "go", "target": {"agent": "stub", "handle": "h2"}},
    )
    await rpc.poll(_history(rpc, "h2"), lambda r: any(t.get("live") for t in r["turns"]))
    assert (await rpc.call("subagents.instance.steer", _steer("h2", "first"))) == {"status": "injected"}
    await rpc.wait_event(lambda e: e.get("type") == "message.complete" and (e.get("payload") or {}).get("target"))
    await rpc.poll(_history(rpc, "h2"), lambda r: not any(t.get("live") for t in r["turns"]))

    # Nothing is running now; the pane's composer falls back to a plain send.
    assert (await rpc.call("subagents.instance.steer", _steer("h2", "late"))) == {"status": "no_turn"}


def _steer(handle: str, text: str) -> dict:
    return {"session_key": SESSION, "agent": "stub", "handle": handle, "text": text}
