"""The permission gate's waterfall and its turn-side enforcement."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import raven.permissions.rules as rules_module
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
from raven.permissions.rules import DEFAULT_ALLOW_TOOLS, exec_rule_tier
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
    from raven.permissions.shell_policy import DELETE_MATCHERS

    gate = gate_for(PermissionsConfig(tools={"exec": {"rm *": "allow"}}), families=DELETE_MATCHERS)
    decision = await gate.check("exec", {"command": "rm stale.txt"})
    assert isinstance(decision, Allow)
    assert decision.source is DecisionSource.USER_ALLOW


@pytest.mark.asyncio
async def test_full_mode_runs_a_declared_family():
    # full means full: the family names a prompt that full mode never shows.
    from raven.permissions.shell_policy import DELETE_MATCHERS

    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    gate = gate_for(PermissionsConfig(mode="full"), families=DELETE_MATCHERS)
    bind(responder)
    assert await gate.enforce("exec", {"command": "rm stale.txt"}) is None
    assert responder.calls == []


@pytest.mark.asyncio
async def test_a_declared_family_names_the_prompt():
    # What a family is for: when the ask tier does prompt, the human reads the
    # family's line instead of the generic one.
    from raven.permissions.shell_policy import DELETE_MATCHERS

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
async def test_a_lapsed_approval_is_not_reported_as_a_refusal():
    """The two shared one sentence, and they are not one fact.

    Observed: three approvals expired unanswered in a row, the model read
    "user denied", stopped asking, went around, and told the reader the task
    had hit a system error -- having delivered something else. What it was
    told is what it acted on.
    """
    gate = gate_for(PermissionsConfig())
    bind(Responder(ApprovalOutcome(ApprovalChoice.DENY, answered=False)))

    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})

    assert refusal is not None
    assert "expired with no answer" in refusal.model_text
    assert "Nobody refused it" in refusal.model_text
    # The word the model acts on must not be there at all: "denied" is what sent
    # it looking for another way.
    assert "denied" not in refusal.model_text
    # Still refused. Failing closed is not what is being changed.
    assert refusal.ok is False


@pytest.mark.asyncio
async def test_an_answered_refusal_still_says_the_user_refused():
    """The other half. A real denial must keep reading as one, feedback and
    all, or the change has only moved the ambiguity."""
    gate = gate_for(PermissionsConfig())
    bind(Responder(ApprovalOutcome(ApprovalChoice.DENY, feedback="not in prod")))

    refusal = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})

    assert refusal is not None
    assert "User denied this action." in refusal.model_text
    assert "not in prod" in refusal.model_text
    assert "expired" not in refusal.model_text


@pytest.mark.asyncio
async def test_the_second_ask_after_a_lapse_says_it_lapsed_too():
    """A lapsed request is still deduped -- it would expire the same way, and
    forty iterations of a 35-second wait is a turn spent waiting. But the
    sentence about the second ask has to be true about the first, or the fix
    only moves the false one further down the turn."""
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.DENY, answered=False))
    bind(responder)

    await gate.enforce("write_file", {"path": "a.txt", "content": "x"})
    second = await gate.enforce("write_file", {"path": "a.txt", "content": "x"})

    assert second is not None
    assert "expired with no answer" in second.model_text
    assert "denied" not in second.model_text
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
async def test_deliver_files_default_runs_without_asking():
    """The one default-allow tool that is not a read. Handing the user a file
    they asked for is not an effect they need to approve, and the approval it
    used to raise expired unanswered often enough to lose finished work."""
    gate = gate_for(PermissionsConfig())
    bind(None)
    assert await gate.enforce("deliver_files", {"files": [{"path": "deck.pptx"}]}) is None


def test_the_documented_default_tier_names_its_one_exception():
    """The set, the module contract and `CONTEXT.md` are three homes for one rule.

    Both texts stated it as "read-only tools run, everything else asks" without
    naming anything, so adding the first non-read default made both false while
    a grep for the tool's name found neither. Pinned against the rendered text
    with whitespace collapsed, so a later rewrap cannot void this silently, and
    sliced to the `Tier` entry so a mention elsewhere cannot fake a pass.
    """

    def flat(text: str) -> str:
        return " ".join(text.split())

    assert "deliver_files" in DEFAULT_ALLOW_TOOLS
    assert "deliver_files" in flat(rules_module.__doc__ or ""), "the module contract does not name it"

    context = (Path(__file__).resolve().parents[1] / "CONTEXT.md").read_text(encoding="utf-8")
    tier = context.split("**Tier**:", 1)[1].split("\n**", 1)[0]
    assert "deliver_files" in flat(tier), "the CONTEXT.md Tier entry does not name it"


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
    assert exec_rule_tier("sudo git push", {"git *": "allow"}) is None
    # Substitution runs something the token view cannot see: only `*` speaks.
    assert exec_rule_tier("git status && echo $(rm -rf /)", table) is Tier.ASK
    assert exec_rule_tier("git log `id`", table) is Tier.ASK
    assert exec_rule_tier("git apply <<'EOF'", table) is Tier.ASK
    # A newline separates segments the same way `;` does.
    assert exec_rule_tier("git status\nrm -rf x", table) is Tier.DENY
    assert exec_rule_tier("git status\ngit log", table) is Tier.ALLOW
    # Punctuation inside quotes is text, not syntax.
    assert exec_rule_tier("git log --format='%h <%ae>'", table) is Tier.ALLOW


def test_exec_rules_judge_a_redirection_by_its_target():
    table = {"git *": "allow", "*": "ask"}
    # Relative without `..` lands under the cwd whatever it is; /dev/null and an
    # fd dup write nothing.
    assert exec_rule_tier("git status > out.txt", table) is Tier.ALLOW
    assert exec_rule_tier("git status >> logs/out.txt", table) is Tier.ALLOW
    assert exec_rule_tier("git status 2>/dev/null", table) is Tier.ALLOW
    assert exec_rule_tier("git status 2>&1", table) is Tier.ALLOW
    # Anywhere else, only `*` speaks.
    assert exec_rule_tier("git status > /tmp/out.txt", table) is Tier.ASK
    assert exec_rule_tier("git status > ../out.txt", table) is Tier.ASK
    assert exec_rule_tier("git status > ~/out.txt", table) is Tier.ASK
    assert exec_rule_tier("git status > $OUT", table) is Tier.ASK
    # A `$VAR` inside a word is a word.
    assert exec_rule_tier('git push origin "$BRANCH"', table) is Tier.ALLOW


@pytest.mark.parametrize(
    ("command", "ask_segments", "pattern"),
    [
        ("git push origin HEAD", ("git push origin HEAD",), "git push *"),
        ("git status | git push", ("git push",), "git push *"),
        # A noun-first CLI carries the verb, or `glab mr *` would allow `glab mr merge`.
        ("glab mr list | head -5", ("glab mr list",), "glab mr list *"),
        ("gh pr view 5 --json title", ("gh pr view 5 --json title",), "gh pr view *"),
        ("gh pr", ("gh pr",), ""),
        ("kubectl config view", ("kubectl config view",), "kubectl config view *"),
        # git is both shapes: a verb second, or a noun and then a verb.
        ("git remote add origin git@x:y.git", ("git remote add origin git@x:y.git",), "git remote add *"),
        ("git worktree add /tmp/w", ("git worktree add /tmp/w",), "git worktree add *"),
        ("git stash push -u", ("git stash push -u",), "git stash push *"),
        # The token after the noun is a name, not a verb: nothing to suggest.
        ("git branch feature", ("git branch feature",), ""),
        ("git branch -D feature", ("git branch -D feature",), ""),
        ("git tag v1", ("git tag v1",), ""),
        ("git config user.name x", ("git config user.name x",), ""),
        ("git reflog delete HEAD@{0}", ("git reflog delete 'HEAD@{0}'",), "git reflog delete *"),
        # The prefix would cover the flag that made the call ask: nothing safe.
        ("git diff --output=/tmp/x HEAD", ("git diff --output=/tmp/x HEAD",), ""),
        ("git ls-remote --upload-pack=touch .", ("git ls-remote --upload-pack=touch .",), ""),
        ("git log --ext-diff", ("git log --ext-diff",), ""),
        ("git grep -O needle", ("git grep -O needle",), ""),
        # Only a program built around subcommands gets a prefix suggested:
        # elsewhere the second token names a file or a host, and the rule
        # would match one call and then sit in the config forever.
        ("hostname newname", ("hostname newname",), ""),
        ("touch report.txt", ("touch report.txt",), ""),
        ("curl https://example.com", ("curl https://example.com",), ""),
        ("ssh host reboot", ("ssh host reboot",), ""),
        ("cargo build --release", ("cargo build --release",), "cargo build *"),
        ("tmux capture-pane -p", ("tmux capture-pane -p",), "tmux capture-pane *"),
        ('git push origin "$B"', ("git push origin '$B'",), "git push *"),
        ("git push origin HEAD > ./log", ("git push origin HEAD",), "git push *"),
        # Two segments to answer for: nothing to suggest.
        ("git fetch origin && git rebase x", ("git fetch origin", "git rebase x"), ""),
        # The five vetoes, one each.
        ("RAVEN_HOME=/x git push", ("RAVEN_HOME=/x git push",), ""),
        ("make", ("make",), ""),
        ("sudo git push", ("sudo git push",), ""),
        ("python3 scripts/x.py", ("python3 scripts/x.py",), ""),
        ("uv run pytest tests/", ("uv run pytest tests/",), ""),
        # Opaque, or reaching out: the whole command is the one segment.
        ("echo $(id)", ("echo $(id)",), ""),
        ("git push > /tmp/log", ("git push > /tmp/log",), ""),
    ],
)
def test_exec_approval_shape(command: str, ask_segments: tuple[str, ...], pattern: str):
    shape = rules_module.exec_approval_shape(command, {})
    assert shape.ask_segments == ask_segments
    assert shape.suggested_pattern == pattern


def test_a_segment_the_user_already_allows_is_not_asked_for():
    shape = rules_module.exec_approval_shape("git fetch origin && git push", {"git fetch *": "allow"})
    assert shape.ask_segments == ("git push",)
    assert shape.suggested_pattern == "git push *"


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [
        ("git push *", True),
        ("uv run *", True),
        ("make", True),
        ("sudo *", False),
        ("bash -c *", False),
        ("*", False),
        ("", False),
        ("git $(x) *", False),
        ("git push > x", False),
        ("a 'b", False),
    ],
)
def test_validate_exec_pattern(pattern: str, ok: bool):
    assert (rules_module.validate_exec_pattern(pattern) is None) is ok


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
        from raven.permissions.shell_policy import DELETE_MATCHERS

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
        from raven.permissions.shell_policy import DELETE_MATCHERS

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


@pytest.mark.parametrize(
    ("command", "reads_only"),
    [
        ("cat pyproject.toml", True),
        ("ls -la", True),
        ("git status", True),
        ("git log --oneline -20", True),
        ("grep -rn foo raven/ | head -50", True),
        ("cd raven && ls", True),
        ("pwd", True),
        ('cat "$CONFIG_PATH"', True),
        ("docker ps", True),
        # A wrapper is not stripped: the privilege is the point of the prompt.
        ("sudo cat /etc/shadow", False),
        ("bash -c 'cat x'", False),
        ("env FOO=1 cat x", False),
        # A path names a program this list never vetted.
        ("/bin/cat x", False),
        # Redirection writes; substitution runs something else.
        ("cat a > b", False),
        ("cat a >> b", False),
        ("python3 <<'PY'", False),
        ("echo $(rm -rf build)", False),
        ("diff <(git show a:f) <(git show b:f)", False),
        # The flag that turns each reader into a writer.
        ("find . -name '*.py' -delete", False),
        ("find . -exec rm {} ;", False),
        ("sort -o out.txt in.txt", False),
        ("date -s 12:00", False),
        ("printf -v shell_var x", False),
        # One segment that does not read makes the command not read-only.
        ("git status && git push", False),
        ("ls | xargs rm", False),
        # Mutating subcommands of a read-only-capable name.
        ("git push origin main", False),
        ("docker run alpine", False),
        # pwd reads only with no argument.
        ("pwd /tmp", False),
        ("find . -name '*.py'", True),
        ("sort in.txt", True),
        # Double quotes stop word splitting, not substitution: these run.
        ('echo "$(touch /tmp/x)"', False),
        ('echo "`id`"', False),
        ('cat "$(echo f)"', False),
        ('echo "a$(id)b"', False),
        # Single quotes and a backslash do make them literal.
        ("echo '$(touch x)'", True),
        ("echo '`id`'", True),
        ('echo "\\$(touch x)"', True),
        # Plain expansion introduces no command.
        ('echo "$HOME"', True),
        ('echo "${HOME}"', True),
        # A quoted separator is an argument. The shell runs one command here,
        # and splitting on it put `-delete` in a segment headed by `cat`, so
        # both halves passed the read-only list while find deleted the tree.
        ("find . ';' cat -delete", False),
        ("sort ';' cat -o out.txt", False),
        ("date ';' cat", False),
        ("base64 ';' cat -o out", False),
        ("grep ';' README.md", True),
        ("echo 'a;b'", True),
        ('echo "a|b"', True),
        # An option whose value names a program, whatever the command.
        ("sort -S 1 --compress-program=/tmp/evil big.txt", False),
        ("sort --compress-program /tmp/evil big.txt", False),
        ("rg --hostname-bin=/tmp/evil needle .", False),
        ("sort -S 1 big.txt", True),
        # A filter whose extra positional is an output file.
        ("uniq in.txt out.txt", False),
        ("uniq -c in.txt /etc/hosts.bak", False),
        ("uniq -c in.txt", True),
        # file -C compiles a magic file; command runs whatever follows it;
        # history -c clears the file.
        ("file -C -m mymagic", False),
        ("file README.md", True),
        ("command rm -rf x", False),
        ("history -c", False),
        ("history", True),
        # An unquoted newline is a second command, not whitespace.
        ("cat pyproject.toml\ntouch /tmp/x", False),
        ("git status\ngit log", True),
        # Named like a query, but the argument makes it a change.
        ("git branch -D feature", False),
        ("git branch feature", False),
        ("git branch --show-current", True),
        ("git branch", True),
        ("git tag release", False),
        ("git tag --sort=-creatordate", True),
        ("git remote remove origin", False),
        ("git remote -v", True),
        ("git remote show origin", True),
        ("sort -oout.txt in.txt", False),
        ("hostname newname", False),
        ("hostname", True),
        ("ifconfig eth0 down", False),
        ("fd -x rm . -e tmp", False),
        ("fd . -e py", True),
        ("rg --pre cat needle", False),
        ("tree -o out.txt", False),
        ("date 0910120026", False),
        ("date +%s", True),
        ("ls --all", True),
        # A query that writes, or runs a program, is not a query.
        ("git diff --output=/tmp/x HEAD~1 HEAD", False),
        ("git diff --output /tmp/x HEAD~1 HEAD", False),
        ("git log --output=/tmp/x", False),
        ("git show --ext-diff HEAD", False),
        ("git grep -O needle", False),
        ("git grep --open-files-in-pager needle", False),
        ("git grep needle", True),
        ("git diff HEAD~1 HEAD", True),
        # The same options reach every query, not just the ones seen first.
        ("git ls-remote --upload-pack='touch /tmp/x' .", False),
        # git spells a long option by any unambiguous prefix.
        ("git ls-remote --upl='touch /tmp/x' .", False),
        ("git ls-remote --u='touch /tmp/x' .", False),
        ("git log -- path/with/dashes", True),
        ("git ls-remote --upload='touch /tmp/x' .", False),
        ("git diff --out=/tmp/x HEAD~1", False),
        ("git log --ext HEAD", False),
        ("git log --oneline -5", True),
        ("git ls-remote origin", True),
        ("git shortlog --output=/tmp/x HEAD", False),
        ("git shortlog -sn", True),
        ("git rev-list --output=/tmp/x HEAD", False),
        ("git rev-list --count HEAD", True),
        ("git cat-file --filters HEAD:f", False),
        ("git cat-file -p HEAD:f", True),
        ("git blame --textconv f", False),
        ("git reflog expire --expire=now --all", False),
        ("git reflog delete HEAD@{0}", False),
        ("git reflog", True),
        ("git reflog show -5", True),
        # Options before the subcommand: only the ones that change nothing.
        ("git -C /elsewhere status", True),
        ("git --no-pager log -1", True),
        ("git -c alias.x='!rm -rf /' x", False),
        ("git -c core.pager=cat log", False),
        ("git --exec-path=/tmp/evil log", False),
    ],
)
def test_read_only_classification(command: str, reads_only: bool):
    assert rules_module.exec_reads_only(command) is reads_only


@pytest.mark.asyncio
async def test_a_read_only_command_runs_without_asking():
    gate = gate_for(PermissionsConfig())
    decision = await gate.check("exec", {"command": "git status | head -20"})
    assert isinstance(decision, Allow)
    assert decision.source is DecisionSource.DEFAULT


@pytest.mark.asyncio
async def test_a_mutating_command_still_asks():
    gate = gate_for(PermissionsConfig())
    assert isinstance(await gate.check("exec", {"command": "git push origin main"}), NeedsApproval)


@pytest.mark.asyncio
async def test_a_read_only_command_on_a_machine_still_asks():
    """The list speaks for this computer's files, not a registered machine's."""
    gate = gate_for(PermissionsConfig())
    decision = await gate.check("exec", {"command": "cat /etc/passwd", "machine": "prod-1"})
    assert isinstance(decision, NeedsApproval)


@pytest.mark.asyncio
async def test_a_user_ask_rule_outranks_the_read_only_default():
    gate = gate_for(PermissionsConfig(tools={"exec": {"git status *": "ask"}}))
    assert isinstance(await gate.check("exec", {"command": "git status"}), NeedsApproval)


@pytest.mark.asyncio
async def test_a_user_deny_rule_outranks_the_read_only_default():
    gate = gate_for(PermissionsConfig(tools={"exec": {"cat *": "deny"}}))
    decision = await gate.check("exec", {"command": "cat secrets.env"})
    assert isinstance(decision, Deny)
    assert decision.source is DecisionSource.USER_DENY


@pytest.fixture
def no_grants():
    from raven.permissions import session as session_module

    session_module._GRANTS.clear()
    yield
    session_module._GRANTS.clear()


@pytest.mark.asyncio
async def test_a_session_grant_stops_the_asking_for_the_rest_of_the_conversation(no_grants):
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW_SESSION))
    bind(responder)

    assert await gate.enforce("exec", {"command": "git push origin HEAD"}) is None
    assert len(responder.calls) == 1
    assert responder.calls[0]["suggested_pattern"] == "git push *"

    # Same segment, read-only company: the only part still asking was granted.
    assert await gate.enforce("exec", {"command": "git status && git push origin HEAD"}) is None
    assert len(responder.calls) == 1
    decision = await gate.check("exec", {"command": "git push origin HEAD"})
    assert isinstance(decision, Allow) and decision.source is DecisionSource.SESSION

    # Another argument is another action.
    assert await gate.enforce("exec", {"command": "git push origin main"}) is None
    assert len(responder.calls) == 2

    # Another directory is another action too.
    start_permission_turn(responder, conversation_id="conv-1", turn_id="turn-2")
    assert await gate.enforce("exec", {"command": "git push origin HEAD", "working_dir": "/elsewhere"}) is None
    assert len(responder.calls) == 3

    # Another conversation never inherits it.
    start_permission_turn(responder, conversation_id="conv-2", turn_id="turn-1")
    assert await gate.enforce("exec", {"command": "git push origin HEAD"}) is None
    assert len(responder.calls) == 4


@pytest.mark.asyncio
async def test_a_session_grant_on_a_file_tool_is_the_path(no_grants):
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW_SESSION))
    bind(responder)

    assert await gate.enforce("write_file", {"path": "notes/a.md", "content": "one"}) is None
    assert await gate.enforce("write_file", {"path": "notes/a.md", "content": "two"}) is None
    assert await gate.enforce("write_file", {"path": "./notes/a.md", "content": "three"}) is None
    assert len(responder.calls) == 1
    assert await gate.enforce("write_file", {"path": "notes/b.md", "content": "one"}) is None
    assert len(responder.calls) == 2


@pytest.mark.asyncio
async def test_allow_always_writes_the_confirmed_pattern_and_grants_the_session(no_grants, monkeypatch):
    from raven.permissions import gate as gate_module

    written: list[str] = []
    monkeypatch.setattr(gate_module, "allow_exec_pattern", lambda pattern: written.append(pattern) or True)
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW_ALWAYS, pattern="git push *"))
    bind(responder)

    assert await gate.enforce("exec", {"command": "git push origin HEAD"}) is None
    assert written == ["git push *"]
    # Config is read live and the rule holds from the next call; until the
    # reload lands, the session grant covers the same action.
    assert await gate.enforce("exec", {"command": "git push origin HEAD"}) is None
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_allow_always_with_a_pattern_that_fails_validation_still_grants_but_writes_nothing(
    no_grants, monkeypatch
):
    from raven.permissions import gate as gate_module

    written: list[str] = []
    monkeypatch.setattr(gate_module, "allow_exec_pattern", lambda pattern: written.append(pattern) or True)
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW_ALWAYS, pattern="sudo *"))
    bind(responder)

    assert await gate.enforce("exec", {"command": "sudo git push"}) is None
    assert written == []
    assert await gate.enforce("exec", {"command": "sudo git push"}) is None
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_the_suggestion_reaches_the_responder_only_when_one_segment_asks(no_grants):
    gate = gate_for(PermissionsConfig())
    responder = Responder(ApprovalOutcome(ApprovalChoice.ALLOW))
    bind(responder)

    await gate.enforce("exec", {"command": "git fetch origin && git rebase origin/main"})
    await gate.enforce("exec", {"command": "uv run pytest"})
    await gate.enforce("exec", {"command": "echo $(id)"})
    await gate.enforce("exec", {"command": "glab mr list --state opened | head -5"})
    assert [c["suggested_pattern"] for c in responder.calls] == ["", "", "", "glab mr list *"]


def test_session_keys_carry_the_whole_call_unless_the_tool_is_a_known_file_writer(monkeypatch):
    from raven.permissions import builtin as builtin_module
    from raven.permissions.builtin import session_keys

    # An unknown tool with a `path` field: its other fields are part of the action.
    staging = session_keys("mcp_upload", {"path": "report.csv", "destination": "staging"})
    production = session_keys("mcp_upload", {"path": "report.csv", "destination": "production"})
    assert staging != production

    # A builtin writer: the path is the identity, the content is not ...
    assert session_keys("write_file", {"path": "a.md", "content": "one"}) == session_keys(
        "write_file", {"path": "./a.md", "content": "two"}
    )
    # ... and a rebound directory makes the same relative path another file.
    before = session_keys("write_file", {"path": "a.md", "content": "one"})
    monkeypatch.setattr(builtin_module, "_bound_workdir", lambda: "/elsewhere")
    assert session_keys("write_file", {"path": "a.md", "content": "one"}) != before


@pytest.mark.asyncio
async def test_a_refused_config_write_is_not_reported_as_persisted(no_grants, monkeypatch):
    from raven.permissions import gate as gate_module

    def refuse(pattern: str) -> bool:
        raise ValueError("permissions.tools.exec is 'ask', a tier for the whole tool, not a table of patterns")

    recorded: list[dict] = []
    monkeypatch.setattr(gate_module, "allow_exec_pattern", refuse)
    monkeypatch.setattr(PermissionGate, "_annotate", staticmethod(lambda attrs: recorded.append(attrs)))
    gate = gate_for(PermissionsConfig())
    bind(Responder(ApprovalOutcome(ApprovalChoice.ALLOW_ALWAYS, pattern="git push *")))

    assert await gate.enforce("exec", {"command": "git push origin HEAD"}) is None
    persisted = [a for a in recorded if "permission.persisted" in a]
    assert persisted and persisted[-1]["permission.persisted"] is False
    assert "not a table" in persisted[-1]["permission.persist.refused"]
