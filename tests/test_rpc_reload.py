"""Tests for the ``reload.mcp`` RPC handler.

The handler now reconciles the live MCP server set instead of returning a fixed
no-op. Two things are pinned here, both of which it used to get wrong:

* the response speaks the shape the TUI reads. It branches on ``status``, and
  the old handler never sent one -- so every ``/reload-mcp``, including a
  failure, printed "reload complete".
* a poll against unchanged config never reaches a transport. The config-watch
  caller fires on a timer, and reconnecting on every tick would be a retry
  storm against every configured server.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from raven.config.schema import MCPServerConfig
from raven.mcp.report import ApplyReport
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.reload import register_reload_methods, reload_mcp
from raven.rpc.models import ReloadMcpParams, ReloadMcpResult


@dataclass
class _FakeLoop:
    """The two methods the handler reaches for, and a record of what it called."""

    changed: bool = True
    applied: list[dict] = None  # type: ignore[assignment]
    probed: int = 0
    raises: BaseException | None = None
    """What the reconcile fails with, if it does. ``apply_config`` re-raises a
    ``SandboxInitError`` from any attempt and any other exception from one, so a
    fake that can only succeed cannot reach the handler's guard."""

    def __post_init__(self) -> None:
        self.applied = []

    def mcp_config_changed(self, servers: dict) -> bool:
        self.probed += 1
        return self.changed

    async def apply_mcp_config(self, servers: dict) -> ApplyReport:
        self.applied.append(servers)
        if self.raises is not None:
            raise self.raises
        return ApplyReport(reloaded=2, tools_changed=True)


@pytest.fixture()
def config(monkeypatch):
    """A loadable config carrying one MCP server.

    Patched at ``raven.config.loader.load_config`` because the handler imports
    it inside the call, so the module attribute is what it resolves.
    """
    servers = {"svc": MCPServerConfig(url="https://svc.test/mcp")}

    class _Tools:
        mcp_servers = servers

    class _Cfg:
        tools = _Tools()

    monkeypatch.setattr("raven.config.loader.load_config", lambda *a, **k: _Cfg())
    return servers


class TestNoLiveLoop:
    async def test_without_a_loop_it_answers_noop_not_an_error(self) -> None:
        result = await reload_mcp({})
        assert result["ok"] is True
        assert result["status"] == "noop"
        assert result["reloaded"] == 0
        assert result["tools_changed"] is False

    async def test_a_factory_that_raises_is_a_noop_too(self) -> None:
        def boom():
            raise RuntimeError("no loop yet")

        result = await reload_mcp({}, agent_loop_factory=boom)
        assert result["status"] == "noop"


class TestUnchangedConfigCostsNothing:
    async def test_it_does_not_apply_when_nothing_changed(self, config) -> None:
        loop = _FakeLoop(changed=False)
        result = await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop)
        assert result["status"] == "noop"
        assert loop.applied == []

    async def test_a_hundred_polls_touch_no_transport(self, config) -> None:
        # The config-watch caller fires on a timer; the probe is the only thing
        # that may run per tick.
        loop = _FakeLoop(changed=False)
        for _ in range(100):
            assert (await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop))["ok"] is True
        assert loop.probed == 100
        assert loop.applied == []


class TestConfirmation:
    async def test_a_pending_change_asks_before_rebuilding_the_cache(self, config) -> None:
        loop = _FakeLoop(changed=True)
        result = await reload_mcp({}, agent_loop_factory=lambda: loop)
        assert result["status"] == "confirm_required"
        assert loop.applied == []
        assert "cache" in result["message"]

    async def test_confirm_applies(self, config) -> None:
        loop = _FakeLoop(changed=True)
        result = await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop)
        assert result["status"] == "reloaded"
        assert result["reloaded"] == 2
        assert result["tools_changed"] is True
        assert len(loop.applied) == 1

    async def test_there_is_no_remember_this_flag(self, config) -> None:
        # `always` used to mean "and stop asking" on the TUI side and was never
        # persisted anywhere. It is gone rather than accepted-and-ignored: a
        # flag the caller believes was stored is worse than one that does not
        # exist, and the TUI printed a promise on the strength of it.
        loop = _FakeLoop(changed=True)
        result = await reload_mcp({"always": True}, agent_loop_factory=lambda: loop)
        assert result["status"] == "confirm_required"
        assert loop.applied == []


class TestContract:
    async def test_extra_params_are_accepted(self, config) -> None:
        loop = _FakeLoop(changed=True)
        result = await reload_mcp(
            {"confirm": True, "force": True, "selective": ["everos"]},
            agent_loop_factory=lambda: loop,
        )
        assert result["ok"] is True

    async def test_every_answer_carries_the_full_shape(self, config) -> None:
        # The TUI reads `status`, the web caller reads `reloaded`/`tools_changed`.
        # A branch that omits one leaves that consumer reading undefined.
        loop = _FakeLoop(changed=True)
        answers = [
            await reload_mcp({}),
            await reload_mcp({}, agent_loop_factory=lambda: loop),
            await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop),
            await reload_mcp({"confirm": True}, agent_loop_factory=lambda: _FakeLoop(changed=False)),
        ]
        for answer in answers:
            assert set(answer) >= {"ok", "status", "message", "reloaded", "tools_changed"}
            assert answer["status"] in ("reloaded", "noop", "confirm_required")

    async def test_a_failed_reconcile_is_an_answer_not_an_error(self, config) -> None:
        """MUST NOT throw is the published contract, and the reconcile does.

        ``apply_config`` re-raises ``SandboxInitError`` from any attempt after
        every pending server has had its turn, so a single misconfigured stdio
        server used to come back as a JSON-RPC internal error with a traceback
        in the log -- for a method whose whole point is that a caller which may
        fire on a timer never has to explain one.
        """
        from raven.sandbox import SandboxInitError

        loop = _FakeLoop(changed=True, raises=SandboxInitError("sandbox image missing"))
        result = await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop)

        assert result["status"] == "noop"
        # The reason survives into the line the TUI prints, rather than being
        # swallowed into a generic failure.
        assert "sandbox image missing" in result["message"]
        assert result["reloaded"] == 0 and result["tools_changed"] is False
        ReloadMcpResult.model_validate(result)

    async def test_the_dispatcher_sees_no_error_for_a_failed_reconcile(self, config) -> None:
        """The contract is about what reaches the client, so assert there too."""
        loop = _FakeLoop(changed=True, raises=RuntimeError("transport refused"))
        d = Dispatcher()
        register_reload_methods(d, agent_loop_factory=lambda: loop)

        resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {"confirm": True}})
        assert "error" not in resp, resp
        assert resp["result"]["status"] == "noop"
        assert "transport refused" in resp["result"]["message"]

    async def test_registered_via_helper(self, config) -> None:
        loop = _FakeLoop(changed=True)
        d = Dispatcher()
        register_reload_methods(d, agent_loop_factory=lambda: loop)
        resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {"confirm": True}})
        assert "error" not in resp
        assert resp["result"]["status"] == "reloaded"

    async def test_the_helper_still_registers_without_a_factory(self) -> None:
        d = Dispatcher()
        register_reload_methods(d)
        resp = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "reload.mcp", "params": {}})
        assert resp["result"]["status"] == "noop"


class TestThePublishedContract:
    """Every branch's response validates against the declared result model.

    The gap this closes: ``test_rpc_schema_match`` compares the OpenRPC schema
    against the Pydantic models, and nothing compared either against what a
    handler actually returns. So when this method stopped being a stub, the
    schema and the model stayed stale *in agreement with each other* -- the
    guardrail passed while all three responses were rejected by the very model
    that claims to describe them, including the ``status`` field the whole
    feature turns on.
    """

    async def test_every_branch_validates_against_the_result_model(self, config) -> None:
        loop = _FakeLoop(changed=True)
        branches = {
            "noop (no loop)": await reload_mcp({}),
            "noop (unchanged)": await reload_mcp(
                {"confirm": True}, agent_loop_factory=lambda: _FakeLoop(changed=False)
            ),
            "confirm_required": await reload_mcp({}, agent_loop_factory=lambda: loop),
            "reloaded": await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop),
        }
        for label, response in branches.items():
            try:
                ReloadMcpResult.model_validate(response)
            except ValidationError as e:
                pytest.fail(f"the {label} branch does not match the declared result model: {e}")

    async def test_the_model_covers_the_status_values_the_handler_emits(self, config) -> None:
        loop = _FakeLoop(changed=True)
        seen = {
            (await reload_mcp({}))["status"],
            (await reload_mcp({}, agent_loop_factory=lambda: loop))["status"],
            (await reload_mcp({"confirm": True}, agent_loop_factory=lambda: loop))["status"],
        }
        declared = set(ReloadMcpResult.model_fields["status"].annotation.__args__)
        assert seen <= declared, f"handler emits {seen - declared} which the model forbids"

    async def test_the_params_the_tui_sends_are_declared(self) -> None:
        # ops.ts sends session_id on every call and adds confirm for `now`.
        # A params model that forbids them would reject the call before the
        # handler ever ran.
        ReloadMcpParams.model_validate({"session_id": "s1", "confirm": True})
        ReloadMcpParams.model_validate({"session_id": None})
        ReloadMcpParams.model_validate({})
