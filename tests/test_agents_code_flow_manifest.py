"""The workspace report raven-code files for its host: the harness manifest.

Every field is machine-read from the working directory -- git facts measured
against the base the session ledger pinned at the session's first turn --
and nothing comes from model prose. The report rides the turn's observer
stash under ``acp_meta`` (the loop_hooks paper) and reaches the ACP client
as the prompt response's ``_meta["raven.harnessManifest"]``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from raven.agent import workdir
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, HostWiring, ToolWiring, TurnPolicy
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.contracts.loop_hooks import AgentHookContext
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.flow import MANIFEST_META_KEY, CodeFlowHook  # noqa: E402
from code_flow.manifest import MANIFEST_FIELDS, build_manifest  # noqa: E402
from code_flow.sessions import SessionLedger  # noqa: E402


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "a.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return git(path, "rev-parse", "HEAD")


def test_the_report_has_exactly_the_v1_fields(tmp_path):
    base = make_repo(tmp_path / "repo")
    report = build_manifest("acp:s1", tmp_path / "repo", base)
    assert set(report) == set(MANIFEST_FIELDS)
    assert report["manifestVersion"] == 1
    assert report["sessionId"] == "acp:s1"
    assert report["scope"] == "workspace"
    assert (report["attribution"], report["sharedWith"]) == ("session", 0)


def test_a_clean_checkout_with_nothing_past_base_reports_no_changes(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    report = build_manifest("s", repo, base)
    assert report["workspaceKind"] == "repository"
    assert report["repositoryRoot"] == str(repo.resolve())
    assert report["workdir"] == str(repo)
    assert report["branch"] == "main"
    assert report["baseCommit"] == base and report["head"] == base
    assert report["commitsPastBase"] == {"count": 0, "items": []}
    assert report["workingTree"] == {"clean": True, "uncommittedCount": 0, "entries": []}
    assert report["status"] == "no_changes"
    assert report["readyForIntegration"] is False
    assert report["blockers"] == ["no commits past base"]


def test_uncommitted_work_reports_needs_commit(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.txt").write_text("n\n")
    report = build_manifest("s", repo, base)
    assert report["workingTree"]["clean"] is False
    assert report["workingTree"]["uncommittedCount"] == 2
    assert sorted(report["workingTree"]["entries"]) == [" M a.py", "?? new.txt"]
    assert report["status"] == "needs_commit"
    assert report["readyForIntegration"] is False
    assert report["blockers"] == ["uncommitted changes must be committed"]


def test_committed_work_past_base_reports_ready_for_integration(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add b")
    head = git(repo, "rev-parse", "HEAD")
    report = build_manifest("s", repo, base)
    assert report["head"] == head and report["baseCommit"] == base
    assert report["commitsPastBase"]["count"] == 1
    assert report["commitsPastBase"]["items"] == [{"sha": head, "subject": "add b"}]
    assert "1 file changed" in report["diffStat"]
    assert report["status"] == "ready_for_integration"
    assert report["readyForIntegration"] is True
    assert report["blockers"] == []


@pytest.fixture
def outside_any_repo(tmp_path, monkeypatch):
    """A plain directory is only plain if git stops looking upward at it: the
    test tree may itself sit inside some checkout."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path


def test_a_plain_directory_is_reported_as_such(outside_any_repo):
    plain = outside_any_repo / "plain"
    plain.mkdir()
    report = build_manifest("s", plain, None)
    assert report["workspaceKind"] == "plain"
    assert report["repositoryRoot"] is None
    assert report["workdir"] == str(plain)
    assert report["baseCommit"] is None and report["head"] is None and report["branch"] is None
    assert report["status"] == "unknown"
    assert report["readyForIntegration"] is False
    assert report["blockers"] == ["not a git repository"]


def test_an_unreadable_directory_degrades_to_unknown_never_to_ready(tmp_path):
    report = build_manifest("s", tmp_path / "does-not-exist", "abc")
    assert report["status"] == "unknown"
    assert report["readyForIntegration"] is False
    assert report["blockers"], "a failure names itself"


def test_a_repository_without_a_pinned_base_cannot_be_ready(tmp_path):
    """No base means no measure of what this session added: known facts are
    reported, readiness is not claimed."""
    repo = tmp_path / "repo"
    head = make_repo(repo)
    report = build_manifest("s", repo, None)
    assert report["head"] == head and report["baseCommit"] is None
    assert report["commitsPastBase"] == {"count": 0, "items": []}
    assert report["status"] == "unknown"
    assert report["readyForIntegration"] is False
    assert "base commit" in report["blockers"][0]


def test_long_listings_are_capped_with_the_count_kept_whole(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    for n in range(105):
        (repo / f"f{n:03}.txt").write_text("x\n")
    report = build_manifest("s", repo, base)
    assert report["workingTree"]["uncommittedCount"] == 105
    assert len(report["workingTree"]["entries"]) == 100


@pytest.mark.asyncio
async def test_the_send_files_the_report_under_the_acp_meta_stash(tmp_path):
    """Measured against the base the ledger pinned at the first turn: a
    commit made during the turn shows as work past base."""
    repo = tmp_path / "repo"
    base = make_repo(repo)
    ledger = SessionLedger()
    hook = CodeFlowHook(ledger=ledger)
    with workdir.bind(repo):
        await hook.before_user_inbound(AgentHookContext(session_key="acp:s1", inbound_content="add b"))
        (repo / "b.py").write_text("y = 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "add b")
        metadata: dict = {}
        await hook.after_send(AgentHookContext(session_key="acp:s1", outbound_content="done", metadata=metadata))

    report = metadata["observers"]["acp_meta"][MANIFEST_META_KEY]
    assert MANIFEST_META_KEY == "raven.harnessManifest"
    assert report["sessionId"] == "acp:s1"
    assert report["workdir"] == str(repo)
    assert report["baseCommit"] == base
    assert report["commitsPastBase"]["count"] == 1
    assert report["status"] == "ready_for_integration"


@pytest.mark.asyncio
async def test_the_send_leaves_other_observers_stashes_alone(outside_any_repo):
    plain = outside_any_repo / "plain"
    plain.mkdir()
    hook = CodeFlowHook(ledger=SessionLedger())
    metadata = {"observers": {"oncall_flow": {"tokens": 3}, "acp_meta": {"vendor.other": {"k": 1}}}}
    with workdir.bind(plain):
        await hook.before_user_inbound(AgentHookContext(session_key="s", inbound_content="t"))
        await hook.after_send(AgentHookContext(session_key="s", outbound_content="d", metadata=metadata))
    assert metadata["observers"]["oncall_flow"] == {"tokens": 3}
    assert metadata["observers"]["acp_meta"]["vendor.other"] == {"k": 1}
    assert metadata["observers"]["acp_meta"][MANIFEST_META_KEY]["workspaceKind"] == "plain"


class _AnswerProvider(LLMProvider):
    """One plain answer, no tools."""

    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="added b.py", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_turn_through_the_trunk_loop_files_the_report_on_its_record(tmp_path):
    """End to end on the trunk loop: the hook rides the host wiring, the
    inbound phase books the session, the send files the report, and the
    loop stamps it onto the turn's last assistant message in the session
    record -- the very message the ACP layer reads back for ``_meta``."""
    repo = tmp_path / "repo"
    base = make_repo(repo)
    agent = AgentLoop(
        provider=_AnswerProvider(),
        workspace=tmp_path / "home",
        model="stub",
        policy=TurnPolicy(max_iterations=4),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never"))),
        host=HostWiring(hooks=[CodeFlowHook(ledger=SessionLedger())]),
    )
    with workdir.bind(repo):
        out = await agent._process_message(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="acp", chat_id="s1", sender_id="user", chat_type=ChatType.DM),
                text="add b.py",
            ),
            session_key="acp:s1",
        )
    assert out is not None and out[0] == "added b.py"

    record = agent.sessions.get_or_create("acp:s1")
    stamped = [m for m in record.messages if m.get("role") == "assistant" and "observers" in m]
    assert len(stamped) == 1
    report = stamped[-1]["observers"]["acp_meta"][MANIFEST_META_KEY]
    assert report["sessionId"] == "acp:s1"
    assert report["workdir"] == str(repo)
    assert report["baseCommit"] == base
    assert report["status"] == "no_changes", report["workingTree"]


# --------------------------------------------------------------------------- #
# attribution: HEAD is shared, so commits past base are one session's only    #
# while no other session shared the directory                                  #
# --------------------------------------------------------------------------- #


def test_commits_past_base_are_not_attributed_when_the_directory_was_shared(tmp_path):
    """Two sessions on one checkout, no worktree between them: A commits, B did
    nothing. ``base..HEAD`` holds A's commit for both. B's report must not
    claim it, and must not come out ready."""
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "A's commit")

    report = build_manifest("acp:B", repo, base, shared_with=1)

    assert report["attribution"] == "shared"
    assert report["sharedWith"] == 1
    assert report["commitsPastBase"]["count"] == 1, "the workspace fact is still reported"
    assert report["status"] == "shared_workspace"
    assert report["readyForIntegration"] is False
    assert "cannot be attributed" in report["blockers"][0]


def test_a_shared_report_still_names_uncommitted_work(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "c.py").write_text("z = 3\n")

    report = build_manifest("acp:B", repo, base, shared_with=2)

    assert report["status"] == "shared_workspace"
    assert "2 other Raven-Code sessions" in report["blockers"][0]
    assert report["blockers"][1] == "uncommitted changes must be committed"


def test_a_lone_session_is_attributed_the_work(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "own commit")

    report = build_manifest("acp:A", repo, base)

    assert (report["attribution"], report["sharedWith"]) == ("session", 0)
    assert report["status"] == "ready_for_integration"
    assert report["readyForIntegration"] is True


def test_an_unreadable_tree_stays_unknown_even_when_shared(tmp_path):
    """``unknown`` outranks ``shared_workspace``: a report that could not read
    git says so first, and is never ready either way."""
    plain = tmp_path / "plain"
    plain.mkdir()
    report = build_manifest("acp:B", plain, None, shared_with=1)
    assert report["attribution"] == "shared"
    assert report["readyForIntegration"] is False


@pytest.mark.asyncio
async def test_the_idle_session_does_not_claim_its_peers_commit(tmp_path):
    """Through the hook and the ledger: A and B start on one checkout, A
    commits during its turn, B's report at its send is shared and unready --
    and so is A's, because from A's side too the tree may hold B's work."""
    repo = tmp_path / "repo"
    make_repo(repo)
    hook = CodeFlowHook(ledger=SessionLedger())
    with workdir.bind(repo):
        await hook.before_user_inbound(AgentHookContext(session_key="acp:A", inbound_content="add b"))
        await hook.before_user_inbound(AgentHookContext(session_key="acp:B", inbound_content="look around"))
        (repo / "b.py").write_text("y = 2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "add b")
        meta_a: dict = {}
        await hook.after_send(AgentHookContext(session_key="acp:A", outbound_content="done", metadata=meta_a))
        meta_b: dict = {}
        await hook.after_send(AgentHookContext(session_key="acp:B", outbound_content="nothing", metadata=meta_b))

    report_b = meta_b["observers"]["acp_meta"][MANIFEST_META_KEY]
    assert report_b["commitsPastBase"]["count"] == 1
    assert report_b["status"] == "shared_workspace"
    assert report_b["readyForIntegration"] is False
    assert (report_b["attribution"], report_b["sharedWith"]) == ("shared", 1)
    report_a = meta_a["observers"]["acp_meta"][MANIFEST_META_KEY]
    assert report_a["status"] == "shared_workspace"
    assert report_a["readyForIntegration"] is False
