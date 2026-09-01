"""Workspace gate: classification, atomic allocation, and the primary-owner race.

Ported from the fork's tests/test_workspace_gate.py against the code-flow
plugin's gate on the trunk tool-gate seam: the gate is driven through its
``adjudicate`` face (the turn's workdir passed explicitly), and the
registry-driven case runs on the trunk's own ToolRegistry with the gate cast
at construction. Everything runs against throwaway git repos under tmp_path;
nothing touches a real checkout. The race test forks real processes because
flock is per-fd and the bug being guarded against - two instances both seeing
"clean, unowned" and both binding the primary checkout - only exists across
processes.
"""

import asyncio
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.gate import (  # noqa: E402
    CHOICE_READONLY,
    CHOICE_RETRY,
    CHOICE_WORKTREE,
    WorkspaceGate,
    _is_readonly_command,
    parse_choice,
    repo_identity,
    workspace_dirty,
)

# --- classification -----------------------------------------------------------

READONLY = [
    "ls -la",
    "cat a.py",
    "pwd",
    "rg foo src",
    "git status && git diff",
    "git log --oneline -5 | head",
    "grep -rn foo src ; wc -l a.py",
    "find . -name '*.py'",
    "find . -name a -o -name b",  # find's own -o is the OR operator, not output
    "sort file.txt",
    "env",
    "env cat a.py",
    "",
]
MUTATING = [
    "pip install requests",
    "git checkout -b x",
    "git commit -m x",
    "git reset --hard",
    "echo hi > f",
    "sed -i s/a/b/ f",
    "python setup.py build",
    "make",
    "rm -rf build",
    "ls $(touch pwned)",  # substitution can hide a write
    "cat a.py && pytest",  # one mutating segment poisons the command
    "npm install",
    # Escape hatches of otherwise-read-only heads: all writes.
    "find . -delete",
    "find . -exec rm {} \\;",
    "find . -ok rm {} \\;",
    "find . -fprintf out.txt %p",
    "sort -o file file",
    "sort --output=file file",
    "env touch file",
    "env VAR=x sh -c 'echo hi'",
    "git diff --output=file",
    "git show --output=file HEAD",
    "fd -x rm",
]


@pytest.mark.parametrize("cmd", READONLY)
def test_readonly_commands_pass(cmd):
    assert _is_readonly_command(cmd)


@pytest.mark.parametrize("cmd", MUTATING)
def test_mutating_commands_flagged(cmd):
    assert not _is_readonly_command(cmd)


def call(gate, name, params):
    """The gate is async (it may await the user); tests drive it to completion."""
    return asyncio.run(gate.adjudicate(name, params, session_workdir=None))


# --- fixtures -------------------------------------------------------------------


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def make_gate(repo: Path, tmp: Path, instance: str) -> WorkspaceGate:
    alloc = tmp / "instances" / instance
    alloc.mkdir(parents=True, exist_ok=True)
    return WorkspaceGate(repo, tmp / "repos", instance=instance, alloc_dir=alloc)


def alloc_of(gate: WorkspaceGate) -> dict:
    return json.loads((gate._alloc_dir / "allocation.json").read_text())


# --- allocation ---------------------------------------------------------------


def test_readonly_tools_never_allocate(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    gate = make_gate(repo, tmp_path, "instance-a")
    assert call(gate, "read_file", {"path": "a.py"}) is None
    assert call(gate, "exec", {"command": "git status"}) is None
    assert not (gate._alloc_dir / "allocation.json").exists()


def test_clean_unowned_binds_primary(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    gate = make_gate(repo, tmp_path, "instance-a")
    assert call(gate, "write_file", {"path": "b.py", "content": ""}) is None
    a = alloc_of(gate)
    assert a["state"] == "working" and a["mode"] == "primary"
    repo_id, _ = repo_identity(repo)
    owner = json.loads((tmp_path / "repos" / repo_id / "primary.json").read_text())
    assert owner["instance"] == "instance-a"
    # Second write in the same process: allowed from cache, no re-allocation.
    assert call(gate, "edit_file", {"path": "b.py"}) is None


def test_dirty_primary_asks(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("uncommitted\n")
    gate = make_gate(repo, tmp_path, "instance-a")
    blocked = call(gate, "write_file", {"path": "b.py"})
    assert blocked and "WRITE BLOCKED" in blocked
    a = alloc_of(gate)
    assert a["state"] == "awaiting_user"
    assert "uncommitted changes" in a["pending"]["question"]
    assert "HEAD" in a["pending"]["question"]
    assert a["pending"]["options"] == ["worktree", "readonly", "retry_later"]
    # The dirty file was not touched, stashed or committed.
    assert (repo / "wip.txt").read_text() == "uncommitted\n"
    assert workspace_dirty(repo) == 1


def test_occupied_asks_second_instance(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    first = make_gate(repo, tmp_path, "instance-a")
    assert call(first, "write_file", {"path": "b.py"}) is None
    second = make_gate(repo, tmp_path, "instance-b")
    blocked = call(second, "write_file", {"path": "c.py"})
    assert blocked and "WRITE BLOCKED" in blocked
    a = alloc_of(second)
    assert a["state"] == "awaiting_user"
    assert "instance-a" in a["pending"]["question"]


def test_same_instance_rebinds_after_crash(tmp_path):
    """allocation.json lost but primary.json says it's ours -> rebind, no ask."""
    repo = tmp_path / "repo"
    make_repo(repo)
    first = make_gate(repo, tmp_path, "instance-a")
    assert call(first, "write_file", {"path": "b.py"}) is None
    (first._alloc_dir / "allocation.json").unlink()
    (repo / "wip.txt").write_text("dirty now\n")  # even dirty: it's our own tree
    again = make_gate(repo, tmp_path, "instance-a")
    assert call(again, "write_file", {"path": "c.py"}) is None
    assert alloc_of(again)["mode"] == "primary"


def test_declined_stays_locked(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    gate = make_gate(repo, tmp_path, "instance-a")
    gate._alloc_dir.mkdir(parents=True, exist_ok=True)
    (gate._alloc_dir / "allocation.json").write_text(json.dumps({"state": "readonly_locked"}))
    blocked = call(gate, "write_file", {"path": "b.py"})
    assert blocked and "declined" in blocked


def test_non_git_binds_without_worktree_offer(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    first = make_gate(plain, tmp_path, "instance-a")
    assert call(first, "write_file", {"path": "b.txt"}) is None
    assert alloc_of(first)["repo"] is None
    second = make_gate(plain, tmp_path, "instance-b")
    blocked = call(second, "write_file", {"path": "c.txt"})
    assert blocked
    assert "worktree" not in alloc_of(second)["pending"]["options"]


def test_worktree_escape_guard(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    wt = tmp_path / "wt"
    gate = make_gate(repo, tmp_path, "instance-a")
    gate._alloc_dir.mkdir(parents=True, exist_ok=True)
    (gate._alloc_dir / "allocation.json").write_text(
        json.dumps(
            {
                "state": "working",
                "mode": "worktree",
                "boundWorkdir": str(wt),
                "repo": {"root": str(repo)},
            }
        )
    )
    assert call(gate, "write_file", {"path": str(repo / "a.py"), "content": ""})
    assert call(gate, "exec", {"command": f"git -C {repo} commit -am x"})
    assert call(gate, "exec", {"command": f"GIT_WORK_TREE={repo} git apply p.diff"})
    assert call(gate, "exec", {"command": f"cd {repo} && sed -i s/a/b/ a.py"})
    # Inside the worktree everything passes.
    assert call(gate, "write_file", {"path": str(wt / "a.py"), "content": ""}) is None
    assert call(gate, "exec", {"command": "pytest -q"}) is None


@pytest.mark.asyncio
async def test_worktree_escape_guard_checks_registry_canonical_parameters(tmp_path):
    """Driven the way the trunk registry drives it: the gate is cast at
    construction and adjudicates the validated parameters; a verdict replaces
    the call before the tool runs. The fork's PTY exec-session escapes
    (exec_write against a session opened in the primary) re-port with the
    exec-workbench wave; the turn-binding escape stands in for the session
    cwd here."""
    from raven.agent import workdir
    from raven.agent.tools.filesystem import EditFileTool, WriteFileTool
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.shell import ExecTool

    repo = tmp_path / "repo"
    make_repo(repo)
    wt = tmp_path / "wt"
    wt.mkdir()
    alloc = tmp_path / "instances" / "instance-b"
    alloc.mkdir(parents=True)
    (alloc / "allocation.json").write_text(
        json.dumps(
            {
                "state": "working",
                "mode": "worktree",
                "boundWorkdir": str(wt),
                "repo": {"root": str(repo)},
            }
        )
    )

    gate = WorkspaceGate(
        repo,
        tmp_path / "repos",
        instance="instance-b",
        alloc_dir=alloc,
    )
    registry = ToolRegistry(tool_gates=[gate])
    registry.register(WriteFileTool(workspace=wt))
    registry.register(EditFileTool(workspace=wt))
    registry.register(ExecTool(working_dir=str(wt), restrict_to_workspace=False))

    write_result = await registry.execute(
        "write_file",
        {"path": str(repo / "escaped.txt"), "content": "bad"},
    )
    relative_write_result = await registry.execute(
        "write_file",
        {
            "path": os.path.relpath(repo / "relative-escaped.txt", wt),
            "content": "bad",
        },
    )
    edit_result = await registry.execute(
        "edit_file",
        {
            "path": str(repo / "a.py"),
            "old_text": "x = 1",
            "new_text": "x = 2",
        },
    )
    exec_result = await registry.execute(
        "exec",
        {"command": "touch exec-escaped.txt", "working_dir": str(repo)},
    )
    direct_exec_result = await registry.execute(
        "exec",
        {"command": f"touch {repo / 'direct-escaped.txt'}"},
    )
    relative_exec_path = os.path.relpath(repo / "exec-relative-escaped.txt", wt)
    relative_exec_result = await registry.execute(
        "exec",
        {"command": f"touch {relative_exec_path}"},
    )
    # A mutating command issued while the turn's live binding still points
    # into the primary is refused the same way (the fork probed the exec
    # session's cwd; the trunk gate reads the explicit session workdir).
    with workdir.bind(repo):
        session_exec_result = await registry.execute(
            "exec",
            {"command": "touch session-escaped.txt"},
        )
    # Inside the worktree the registry path stays open.
    allowed = await registry.execute("exec", {"command": "pwd"})

    assert "WRITE BLOCKED" not in str(allowed)
    assert all(
        "WRITE BLOCKED" in str(result)
        for result in (
            write_result,
            relative_write_result,
            edit_result,
            exec_result,
            direct_exec_result,
            relative_exec_result,
            session_exec_result,
        )
    )
    assert not (repo / "escaped.txt").exists()
    assert not (repo / "relative-escaped.txt").exists()
    assert not (repo / "exec-escaped.txt").exists()
    assert not (repo / "direct-escaped.txt").exists()
    assert not (repo / "exec-relative-escaped.txt").exists()
    assert not (repo / "session-escaped.txt").exists()
    assert (repo / "a.py").read_text() == "x = 1\n"


# --- the race -------------------------------------------------------------------


def _race_worker(repo: str, tmp: str, instance: str, plugin_dir: str, q):
    import asyncio as _asyncio
    import sys as _sys

    _sys.path.insert(0, plugin_dir)
    from code_flow.gate import WorkspaceGate as _WorkspaceGate

    g = _WorkspaceGate(Path(repo), Path(tmp) / "repos", instance=instance, alloc_dir=Path(tmp) / "instances" / instance)
    (Path(tmp) / "instances" / instance).mkdir(parents=True, exist_ok=True)
    verdict = _asyncio.run(g.adjudicate("write_file", {"path": "x"}, session_workdir=None))
    q.put((instance, verdict is None))


def test_concurrent_first_write_single_primary(tmp_path):
    """N processes race the first write on a clean repo: exactly one binds."""
    repo = tmp_path / "repo"
    make_repo(repo)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_race_worker, args=(str(repo), str(tmp_path), f"instance-{i}", str(PLUGIN_DIR), q))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    results = [q.get(timeout=10) for _ in procs]
    winners = [name for name, allowed in results if allowed]
    assert len(winners) == 1, f"expected one primary owner, got {winners}"
    repo_id, _ = repo_identity(repo)
    owner = json.loads((tmp_path / "repos" / repo_id / "primary.json").read_text())
    assert owner["instance"] == winners[0]


# --- the in-turn ask path (ACP: broker present, answer arrives mid-turn) -------


def answering(reply):
    async def ask(prompt, choices, default=""):
        ask.asked = (prompt, list(choices))
        return reply

    ask.asked = None
    return ask


def test_in_turn_approve_creates_worktree_and_rebinds(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("uncommitted\n")
    gate = make_gate(repo, tmp_path, "instance-a")
    gate.ask = answering("1")
    rebound = []
    gate.rebind = rebound.append

    msg = call(gate, "write_file", {"path": "b.py"})
    assert msg and "authorization granted" in msg and "worktree" in msg
    prompt, choices = gate.ask.asked
    assert "uncommitted changes" in prompt and "WITHOUT" in prompt  # HEAD-based, dirty excluded
    assert choices == ["worktree", "readonly", "retry_later"]
    a = alloc_of(gate)
    assert a["state"] == "working" and a["mode"] == "worktree"
    wt = Path(a["boundWorkdir"])
    assert wt.is_dir() and not wt.resolve().is_relative_to(repo.resolve())
    assert rebound and rebound[0] == wt
    # The user's uncommitted file did not leak into the worktree.
    assert not (wt / "wip.txt").exists()
    # And the next write goes straight through.
    assert call(gate, "write_file", {"path": str(wt / "b.py")}) is None


def test_in_turn_decline_locks_readonly(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("x\n")
    gate = make_gate(repo, tmp_path, "instance-a")
    gate.ask = answering("2")
    msg = call(gate, "write_file", {"path": "b.py"})
    assert msg and "declined" in msg
    assert alloc_of(gate)["state"] == "readonly_locked"
    # Terminal: asked once, never again.
    gate.ask = answering("1")
    msg2 = call(gate, "write_file", {"path": "c.py"})
    assert msg2 and "declined" in msg2
    assert gate.ask.asked is None


def test_in_turn_no_answer_stays_unlocked_and_reasks(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("x\n")
    gate = make_gate(repo, tmp_path, "instance-a")
    gate.ask = answering("")  # broker timeout/cancel returns the default
    msg = call(gate, "write_file", {"path": "b.py"})
    assert msg and "did not answer" in msg
    # Nothing terminal was persisted: no lock-in, no pending.
    assert not (gate._alloc_dir / "allocation.json").exists()
    gate.ask = answering("worktree")  # the user comes back
    msg2 = call(gate, "write_file", {"path": "b.py"})
    assert msg2 and "authorization granted" in msg2


def test_acp_layout_keys_allocation_by_session(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    base = tmp_path / "acp-state"
    gate = WorkspaceGate(repo, tmp_path / "repos", alloc_base=base)
    keys = ["acp:sess-A"]
    gate.cid = lambda: keys[0]
    assert call(gate, "write_file", {"path": "b.py"}) is None  # A binds primary
    keys[0] = "acp:sess-B"
    gate.ask = answering("1")
    msg = call(gate, "write_file", {"path": "c.py"})
    assert msg and "authorization granted" in msg  # B gets a worktree
    a_rec = json.loads((base / "allocations" / "acp-acp_sess-A.json").read_text())
    b_rec = json.loads((base / "allocations" / "acp-acp_sess-B.json").read_text())
    assert a_rec["mode"] == "primary" and b_rec["mode"] == "worktree"


# --- authorization answer parsing ---------------------------------------------

ALL = [CHOICE_WORKTREE, CHOICE_READONLY, CHOICE_RETRY]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("worktree", CHOICE_WORKTREE),
        ("  Worktree ", CHOICE_WORKTREE),
        ("1", CHOICE_WORKTREE),
        ("1.", CHOICE_WORKTREE),
        ("readonly", CHOICE_READONLY),
        ("2", CHOICE_READONLY),
        ("read-only", CHOICE_READONLY),
        ("retry_later", CHOICE_RETRY),
        ("3", CHOICE_RETRY),
        ("later", CHOICE_RETRY),
    ],
)
def test_exact_answers_parse(answer, expected):
    assert parse_choice(answer, ALL) == expected


@pytest.mark.parametrize(
    "answer",
    [
        "do not create a worktree",
        "no worktree",
        "worktree is a bad idea",
        "yes",
        "agree",
        "create it",
        "approve",
        "4",
        "worktree, but wait a moment",
        "sounds good",
        "",
        "   ",
    ],
)
def test_ambiguous_or_negated_answers_do_not_authorize(answer):
    # None keeps the turn read-only and lets the gate ask again - never a write.
    assert parse_choice(answer, ALL) is None


def test_a_choice_not_offered_is_not_parsed():
    assert parse_choice("1", [CHOICE_READONLY, CHOICE_RETRY]) is None
    assert parse_choice("worktree", [CHOICE_READONLY, CHOICE_RETRY]) is None
