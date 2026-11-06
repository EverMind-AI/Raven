"""The on-call's approval gate: three layers read against the owner's mode.

The gate exists because the trunk gate refused the on-call's own wake call in
a turn nobody attended (a reviewer timed out on a busy provider), and the
campaign stopped waking. These pin that the on-call's bookkeeping never waits
on anyone, that the owner's mode decides the rest, and that handing a call to
the owner files it, asks once for every pending item, and lets the approved
call through when it is sent again.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import approval  # noqa: E402
from oncall_flow.approval import (  # noqa: E402
    APPROVE_ALL,
    LAYER_ONE,
    MAX_ACTION_CHARS,
    REFUSE_ALL,
    ApprovalStore,
    ApprovalStoreUnreadableError,
    OncallApprovalGate,
    apply_answer,
    make_approval_gate,
    pending_block,
    read_answer,
)
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsAskOwnerTool  # noqa: E402

from raven.contracts.plugin_surface import ServiceLocator  # noqa: E402
from raven.plugins.context import PluginContext, RuntimeHandles  # noqa: E402

SUBMIT = {"campaign": "c", "trials": [{"seed": 1}]}
REMOTE_WRITE = {"command": "rm -rf /root/workspace/run/old", "machine": "conn_cpu_64106"}


class _Call:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments


class _Reply:
    def __init__(self, tool_calls: list[_Call]) -> None:
        self.tool_calls = tool_calls


class _Provider:
    """Answers every call with one scripted tool call, or hangs when told to."""

    def __init__(self, name: str = "", arguments: dict[str, Any] | None = None, *, hang: bool = False) -> None:
        self._reply = _Reply([_Call(name, arguments or {})]) if name else _Reply([])
        self._hang = hang
        self.calls = 0

    async def chat_with_retry(self, **_: Any) -> _Reply:
        self.calls += 1
        if self._hang:
            await asyncio.sleep(10)
        return self._reply


def _gate(tmp_path: Path, mode: str, provider: Any = None, timeout: float = 5.0) -> OncallApprovalGate:
    return OncallApprovalGate(home=tmp_path, mode=mode, provider=provider, review_timeout_s=timeout)


async def _ask(gate: OncallApprovalGate, name: str, params: dict[str, Any]) -> str | None:
    return await gate.adjudicate(name, params, session_workdir=None)


# --- layer one: the on-call's own tools never wait ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "smart", "ask"])
async def test_the_oncalls_own_tools_pass_in_every_mode(tmp_path, mode):
    provider = _Provider(hang=True)
    gate = _gate(tmp_path, mode, provider, timeout=0.01)
    for name in sorted(LAYER_ONE):
        assert await _ask(gate, name, {"campaign": "c"}) is None, name
    assert provider.calls == 0, "the reviewer is never consulted for the on-call's own bookkeeping"
    assert ApprovalStore(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_a_trunk_default_read_passes_even_in_ask_mode(tmp_path):
    assert await _ask(_gate(tmp_path, "ask"), "read_file", {"path": "/tmp/x"}) is None


def test_scheduling_the_next_wake_is_layer_one():
    assert "ops_check_later" in LAYER_ONE and "ops_ask_owner" in LAYER_ONE
    assert "ops_submit" not in LAYER_ONE and "ops_kill" not in LAYER_ONE


@pytest.mark.asyncio
async def test_a_declaration_is_approved_like_work_because_its_readings_run_on_the_machine(tmp_path):
    assert "ops_declare" not in LAYER_ONE
    declare = {"campaign": "c", "readings": [{"name": "x", "command": "python3 -c \"open('m','w')\""}]}
    out = await _ask(_gate(tmp_path, "ask"), "ops_declare", declare)
    assert out is not None and out.startswith("NOT RUN")
    assert ApprovalStore(tmp_path).pending()[0]["tool"] == "ops_declare"


@pytest.mark.asyncio
async def test_a_pending_declaration_shows_every_reading_and_action_command(tmp_path):
    readings = [{"name": f"r{i}", "command": f"echo reading-{i}", "when": "each_wake"} for i in range(5)]
    readings.append({"name": "w", "command": "python3 -c \"open('/tmp/x','w').write('x')\"", "when": "at_declare"})
    declare = {
        "campaign": "production-health-watch",
        "condition": "Keep error rate below threshold",
        "connection": "conn_prod",
        "objective": "error rate",
        "objective_kind": "watch",
        "command": "echo would-alert",
        "actions": [{"name": "page", "command": "notify --team ops", "repeat": "harmful"}],
        "readings": readings,
    }
    await _ask(_gate(tmp_path, "ask"), "ops_declare", declare)
    shown = pending_block(ApprovalStore(tmp_path).pending())
    for reading in readings:
        assert reading["command"] in shown and f"when={reading['when']!r}" in shown
    assert "notify --team ops" in shown and "echo would-alert" in shown and "conn_prod" in shown
    assert "more" not in shown


# --- layer two follows the owner's mode ---------------------------------------


@pytest.mark.asyncio
async def test_full_passes_work_without_review(tmp_path):
    provider = _Provider(hang=True)
    gate = _gate(tmp_path, "full", provider)
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    assert await _ask(gate, "exec", REMOTE_WRITE) is None
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_ask_files_the_call_and_says_how_to_carry_on(tmp_path):
    out = await _ask(_gate(tmp_path, "ask"), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN")
    assert "ops_ask_owner" in out and "pending item 1" in out and "ops_check_later" in out
    pending = ApprovalStore(tmp_path).pending()
    assert [item["n"] for item in pending] == [1] and pending[0]["tool"] == "ops_submit"


@pytest.mark.asyncio
async def test_the_same_call_sent_twice_is_filed_once(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "exec", REMOTE_WRITE)
    await _ask(gate, "exec", REMOTE_WRITE)
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_smart_runs_what_the_reviewer_allows(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "allow", "reason": "ordinary"})
    assert await _ask(_gate(tmp_path, "smart", provider), "ops_submit", SUBMIT) is None
    assert ApprovalStore(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_smart_hands_an_escalation_to_the_owner(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "escalate", "reason": "deletes data"})
    out = await _ask(_gate(tmp_path, "smart", provider), "exec", REMOTE_WRITE)
    assert out is not None and "deletes data" in out
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_a_reviewer_that_times_out_hands_the_call_to_the_owner_instead_of_refusing_it(tmp_path):
    out = await _ask(_gate(tmp_path, "smart", _Provider(hang=True), timeout=0.01), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN") and "timed out" in out
    assert len(ApprovalStore(tmp_path).pending()) == 1


@pytest.mark.asyncio
async def test_an_already_pending_call_is_not_reviewed_again(tmp_path):
    provider = _Provider("report_permission_review", {"decision": "escalate", "reason": "x"})
    gate = _gate(tmp_path, "smart", provider)
    await _ask(gate, "ops_submit", SUBMIT)
    await _ask(gate, "ops_submit", SUBMIT)
    assert provider.calls == 1


# --- the owner's answer --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_call_runs_when_it_is_sent_again(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "exec", REMOTE_WRITE)
    approved, refused = await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert (approved, refused) == ([1], [])
    assert await _ask(gate, "exec", REMOTE_WRITE) is None
    other = dict(REMOTE_WRITE, command="rm -rf /root/workspace/run/new")
    assert (await _ask(gate, "exec", other) or "").startswith("NOT RUN"), "an approval covers that exact call"


@pytest.mark.asyncio
async def test_a_refused_call_is_refused_with_the_owners_words(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    await apply_answer(tmp_path, REFUSE_ALL, [1])
    out = await _ask(gate, "ops_submit", SUBMIT)
    assert out is not None and out.startswith("Error: the owner did not approve") and REFUSE_ALL in out


@pytest.mark.asyncio
async def test_a_reply_in_the_owners_own_words_is_read_by_the_model(tmp_path):
    store = ApprovalStore(tmp_path)
    store.file("d1", "exec", "a", "ask")
    store.file("d2", "exec", "b", "ask")
    provider = _Provider("report_owner_decision", {"approve": [1], "refuse": [2]})
    assert await read_answer("first one yes, not the second", store.pending(), provider) == ({1}, {2}, True)


@pytest.mark.asyncio
async def test_a_reply_nobody_can_read_leaves_everything_pending(tmp_path):
    store = ApprovalStore(tmp_path)
    store.file("d1", "exec", "a", "ask")
    assert await read_answer("hmm, let me think", store.pending(), None) == (set(), set(), False)
    assert await read_answer("hmm", store.pending(), _Provider()) == (set(), set(), False)
    assert ApprovalStore(tmp_path).pending()[0]["n"] == 1


@pytest.mark.asyncio
async def test_a_free_text_reply_without_a_model_is_kept_and_read_by_the_gate(tmp_path):
    gate = _gate(tmp_path, "ask", _Provider("report_owner_decision", {"approve": [1], "refuse": []}))
    await _ask(gate, "ops_submit", SUBMIT)
    assert await apply_answer(tmp_path, "go ahead", [1]) == ([], [])
    assert ApprovalStore(tmp_path).pending()[0]["answer"] == "go ahead"
    assert await _ask(gate, "ops_submit", SUBMIT) is None


# --- one question carries every pending item -----------------------------------


class _Owner:
    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answers: list[asyncio.Future] = []

    async def __call__(self, prompt, choices, conversation_id, timeout_s):
        answer = asyncio.get_running_loop().create_future()
        self.asked.append({"prompt": prompt, "choices": choices})
        self.answers.append(answer)
        try:
            return await answer
        except asyncio.CancelledError:
            return ""


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "camp"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"host": "h", "port": 1, "key": "k", "start_hour": 9}))
    return cdir


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_every_pending_item_rides_one_question_and_the_answer_settles_them(tmp_path):
    home = tmp_path / "ops"
    tools_base.set_home(home)
    gate = _gate(home, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    await _ask(gate, "exec", REMOTE_WRITE)
    owner = _Owner()
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    cdir = _campaign(tmp_path)

    out = await tool.execute(
        campaign="c",
        question="Two steps need you.",
        expected_loss_minutes=60,
        ledger=str(cdir / "ledger.json"),
    )
    await _settle()

    assert "put to the owner as a question" in out
    prompt = owner.asked[0]["prompt"]
    assert "2 action(s) are waiting for your approval" in prompt and "  1. " in prompt and "  2. " in prompt
    assert owner.asked[0]["choices"] == [APPROVE_ALL, REFUSE_ALL]

    owner.answers[0].set_result(APPROVE_ALL)
    await _settle()
    assert ApprovalStore(home).pending() == []
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    assert await _ask(gate, "exec", REMOTE_WRITE) is None


@pytest.mark.asyncio
async def test_a_question_with_nothing_pending_is_unchanged(tmp_path):
    tools_base.set_home(tmp_path / "ops")
    owner = _Owner()
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    cdir = _campaign(tmp_path)
    await tool.execute(campaign="c", question="Which seed?", expected_loss_minutes=60, ledger=str(cdir / "ledger.json"))
    await _settle()
    assert "waiting for your approval" not in owner.asked[0]["prompt"]
    assert owner.asked[0]["choices"] is None or owner.asked[0]["choices"] == []


# --- the factory ---------------------------------------------------------------


def _ctx(tmp_path: Path, config: dict) -> PluginContext:
    services = ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a")
    return PluginContext(config=config, services=services)


def test_the_gate_declines_without_a_rendered_policy(tmp_path):
    assert make_approval_gate(_ctx(tmp_path, {"enabled": True})) is None
    assert make_approval_gate(_ctx(tmp_path, {"enabled": False, "approval": {"mode": "ask"}})) is None


def test_the_gate_takes_the_rendered_mode(tmp_path):
    gate = make_approval_gate(_ctx(tmp_path, {"enabled": True, "approval": {"mode": "ask"}}))
    assert isinstance(gate, OncallApprovalGate) and gate.mode == "ask"
    assert gate.name == approval.OncallApprovalGate.name == "oncall_approval"


# --- review findings: scope, a reply that failed to read, the file on disk ------


@pytest.mark.asyncio
async def test_an_approval_covers_the_directory_it_was_given_for(tmp_path):
    gate = _gate(tmp_path, "ask")
    here = {"command": "rm -rf build", "working_dir": "/srv/project-a"}
    there = {"command": "rm -rf build", "working_dir": "/srv/project-b"}
    await _ask(gate, "exec", here)
    await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert await _ask(gate, "exec", here) is None
    assert (await _ask(gate, "exec", there) or "").startswith("NOT RUN")


@pytest.mark.asyncio
async def test_a_relative_command_is_scoped_to_the_turns_directory(tmp_path):
    gate = _gate(tmp_path, "ask")
    call = {"command": "rm -rf build"}
    await gate.adjudicate("exec", call, session_workdir=Path("/srv/a"))
    await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert await gate.adjudicate("exec", call, session_workdir=Path("/srv/a")) is None
    assert (await gate.adjudicate("exec", call, session_workdir=Path("/srv/b")) or "").startswith("NOT RUN")
    assert f"[in {Path('/srv/b')}]" in ApprovalStore(tmp_path).pending()[0]["action"]


class _Flaky:
    """Fails the first reply-reading call, then answers like a working model."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **_: Any) -> _Reply:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("HTTP 503: Service is too busy")
        return _Reply([_Call("report_owner_decision", {"approve": [1], "refuse": []})])


@pytest.mark.asyncio
async def test_a_reply_that_failed_to_read_is_kept_and_read_again(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "_RETRY_READ_AFTER_S", 0)
    flaky = _Flaky()
    gate = _gate(tmp_path, "ask", flaky)
    await _ask(gate, "ops_submit", SUBMIT)
    assert await apply_answer(tmp_path, "yes, go ahead", [1], flaky) == ([], [])
    assert ApprovalStore(tmp_path).pending()[0]["answer"] == "yes, go ahead"
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    assert flaky.calls == 2


@pytest.mark.asyncio
async def test_a_failed_read_waits_before_trying_again(tmp_path):
    class _Down:
        calls = 0

        async def chat_with_retry(self, **_: Any) -> _Reply:
            _Down.calls += 1
            raise RuntimeError("down")

    gate = _gate(tmp_path, "ask", _Down())
    await _ask(gate, "ops_submit", SUBMIT)
    ApprovalStore(tmp_path).record_answer([1], "fine by me")
    for _ in range(3):
        await _ask(gate, "ops_submit", SUBMIT)
    assert _Down.calls == 1
    assert ApprovalStore(tmp_path).pending()[0]["answer"] == "fine by me"


@pytest.mark.asyncio
async def test_an_unreadable_file_is_never_overwritten_and_the_call_does_not_run(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text("{ half a file", encoding="utf-8")
    out = await _ask(_gate(tmp_path, "ask"), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN") and "could not be read" in out
    assert path.read_text(encoding="utf-8") == "{ half a file"
    with pytest.raises(ApprovalStoreUnreadableError):
        ApprovalStore(tmp_path).pending()


@pytest.mark.asyncio
async def test_a_replace_the_platform_refuses_briefly_is_retried(tmp_path, monkeypatch):
    import raven.utils.atomic_io as atomic_io

    real = atomic_io.atomic_replace
    refusals = {"left": 2}

    def held(path, data, **kw):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real(path, data, **kw)

    monkeypatch.setattr(atomic_io, "atomic_replace", held)
    monkeypatch.setattr(approval, "_PAUSE_S", 0)
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    await _ask(gate, "exec", REMOTE_WRITE)
    assert [item["n"] for item in ApprovalStore(tmp_path).pending()] == [1, 2]
    assert refusals["left"] == 0


# --- second review: what the owner sees, how long it lasts, failing closed ----


@pytest.mark.asyncio
async def test_the_owner_sees_the_whole_call_with_where_it_runs_first(tmp_path):
    command = "cat /var/log/app/" + "a" * 290 + ".log; rm -rf /srv/data"
    await _ask(_gate(tmp_path, "ask"), "exec", {"command": command, "machine": "conn_prod"})
    shown = approval.pending_block(ApprovalStore(tmp_path).pending())
    assert "[on conn_prod]" in shown and "rm -rf /srv/data" in shown
    assert shown.index("[on conn_prod]") < shown.index("cat /var/log")


@pytest.mark.asyncio
async def test_a_call_too_long_to_show_whole_is_not_filed(tmp_path):
    out = await _ask(
        _gate(tmp_path, "ask"), "exec", {"command": "echo " + "x" * MAX_ACTION_CHARS, "machine": "conn_prod"}
    )
    assert out is not None and "too long to show them whole" in out
    assert ApprovalStore(tmp_path).pending() == []


@pytest.mark.asyncio
async def test_an_approval_ends_with_the_campaign_it_was_asked_under(tmp_path):
    cdir = tmp_path / "campaign-a"
    cdir.mkdir()
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "ops_submit", SUBMIT)
    ApprovalStore(tmp_path).scope([1], str(cdir))
    await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert await _ask(gate, "ops_submit", SUBMIT) is None
    (cdir / "concluded.json").write_text("{}", encoding="utf-8")
    assert (await _ask(gate, "ops_submit", SUBMIT) or "").startswith("NOT RUN")


@pytest.mark.asyncio
async def test_two_spellings_of_one_directory_are_one_approval(tmp_path):
    gate = _gate(tmp_path, "ask")
    await _ask(gate, "exec", {"command": "make", "working_dir": "/srv/a/"})
    await apply_answer(tmp_path, APPROVE_ALL, [1])
    assert await _ask(gate, "exec", {"command": "make", "working_dir": "/srv/./a"}) is None


@pytest.mark.asyncio
async def test_a_store_the_platform_keeps_refusing_answers_not_run(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError(13, "held by another process")

    monkeypatch.setattr(ApprovalStore, "file", refuse)
    out = await _ask(_gate(tmp_path, "ask"), "ops_submit", SUBMIT)
    assert out is not None and out.startswith("NOT RUN") and "could not be read" in out


def test_a_misspelt_review_timeout_still_builds_the_gate(tmp_path):
    assert approval.review_timeout("60s") == approval.DEFAULT_REVIEW_TIMEOUT_S
    assert approval.review_timeout(-3) == approval.DEFAULT_REVIEW_TIMEOUT_S
    assert approval.review_timeout("15") == 15.0
    gate = make_approval_gate(
        _ctx(tmp_path, {"enabled": True, "approval": {"mode": "ask", "reviewTimeoutSeconds": "60s"}})
    )
    assert isinstance(gate, OncallApprovalGate)


@pytest.mark.asyncio
async def test_the_approval_choices_ride_alongside_the_models_own_options(tmp_path):
    home = tmp_path / "ops"
    tools_base.set_home(home)
    await _ask(_gate(home, "ask"), "ops_submit", SUBMIT)
    owner = _Owner()
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    cdir = _campaign(tmp_path)
    await tool.execute(
        campaign="c",
        question="Which seed?",
        expected_loss_minutes=60,
        options=["seed 1", "seed 2"],
        ledger=str(cdir / "ledger.json"),
    )
    await _settle()
    assert owner.asked[0]["choices"] == ["seed 1", "seed 2", APPROVE_ALL, REFUSE_ALL]
    assert ApprovalStore(home).pending()[0]["scope"] == str(cdir)
