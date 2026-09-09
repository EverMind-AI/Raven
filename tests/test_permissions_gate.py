"""The permission gate's waterfall and its turn-side enforcement."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.config.schema import PermissionsConfig
from raven.contracts.permissions import (
    Allow,
    ApprovalChoice,
    ApprovalOutcome,
    DecisionSource,
    Deny,
    NeedsApproval,
    Tier,
)
from raven.contracts.tool import Continuation
from raven.permissions.builtin import BuiltinRulings, action_digest, action_line
from raven.permissions.gate import PermissionGate
from raven.permissions.rules import exec_rule_tier
from raven.permissions.session import set_session_mode
from raven.permissions.turn import start_permission_turn


class Responder:
    def __init__(self, outcome: ApprovalOutcome):
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def await_approval(self, **kwargs: Any) -> ApprovalOutcome:
        self.calls.append(kwargs)
        return self.outcome


def gate_for(config: PermissionsConfig, *, judge=None, allow_ask: bool = True, families=()) -> PermissionGate:
    builtin = BuiltinRulings()
    for name, matcher in families:
        builtin._policy.register_approval_matcher(name, matcher)
    return PermissionGate(
        config_source=lambda: config,
        builtin=builtin,
        judge_provider_for=judge,
        allow_ask=allow_ask,
    )


def bind(responder: Responder | None) -> None:
    start_permission_turn(responder, conversation_id="conv-1", turn_id="turn-1")


@pytest.mark.asyncio
async def test_builtin_deny_outranks_user_allow():
    gate = gate_for(PermissionsConfig(tools={"exec": {"*": "allow"}}))
    decision = await gate.check("exec", {"command": "dd if=/dev/zero of=/dev/sda"})
    assert isinstance(decision, Deny)
    assert decision.source is DecisionSource.BUILTIN_DENY


@pytest.mark.asyncio
async def test_user_deny_holds_in_full_mode():
    gate = gate_for(PermissionsConfig(mode="full", tools={"exec": {"git push *": "deny"}}))
    decision = await gate.check("exec", {"command": "git push origin main"})
    assert isinstance(decision, Deny)
    assert decision.source is DecisionSource.USER_DENY


@pytest.mark.asyncio
async def test_a_user_allow_rule_lifts_a_declared_family():
    # A declared family is not a mandate: the user's own allow rule is the
    # user's decision, and it wins.
    from raven.agent.tools.shell_policy import DELETE_MATCHERS

    gate = gate_for(PermissionsConfig(tools={"exec": {"rm *": "allow"}}), families=DELETE_MATCHERS)
    decision = await gate.check("exec", {"command": "rm stale.txt"})
    assert isinstance(decision, Allow)
    assert decision.source is DecisionSource.USER_ALLOW


@pytest.mark.asyncio
async def test_full_mode_runs_a_declared_family():
    # full means full: the family names a prompt that full mode never shows.
    from raven.agent.tools.shell_policy import DELETE_MATCHERS

    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    gate = gate_for(PermissionsConfig(mode="full"), families=DELETE_MATCHERS)
    bind(responder)
    assert await gate.enforce("exec", {"command": "rm stale.txt"}) is None
    assert responder.calls == []


@pytest.mark.asyncio
async def test_a_declared_family_names_the_prompt():
    # What a family is for: when the ask tier does prompt, the human reads the
    # family's line instead of the generic one.
    from raven.agent.tools.shell_policy import DELETE_MATCHERS

    gate = gate_for(PermissionsConfig(mode="ask"), families=DELETE_MATCHERS)
    decision = await gate.check("exec", {"command": "rm stale.txt"})
    assert isinstance(decision, NeedsApproval)
    assert decision.description == "Delete files using a shell command"
    assert decision.family == "delete_command"


@pytest.mark.asyncio
async def test_full_mode_allows_ask_tier():
    gate = gate_for(PermissionsConfig(mode="full"))
    decision = await gate.check("write_file", {"path": "a.txt", "content": "x"})
    assert isinstance(decision, Allow)
    assert decision.source is DecisionSource.MODE


@pytest.mark.asyncio
async def test_ask_tier_unattended_denies_and_continues():
    gate = gate_for(PermissionsConfig())
    bind(None)
    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert refusal is not None
    assert refusal.continuation is Continuation.CONTINUE
    assert "not interactive" in refusal.model_text
    assert refusal.blocks_call and not refusal.retryable


@pytest.mark.asyncio
async def test_approval_allow_waves_call_through():
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    bind(responder)
    assert await gate.enforce("write_file", {"path": "a.txt", "content": "x"}) is None
    assert len(responder.calls) == 1
    assert responder.calls[0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_denied_digest_not_reasked_within_turn():
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.DENY, feedback="use the draft dir"))
    bind(responder)
    first = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert first is not None and "use the draft dir" in first.model_text
    second = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert second is not None and "earlier in the current turn" in second.model_text
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_deny_stop_aborts_turn():
    gate = gate_for(PermissionsConfig())
    bind(Responder(ApprovalOutcome(ApprovalChoice.DENY_STOP)))
    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert refusal is not None
    assert refusal.continuation is Continuation.ABORT_TURN


@pytest.mark.asyncio
async def test_unattended_gate_ignores_inherited_responder():
    gate = gate_for(PermissionsConfig(), allow_ask=False)
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    bind(responder)
    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert refusal is not None
    assert "not interactive" in refusal.model_text
    assert responder.calls == []


@pytest.mark.asyncio
async def test_read_only_default_runs_without_asking():
    gate = gate_for(PermissionsConfig())
    bind(None)
    assert await gate.enforce("read_file", {"path": "a.txt"}) is None


@pytest.mark.asyncio
async def test_smart_mode_judge_allow_runs_call(monkeypatch):
    from raven.permissions import gate as gate_module
    from raven.permissions.judge import JudgeOutcome

    async def fake_review(provider, **kwargs):
        return JudgeOutcome(allow=True, reason="read-only in effect")

    monkeypatch.setattr(gate_module, "review", fake_review)
    gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())
    bind(None)
    assert await gate.enforce("write_file", {"path": "a.txt", "content": "x"}) is None


@pytest.mark.asyncio
async def test_smart_mode_escalation_reaches_responder(monkeypatch):
    from raven.permissions import gate as gate_module
    from raven.permissions.judge import JudgeOutcome

    async def fake_review(provider, **kwargs):
        return JudgeOutcome(allow=False, reason="writes outside workspace")

    monkeypatch.setattr(gate_module, "review", fake_review)
    gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    bind(responder)
    assert await gate.enforce("write_file", {"path": "a.txt", "content": "x"}) is None
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_smart_mode_judge_failure_escalates_then_denies_unattended(monkeypatch):
    from raven.permissions import gate as gate_module
    from raven.permissions.judge import JudgeOutcome

    async def fake_review(provider, **kwargs):
        return JudgeOutcome(allow=False, reason="review timed out", failed=True)

    monkeypatch.setattr(gate_module, "review", fake_review)
    gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())
    bind(None)
    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    assert refusal is not None
    assert "not interactive" in refusal.model_text


@pytest.mark.asyncio
async def test_mode_reads_live_between_calls():
    configs = iter([PermissionsConfig(mode="full"), PermissionsConfig(mode="ask")])
    holder = {"cfg": None}

    def source():
        holder["cfg"] = next(configs)
        return holder["cfg"]

    gate = PermissionGate(config_source=source, builtin=BuiltinRulings(), allow_ask=True)
    bind(None)
    assert await gate.enforce("write_file", {"path": "a", "content": "x"}) is None
    refusal = await gate.enforce("write_file", {"path": "b", "content": "x"})
    assert refusal is not None


def test_exec_rules_compound_and_opaque():
    table = {"git *": "allow", "npm run *": "allow", "rm *": "deny", "*": "ask"}
    assert exec_rule_tier("git status && git log", table) is Tier.ALLOW
    assert exec_rule_tier("git status && pip install x", table) is Tier.ASK
    assert exec_rule_tier("git status && rm -rf /", table) is Tier.DENY
    assert exec_rule_tier("git status > out.txt", table) is Tier.ASK
    assert exec_rule_tier("sudo git push", {"git *": "allow"}) is None


@pytest.mark.asyncio
async def test_smart_mode_review_is_announced_to_the_surface(monkeypatch):
    from raven.permissions import gate as gate_module
    from raven.permissions.judge import JudgeOutcome

    async def fake_review(provider, **kwargs):
        return JudgeOutcome(allow=True, reason="fine")

    monkeypatch.setattr(gate_module, "review", fake_review)
    phases: list[tuple[str, str]] = []

    async def on_review(phase: str, tool: str) -> None:
        phases.append((phase, tool))
        if phase == "ended":
            raise RuntimeError("a display failure must not change the decision")

    start_permission_turn(None, conversation_id="conv", turn_id="turn", on_review=on_review)
    gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())

    assert await gate.enforce("write_file", {"path": "a", "content": "x"}) is None
    assert phases == [("started", "write_file"), ("ended", "write_file")]


@pytest.mark.asyncio
async def test_sandboxing_earns_no_relaxation_at_the_gate():
    # The Boxlite VM mounts the real workspace read-write, so "the sandbox
    # holds it" is false for host data: rm -rf /workspace deletes real files.
    # The gate never reads the executor's sandbox flag -- the ask tier asks
    # and the deny list refuses inside the VM exactly as on the host.
    gate = gate_for(PermissionsConfig(mode="ask"))
    bind(None)
    assert isinstance(await gate.check("exec", {"command": "rm -rf /workspace"}), NeedsApproval)
    assert isinstance(await gate.check("write_file", {"path": "a.txt", "content": "x"}), NeedsApproval)
    full = gate_for(PermissionsConfig(mode="full"))
    assert isinstance(await full.check("exec", {"command": "rm -rf /"}), Deny)


@pytest.mark.asyncio
async def test_an_unparseable_command_says_how_to_fix_it_rather_than_to_give_up():
    """Fail-closed, but not as a protected action.

    An unbalanced quote is the model's own to fix. Landing it like a refused
    ``rm -rf /`` -- siblings blocked, "do not retry by any other means" -- is
    what once ended a build with nothing published, so the parse failure keeps
    the call open and says which repair to make.
    """
    gate = gate_for(PermissionsConfig(mode="full"))
    bind(None)

    decision = await gate.check("exec", {"command": "echo 'unterminated"})
    assert isinstance(decision, Deny)
    assert decision.source is DecisionSource.BUILTIN_PARSE_ERROR

    result = await gate.enforce("exec", {"command": "echo 'unterminated"})
    assert result is not None and not result.ok
    assert "Close the quote" in result.model_text
    assert "carry on with the rest of the task" not in result.model_text
    # The turn goes on and the call stays the model's to retry.
    assert result.continuation is Continuation.CONTINUE
    assert result.blocks_call is False and result.retryable is True


@pytest.mark.asyncio
async def test_a_protected_refusal_still_tells_the_model_to_move_on():
    """The other half: a real refusal keeps the stop instruction, and that
    instruction refuses the command rather than the task."""
    gate = gate_for(PermissionsConfig(mode="full"))
    bind(None)

    result = await gate.enforce("exec", {"command": "rm -rf /"})
    assert result is not None and result.blocks_call is True
    assert "carry on with the rest of the task without it" in result.model_text


def _probe_tool(tool_name: str, execute=None):
    from raven.contracts.tool import Tool

    class _Probe(Tool):
        @property
        def name(self):
            return tool_name

        @property
        def description(self):
            return "probe"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            if execute is not None:
                return await execute(**kwargs)
            return "ok"

    return _Probe()


class TestDecisionsLandOnTheEmittedSpan:
    """The audit contract: every terminal gate decision is on the tool.call
    span that dispatched the call -- reviewer verdict and final decision
    together, and a nested dispatch keeps its own record. Asserted over spans
    actually written by the tracing store, not over check() return values."""

    @pytest.fixture
    def trace_dir(self, tmp_path, monkeypatch):
        from raven.tracing import spans as _spans

        monkeypatch.setenv("RAVEN_TRACING", "1")
        monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
        _spans._store = None
        yield tmp_path
        _spans._store = None

    def _spans_written(self, trace_dir):
        import json

        path = trace_dir / "logs" / "audit-spans.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def _registry(self, gate):
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry(permission_gate=gate)
        registry.register(_probe_tool("write_file"))
        return registry

    @pytest.mark.asyncio
    async def test_an_approved_ask_call_records_the_human_decision(self, trace_dir):
        gate = gate_for(PermissionsConfig())
        bind(Responder(ApprovalOutcome(ApprovalChoice.ALLOW)))
        registry = self._registry(gate)

        await registry.execute("write_file", {})

        span = next(s for s in self._spans_written(trace_dir) if s["name"] == "tool.call")
        attrs = span["attributes"]
        assert attrs["permission.decision"] == "allow"
        assert attrs["permission.source"] == "approval"
        assert attrs["permission.approval.choice"] == "allow"

    @pytest.mark.asyncio
    async def test_a_declared_family_is_recorded_whichever_way_the_tiers_decide(self, trace_dir):
        # The family does not decide, but the audit keeps the classification:
        # full mode runs the delete, and the span still says which family it was.
        from raven.agent.tools.shell_policy import DELETE_MATCHERS

        gate = gate_for(PermissionsConfig(mode="full"), families=DELETE_MATCHERS)
        bind(None)
        registry = self._registry(gate)
        registry.register(_probe_tool("exec"))

        await registry.execute("exec", {"command": "rm stale.txt"})

        span = next(s for s in self._spans_written(trace_dir) if s["name"] == "tool.call")
        attrs = span["attributes"]
        assert attrs["permission.decision"] == "allow"
        assert attrs["permission.source"] == "mode"
        assert attrs["permission.family"] == "delete_command"

    @pytest.mark.asyncio
    async def test_a_declared_family_is_recorded_on_a_user_deny_too(self, trace_dir):
        from raven.agent.tools.shell_policy import DELETE_MATCHERS

        gate = gate_for(PermissionsConfig(mode="full", tools={"exec": {"rm *": "deny"}}), families=DELETE_MATCHERS)
        bind(None)
        registry = self._registry(gate)
        registry.register(_probe_tool("exec"))

        await registry.execute("exec", {"command": "rm stale.txt"})

        span = next(s for s in self._spans_written(trace_dir) if s["name"] == "tool.call")
        attrs = span["attributes"]
        assert attrs["permission.decision"] == "deny"
        assert attrs["permission.source"] == "user_deny"
        assert attrs["permission.family"] == "delete_command"

    @pytest.mark.asyncio
    async def test_a_smart_allow_keeps_reviewer_and_final_fields_together(self, trace_dir, monkeypatch):
        from raven.permissions import gate as gate_module
        from raven.permissions.judge import JudgeOutcome

        async def fake_review(provider, **kwargs):
            return JudgeOutcome(allow=True, reason="benign write")

        monkeypatch.setattr(gate_module, "review", fake_review)
        gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())
        bind(None)
        registry = self._registry(gate)

        await registry.execute("write_file", {})

        span = next(s for s in self._spans_written(trace_dir) if s["name"] == "tool.call")
        attrs = span["attributes"]
        assert attrs["permission.judge.decision"] == "allow"
        assert attrs["permission.judge.reason"] == "benign write"
        assert attrs["permission.decision"] == "allow"
        assert attrs["permission.source"] == "judge"

    @pytest.mark.asyncio
    async def test_a_human_approved_escalation_records_both_verdicts(self, trace_dir, monkeypatch):
        from raven.permissions import gate as gate_module
        from raven.permissions.judge import JudgeOutcome

        async def fake_review(provider, **kwargs):
            return JudgeOutcome(allow=False, reason="writes host state")

        monkeypatch.setattr(gate_module, "review", fake_review)
        gate = gate_for(PermissionsConfig(mode="smart"), judge=lambda: object())
        bind(Responder(ApprovalOutcome(ApprovalChoice.ALLOW)))
        registry = self._registry(gate)

        await registry.execute("write_file", {})

        span = next(s for s in self._spans_written(trace_dir) if s["name"] == "tool.call")
        attrs = span["attributes"]
        assert attrs["permission.judge.decision"] == "escalate"
        assert attrs["permission.decision"] == "allow"
        assert attrs["permission.source"] == "approval"

    @pytest.mark.asyncio
    async def test_a_nested_dispatch_keeps_each_calls_own_record(self, trace_dir):
        from raven.agent.tools.registry import ToolRegistry

        gate = gate_for(PermissionsConfig(mode="full"))
        bind(None)
        registry = ToolRegistry(permission_gate=gate)

        async def _forward(**kwargs):
            return str(await registry.execute("write_file", {}))

        registry.register(_probe_tool("write_file"))
        registry.register(_probe_tool("tool_call", execute=_forward))

        await registry.execute("tool_call", {})

        spans = [s for s in self._spans_written(trace_dir) if s["name"] == "tool.call"]
        by_tool = {s["attributes"].get("tool.name"): s["attributes"] for s in spans}
        assert by_tool["write_file"]["permission.decision"] == "allow"
        assert by_tool["write_file"]["permission.source"] == "mode"
        # tool_call is on the read-only default-allow list; the outer span must
        # keep ITS decision even though the inner dispatch annotated its own.
        assert by_tool["tool_call"]["permission.decision"] == "allow"
        assert by_tool["tool_call"]["permission.source"] == "default"


@pytest.mark.asyncio
async def test_a_conversation_runs_in_its_own_mode_over_the_default():
    gate = gate_for(PermissionsConfig(mode="ask"))
    set_session_mode("conv-1", "full")
    try:
        bind(None)
        decision = await gate.check("write_file", {"path": "x", "content": "y"})
        assert isinstance(decision, Allow)
        assert decision.source is DecisionSource.MODE
        # Another conversation in the same process still reads the default.
        start_permission_turn(None, conversation_id="conv-2", turn_id="turn-1")
        assert isinstance(await gate.check("write_file", {"path": "x", "content": "y"}), NeedsApproval)
    finally:
        set_session_mode("conv-1", None)
    bind(None)
    assert isinstance(await gate.check("write_file", {"path": "x", "content": "y"}), NeedsApproval)


def test_a_command_bound_for_another_machine_says_so_on_the_prompt():
    # The same text deletes different data on different computers; the human
    # reads which one before the key turns. A local command reads as before.
    here = {"command": "rm -rf /data"}
    there = {"command": "rm -rf /data", "machine": "prod-gpu-1"}
    assert action_line("exec", here) == "rm -rf /data"
    assert action_line("exec", there) == "rm -rf /data (on prod-gpu-1)"
    assert action_line("exec", {"command": "ls", "machine": ""}) == "ls"


def test_the_machine_is_part_of_the_action_for_dedup_and_the_local_key_is_unchanged():
    from hashlib import sha256

    here = {"command": "rm -rf /data"}
    there = {"command": "rm -rf /data", "machine": "prod-gpu-1"}
    assert action_digest("exec", here) == sha256(b"exec\x00rm -rf /data").hexdigest()
    assert action_digest("exec", here) != action_digest("exec", there)
    assert action_digest("exec", there) != action_digest("exec", {**there, "machine": "prod-gpu-2"})


@pytest.mark.asyncio
async def test_refusing_a_command_here_does_not_pre_refuse_it_on_another_machine():
    # Dedup exists so one refusal is not re-asked in the same turn. A different
    # machine is a different action, so it is asked about on its own.
    gate = gate_for(PermissionsConfig(mode="ask"))
    responder = Responder(ApprovalOutcome(ApprovalChoice.DENY))
    bind(responder)

    first = await gate.enforce("exec", {"command": "rm -rf /data"})
    second = await gate.enforce("exec", {"command": "rm -rf /data", "machine": "prod-gpu-1"})

    assert first is not None and "denied" in first.model_text
    assert second is not None and "denied" in second.model_text
    assert len(responder.calls) == 2, "the remote command was asked about, not refused as a repeat"
    assert responder.calls[1]["command"] == "rm -rf /data (on prod-gpu-1)"
    assert "(on prod-gpu-1)" in responder.calls[1]["description"]


class _RaisingProvider:
    async def chat_with_retry(self, **kwargs: Any) -> Any:
        raise RuntimeError("provider is down")


class _HangingProvider:
    async def chat_with_retry(self, **kwargs: Any) -> Any:
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_a_reviewer_that_throws_escalates_rather_than_allows():
    # The gate's handling of a failed review is pinned elsewhere by stubbing
    # ``review``; this pins ``review`` itself, where the fail-closed promise is
    # actually kept. A provider error must never read as permission.
    from raven.permissions.judge import review

    outcome = await review(_RaisingProvider(), tool_name="exec", params={"command": "rm -rf build"})

    assert outcome.allow is False
    assert outcome.failed is True
    assert "review failed" in outcome.reason


@pytest.mark.asyncio
async def test_a_reviewer_that_hangs_escalates_at_the_deadline():
    from raven.permissions.judge import review

    outcome = await review(_HangingProvider(), tool_name="exec", params={"command": "rm -rf build"}, timeout_s=0.05)

    assert outcome.allow is False
    assert outcome.failed is True
    assert "timed out" in outcome.reason
