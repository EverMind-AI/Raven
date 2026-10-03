"""Strict native DAG verification, input authority, and runtime boundaries."""

import pytest

from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest


class FileProvider:
    def __init__(self, path=None):
        self.path = path
        self.calls = []

    async def chat_with_retry(self, *, messages, tools, model):
        self.calls.append((list(messages), tools))
        if self.path is not None and len(self.calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="read", name="read_file", arguments={"path": str(self.path)})],
            )
        return LLMResponse(content="candidate")


@pytest.mark.asyncio
async def test_strict_native_tools_are_confined_and_history_is_fresh(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    latest = home / "latest.output.md"
    latest.write_text("UNACCEPTED", encoding="utf-8")
    workspace = tmp_path / "attempt"
    workspace.mkdir()
    provider = FileProvider(latest)
    backend = RavenLoopBackend(provider=provider, model="fixture", agent_home=home)
    history = [{"role": "system", "content": "UNACCEPTED_HISTORY"}]
    captured = []
    await backend.run(
        "inspect only accepted input",
        task_id="attempt",
        workspace=workspace,
        executor=None,
        history=history,
        on_messages=captured.append,
        allowed_dirs=(workspace,),
        tools_allow=("read_file", "write_file", "edit_file", "list_dir"),
    )
    offered = {tool["function"]["name"] for tool in provider.calls[0][1]}
    assert offered == {"read_file", "write_file", "edit_file", "list_dir"}
    assert "UNACCEPTED_HISTORY" not in str(provider.calls[0][0])
    result = provider.calls[1][0][-1]["content"]
    assert "outside allowed directories" in result
    assert "UNACCEPTED" not in result
    assert backend.tools_allow is None
    assert captured and captured[-1][-1]["content"] == "candidate"
    assert "UNACCEPTED_HISTORY" not in str(captured)


@pytest.mark.asyncio
async def test_ordinary_native_keeps_home_access_tools_and_history(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    latest = home / "latest.output.md"
    latest.write_text("ordinary output", encoding="utf-8")
    workspace = tmp_path / "ordinary"
    workspace.mkdir()
    provider = FileProvider(latest)
    backend = RavenLoopBackend(provider=provider, model="fixture", agent_home=home, restrict_to_workspace=True)
    history = [{"role": "system", "content": "ordinary history"}]
    await backend.run("read", task_id="ordinary", workspace=workspace, executor=None, history=history)
    offered = {tool["function"]["name"] for tool in provider.calls[0][1]}
    assert {"exec", "web_fetch", "read_file"} <= offered
    assert provider.calls[0][0][0] == history[0]
    assert "ordinary output" in provider.calls[1][0][-1]["content"]


@pytest.mark.asyncio
async def test_saved_check_uses_real_exit_and_immutable_fingerprint(tmp_path):
    import sys

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    (tmp_path / "input.txt").write_text("accepted", encoding="utf-8")
    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    before = verifier.fingerprint()
    observation = await verifier.check((sys.executable, "-c", "print('TEST_PASSED'); raise SystemExit(7)"), timeout=2)
    assert observation.exit_code == 7
    assert observation.error is None
    assert observation.before_fingerprint == observation.after_fingerprint == before


@pytest.mark.asyncio
async def test_saved_check_detects_actual_snapshot_mutation(tmp_path):
    import sys

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    (tmp_path / "input.txt").write_text("accepted", encoding="utf-8")
    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    observation = await verifier.check(
        (sys.executable, "-c", "from pathlib import Path; Path('input.txt').write_text('mutated')"), timeout=2
    )
    assert observation.exit_code == 0
    assert observation.before_fingerprint != observation.after_fingerprint


@pytest.mark.asyncio
async def test_saved_check_timeout_is_unknown_and_no_shell_expands(tmp_path):
    import sys

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    observation = await verifier.check((sys.executable, "-c", "import time; time.sleep(5)"), timeout=1)
    assert observation.error == "timeout"
    assert observation.exit_code is None
    shell = await verifier.check(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch injected)"), timeout=2
    )
    assert shell.exit_code == 0
    assert not (tmp_path / "injected").exists()


def test_snapshot_refuses_symlinks_and_nonregular_files(tmp_path):
    from raven.agent.subagent.dag_strict import SnapshotVerifier
    from raven.contracts.mailbox import MailboxError

    (tmp_path / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(MailboxError, match="unsafe_snapshot"):
        SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40).fingerprint()


@pytest.mark.asyncio
async def test_saved_check_confines_and_uses_relative_cwd(tmp_path):
    import sys

    from raven.agent.subagent.dag_strict import SnapshotVerifier
    from raven.contracts.mailbox import MailboxError

    (tmp_path / "nested").mkdir()
    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    with pytest.raises(MailboxError, match="unsafe_snapshot"):
        await verifier.check((sys.executable, "-c", "pass"), timeout=2, cwd="..")
    observed = await verifier.check((sys.executable, "-c", "import os; print(os.getcwd())"), timeout=2, cwd="nested")
    assert observed.exit_code == 0
    assert verifier.stdout_path.read_text().strip() == str(tmp_path / "nested")


def test_baseline_reads_commit_objects_and_preserves_checker_provenance(tmp_path):
    import hashlib
    import io
    import subprocess
    import tarfile

    from raven.agent.subagent.dag_strict import export_baseline
    from raven.contracts.mailbox import MailboxError

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    checker = tmp_path / "check.py"
    checker.write_text("assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "check.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    checker.write_text("assert False\n", encoding="utf-8")
    manifest, archive = export_baseline(tmp_path, revision, ("check.py",))
    assert manifest.revision_commit == revision
    assert manifest.baseline_sha256 == hashlib.sha256(archive).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        assert source.extractfile("check.py").read() == b"assert True\n"
    with pytest.raises(MailboxError, match="invalid_revision"):
        export_baseline(tmp_path, "HEAD", ("check.py",))
    with pytest.raises(MailboxError, match="unsafe_snapshot"):
        export_baseline(tmp_path, revision, ("../check.py",))


@pytest.mark.asyncio
async def test_changed_executable_never_produces_a_pass(tmp_path):
    import sys

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    observed = await verifier.check((sys.executable, "-c", "pass"), timeout=2, executable_sha256="0" * 64)
    assert observed.error == "exception"
    assert observed.exit_code is None


@pytest.mark.asyncio
async def test_overflow_closes_both_evidence_streams_before_hashing(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    import sys

    from raven.agent.subagent import dag_strict

    monkeypatch.setattr(dag_strict, "_OUTPUT_LIMIT", 64)
    original = asyncio.StreamReader.read
    drains = 0

    async def delayed_stderr(reader, n=-1):
        nonlocal drains
        if n == 65536:
            drains += 1
            if drains == 2:
                await asyncio.sleep(0.1)
        return await original(reader, n)

    monkeypatch.setattr(asyncio.StreamReader, "read", delayed_stderr)
    verifier = dag_strict.SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    observed = await verifier.check(
        (sys.executable, "-c", "import os; os.write(1,b'x'*65); os.write(2,b'evidence')"), timeout=2
    )
    assert observed.error == "exception"
    pending = [
        task for task in asyncio.all_tasks() if "SnapshotVerifier.check.<locals>.drain" in task.get_coro().__qualname__
    ]
    assert not pending
    await asyncio.sleep(0.15)
    assert hashlib.sha256(verifier.stderr_path.read_bytes()).hexdigest() == observed.stderr_sha256


@pytest.mark.asyncio
async def test_process_start_time_does_not_extend_the_saved_deadline(tmp_path):
    import sys
    import time

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    verifier = SnapshotVerifier(tmp_path, revision_commit="1" * 40, revision_tree="2" * 40)
    start = time.monotonic()
    observed = await verifier.check(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout=1,
        deadline_ms=int(time.time() * 1000) + 100,
        on_started=lambda pid: time.sleep(0.2),
    )
    assert observed.error == "timeout"
    assert time.monotonic() - start < 0.28


def test_materialization_verifies_archive_and_every_changed_artifact(tmp_path):
    import hashlib
    import io
    import tarfile
    from dataclasses import replace

    from raven.agent.subagent.dag_strict import materialize_snapshot, publish_file
    from raven.contracts.mailbox import MailboxError
    from raven.mailbox.dag import ArtifactEntry, SnapshotManifest
    from raven.mailbox.store import MailboxStore

    store = MailboxStore(tmp_path / "mailbox")
    store.init(
        agent_id="11111111-1111-4111-8111-111111111111",
        request_id="22222222-2222-4222-8222-222222222222",
        allowed_scopes=[{"task_id": "task", "workspace_id": "workspace"}],
        allowed_kinds=["task.result"],
    )
    content = b"trusted checker"
    archive = tmp_path / "baseline.tar"
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("check.py")
        info.size = len(content)
        output.addfile(info, io.BytesIO(content))
    descriptor = publish_file(store, archive, "baseline.tar")
    entry = ArtifactEntry("check.py", hashlib.sha256(content).hexdigest(), len(content))
    baseline = SnapshotManifest("1" * 40, "2" * 40, descriptor["sha256"], (entry,), "")
    destination = tmp_path / "verification"
    materialize_snapshot(store, baseline, destination)
    assert (destination / "check.py").read_bytes() == content
    authored = tmp_path / "authored.txt"
    authored.write_bytes(b"authored")
    artifact = publish_file(store, authored, "authored.txt")
    changed = ArtifactEntry("authored.txt", artifact["sha256"], artifact["size"])
    snapshot = replace(baseline, entries=(entry, changed))
    materialize_snapshot(store, snapshot, tmp_path / "downstream")
    assert (tmp_path / "downstream" / "authored.txt").read_bytes() == b"authored"
    (store.root / "blobs" / "sha256" / artifact["sha256"]).write_bytes(b"corrupt")
    with pytest.raises(MailboxError, match="storage_conflict"):
        materialize_snapshot(store, snapshot, tmp_path / "corrupt")


@pytest.fixture
def native_runtime(tmp_path):
    import shutil
    import subprocess
    from types import SimpleNamespace
    from uuid import uuid4

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.mailbox.dag import StrictDagLedger
    from raven.mailbox.handoff import MailboxHandoff
    from raven.mailbox.store import MailboxStore

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "check.py").write_text(
        "from pathlib import Path\nimport sys\nsys.exit(0 if Path('answer.txt').read_text() == 'correct' else 1)\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    scope = {"task_id": "task", "workspace_id": "workspace"}
    store = MailboxStore(tmp_path / "a2a", authority_id=str(uuid4()))
    owner = store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[scope],
        allowed_kinds=["task.result"],
    )
    MailboxHandoff(store).create_task(**scope, owner_agent_id=owner.agent_id, request_id=str(uuid4()))
    provider = RepairProvider()
    native = RavenLoopBackend(provider=provider, model="fixture", agent_home=tmp_path / "home")
    tool = SubAgentDagTool(workspace=repo, session_dir=lambda _: tmp_path / "session")
    tool.registry.set_builtin_builder(lambda row, narrowed: native)
    from raven.agent.subagent.dag_strict import StrictDagRuntime, executor_identity

    runtime = StrictDagRuntime(
        StrictDagLedger(store, upgrade=True),
        tool,
        executor_identity=executor_identity(),
        repository_for=lambda repo_id, session: repo,
        executable_for=lambda command: __import__("pathlib").Path(shutil.which(command)).resolve(),
    )
    binding = SimpleNamespace(ref=owner, scope=scope, session_key="session")
    return runtime, tool, binding, provider, repo, commit


class RepairProvider:
    def __init__(self):
        self.calls = []
        self.attempts = 0

    async def chat_with_retry(self, *, messages, tools, model):
        self.calls.append((list(messages), tools))
        if messages[-1]["role"] == "user":
            self.attempts += 1
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="write",
                        name="write_file",
                        arguments={"path": "answer.txt", "content": "wrong" if self.attempts == 1 else "correct"},
                    )
                ],
            )
        return LLMResponse(content="TEST_PASSED")


@pytest.mark.asyncio
async def test_runtime_uses_admitted_results_real_checks_and_saved_repairs(native_runtime):
    import sys
    from uuid import uuid4

    runtime, tool, binding, provider, repo, commit = native_runtime
    root = await runtime.create(
        binding,
        request_id=str(uuid4()),
        history_root=None,
        session_key="session",
        graph={
            "task_summary": "Fix answer",
            "nodes": [
                {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"}
            ],
        },
        revision_selector={"repo_id": "workspace", "ref": commit},
        node_policies=[
            {
                "logical_node_id": "A",
                "depends_on": [],
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [
                    {
                        "check_id": "answer",
                        "argv": [sys.executable, "check.py"],
                        "cwd": ".",
                        "timeout_seconds": 10,
                        "repairable_exit_codes": [1],
                        "nonrepairable_exit_codes": [],
                    }
                ],
                "subjective_review": False,
            }
        ],
    )
    (repo / "check.py").write_text('raise RuntimeError("MUTABLE_CHECK")')
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    task = tool._runs[root.run_id]
    await task
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert [a.state for a in view.attempts] == ["failed", "accepted"]
    assert [a.check_runs[0].observation.exit_code for a in view.attempts] == [1, 0]
    assert provider.attempts == 2
    assert view.attempts[0].repair_count == 1
    assert all(a.result is not None for a in view.attempts)
    from pathlib import Path

    history = await tool.read_run(root.run_id, "session")
    physical = history["files"][0]["node"]
    nodes = Path(tool._nodes_root("session"))
    assert "wrong" in (nodes / f"{physical}.attempt-1.transcript.jsonl").read_text()
    assert "correct" in (nodes / f"{physical}.attempt-2.transcript.jsonl").read_text()
    assert "correct" in (nodes / f"{physical}.transcript.jsonl").read_text()
    assert (nodes / f"{physical}.attempt-1.prompt.md").is_file()
    assert (nodes / f"{physical}.attempt-2.out.md").read_text() == "TEST_PASSED"
    assert all(
        {t["function"]["name"] for t in tools} == {"read_file", "write_file", "edit_file", "list_dir"}
        for _, tools in provider.calls
    )
    assert "MUTABLE_CHECK" not in str(provider.calls)
    assert "TEST_PASSED" in str(provider.calls[2][0])
    assert "exit_code" in str(provider.calls[2][0])
    with runtime.ledger.db.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM messages WHERE json_extract(CAST(envelope_bytes AS TEXT), '$.kind')='task.request'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM messages WHERE json_extract(CAST(envelope_bytes AS TEXT), '$.kind')='task.result' AND phase='completed'"
            ).fetchone()[0]
            == 2
        )


def test_actual_zombie_executor_is_dead_before_reaping():
    import subprocess
    import sys
    import time
    from dataclasses import replace
    from pathlib import Path

    from raven.agent.subagent.dag_strict import _start_ticks, executor_identity, observe_process

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.1)"])
    identity = replace(executor_identity(), pid=process.pid, process_start_ticks=_start_ticks(process.pid))
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        assert observe_process(identity).state == "dead"
    finally:
        process.wait()


@pytest.mark.asyncio
async def test_create_rejects_unsafe_request_before_materialization(native_runtime):
    runtime, _, binding, _, _, commit = native_runtime
    with pytest.raises(__import__("raven.contracts.mailbox", fromlist=["MailboxError"]).MailboxError):
        await runtime.create(
            binding,
            request_id="../escape",
            history_root=None,
            session_key="session",
            graph={},
            revision_selector={"repo_id": "workspace", "ref": commit},
            node_policies=[],
        )


@pytest.mark.asyncio
async def test_native_review_blocks_successor_until_typed_host_approval(native_runtime):
    import sys
    from uuid import uuid4

    runtime, tool, binding, provider, _, commit = native_runtime
    provider.attempts = 1
    graph = {
        "task_summary": "Review predecessor",
        "nodes": [
            {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"},
            {
                "id": "B",
                "subagent": "Raven",
                "node_summary": "Read accepted answer",
                "prompt_template": "Read {{A.output}} and {{ref:answer.txt}}",
                "depends_on": ["A"],
            },
        ],
    }
    check = {
        "check_id": "answer",
        "argv": [sys.executable, "check.py"],
        "cwd": ".",
        "timeout_seconds": 10,
        "repairable_exit_codes": [1],
    }
    root = await runtime.create(
        binding,
        request_id=str(uuid4()),
        history_root=None,
        session_key="session",
        graph=graph,
        revision_selector={"repo_id": "workspace", "ref": commit},
        node_policies=[
            {
                "logical_node_id": n,
                "depends_on": [] if n == "A" else ["A"],
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [check],
                "subjective_review": n == "A",
            }
            for n in ("A", "B")
        ],
    )
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert len(view.attempts) == 1
    attempt = view.attempts[0]
    assert attempt.human_reason == "subjective_review"
    assert not tool.resolve_node(root.run_id, "A", "accept", None)
    await runtime.resolve(
        binding,
        root_id=root.root_id,
        attempt_id=attempt.attempt_id,
        expected_owner_epoch=view.root.owner_epoch,
        request_id=str(uuid4()),
        resolution={
            "action": "subjective_approve",
            "snapshot_fingerprint": attempt.snapshot.fingerprint,
            "decision_note": "Approved candidate",
        },
    )
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert [a.state for a in view.attempts] == ["accepted", "accepted"]
    assert "TEST_PASSED" in str(provider.calls[-2][0])
    assert "correct" in str(provider.calls[-2][0])
    assert "Upstream memory" not in str(provider.calls[-2][0])


@pytest.mark.asyncio
async def test_strict_reader_rebuilds_projection_and_ignores_mutable_latest(native_runtime):
    import shutil
    import sys
    from pathlib import Path
    from uuid import uuid4

    runtime, tool, binding, provider, _, commit = native_runtime
    provider.attempts = 1
    root = await runtime.create(
        binding,
        request_id=str(uuid4()),
        history_root=None,
        session_key="session",
        graph={
            "task_summary": "Answer",
            "nodes": [
                {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"}
            ],
        },
        revision_selector={"repo_id": "workspace", "ref": commit},
        node_policies=[
            {
                "logical_node_id": "A",
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [
                    {
                        "check_id": "answer",
                        "argv": [sys.executable, "check.py"],
                        "cwd": ".",
                        "timeout_seconds": 10,
                        "repairable_exit_codes": [1],
                    }
                ],
            }
        ],
    )
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    run = await tool.read_run(root.run_id, "session")
    path = Path(run["files"][0]["output_file"])
    path.write_text("UNACCEPTED_LATEST")
    shutil.rmtree(run["dir"])
    run = await tool.read_run(root.run_id, "session")
    assert run["files"][0]["status"] == "completed"
    assert Path(run["files"][0]["output_file"]).read_text() == "TEST_PASSED"
    assert not list(Path(run["dir"]).parent.glob("strict_*"))
    shutil.rmtree(tool._history_root("session"))
    assert root.run_id in await tool.session_run_ids("session")
    restored = await tool.read_run(root.run_id, "session")
    assert restored["strict"]["attempts"][0]["state"] == "accepted"
    assert restored["files"][0]["status"] == "completed"

    with pytest.raises(Exception):
        await tool.read_run(root.run_id, "other-session")


@pytest.mark.asyncio
async def test_strict_protected_deletion_cannot_be_admitted(native_runtime, monkeypatch):
    import sys
    from uuid import uuid4

    runtime, tool, binding, provider, _, commit = native_runtime
    native = tool.registry.backend("Raven")
    original = native.run

    async def delete_checker(*args, **kwargs):
        result = await original(*args, **kwargs)
        (kwargs["workspace"] / "check.py").unlink()
        return result

    monkeypatch.setattr(native, "run", delete_checker)
    root = await runtime.create(
        binding,
        request_id=str(uuid4()),
        history_root=None,
        session_key="session",
        graph={
            "task_summary": "Answer",
            "nodes": [
                {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"}
            ],
        },
        revision_selector={"repo_id": "workspace", "ref": commit},
        node_policies=[
            {
                "logical_node_id": "A",
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [
                    {
                        "check_id": "answer",
                        "argv": [sys.executable, "check.py"],
                        "cwd": ".",
                        "timeout_seconds": 10,
                        "repairable_exit_codes": [1],
                    }
                ],
            }
        ],
    )
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    attempt = runtime.ledger.status(root.root_id, scope=binding.scope).attempts[0]
    assert attempt.state == "unknown"
    assert attempt.staged_envelope_bytes is None
    assert provider.attempts == 1


@pytest.mark.asyncio
async def test_shared_instance_request_fails_before_any_native_call(native_runtime):
    import sys
    from uuid import uuid4

    from raven.contracts.mailbox import MailboxError

    runtime, _, binding, provider, _, commit = native_runtime
    with pytest.raises(MailboxError, match="strict_shared_instance_unsupported"):
        await runtime.create(
            binding,
            request_id=str(uuid4()),
            history_root=None,
            session_key="session",
            graph={
                "task_summary": "Shared history",
                "nodes": [
                    {
                        "id": "A",
                        "subagent": "Raven",
                        "node_summary": "Write answer",
                        "prompt_template": "Write answer.txt",
                        "instance": "shared",
                    }
                ],
            },
            revision_selector={"repo_id": "workspace", "ref": commit},
            node_policies=[
                {
                    "logical_node_id": "A",
                    "output_paths": ["answer.txt"],
                    "protected_paths": ["check.py"],
                    "checks": [
                        {"check_id": "answer", "argv": [sys.executable, "check.py"], "cwd": ".", "timeout_seconds": 10}
                    ],
                }
            ],
        )
    assert provider.calls == []


@pytest.mark.asyncio
async def test_strict_native_provider_error_is_execution_unknown(tmp_path):
    from raven.contracts.subagent_backend import SubagentNoAnswerError

    class ErrorProvider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="Provider unavailable", finish_reason="error")

    native = RavenLoopBackend(provider=ErrorProvider(), model="fixture", agent_home=tmp_path / "home")
    with pytest.raises(SubagentNoAnswerError):
        await native.run(
            "Write a candidate",
            task_id="attempt",
            workspace=tmp_path,
            executor=None,
            allowed_dirs=(tmp_path,),
            tools_allow=("read_file", "write_file", "edit_file", "list_dir"),
            mcps=[],
        )


async def _root_for_runtime(
    fixture, *, request=None, revision=None, graph=None, budget=3, subjective=False, subjective_nodes=()
):
    import sys
    from uuid import uuid4

    runtime, _, binding, _, _, commit = fixture
    graph = graph or {
        "task_summary": "Checked answer",
        "nodes": [
            {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"}
        ],
    }
    params = {
        "request_id": request or str(uuid4()),
        "history_root": None,
        "session_key": "session",
        "graph": graph,
        "revision_selector": {"repo_id": "workspace", "ref": revision or commit},
        "node_policies": [
            {
                "logical_node_id": n["id"],
                "depends_on": n.get("depends_on", []),
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [
                    {
                        "check_id": "answer",
                        "argv": [sys.executable, "check.py"],
                        "cwd": ".",
                        "timeout_seconds": 10,
                        "repairable_exit_codes": [1],
                    }
                ],
                "subjective_review": subjective or n["id"] in subjective_nodes,
            }
            for n in graph["nodes"]
        ],
        "max_auto_repairs": budget,
    }
    return await runtime.create(binding, **params), params


@pytest.mark.asyncio
async def test_create_replay_does_not_resolve_moved_branch_or_executable(native_runtime, monkeypatch):
    import subprocess

    from raven.contracts.mailbox import MailboxError

    runtime, _, binding, _, repo, _ = native_runtime
    subprocess.run(["git", "-C", str(repo), "branch", "trusted"], check=True)
    root, params = await _root_for_runtime(native_runtime, revision="refs/heads/trusted")
    (repo / "check.py").write_text('raise RuntimeError("changed")')
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-qm",
            "changed",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "-f", "trusted", "HEAD"], check=True)

    def forbid(*args):
        raise AssertionError("Exact replay must not reread mutable host inputs")

    monkeypatch.setattr(runtime, "_plan", forbid)
    assert await runtime.create(binding, **params) == root
    with pytest.raises(MailboxError, match="request_conflict"):
        await runtime.create(binding, **{**params, "max_auto_repairs": 4})


@pytest.mark.asyncio
async def test_host_replan_repins_checks_preserves_root_and_historical_graph(native_runtime):
    from dataclasses import asdict
    from uuid import uuid4

    runtime, tool, binding, provider, _, _ = native_runtime
    root, _ = await _root_for_runtime(native_runtime, budget=0)
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert view.attempts[0].human_reason == "repair_budget_exhausted"
    successor = dict(root.graph)
    successor["task_summary"] = "Replanned checked answer"
    plan = __import__("json").loads(__import__("json").dumps(asdict(root.verification_plan)))
    plan["nodes"][0]["checks"][0]["executable_sha256"] = "f" * 64
    request = str(uuid4())
    resolution = {
        "action": "replan",
        "successor_graph": successor,
        "successor_verification_plan": plan,
        "logical_node_mapping": {"A": "A"},
        "decision_note": "Use a replacement graph",
    }
    result = await runtime.resolve(
        binding,
        root_id=root.root_id,
        attempt_id=view.attempts[0].attempt_id,
        expected_owner_epoch=view.root.owner_epoch,
        request_id=request,
        resolution=resolution,
    )
    new = runtime.ledger.status(root.root_id, scope=binding.scope).root
    assert new.run_id != root.run_id
    assert new.max_auto_repairs == 0
    assert (
        new.verification_plan.nodes[0].checks[0].executable_sha256
        == root.verification_plan.nodes[0].checks[0].executable_sha256
    )
    assert (
        await runtime.resolve(
            binding,
            root_id=root.root_id,
            attempt_id=view.attempts[0].attempt_id,
            expected_owner_epoch=view.root.owner_epoch,
            request_id=request,
            resolution=resolution,
        )
        == result
    )
    await tool._runs[new.run_id]
    assert runtime.ledger.status(root.root_id, scope=binding.scope).attempts[-1].state == "accepted"
    old = await tool.read_run(root.run_id, "session")
    current = await tool.read_run(new.run_id, "session")
    assert old["task_summary"] == "Checked answer"
    assert current["task_summary"] == "Replanned checked answer"
    assert old["files"][0]["node"] != current["files"][0]["node"]
    refusal = await tool.prepare_replan(new.run_id, current["files"][0]["node"], [], "Bypass", "session", current)
    assert "host mailbox.dag.resolve" in refusal
    assert provider.attempts == 2


@pytest.mark.asyncio
async def test_final_snapshot_mutation_after_exit_zero_remains_human(native_runtime, monkeypatch):
    from uuid import uuid4

    from raven.agent.subagent.dag_strict import SnapshotVerifier

    runtime, tool, binding, provider, _, _ = native_runtime
    provider.attempts = 1
    root, _ = await _root_for_runtime(native_runtime)
    original = SnapshotVerifier.check

    async def mutate(self, *args, **kwargs):
        observation = await original(self, *args, **kwargs)
        (self.root / "answer.txt").write_text("mutated after observation")
        return observation

    monkeypatch.setattr(SnapshotVerifier, "check", mutate)
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert view.attempts[0].check_runs[0].observation.exit_code == 0
    assert view.attempts[0].state != "accepted"
    assert view.attempts[0].human_reason == "snapshot_changed"
    assert len(view.attempts) == 1


@pytest.mark.asyncio
async def test_diamond_waits_for_both_accepted_branches_and_ignores_latest(native_runtime):
    from pathlib import Path
    from uuid import uuid4

    runtime, tool, binding, provider, _, _ = native_runtime
    provider.attempts = 1
    graph = {
        "task_summary": "Diamond acceptance",
        "nodes": [
            {
                "id": n,
                "subagent": "Raven",
                "node_summary": "Write checked branch",
                "prompt_template": "Write answer.txt"
                if n == "A"
                else "Write answer.txt using "
                + (" {{A.output}}" if n in ("B", "C") else "{{B.output}} and {{C.output}} and {{ref:answer.txt}}"),
                "depends_on": [] if n == "A" else ["A"] if n in ("B", "C") else ["B", "C"],
            }
            for n in ("A", "B", "C", "D")
        ],
    }
    root, _ = await _root_for_runtime(native_runtime, graph=graph, subjective_nodes=("B",))
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    by_node = {a.logical_node_id: a for a in view.attempts}
    assert set(by_node) == {"A", "B", "C"}
    assert by_node["B"].human_reason == "subjective_review"
    assert by_node["C"].state == "accepted"
    projection = await tool.read_run(root.run_id, "session")
    completed = next(e for e in projection["files"] if e["status"] == "completed")
    Path(completed["output_file"]).write_text("UNACCEPTED_LATEST")
    await runtime.resolve(
        binding,
        root_id=root.root_id,
        attempt_id=by_node["B"].attempt_id,
        request_id=str(uuid4()),
        expected_owner_epoch=view.root.owner_epoch,
        resolution={
            "action": "subjective_approve",
            "snapshot_fingerprint": by_node["B"].snapshot.fingerprint,
            "decision_note": "Approve checked branch",
        },
    )
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert len(view.attempts) == 4
    assert all(a.state == "accepted" for a in view.attempts)
    assert "UNACCEPTED_LATEST" not in str(provider.calls[-2][0])
    assert "correct" in str(provider.calls[-2][0])
    assert provider.attempts == 5


@pytest.mark.asyncio
async def test_ordinary_native_provider_error_retains_existing_result(tmp_path):
    class ErrorProvider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="Provider unavailable", finish_reason="error")

    native = RavenLoopBackend(provider=ErrorProvider(), model="fixture", agent_home=tmp_path / "home")
    assert await native.run("Task", task_id="ordinary", workspace=tmp_path, executor=None) == "Provider unavailable"


@pytest.mark.asyncio
async def test_omitted_exit_classification_does_not_authorize_repairs(native_runtime):
    from uuid import uuid4

    runtime, tool, binding, provider, _, _ = native_runtime
    _, params = await _root_for_runtime(native_runtime)
    params["request_id"] = str(uuid4())
    params["node_policies"][0]["checks"][0].pop("repairable_exit_codes")
    root = await runtime.create(binding, **params)
    assert root.verification_plan.nodes[0].checks[0].repairable_exit_codes == ()
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    assert len(view.attempts) == 1
    assert view.attempts[0].state == "failed"
    assert view.attempts[0].repair_count == 0
    assert provider.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [False, "write", "delete"])
async def test_accepted_output_path_is_readable_by_attempt_only_file_tools(native_runtime, mutate):
    from uuid import uuid4

    runtime, tool, binding, _, _, _ = native_runtime

    class PathProvider(RepairProvider):
        def __init__(self):
            super().__init__()
            self.attempts = 1
            self.read_result = None

        async def chat_with_retry(self, *, messages, tools, model):
            prompt = messages[-1].get("content", "")
            if messages[-1]["role"] == "user" and prompt.startswith("Read accepted path: "):
                self.calls.append((list(messages), tools))
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id="read-accepted",
                            name="read_file",
                            arguments={"path": prompt.removeprefix("Read accepted path: ").strip()},
                        )
                    ],
                )
            if messages[-1]["role"] == "tool" and messages[-1].get("tool_call_id") == "read-accepted":
                self.calls.append((list(messages), tools))
                self.read_result = messages[-1]["content"]
                if mutate:
                    path = messages[-2]["tool_calls"][0]["function"]["arguments"]
                    if mutate == "delete":
                        from pathlib import Path

                        Path(__import__("json").loads(path)["path"]).unlink()
                        return LLMResponse(content="READ_ACCEPTED")
                    return LLMResponse(
                        content=None,
                        tool_calls=[
                            ToolCallRequest(
                                id="mutate-input",
                                name="write_file",
                                arguments={"path": __import__("json").loads(path)["path"], "content": "UNACCEPTED"},
                            )
                        ],
                    )
                return LLMResponse(content="READ_ACCEPTED")
            if messages[-1]["role"] == "tool" and messages[-1].get("tool_call_id") == "mutate-input":
                return LLMResponse(content="READ_ACCEPTED")
            return await super().chat_with_retry(messages=messages, tools=tools, model=model)

    provider = PathProvider()
    tool.registry.set_builtin_builder(
        lambda row, narrowed: RavenLoopBackend(
            provider=provider, model="fixture", agent_home=tool._workspace.parent / "home"
        )
    )
    graph = {
        "task_summary": "Read accepted path",
        "nodes": [
            {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"},
            {
                "id": "B",
                "subagent": "Raven",
                "node_summary": "Read predecessor",
                "prompt_template": "Read accepted path: {{A.output_path}}",
                "depends_on": ["A"],
            },
        ],
    }
    root, _ = await _root_for_runtime(native_runtime, graph=graph)
    await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
    await tool._runs[root.run_id]
    assert "TEST_PASSED" in provider.read_result
    assert "outside allowed directories" not in provider.read_result
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    consumer = next(a for a in view.attempts if a.logical_node_id == "B")
    if mutate:
        assert consumer.result is None
        assert consumer.state == "unknown"
    else:
        assert consumer.state == "accepted"
        refs = [entry for entry in consumer.snapshot.entries if entry.path.startswith(".raven-inputs/")]
        assert len(refs) == 1
        with runtime.ledger.db.connection() as conn:
            pins = conn.execute(
                "SELECT artifact_refs FROM strict_attempts WHERE attempt_id=?", (consumer.attempt_id,)
            ).fetchone()[0]
        assert refs[0].sha256 in __import__("json").loads(pins)
