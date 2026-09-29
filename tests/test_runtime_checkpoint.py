"""Per-turn shadow-git checkpoint + max-iter interrupted handling.

Covers the runtime-discipline safety net gated by
``config.runtime.checkpoint.policy`` × ``AgentLoop(interactive=...)``:
- CheckpointService snapshots the worktree without touching the user's .git.
- A max-iteration turn reports ``status="interrupted"`` (not a fake
  completion) and the workspace is snapshotted for recovery.
- With policy="never" (or "interactive" + interactive=False), the loop is
  behaviorally unchanged from baseline.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from raven.agent import workdir
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.loop.checkpoint import CheckpointService
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


async def _run_turn_body(agent: AgentLoop, workspace: Path):
    """Drive ``_run_agent_loop`` the way ``run_turn`` does: inside the
    working-directory binding. The per-turn checkpoint reads that binding, so
    an unbound call would snapshot nothing at all.
    """
    with workdir.bind(workspace):
        return await agent._run_agent_loop([{"role": "user", "content": "go"}])


# ---------------------------------------------------------------------------
# CheckpointService unit
# ---------------------------------------------------------------------------


async def test_checkpoint_commits_then_noops_when_unchanged(workspace):
    svc = CheckpointService(workspace)
    (workspace / "a.py").write_text("print(1)\n", encoding="utf-8")

    cid, changed = await svc.commit_turn("turn 1")
    assert cid is not None
    assert "a.py" in changed

    # Nothing changed since the last snapshot → no new commit.
    cid2, changed2 = await svc.commit_turn("turn 2")
    assert cid2 is None
    assert changed2 == []


async def test_checkpoint_does_not_touch_user_git(workspace):
    """When the workspace is itself a git repo, the shadow checkpoint must
    not add commits to the user's real history (plan §7 fixture)."""
    env = {"GIT_AUTHOR_NAME": "u", "GIT_AUTHOR_EMAIL": "u@u", "GIT_COMMITTER_NAME": "u", "GIT_COMMITTER_EMAIL": "u@u"}
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "base"], cwd=workspace, check=True, env={**_os_environ(), **env}
    )
    head_before = _git_head(workspace)
    count_before = _git_count(workspace)

    svc = CheckpointService(workspace)
    (workspace / "b.py").write_text("x = 1\n", encoding="utf-8")
    cid, changed = await svc.commit_turn("snap")
    assert cid is not None
    assert "b.py" in changed

    assert _git_head(workspace) == head_before, "user HEAD must be unchanged"
    assert _git_count(workspace) == count_before, "no commits added to user repo"


async def test_a_staged_tree_holds_what_a_file_said_before_it_changed(workspace):
    """What a command's diff is read against: the file as it stood when the tree
    was staged, after the file has been rewritten on disk."""
    svc = CheckpointService(workspace)
    kept = workspace / "kept.txt"
    kept.write_text("one\n", encoding="utf-8")

    tree = await svc.stage_tree()
    assert tree is not None
    kept.write_text("two\n", encoding="utf-8")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")

    held = await svc.read_blobs(tree, [str(kept), str(workspace / "new.txt")], max_bytes=1024)
    assert held == {str(kept): b"one\n"}, "a file the tree never had is unknown, not empty"


async def test_a_blob_is_left_out_past_the_cap_or_outside_the_work_tree(workspace, tmp_path_factory):
    svc = CheckpointService(workspace)
    big = workspace / "big.txt"
    big.write_text("x" * 64, encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside") / "o.txt"
    outside.write_text("o\n", encoding="utf-8")

    tree = await svc.stage_tree()
    assert tree is not None

    assert await svc.read_blobs(tree, [str(big), str(outside)], max_bytes=63) == {}
    assert await svc.read_blobs(tree, [str(big)], max_bytes=64) == {str(big): b"x" * 64}


async def test_staging_a_tree_leaves_the_turn_commit_its_own_changes(workspace):
    """The stage has an index of its own. Staged into the shared one, the turn's
    commit would find this file already staged and report the turn as having
    changed nothing -- the recovery prompt would lose it."""
    svc = CheckpointService(workspace)
    (workspace / "a.py").write_text("print(1)\n", encoding="utf-8")

    assert await svc.stage_tree() is not None
    cid, changed = await svc.commit_turn("turn 1")

    assert cid is not None
    assert changed == ["a.py"]


async def test_a_stale_staging_index_is_pruned_and_a_fresh_one_kept(workspace):
    """A process that died leaves its staging index behind. Pruned by age, never
    by asking whether its pid is alive -- on Windows that question kills it."""
    import os
    import time

    svc = CheckpointService(workspace)
    assert await svc.stage_tree() is not None
    git_dir = workspace / ".raven" / "shadow.git"
    stale = git_dir / "exec-1.index"
    fresh = git_dir / "exec-2.index"
    stale.write_bytes(b"")
    fresh.write_bytes(b"")
    old = time.time() - 8 * 24 * 3600
    os.utime(stale, (old, old))

    again = CheckpointService(workspace)
    again.note_write()
    assert await again.stage_tree() is not None

    assert not stale.exists()
    assert fresh.exists()
    assert (git_dir / f"exec-{os.getpid()}.index").exists()


async def test_a_stage_that_timed_out_leaves_no_lock_behind(workspace, monkeypatch):
    """A killed ``git add`` leaves its ``index.lock``, and every later stage would
    fail on it for the rest of the process -- one slow command would cost every
    command after it its diff."""
    import os
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    svc = CheckpointService(workspace)
    assert await svc.stage_tree() is not None
    lock = workspace / ".raven" / "shadow.git" / f"exec-{os.getpid()}.index.lock"
    real = subprocess.run

    def _killed(cmd, **kwargs):
        lock.write_bytes(b"")
        raise subprocess.TimeoutExpired(cmd, 0.05)

    monkeypatch.setattr(cp_module.subprocess, "run", _killed)
    svc.note_write()
    assert await svc.stage_tree() is None
    assert not lock.exists()

    monkeypatch.setattr(cp_module.subprocess, "run", real)
    svc.note_write()
    assert await svc.stage_tree() is not None


async def test_a_slow_first_stage_is_left_to_warm_the_index_behind_the_command(workspace, monkeypatch):
    """The first stage in a directory hashes every file and can take seconds.
    Past the budget the call is told so, the stage keeps running, a command
    that arrives meanwhile does not start a second one, and the one after it
    finds the index warm."""
    import asyncio
    import subprocess
    import threading

    import raven.agent.loop.checkpoint as cp_module

    svc = CheckpointService(workspace)
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    real = subprocess.run
    release = threading.Event()
    adds: list[object] = []

    def _slow(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
            release.wait(10)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(cp_module.subprocess, "run", _slow)

    with pytest.raises(cp_module.StagingTimeoutError):
        await svc.stage_tree()
    with pytest.raises(cp_module.StagingTimeoutError):
        await svc.stage_tree()
    assert len(adds) == 1, "a stage still running is not started again"

    release.set()
    assert await asyncio.wrap_future(cp_module._STAGING[svc._stage_path()]) is not None
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 30.0)
    assert await svc.stage_tree() is not None


async def test_a_stage_waits_out_the_warm_up_and_stages_again(workspace, monkeypatch):
    """The warm-up started with the turn may still be running when the first
    command arrives. When a tool call has written since it began, its tree may
    miss that write, so it is waited for and never used; the staging after it
    is the one read."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    svc = CheckpointService(workspace)
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    real = subprocess.run
    adds: list[object] = []

    def _count(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
            if len(adds) == 1:
                time.sleep(0.3)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module.subprocess, "run", _count)

    await svc.warm()
    (workspace / "a.txt").write_text("changed while warming\n", encoding="utf-8")
    svc.note_write()
    tree = await svc.stage_tree()

    assert tree is not None
    assert len(adds) == 2
    held = await svc.read_blobs(tree, [str(workspace / "a.txt")], max_bytes=1024)
    assert held == {str(workspace / "a.txt"): b"changed while warming\n"}


async def test_a_warm_up_already_running_is_not_started_again(workspace, monkeypatch):
    """Two turns starting in one directory warm it once: the second warm-up
    would only queue a second hash of the same tree behind the first."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run
    adds: list[object] = []

    def _count(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
            time.sleep(0.2)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module.subprocess, "run", _count)
    svc = CheckpointService(workspace)

    await svc.warm()
    await CheckpointService(workspace).warm()
    await asyncio.wrap_future(cp_module._STAGING[svc._stage_path()])

    assert len(adds) == 1


async def test_a_warm_up_returns_before_the_repo_is_even_set_up(workspace, monkeypatch):
    """The turn awaits ``warm`` in front of its first model call, so everything
    the warm-up does -- the repo's own ``git init`` and config as much as the
    staging -- belongs on its thread. Awaited, a slow setup would be a first
    reply that waits for it."""
    import raven.agent.loop.checkpoint as cp_module

    real = CheckpointService._ensure_init

    async def _slow_init(self) -> bool:
        await asyncio.sleep(1.0)
        return await real(self)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(CheckpointService, "_ensure_init", _slow_init)
    svc = CheckpointService(workspace)

    started = time.monotonic()
    await svc.warm()
    assert time.monotonic() - started < 0.2

    assert await asyncio.wrap_future(cp_module._STAGING[svc._stage_path()]) is not None


async def test_a_turn_that_ends_during_the_warm_up_still_commits(workspace, monkeypatch):
    """A turn can end before its warm-up has set the repo up. Two setups at once
    fail on the config lock, and the one that lost was the turn's commit -- so
    the commit waits for the warm-up's setup instead of racing it."""
    import threading

    import raven.agent.loop.checkpoint as cp_module

    real = CheckpointService._init_repo
    active = [0]
    overlap = [0]

    async def _tracked(self) -> bool:
        active[0] += 1
        overlap[0] = max(overlap[0], active[0])
        try:
            if threading.current_thread().name == "raven-stage":
                await asyncio.sleep(0.3)
            return await real(self)
        finally:
            active[0] -= 1

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(CheckpointService, "_init_repo", _tracked)
    (workspace / "a.py").write_text("print(1)\n", encoding="utf-8")
    svc = CheckpointService(workspace)

    await svc.warm()
    cid, changed = await svc.commit_turn("turn 1")

    assert cid is not None and changed == ["a.py"]
    assert overlap[0] == 1, "the commit's setup ran beside the warm-up's"


async def test_a_directory_is_warmed_once_and_not_every_turn(workspace, monkeypatch):
    """Every turn starts with a warm-up call, and only the first does any work:
    after it each command's own staging keeps the index warm, so another would
    be one more stat walk of the whole tree per message for nothing."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run
    adds: list[object] = []

    def _count(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module.subprocess, "run", _count)
    svc = CheckpointService(workspace)

    await svc.warm()
    await asyncio.wrap_future(cp_module._STAGING[svc._stage_path()])
    await svc.warm()
    await svc.warm()

    assert len(adds) == 1


async def test_two_commands_waiting_on_one_warm_up_share_the_staging_after_it(workspace, monkeypatch):
    """Two sessions in one directory both find a warm-up running that a write has
    made stale. The first to wake starts the next staging; the second takes that
    one rather than starting a third ``git add`` on the same index."""
    import asyncio
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    svc = CheckpointService(workspace)
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    real = subprocess.run
    adds: list[object] = []

    def _count(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
            if len(adds) == 1:
                time.sleep(0.3)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module.subprocess, "run", _count)

    await svc.warm()
    svc.note_write()
    first, second = await asyncio.gather(svc.stage_tree(), CheckpointService(workspace).stage_tree())

    assert first is not None and second is not None
    assert len(adds) == 2


async def test_a_slow_first_staging_is_not_cut_off_at_the_git_call_ceiling(workspace, monkeypatch):
    """Every command is held back until the staging finishes. Killed at the
    ceiling a turn's own git calls use, a staging that needs longer is started
    over, killed again, and never finishes -- so no command would ever run."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run
    ceilings: list[float] = []

    def _spy(cmd, **kwargs):
        if "add" in cmd:
            ceilings.append(kwargs["timeout"])
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module.subprocess, "run", _spy)
    assert await CheckpointService(workspace).stage_tree() is not None

    assert ceilings == [cp_module._STAGE_ADD_TIMEOUT_SECONDS]
    assert ceilings[0] > cp_module._GIT_TIMEOUT_SECONDS * 10


async def test_retries_wait_out_a_staging_slower_than_the_budget(workspace, monkeypatch):
    """A tree whose every staging takes longer than a command waits. Each retry
    waits for the staging the refused call left running -- nothing has written
    since it began -- instead of starting an equally slow one, so every command
    runs within a retry or two however slow the tree, rather than being refused
    for good."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run
    adds: list[object] = []

    def _slow(cmd, **kwargs):
        if "add" in cmd:
            adds.append(cmd)
            time.sleep(0.5)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_STARTED", {})
    monkeypatch.setattr(cp_module, "_WRITTEN_AT", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(cp_module.subprocess, "run", _slow)
    svc = CheckpointService(workspace)
    attempts: list[int] = []

    for command in range(3):
        for attempt in range(1, 11):
            try:
                tree = await svc.stage_tree()
            except cp_module.StagingTimeoutError:
                continue
            assert tree is not None
            attempts.append(attempt)
            break
        svc.note_write()

    assert len(attempts) == 3, "a command was refused on every retry"
    assert all(n > 1 for n in attempts), "the staging was faster than the budget"
    assert len(adds) == 3, "a retry must not start a staging of its own"


async def test_a_staging_under_the_budget_is_never_refused(workspace, monkeypatch):
    """Under the budget no command is held back, even when a staging is already
    running as it arrives: one the command can use is waited for inside the
    budget, not waited out and then followed by a second."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run

    def _slow(cmd, **kwargs):
        if "add" in cmd:
            time.sleep(0.8)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_STARTED", {})
    monkeypatch.setattr(cp_module, "_WRITTEN_AT", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 1.2)
    monkeypatch.setattr(cp_module.subprocess, "run", _slow)
    svc = CheckpointService(workspace)

    # The rest of the warm-up plus a fresh staging is past the budget; the
    # warm-up alone and a fresh one alone are each inside it.
    svc.note_write()
    await svc.warm()
    await asyncio.sleep(0.1)
    for _ in range(4):
        assert await svc.stage_tree() is not None
        svc.note_write()


async def test_a_command_behind_a_stale_staging_past_the_budget_is_held_back(workspace, monkeypatch):
    """A write made the running staging stale, and it has not finished within
    the budget. The command is held back like any other that cannot be
    measured yet -- not run unmeasured because the staging in its way was not
    its own."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run

    def _slow(cmd, **kwargs):
        if "add" in cmd:
            time.sleep(0.5)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(cp_module.subprocess, "run", _slow)
    svc = CheckpointService(workspace)

    await svc.warm()
    warm_up = cp_module._STAGING[svc._stage_path()]
    svc.note_write()
    with pytest.raises(cp_module.StagingTimeoutError):
        await svc.stage_tree()

    # Giving up on it must not have cancelled it: a later command reuses it.
    assert await asyncio.wrap_future(warm_up) is not None
    assert not warm_up.cancelled()


async def test_a_staging_from_before_a_write_is_not_reused(workspace):
    """Reuse is what lets a retry succeed, and what must not hand a command a
    tree from before a write: the old text of a file a tool changed since then
    is not what the command changed."""
    svc = CheckpointService(workspace)
    kept = workspace / "a.txt"
    kept.write_text("one\n", encoding="utf-8")
    first = await svc.stage_tree()
    assert await svc.stage_tree() == first, "nothing written: the same tree"

    kept.write_text("two\n", encoding="utf-8")
    svc.note_write()
    second = await svc.stage_tree()

    assert second != first
    assert await svc.read_blobs(second, [str(kept)], max_bytes=64) == {str(kept): b"two\n"}


async def test_a_first_stage_starts_from_the_last_turns_index(workspace, monkeypatch):
    """The turn's commit has already hashed the tree into the shared index, and a
    staging index copied from it only has to stat what changed since. Started
    empty, the first command in every process would pay for hashing the whole
    tree again."""
    import subprocess

    import raven.agent.loop.checkpoint as cp_module

    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    assert (await CheckpointService(workspace).commit_turn("turn 1"))[0] is not None
    shared = (workspace / ".raven" / "shadow.git" / "index").read_bytes()
    real = subprocess.run
    started_from: list[bytes] = []

    def _spy(cmd, **kwargs):
        if "add" in cmd:
            started_from.append(Path(kwargs["env"]["GIT_INDEX_FILE"]).read_bytes())
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module.subprocess, "run", _spy)
    assert await CheckpointService(workspace).stage_tree() is not None
    assert started_from == [shared]


def test_a_staging_still_running_does_not_hold_the_loop_open(workspace, monkeypatch):
    """Closing a loop cancels what is left on it. An asyncio subprocess still
    starting at that moment never finishes cancelling (CPython 3.12, macOS) and
    the close hangs -- which, for the gateway, is a shutdown that never ends."""
    import asyncio
    import subprocess
    import threading
    import time

    import raven.agent.loop.checkpoint as cp_module

    real = subprocess.run
    release = threading.Event()

    def _slow(cmd, **kwargs):
        release.wait(10)
        return real(cmd, **kwargs)

    monkeypatch.setattr(cp_module, "_STAGING", {})
    monkeypatch.setattr(cp_module, "_STAGE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(cp_module.subprocess, "run", _slow)

    async def one_call() -> bool:
        try:
            await CheckpointService(workspace).stage_tree()
        except cp_module.StagingTimeoutError:
            return True
        return False

    # On a thread of its own: asyncio.run on the main thread clears the default
    # loop, and pytest-asyncio then makes one it never closes.
    outcome: list[bool] = []
    started = time.monotonic()
    runner = threading.Thread(target=lambda: outcome.append(asyncio.run(one_call())))
    runner.start()
    runner.join(10)
    assert outcome == [True]
    assert time.monotonic() - started < 5
    release.set()


def _os_environ():
    import os

    return dict(os.environ)


def _git_head(cwd: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


def _git_count(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Loop-level: max-iter interrupted vs baseline
# ---------------------------------------------------------------------------


class _ToolLoopProvider(LLMProvider):
    """Always asks to write the same file — never finishes, forcing max-iter."""

    def __init__(self, path: str = "a.py") -> None:
        super().__init__(api_key="test")
        self._path = path

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
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="write_file",
                    arguments={"path": self._path, "content": "x = 1\n"},
                )
            ],
            finish_reason="tool_calls",
        )

    def get_default_model(self) -> str:
        return "stub"


def _loop_agent(workspace: Path, *, checkpoint_enabled: bool) -> AgentLoop:
    return AgentLoop(
        provider=_ToolLoopProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(
                checkpoint=CheckpointConfig(
                    policy="always" if checkpoint_enabled else "never",
                ),
            )
        ),
    )


async def test_max_iter_interrupted_with_checkpoint(workspace):
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    final, _used, _msgs, outcome = await _run_turn_body(agent, workspace)
    assert outcome.status == "interrupted"
    # Checkpoint-on no longer short-circuits to a fixed "iteration limit"
    # notice: exhaustion always runs the synthesis wrap-up (here the stub
    # cannot produce content, so it lands on the static fallback). The
    # checkpoint metadata below — not the reply text — is what marks the turn
    # interrupted and recoverable.
    assert "maximum number of tool call iterations" in (final or "")
    # The turn's edits were snapshotted and offered for recovery.
    assert outcome.checkpoint_id is not None
    assert "a.py" in outcome.edited_files


async def test_max_iter_baseline_preserved_when_disabled(workspace):
    agent = _loop_agent(workspace, checkpoint_enabled=False)
    final, _used, _msgs, outcome = await _run_turn_body(agent, workspace)
    # Status is reported (harmless metadata) but behavior is baseline:
    # original message text, no checkpoint.
    assert outcome.status == "interrupted"
    assert "maximum number of tool call iterations" in (final or "")
    assert outcome.checkpoint_id is None
    assert outcome.edited_files == []


# --- I3/I4: the soul cases — "half-done work must not pollute memory" --------


# Note: tests that spied on ``_trigger_local_extraction`` were removed when the
# embedded extraction path was retired by feature/integrate-everos (Phase B-1).
# The axiom "interrupted turn != completed turn" now lives in two places
# preserved by this merge:
#   1. Shadow-git snapshot is taken regardless (see test_max_iter_snapshot...)
#   2. ``outcome.status`` distinguishes interrupted vs completed for any caller
#      that wants to gate downstream actions on it (the new after-turn
#      pipeline at the caller level can choose to honor this — out of scope
#      for the checkpoint itself).


# --- I5/I6: completed and error terminal states ------------------------------


class _WriteThenStopProvider(LLMProvider):
    """First turn writes a file, then stops — a normal completion that also
    left an edit to snapshot."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self._n = 0

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
        self._n += 1
        if self._n == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments={"path": "done.py", "content": "ok\n"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="all done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _ErrorProvider(LLMProvider):
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
        return LLMResponse(content="fatal model error: bad request", finish_reason="error")

    def get_default_model(self) -> str:
        return "stub"


async def test_completed_status_and_snapshot(workspace):
    agent = AgentLoop(
        provider=_WriteThenStopProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=5),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="always"))),
    )
    final, _used, _msgs, outcome = await _run_turn_body(agent, workspace)
    assert outcome.status == "completed"
    assert "all done" in (final or "")
    # The completed turn's edit was snapshotted...
    assert outcome.checkpoint_id is not None
    # ...but edited_files is only surfaced for interrupted turns (recovery).
    assert outcome.edited_files == []


async def test_error_status(workspace):
    agent = AgentLoop(
        provider=_ErrorProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=5),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="always"))),
    )
    _final, _used, _msgs, outcome = await _run_turn_body(agent, workspace)
    assert outcome.status == "error"


# ---------------------------------------------------------------------------
# Recovery prompt injection
# ---------------------------------------------------------------------------


def test_recovery_block_injected_into_next_user_message(workspace):
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    agent._pending_recovery["sess"] = {"checkpoint_id": "abc123", "files": ["a.py", "b.py"]}

    messages = [{"role": "user", "content": "continue please"}]
    agent._inject_recovery_block("sess", messages)

    injected = messages[-1]["content"]
    assert "previous turn was interrupted" in injected.lower()
    assert "a.py" in injected and "b.py" in injected
    assert "abc123" in injected
    assert "continue please" in injected
    # Consumed exactly once.
    assert "sess" not in agent._pending_recovery


def test_recovery_block_noop_without_pending(workspace):
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    messages = [{"role": "user", "content": "hello"}]
    agent._inject_recovery_block("sess", messages)
    assert messages[-1]["content"] == "hello"


# --- U6/U8/U9: edge coverage -------------------------------------------------


async def test_checkpoint_captures_deletion(workspace):
    """add -A is edit-source-agnostic: a deletion (or any non-tool change) is
    still snapshotted — the advantage over edit-tool-triggered approaches."""
    svc = CheckpointService(workspace)
    (workspace / "x.py").write_text("a\n", encoding="utf-8")
    await svc.commit_turn("t1")
    (workspace / "x.py").unlink()
    cid, changed = await svc.commit_turn("t2")
    assert cid is not None
    assert "x.py" in changed


def test_recovery_block_injects_into_list_content(workspace):
    """Multimodal user message (content is a list) → recovery prepended as a
    leading text block."""
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    agent._pending_recovery["s"] = {"checkpoint_id": "cid9", "files": ["a.py"]}
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    ]
    agent._inject_recovery_block("s", messages)
    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "interrupted" in content[0]["text"].lower()
    # original blocks preserved after the injected one
    assert content[1]["text"] == "hi"


def test_recovery_block_kept_when_last_not_user(workspace):
    """If the last message isn't the user turn, don't inject and don't lose the
    pending recovery — keep it for the next (user-terminated) assembly."""
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    agent._pending_recovery["s"] = {"checkpoint_id": "c", "files": ["a.py"]}
    messages = [{"role": "assistant", "content": "x"}]
    agent._inject_recovery_block("s", messages)
    assert messages[-1]["content"] == "x"
    assert "s" in agent._pending_recovery  # not consumed


# --- E1/E2: cross-turn recovery through real assembly ------------------------


async def test_recovery_flows_interrupt_to_next_assembly(workspace):
    """End-to-end: an interrupted turn stashes recovery (as the caller does),
    and the NEXT context assembly injects it into the user message — exercising
    the real stash -> _assemble_context_messages -> inject wiring."""
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    key = "tui:default"

    # Turn 1 — force a max-iter interruption, then stash like the caller.
    _f, _u, _m, outcome = await _run_turn_body(agent, workspace)
    assert outcome.status == "interrupted"
    with workdir.bind(workspace):
        agent._stash_recovery(key, outcome)
    assert key in agent._pending_recovery

    # Turn 2 — real assembly must carry the recovery notice.
    session = agent.sessions.get_or_create(key)
    messages = await agent._assemble_context_messages(
        session=session,
        session_key=key,
        current_message="continue",
    )
    last_user = messages[-1]
    assert last_user["role"] == "user"
    text = (
        last_user["content"]
        if isinstance(last_user["content"], str)
        else next(b["text"] for b in last_user["content"] if b.get("type") == "text")
    )
    assert "interrupted" in text.lower()
    assert "continue" in text


async def test_recovery_consumed_once(workspace):
    """A third assembly (after consumption) must not re-inject."""
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    key = "tui:default"
    agent._pending_recovery[key] = {"checkpoint_id": "c", "files": ["a.py"]}
    session = agent.sessions.get_or_create(key)

    m1 = await agent._assemble_context_messages(
        session=session,
        session_key=key,
        current_message="first",
    )
    t1 = m1[-1]["content"]
    assert "interrupted" in (t1 if isinstance(t1, str) else str(t1)).lower()

    m2 = await agent._assemble_context_messages(
        session=session,
        session_key=key,
        current_message="second",
    )
    t2 = m2[-1]["content"]
    assert "interrupted" not in (t2 if isinstance(t2, str) else str(t2)).lower()


# --- F1: robustness — checkpoint must degrade, never crash the turn ----------


async def test_checkpoint_degrades_when_git_missing(workspace, monkeypatch):
    """If git is unavailable, commit_turn returns (None, []) and never raises —
    the safety net must not break the turn it protects."""
    import raven.agent.loop.checkpoint as ckpt_mod

    async def _boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(ckpt_mod.asyncio, "create_subprocess_exec", _boom)
    svc = ckpt_mod.CheckpointService(workspace)
    (workspace / "a.py").write_text("x\n", encoding="utf-8")
    cid, changed = await svc.commit_turn("t1")
    assert cid is None
    assert changed == []


async def test_loop_survives_checkpoint_failure(workspace, monkeypatch):
    """Same failure, but through the loop: the turn still returns a result."""
    import raven.agent.loop.checkpoint as ckpt_mod

    async def _boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(ckpt_mod.asyncio, "create_subprocess_exec", _boom)
    agent = _loop_agent(workspace, checkpoint_enabled=True)
    final, _u, _m, outcome = await _run_turn_body(agent, workspace)
    assert outcome.status == "interrupted"
    assert outcome.checkpoint_id is None  # commit failed, but no crash


# ---------------------------------------------------------------------------
# Scope of what gets snapshotted
#
# The working directory is the launch directory now, so `cd ~ && raven tui`
# would aim the shadow repo at a whole home. These pin both halves of the
# answer: refuse that root outright, and keep private keys out of the roots
# that are accepted.
# ---------------------------------------------------------------------------


def test_checkpoint_refuses_a_home_or_wider_root(tmp_path, monkeypatch):
    """A home directory is not a workspace. `add -A` over one copies whatever
    the user keeps there into a repo with history, every turn, and an
    interrupted turn edits a project rather than a home -- so there is nothing
    to recover in exchange."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    for refused in (home, tmp_path, Path(tmp_path.anchor)):
        with pytest.raises(ValueError, match="home directory"):
            CheckpointService(refused)

    CheckpointService(home / "proj")  # a project under home is still fine


async def test_checkpoint_excludes_private_keys(tmp_path, monkeypatch):
    """``*.key`` / ``*.pem`` never match ``id_ed25519``: private keys are
    conventionally extensionless, so the credential patterns that look
    exhaustive miss the most common secret on the machine."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj = home / "proj"
    (proj / ".ssh").mkdir(parents=True)
    (proj / ".ssh" / "id_ed25519").write_text("PRIVATE\n", encoding="utf-8")
    (proj / "id_rsa").write_text("PRIVATE\n", encoding="utf-8")
    (proj / "deploy.pem").write_text("PRIVATE\n", encoding="utf-8")
    (proj / "main.py").write_text("print(1)\n", encoding="utf-8")

    svc = CheckpointService(proj)
    await svc.commit_turn("t1")
    _rc, out, _err = await svc._git("ls-tree", "-r", "--name-only", "HEAD")

    assert sorted(out.split()) == ["main.py"]


async def test_loop_runs_the_turn_when_the_root_is_refused(tmp_path, monkeypatch):
    """A refused root disables the safety net, it does not break the turn.

    `raven tui` from a home directory is an ordinary thing to do, so the guard
    has to degrade the way a bad `shadow_dir` already does -- one warning, and
    the work still happens.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".raven").mkdir()
    (home / ".raven" / "config.json").write_text('{"permissions": {"mode": "full"}}')

    agent = _loop_agent(home, checkpoint_enabled=True)
    final, _used, _msgs, outcome = await _run_turn_body(agent, home)

    assert outcome.status == "interrupted"  # max-iter, as in the sibling tests
    assert outcome.checkpoint_id is None  # refused, so nothing was snapshotted
    assert (home / "a.py").exists()  # the turn's edit still landed
