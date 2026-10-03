"""Exercise strict native DAG process recovery and optional Codex OAuth acceptance."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_subagent_dag_strict import native_runtime as native_runtime

_CHILD = r"""
import asyncio, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from raven.agent.subagent.dag_strict import StrictDagRuntime, executor_identity
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.contracts.mailbox import MailboxInstanceRef
from raven.mailbox.store import MailboxStore
from raven.mailbox.dag import StrictDagLedger
from tests.test_subagent_dag_strict import RepairProvider
config=json.loads(Path(sys.argv[1]).read_text())
store=MailboxStore(Path(config['mailbox']), authority_id=config['authority_id'])
ledger=StrictDagLedger(store)
tool=SubAgentDagTool(workspace=Path(config['repo']), session_dir=lambda _:Path(config['session']))
provider=RepairProvider()
provider.attempts=config.get('provider_seed',0)
backend=RavenLoopBackend(provider=provider,model='fixture',agent_home=Path(config['home']))
if config['window']=='native_response_lost':
    original_run=backend.run
    async def lose_response(*args,**kwargs):
        await original_run(*args,**kwargs)
        Path(config['marker']).write_text('native_return_lost')
        os.kill(os.getpid(),signal.SIGKILL)
    backend.run=lose_response
tool.registry.set_builtin_builder(lambda row,narrowed:backend)
binding=SimpleNamespace(ref=MailboxInstanceRef(**config['owner']),scope=config['scope'],session_key='session')
runtime=StrictDagRuntime(ledger,tool,executor_identity=executor_identity(),repository_for=lambda *_:Path(config['repo']),executable_for=lambda executable:Path(executable).resolve())
original=getattr(ledger,config.get('method',config['window'])) if config['window']!='native_response_lost' else None
def crash(*args,**kwargs):
    value=original(*args,**kwargs)
    Path(config['marker']).write_text('committed')
    os.kill(os.getpid(),signal.SIGKILL)
if original is not None:
    setattr(ledger,config.get('method',config['window']),crash)
async def run():
    await runtime.start(binding,root_id=config['root_id'],request_id=config['request_id'],expected_owner_epoch=0)
    await tool._runs[config['run_id']]
asyncio.run(run())
"""


async def _create(runtime, binding, commit, *, graph=None, subjective=False):
    check = {
        "check_id": "answer",
        "argv": [sys.executable, "check.py"],
        "cwd": ".",
        "timeout_seconds": 30,
        "repairable_exit_codes": [1],
    }
    graph = graph or {
        "task_summary": "Produce checked answer",
        "nodes": [
            {"id": "A", "subagent": "Raven", "node_summary": "Write answer", "prompt_template": "Write answer.txt"}
        ],
    }
    return await runtime.create(
        binding,
        request_id=str(uuid4()),
        history_root=None,
        session_key="session",
        graph=graph,
        revision_selector={"repo_id": "workspace", "ref": commit},
        node_policies=[
            {
                "logical_node_id": node["id"],
                "depends_on": node.get("depends_on", []),
                "output_paths": ["answer.txt"],
                "protected_paths": ["check.py"],
                "checks": [check],
                "subjective_review": subjective,
            }
            for node in graph["nodes"]
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window",
    [
        "mark_submitting",
        "native_response_lost",
        "stage_result",
        "record_result",
        "decide",
        "accepted_decide",
        "successor_intent",
    ],
)
async def test_owned_sigkill_native_runtime_preserves_submission_and_evidence(native_runtime, tmp_path, window):
    runtime, tool, binding, provider, repo, commit = native_runtime
    graph = None
    if window == "successor_intent":
        graph = {
            "task_summary": "Accept then release successor",
            "nodes": [
                {
                    "id": "A",
                    "subagent": "Raven",
                    "node_summary": "Produce accepted answer",
                    "prompt_template": "Write answer.txt",
                },
                {
                    "id": "B",
                    "subagent": "Raven",
                    "node_summary": "Use accepted predecessor",
                    "depends_on": ["A"],
                    "prompt_template": "Accepted predecessor: {{A.output}}. Preserve correct answer.txt.",
                },
            ],
        }
    root = await _create(runtime, binding, commit, graph=graph)
    config = {
        "mailbox": str(runtime.ledger.store.root),
        "authority_id": binding.ref.authority_id,
        "repo": str(repo),
        "session": str(tool._session_dir_for("session")),
        "home": str(tmp_path / "home"),
        "owner": binding.ref.model_dump(),
        "scope": binding.scope,
        "root_id": root.root_id,
        "run_id": root.run_id,
        "request_id": str(uuid4()),
        "marker": str(tmp_path / "killed"),
        "window": window,
    }
    if window in ("accepted_decide", "successor_intent"):
        config["provider_seed"] = 1
        config["method"] = "decide"
    script, parameters = tmp_path / "child.py", tmp_path / "parameters.json"
    script.write_text(_CHILD)
    parameters.write_text(json.dumps(config))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(parameters),
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == -9, error.decode()
    assert (tmp_path / "killed").read_text() == (
        "native_return_lost" if window == "native_response_lost" else "committed"
    )
    view = runtime.ledger.status(root.root_id, scope=binding.scope)
    successor = view.prepared_intents[0] if window == "successor_intent" else None
    original_result = view.attempts[0].result
    if window not in ("mark_submitting", "native_response_lost"):
        provider.attempts = 1
    before = provider.attempts
    await runtime.recover(
        binding,
        root_id=root.root_id,
        request_id=str(uuid4()),
        expected_owner_epoch=view.root.owner_epoch,
        expected_executor_id=view.root.executor.executor_id,
        replace_owner=True,
    )
    await tool._runs[root.run_id]
    recovered = runtime.ledger.status(root.root_id, scope=binding.scope)
    if window in ("mark_submitting", "native_response_lost"):
        assert recovered.attempts[0].state == "unknown"
        assert recovered.attempts[0].result is None
        assert provider.attempts == before
    elif window == "accepted_decide":
        assert recovered.attempts[0].state == "accepted"
        assert recovered.attempts[0].result == original_result
        assert recovered.attempts[0].repair_count == 0
        assert len(recovered.attempts) == 1 and provider.attempts == before
        assert recovered.root.state == "accepted"
    elif window == "successor_intent":
        assert [(a.logical_node_id, a.state) for a in recovered.attempts] == [("A", "accepted"), ("B", "accepted")]
        assert recovered.attempts[0].result == original_result
        assert recovered.attempts[1].attempt_id == successor.attempt_id
        with runtime.ledger.db.connection() as conn:
            assert runtime.ledger._attempt_intent(conn, successor.attempt_id).dispatch_id == successor.dispatch_id
        assert provider.attempts == before + 1 and recovered.root.state == "accepted"
    else:
        assert recovered.attempts[0].result is not None
        assert recovered.attempts[-1].state == "accepted"
        assert recovered.attempts[-1].repair_count == 1
        assert len(recovered.attempts) == 2
        assert provider.attempts == before + 1


def _private_codex_auth(destination):
    import base64
    import time

    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    data = json.loads(source.read_text())
    tokens = data.get("tokens", data)
    required = ("access_token", "refresh_token", "id_token", "account_id")
    if any(not isinstance(tokens.get(key), str) or not tokens[key] for key in required):
        raise RuntimeError("Existing Codex OAuth credential is unavailable")
    payload = tokens["access_token"].split(".")[1]
    expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    if expiry <= time.time() + 900:
        raise RuntimeError("Existing credential needs refresh before the closed native test")
    target = destination / "auth.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({**{key: tokens[key] for key in required}, "expires_at": expiry}, output)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RAVEN_STRICT_NATIVE_ACCEPTANCE") != "1", reason="Requires reviewed native Codex OAuth harness"
)
async def test_native_codex_repairs_checked_candidate_then_releases_successor(native_runtime, tmp_path, monkeypatch):
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
    from raven.contracts.llm_provider import GenerationSettings
    from raven.providers.openai_codex_provider import OpenAICodexProvider

    runtime, tool, binding, _, repo, commit = native_runtime
    auth = tmp_path / "private-auth"
    auth.mkdir(mode=0o700)
    _private_codex_auth(auth)
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(auth))
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "auth.json")
    calls = []
    dependency_reads = []
    provider = OpenAICodexProvider(default_model="gpt-6-luna")
    provider.generation = GenerationSettings(reasoning_effort="max", timeout=120, stream_idle_timeout=60)
    original = provider.chat

    async def observed(*args, **kwargs):
        names = {tool["function"]["name"] for tool in kwargs.get("tools", []) or []}
        assert names <= {"read_file", "write_file", "edit_file", "list_dir"}
        assert kwargs.get("reasoning_effort") == "max"
        assert kwargs.get("model") == "gpt-6-luna"
        messages = kwargs.get("messages", args[0] if args else [])
        for message in messages:
            if message.get("role") == "tool" and message.get("name") == "read_file":
                content = str(message.get("content", ""))
                if "TEST_PASSED" in content:
                    dependency_reads.append(content)
        calls.append({"tools": sorted(names), "reasoning_effort": "max"})
        return await original(*args, **kwargs)

    monkeypatch.setattr(provider, "chat", observed)
    native = RavenLoopBackend(
        provider=provider, model="gpt-6-luna", agent_home=tmp_path / "native-home", skills_allow=[]
    )
    tool.registry.set_builtin_builder(lambda row, narrowed: native)
    graph = {
        "task_summary": "Verify a native repair and accepted dependency",
        "nodes": [
            {
                "id": "A",
                "subagent": "Raven",
                "node_summary": "Write controlled candidate",
                "prompt_template": "This is a controlled verification exercise. On your initial attempt, write exactly wrong to answer.txt using write_file, and return TEST_PASSED. When the host appends Fixed check feedback after an earlier candidate, repair answer.txt so check.py passes, then return TEST_PASSED. Do not execute checks yourself.",
            },
            {
                "id": "B",
                "subagent": "Raven",
                "node_summary": "Use accepted predecessor",
                "depends_on": ["A"],
                "prompt_template": "Accepted predecessor answer: {{ref:answer.txt}}. Accepted predecessor report: {{A.output}}. Use read_file to read the accepted predecessor report file at {{A.output_path}} and confirm TEST_PASSED in its content. Read answer.txt and preserve exactly correct in it. Return DEPENDENCY_ACCEPTED.",
            },
        ],
    }
    try:
        root = await _create(runtime, binding, commit, graph=graph)
        (repo / "check.py").write_text("raise RuntimeError('MUTABLE_CHECK_MUST_NOT_RUN')")
        await runtime.start(binding, root_id=root.root_id, request_id=str(uuid4()), expected_owner_epoch=0)
        await asyncio.wait_for(tool._runs[root.run_id], 600)
        view = runtime.ledger.status(root.root_id, scope=binding.scope)
        assert [(a.logical_node_id, a.state) for a in view.attempts] == [
            ("A", "failed"),
            ("A", "accepted"),
            ("B", "accepted"),
        ]
        assert [a.check_runs[0].observation.exit_code for a in view.attempts] == [1, 0, 0]
        assert view.attempts[1].repair_count == 1
        assert all(a.result for a in view.attempts)
        assert dependency_reads, "native successor must actually read the accepted output_path artifact"
        assert calls and len(calls) <= 12
        print(
            json.dumps(
                {
                    "root_id": root.root_id,
                    "model": "gpt-6-luna",
                    "reasoning_effort": "max",
                    "native_model_calls": len(calls),
                    "accepted_output_path_read_results": dependency_reads,
                    "attempts": [
                        (a.logical_node_id, a.state, a.check_runs[0].observation.exit_code) for a in view.attempts
                    ],
                    "host_bridge": "admitted task.result",
                    "max_auto_repairs": view.root.max_auto_repairs,
                }
            )
        )
    finally:
        tool.abort_run(root.run_id if "root" in locals() else "")
        await asyncio.gather(*list(tool._runs.values()), return_exceptions=True)
        (auth / "auth.json").unlink(missing_ok=True)
