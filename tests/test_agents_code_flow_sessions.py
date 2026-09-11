"""The code flow's session ledger and the concurrency notice it drives.

Parallel raven-code DAG nodes are sessions of ONE raven-code process on ONE
working directory (the host's connection pool keeps one process per agent
name and mints each node its own session). Nothing locks that directory any
more, so a session is told -- at the start of its turn, in its own prompt --
how many other sessions are working beside it, and only then: a lone session
gets no notice.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHookContext, HookDecision

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.flow import CodeFlowHook, make_session_forget  # noqa: E402
from code_flow.sessions import SessionLedger  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


async def _inbound(hook: CodeFlowHook, key: str, cwd: Path, text: str = "do the task") -> HookDecision:
    with workdir.bind(cwd):
        return await hook.before_user_inbound(AgentHookContext(session_key=key, inbound_content=text))


async def _sent(hook: CodeFlowHook, key: str, cwd: Path) -> HookDecision:
    with workdir.bind(cwd):
        return await hook.after_send(AgentHookContext(session_key=key, outbound_content="done", metadata={}))


@pytest.mark.asyncio
async def test_a_lone_session_gets_no_notice(tmp_path):
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    decision = await _inbound(hook, "s1", tmp_path)
    assert decision.modified_content is None


@pytest.mark.asyncio
async def test_a_second_session_on_the_same_directory_is_told_about_the_first(tmp_path):
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    await _inbound(hook, "s1", tmp_path)
    decision = await _inbound(hook, "s2", tmp_path, "fix the parser")
    assert decision.modified_content is not None
    assert decision.modified_content.startswith("# Workspace concurrency")
    assert "1 other Raven-Code session" in decision.modified_content
    assert decision.modified_content.endswith("fix the parser"), "the task itself stays intact, after the notice"


@pytest.mark.asyncio
async def test_the_notice_counts_every_other_session_in_flight(tmp_path):
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    await _inbound(hook, "s1", tmp_path)
    await _inbound(hook, "s2", tmp_path)
    decision = await _inbound(hook, "s3", tmp_path)
    assert "2 other Raven-Code sessions" in decision.modified_content


@pytest.mark.asyncio
async def test_sessions_on_different_directories_do_not_hear_about_each_other(tmp_path):
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    await _inbound(hook, "s1", tmp_path / "one")
    decision = await _inbound(hook, "s2", tmp_path / "two")
    assert decision.modified_content is None


@pytest.mark.asyncio
async def test_a_session_that_finished_its_turn_no_longer_counts(tmp_path):
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    await _inbound(hook, "s1", tmp_path)
    await _sent(hook, "s1", tmp_path)
    decision = await _inbound(hook, "s2", tmp_path)
    assert decision.modified_content is None


@pytest.mark.asyncio
async def test_a_session_that_never_reported_back_expires_after_the_ttl(tmp_path):
    """A failed or cancelled turn fires no after_send, so its in-flight mark
    would otherwise stick forever and every later session would be warned
    about a ghost."""
    clock = _Clock()
    hook = CodeFlowHook(ledger=SessionLedger(clock=clock, ttl_s=60.0))
    await _inbound(hook, "s1", tmp_path)
    clock.now += 61.0
    decision = await _inbound(hook, "s2", tmp_path)
    assert decision.modified_content is None


@pytest.mark.asyncio
async def test_a_deleted_session_is_forgotten(tmp_path):
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", tmp_path)
    make_session_forget(ledger).on_session_deleted("s1", True)
    decision = await _inbound(hook, "s2", tmp_path)
    assert decision.modified_content is None
    assert ledger.record("s1") is None


@pytest.mark.asyncio
async def test_the_notice_does_not_need_a_git_repository(tmp_path):
    """Nothing here reads git to decide: two sessions in a plain directory
    are as concurrent as two in a checkout."""
    hook = CodeFlowHook(ledger=SessionLedger(clock=_Clock()))
    await _inbound(hook, "s1", tmp_path)
    decision = await _inbound(hook, "s2", tmp_path)
    assert decision.modified_content is not None


@pytest.mark.asyncio
async def test_the_ledger_pins_a_sessions_base_commit_at_its_first_turn(tmp_path):
    """The base a later workspace report measures against: HEAD when the
    session first spoke, kept across its turns, None outside git."""
    repo = tmp_path / "repo"
    head = make_repo(repo)
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", repo)
    assert ledger.record("s1").base_commit == head
    (repo / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "more"], check=True)
    await _sent(hook, "s1", repo)
    await _inbound(hook, "s1", repo)
    assert ledger.record("s1").base_commit == head, "the base does not move with the session's own commits"
    await _inbound(hook, "plain", tmp_path / "plain")
    assert ledger.record("plain").base_commit is None


# --------------------------------------------------------------------------- #
# who shared a directory, and a long turn that keeps iterating                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sessions_that_coexist_on_a_directory_name_each_other_as_peers_for_life(tmp_path):
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", tmp_path)
    assert ledger.record("s1").shared is False
    await _inbound(hook, "s2", tmp_path)
    assert ledger.record("s1").peers == {"s2"} and ledger.record("s2").peers == {"s1"}
    await _sent(hook, "s1", tmp_path)
    await _sent(hook, "s2", tmp_path)
    assert ledger.record("s1").shared and ledger.record("s2").shared, "finishing a turn does not unshare a tree"


@pytest.mark.asyncio
async def test_sessions_on_different_directories_are_not_peers(tmp_path):
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", tmp_path / "a")
    await _inbound(hook, "s2", tmp_path / "b")
    assert ledger.record("s1").shared is False and ledger.record("s2").shared is False


@pytest.mark.asyncio
async def test_a_session_arriving_after_its_predecessor_was_deleted_is_not_shared(tmp_path):
    """A deleted session left the ledger, and its commits sit before the
    newcomer's base anyway, so the newcomer's report stays its own."""
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", tmp_path)
    await _sent(hook, "s1", tmp_path)
    make_session_forget(ledger).on_session_deleted("s1", True)
    await _inbound(hook, "s2", tmp_path)
    assert ledger.record("s2").shared is False


@pytest.mark.asyncio
async def test_a_predecessor_still_on_the_ledger_counts_as_having_shared(tmp_path):
    """Records outlive turns: a session that finished a turn but was not
    deleted may speak again, so the newcomer is shared with it."""
    ledger = SessionLedger(clock=_Clock())
    hook = CodeFlowHook(ledger=ledger)
    await _inbound(hook, "s1", tmp_path)
    await _sent(hook, "s1", tmp_path)
    await _inbound(hook, "s2", tmp_path)
    assert ledger.record("s2").peers == {"s1"}


@pytest.mark.asyncio
async def test_a_long_turn_that_keeps_iterating_stays_counted_past_the_ttl(tmp_path):
    """The ttl is for a turn that stopped reporting. One that is still
    iterating refreshes its mark at every iteration, so a session working for
    longer than the ttl is still announced to a newcomer."""
    clock = _Clock()
    hook = CodeFlowHook(ledger=SessionLedger(clock=clock, ttl_s=60.0))
    await _inbound(hook, "s1", tmp_path)
    clock.now += 50.0
    await hook.before_iteration(AgentHookContext(session_key="s1"))
    clock.now += 50.0
    decision = await _inbound(hook, "s2", tmp_path)
    assert decision.modified_content is not None, "100s after its start the still-iterating s1 was forgotten"


@pytest.mark.asyncio
async def test_an_iteration_of_an_unknown_or_finished_session_refreshes_nothing(tmp_path):
    clock = _Clock()
    ledger = SessionLedger(clock=clock, ttl_s=60.0)
    hook = CodeFlowHook(ledger=ledger)
    await hook.before_iteration(AgentHookContext(session_key="ghost"))
    assert ledger.record("ghost") is None
    await _inbound(hook, "s1", tmp_path)
    await _sent(hook, "s1", tmp_path)
    clock.now += 10.0
    await hook.before_iteration(AgentHookContext(session_key="s1"))
    assert ledger.record("s1").in_flight is False
