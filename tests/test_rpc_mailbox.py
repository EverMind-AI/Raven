"""Authenticated receiver RPC rejects identity and method escapes."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import ClientSession, web

from raven.rpc import connection
from raven.rpc.dispatcher import Dispatcher
from tests.test_mailbox_handoff import handoff as handoff
from tests.test_mailbox_receiver import grant
from tests.test_mailbox_receiver import receivers as receivers
from tests.test_mailbox_store import NOW, wire
from tests.test_mailbox_store import mailbox as mailbox
from tests.test_subagent_dag_strict import native_runtime as native_runtime


@pytest.fixture
def strict_rpc(rpc):
    dispatcher, service, _, _, methods = rpc
    scope = {"task_id": "task", "workspace_id": "workspace"}
    owner = service.store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[scope, {"task_id": "other", "workspace_id": "workspace"}],
        allowed_kinds=["task.result"],
    )
    service.registry.register("worker", kind_ref="worker", task_ref="task", session_key="native-session")
    issued = service.grant("worker", owner, scope, request_id=str(uuid4()))
    binding = service.authenticate(issued["credential"])
    return dispatcher, service, binding, methods


@pytest.mark.parametrize("subjective_review", [False, True])
@pytest.mark.parametrize("resume_before_start", [False, True])
async def test_strict_dag_rpc_runs_registered_native_tool_and_replays_without_work(
    native_runtime, tmp_path, monkeypatch, subjective_review, resume_before_start
):
    import subprocess
    import sys

    from raven.agent.registry.identity import IdentityRegistry
    from raven.mailbox.receiver import ReceiverService
    from raven.rpc.bootstrap import build_rpc_stack
    from tests.test_rpc_bootstrap import _FakeLoop, _sink

    runtime, tool, owner, provider, repo, commit = native_runtime
    with runtime.ledger.db.connection(write=True) as conn:
        for table in ("strict_dispatch_outbox", "strict_attempts", "strict_nodes", "strict_roots"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=2")
    registry = IdentityRegistry(tmp_path / "identity.json", config_rows=lambda: [{"name": "raven", "kind": "raven"}])
    registry.register("raven", kind_ref="raven", task_ref="task", session_key="session")
    service = ReceiverService(runtime.ledger.store, registry)
    issued = service.grant("raven", owner.ref, owner.scope, request_id=str(uuid4()))
    binding = service.authenticate(issued["credential"])
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "cold-bootstrap"))
    loop = _FakeLoop()
    loop.tools["run_subagent_dag"] = tool
    loop.peek_session_workdir = lambda session: repo
    stack = await build_rpc_stack(_sink, agent_loop=loop, channel="acp")
    try:
        dispatcher = stack.dispatcher
        methods = dispatcher.mailbox_receivers
        methods._receivers = service
        methods._dag_ledger = runtime.ledger
        runtime = methods.dag_runtime()
        assert runtime.tool is loop.tools.get("run_subagent_dag")
        assert tool._strict_runtime_factory() is runtime
        unavailable = await call(dispatcher, "mailbox.dag.status", {"root_id": str(uuid4())}, binding=binding)
        assert unavailable["error"]["data"]["code"] == "receiver_capability_unavailable"
        create = {
            "binding_id": binding.binding_id,
            "request_id": str(uuid4()),
            "session_key": "session",
            "graph": {
                "task_summary": "Fix answer",
                "nodes": [
                    {
                        "id": "A",
                        "subagent": "Raven",
                        "node_summary": "Write answer",
                        "prompt_template": "Write answer.txt",
                    }
                ],
            },
            "revision_selector": {
                "repo_id": "workspace",
                "ref": subprocess.check_output(["git", "-C", str(repo), "symbolic-ref", "HEAD"], text=True).strip(),
            },
            "node_policies": [
                {
                    "logical_node_id": "A",
                    "output_paths": ["answer.txt"],
                    "protected_paths": ["check.py"],
                    "subjective_review": subjective_review,
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
        }
        invalid = await call(dispatcher, "mailbox.dag.create", {**create, "graph": {}}, admin=True)
        assert "error" in invalid
        invalid_policy = await call(
            dispatcher,
            "mailbox.dag.create",
            {
                **create,
                "node_policies": [{**create["node_policies"][0], "checks": [], "subjective_review": False}],
            },
            admin=True,
        )
        assert invalid_policy["error"]["data"]["code"] == "invalid_verification_plan"
        with runtime.ledger.db.connection() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        created = await call(dispatcher, "mailbox.dag.create", create, admin=True)
        assert "error" not in created, created
        with runtime.ledger.db.connection() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        root = created["result"]["data"]
        assert root["verification_plan"]["revision_commit"] == commit
        (repo / "check.py").write_text('raise RuntimeError("MUTABLE_CHECK")')
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
                "advance",
            ],
            check=True,
        )
        replay_create = await call(dispatcher, "mailbox.dag.create", create, admin=True)
        assert replay_create == created
        if resume_before_start:
            resumed = service.store.resume(binding.ref, request_id=str(uuid4()))
            binding = service.authenticate(
                service.grant("raven", resumed, binding.scope, request_id=str(uuid4()))["credential"]
            )
        start = {
            "binding_id": binding.binding_id,
            "root_id": root["root_id"],
            "request_id": str(uuid4()),
            "expected_owner_epoch": 0,
        }
        started = await call(dispatcher, "mailbox.dag.start", start, admin=True)
        assert "error" not in started, started
        assert started["result"]["data"]["root"]["task_owner_ref"] == binding.ref.model_dump()
        assert started["result"]["data"]["root"]["owner_epoch"] == 1
        await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        status = await call(dispatcher, "mailbox.dag.status", {"root_id": root["root_id"]}, binding=binding)
        assert "error" not in status, status
        view = status["result"]["data"]
        assert [attempt["state"] for attempt in view["attempts"]] == [
            "failed",
            "review" if subjective_review else "accepted",
        ]
        assert [attempt["check_runs"][0]["observation"]["exit_code"] for attempt in view["attempts"]] == [1, 0]
        assert provider.attempts == 2
        assert "staged_envelope_bytes" not in str(view)
        assert "submit_now" not in str(view)
        assert "MUTABLE_CHECK" not in str(provider.calls)
        if subjective_review:
            final = view["attempts"][-1]
            resolution = {
                "binding_id": binding.binding_id,
                "root_id": root["root_id"],
                "attempt_id": final["attempt_id"],
                "request_id": str(uuid4()),
                "expected_owner_epoch": view["root"]["owner_epoch"],
                "resolution": {
                    "action": "subjective_approve",
                    "snapshot_fingerprint": final["snapshot"]["fingerprint"],
                    "decision_note": "Inspected the pinned candidate and recorded checks",
                },
            }
            approved = await call(dispatcher, "mailbox.dag.resolve", resolution, admin=True)
            assert "error" not in approved, approved
            assert approved["result"]["data"]["decision"] == "accepted"
            assert "staged_envelope_bytes" not in str(approved)
            if root["run_id"] in tool._runs:
                await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        recovered = await call(
            dispatcher,
            "mailbox.dag.recover",
            {
                "binding_id": binding.binding_id,
                "root_id": root["root_id"],
                "request_id": str(uuid4()),
                "expected_owner_epoch": view["root"]["owner_epoch"],
                "expected_executor_id": view["root"]["executor"]["executor_id"],
            },
            admin=True,
        )
        assert "error" not in recovered, recovered
        if root["run_id"] in tool._runs:
            await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        replayed = await call(dispatcher, "mailbox.dag.start", start, admin=True)
        assert "error" not in replayed, replayed
        if root["run_id"] in tool._runs:
            await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        assert provider.attempts == 2
        with runtime.ledger.db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM strict_attempts").fetchone()[0] == 2
            original_intents = [
                runtime.ledger._attempt_intent(conn, attempt["attempt_id"]) for attempt in view["attempts"]
            ]
        resumed = service.store.resume(binding.ref, request_id=str(uuid4()))
        current = service.authenticate(
            service.grant("raven", resumed, binding.scope, request_id=str(uuid4()))["credential"]
        )
        fenced_receiver = await call(dispatcher, "mailbox.dag.status", {"root_id": root["root_id"]}, binding=binding)
        assert "error" in fenced_receiver
        generation_recovery = {
            "binding_id": current.binding_id,
            "root_id": root["root_id"],
            "request_id": str(uuid4()),
            "expected_owner_epoch": view["root"]["owner_epoch"],
            "expected_executor_id": view["root"]["executor"]["executor_id"],
        }
        adopted = await call(dispatcher, "mailbox.dag.recover", generation_recovery, admin=True)
        assert "error" not in adopted, adopted
        if root["run_id"] in tool._runs:
            await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        status = await call(dispatcher, "mailbox.dag.status", {"root_id": root["root_id"]}, binding=current)
        adopted_view = status["result"]["data"]
        assert adopted_view["root"]["task_owner_ref"] == resumed.model_dump()
        assert adopted_view["root"]["owner_epoch"] == view["root"]["owner_epoch"] + 1
        assert adopted_view["root"]["state"] == "accepted"
        assert [attempt["result"] for attempt in adopted_view["attempts"]] == [
            attempt["result"] for attempt in view["attempts"]
        ]
        assert provider.attempts == 2
        with runtime.ledger.db.connection() as conn:
            assert [
                runtime.ledger._attempt_intent(conn, attempt["attempt_id"]) for attempt in view["attempts"]
            ] == original_intents
        replay_adoption = await call(dispatcher, "mailbox.dag.recover", generation_recovery, admin=True)
        assert "error" not in replay_adoption, replay_adoption
        assert replay_adoption["result"]["data"]["root"]["owner_epoch"] == adopted_view["root"]["owner_epoch"]
        if root["run_id"] in tool._runs:
            await asyncio.wait_for(tool._runs[root["run_id"]], timeout=20)
        assert provider.attempts == 2
    finally:
        await stack.teardown()


def strict_params(binding, method):
    params = {"binding_id": binding.binding_id, "root_id": str(uuid4())}
    if method != "status":
        params.update(request_id=str(uuid4()), expected_owner_epoch=1)
    if method == "create":
        params.pop("root_id")
        params.pop("expected_owner_epoch")
        params.update(
            history_root="/private/new-root",
            session_key="native-session",
            graph={"nodes": []},
            revision_selector={"repo_id": "workspace", "ref": "refs/heads/main"},
            node_policies=[],
        )
    if method == "recover":
        params["expected_executor_id"] = str(uuid4())
    if method == "resolve":
        params.update(attempt_id=str(uuid4()), resolution={"action": "abandon", "decision_note": "Stop"})
    return params


@pytest.mark.parametrize("method", ["create", "start", "recover", "resolve"])
@pytest.mark.parametrize("principal", ["unbound", "receiver"])
async def test_strict_dag_mutations_require_host_principal(strict_rpc, method, principal):
    dispatcher, _, binding, _ = strict_rpc
    response = await call(
        dispatcher,
        f"mailbox.dag.{method}",
        strict_params(binding, method),
        binding=binding if principal == "receiver" else None,
    )
    error = response["error"]
    if principal == "receiver":
        assert error["message"] == "receiver_method_forbidden"
    else:
        assert error["data"]["code"] == "receiver_unauthorized"


@pytest.mark.parametrize("method", ["create", "start", "recover", "resolve"])
async def test_strict_dag_dispatch_awaits_runtime_on_host_loop(strict_rpc, method):
    dispatcher, _, binding, methods = strict_rpc
    loop = asyncio.get_running_loop()
    observed = []

    @dataclass
    class Saved:
        state: str
        staged_envelope_bytes: bytes = b"private candidate"
        submit_now: bool = True

    async def mutation(current, **params):
        assert asyncio.get_running_loop() is loop
        assert connection.current_state()["mailbox_admin"] is True
        assert current == binding
        observed.append(params)
        await asyncio.sleep(0)
        return {"root": Saved("prepared"), "intents": [Saved("submitting")]}

    methods.dag_factory = lambda: SimpleNamespace(**{method: mutation})
    response = await call(dispatcher, f"mailbox.dag.{method}", strict_params(binding, method), admin=True)
    assert response["result"]["data"] == {"root": {"state": "prepared"}, "intents": [{"state": "submitting"}]}
    assert len(observed) == 1
    assert "binding_id" not in observed[0]
    assert "executor_identity" not in observed[0]


async def test_strict_dag_status_uses_current_exact_scope_without_runtime(strict_rpc):
    from raven.contracts.mailbox import MailboxError

    dispatcher, _, binding, methods = strict_rpc
    root_id = str(uuid4())
    calls = []

    def status(requested, *, scope):
        calls.append((requested, scope))
        if requested != root_id:
            raise MailboxError("scope_denied")
        return {"root_id": requested, "state": "human"}

    methods._dag_ledger = SimpleNamespace(status=status)
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": root_id}, binding=binding)
    assert response["result"]["data"] == {"root_id": root_id, "state": "human"}
    assert calls == [(root_id, binding.scope)]
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": str(uuid4())}, binding=binding)
    assert response["error"]["data"]["code"] == "scope_denied"


async def test_strict_dag_status_rechecks_revoked_binding(strict_rpc):
    dispatcher, service, binding, methods = strict_rpc
    methods._dag_ledger = SimpleNamespace(status=lambda *_args, **_kwargs: pytest.fail("revoked read"))
    service.revoke(binding.binding_id)
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": str(uuid4())}, binding=binding)
    assert response["error"]["data"]["code"] == "receiver_revoked"


@pytest.mark.parametrize("forged", [{"executor_identity": {}}, {"scope": {}}, {"expected_owner_agent_id": "fake"}])
async def test_strict_dag_create_cannot_supply_host_identity(strict_rpc, forged):
    dispatcher, _, binding, methods = strict_rpc
    methods.dag_factory = lambda: pytest.fail("invalid create must not resolve runtime")
    response = await call(dispatcher, "mailbox.dag.create", {**strict_params(binding, "create"), **forged}, admin=True)
    assert response["error"]["data"]["code"] == "invalid_argument"


async def test_strict_dag_create_requires_bound_session(strict_rpc):
    dispatcher, _, binding, methods = strict_rpc
    params = strict_params(binding, "create")
    params["session_key"] = "other-session"
    methods.dag_factory = lambda: pytest.fail("wrong session must not resolve runtime")
    response = await call(dispatcher, "mailbox.dag.create", params, admin=True)
    assert response["error"]["data"]["code"] == "scope_denied"


@pytest.mark.parametrize(
    "resolution",
    [
        {"action": "accept", "exit_code": 0},
        {"action": "reconcile_verification", "check_run_id": str(uuid4()), "exit_code": 0},
        {"action": "abandon", "decision_note": "Stop", "scope": {}},
    ],
)
async def test_strict_dag_resolution_cannot_forge_objective_evidence(strict_rpc, resolution):
    dispatcher, _, binding, methods = strict_rpc
    methods.dag_factory = lambda: pytest.fail("forged evidence must not resolve runtime")
    params = strict_params(binding, "resolve")
    params["resolution"] = resolution
    response = await call(dispatcher, "mailbox.dag.resolve", params, admin=True)
    assert response["error"]["data"]["code"] == "invalid_argument"


async def test_strict_dag_resolution_requires_explicit_epoch(strict_rpc):
    dispatcher, _, binding, methods = strict_rpc
    params = strict_params(binding, "resolve")
    params.pop("expected_owner_epoch")
    response = await call(dispatcher, "mailbox.dag.resolve", params, admin=True)
    assert response["error"]["data"]["code"] == "invalid_argument"


async def test_strict_dag_schema_keeps_authority_and_check_observations_private():
    from raven.rpc.mailbox_models import MAILBOX_METHOD_MODELS

    create = MAILBOX_METHOD_MODELS["mailbox.dag.create"][0].model_json_schema()
    assert create["additionalProperties"] is False
    assert create["properties"]["max_auto_repairs"]["default"] == 3
    assert "executor_identity" not in create["properties"]
    assert "scope" not in create["properties"]
    check = create["$defs"]["MailboxDagCheck"]
    assert check["additionalProperties"] is False
    assert "executable_sha256" not in check["properties"]
    assert "exit_code" not in check["properties"]
    assert create["$defs"]["MailboxDagRevision"]["properties"]["repo_id"]["const"] == "workspace"


async def test_strict_dag_bootstrap_binds_registered_tool_lazily(monkeypatch, tmp_path):
    import sys

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.contracts.mailbox import MailboxError
    from raven.rpc.bootstrap import build_rpc_stack
    from tests.test_rpc_bootstrap import _FakeLoop, _sink

    captured = []
    identity = object()

    def runtime(ledger, tool, **kwargs):
        captured.append((ledger, tool, kwargs))
        return object()

    monkeypatch.setitem(
        sys.modules,
        "raven.agent.subagent.dag_strict",
        SimpleNamespace(StrictDagRuntime=runtime, executor_identity=lambda: identity),
    )
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "absent-mailbox"))
    loop = _FakeLoop()
    tool = SubAgentDagTool(workspace=tmp_path)
    loop.tools["run_subagent_dag"] = tool
    loop.peek_session_workdir = lambda session: tmp_path / session
    stack = await build_rpc_stack(_sink, agent_loop=loop, channel="acp")
    try:
        assert captured == []
        assert not (tmp_path / "absent-mailbox").exists()
        methods = stack.dispatcher.mailbox_receivers
        ledger = object()
        methods._dag_ledger = ledger
        assert tool._strict_runtime_factory() is methods.dag_runtime()
        assert methods.dag_runtime() is methods.dag_runtime()
        assert len(captured) == 1
        actual_ledger, actual_tool, policy = captured[0]
        assert actual_ledger is ledger
        assert actual_tool is tool
        assert policy["executor_identity"] is identity
        assert policy["repository_for"]("workspace", "session") == tmp_path / "session"
        with pytest.raises(MailboxError):
            policy["repository_for"]("/arbitrary/repo", "session")
        assert policy["executable_for"](sys.executable).is_absolute()
    finally:
        await stack.teardown()


async def test_strict_dag_status_reads_real_authority_and_denies_other_scope(strict_rpc, tmp_path):
    from raven.mailbox.dag import ExecutorIdentity, NodePolicy, SnapshotManifest, StrictDagLedger, VerificationPlan
    from raven.mailbox.handoff import MailboxHandoff
    from tests.test_mailbox_dag_runner import admitted_candidate

    dispatcher, service, binding, methods = strict_rpc
    ledger = StrictDagLedger(service.store, upgrade=True)
    MailboxHandoff(service.store, upgrade=False).create_task(
        **binding.scope, owner_agent_id=binding.ref.agent_id, request_id=str(uuid4())
    )
    baseline = SnapshotManifest("a" * 40, "b" * 40, "c" * 64, (), "d" * 64)
    policy = NodePolicy("review", (), (), (), (), subjective_review=True)
    plan = VerificationPlan(baseline.revision_commit, baseline.revision_tree, baseline, (policy,))
    root = ledger.create_root(
        task_owner_ref=binding.ref,
        scope=binding.scope,
        history_root=str(tmp_path / "fresh-history"),
        session_key=binding.session_key,
        graph={"nodes": [{"id": "review"}]},
        verification_plan=plan,
        request_id=str(uuid4()),
    )
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": root.root_id}, binding=binding)
    assert response["result"]["data"]["root"]["root_id"] == root.root_id
    assert response["result"]["data"]["root"]["max_auto_repairs"] == 3
    assert methods._dag_runtime is None
    fence = ledger.acquire_owner(
        root.root_id,
        task_owner_ref=binding.ref,
        executor=ExecutorIdentity(str(uuid4()), 123, "test-boot", 456),
        expected_owner_epoch=0,
        request_id=str(uuid4()),
    )
    intent, attempt = ledger.claim_node(fence, "review", run_id=root.run_id, request_id=str(uuid4()))
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": root.root_id}, binding=binding)
    assert "submit_now" not in response["result"]["data"]["prepared_intents"][0]
    ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
    admitted_candidate(ledger, fence, intent, attempt, admit=False)
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": root.root_id}, binding=binding)
    assert "staged_envelope_bytes" not in response["result"]["data"]["attempts"][0]
    response = await call(
        dispatcher, "mailbox.dag.status", {"root_id": root.root_id, "binding_id": binding.binding_id}, admin=True
    )
    assert response["result"]["data"]["root"]["run_id"] == root.run_id
    service.registry.register("worker", kind_ref="worker", task_ref="other", session_key="other-session")
    issued = service.grant(
        "worker", binding.ref, {"task_id": "other", "workspace_id": "workspace"}, request_id=str(uuid4())
    )
    other = service.authenticate(issued["credential"])
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": root.root_id}, binding=other)
    assert response["error"]["data"]["code"] == "scope_denied"


async def test_strict_dag_status_on_schema_two_is_unavailable_without_migration(strict_rpc):
    dispatcher, service, binding, _ = strict_rpc
    response = await call(dispatcher, "mailbox.dag.status", {"root_id": str(uuid4())}, binding=binding)
    assert response["error"]["data"]["code"] == "receiver_capability_unavailable"
    with service.store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


async def test_receiver_cannot_escape_dispatcher_method_allowlist(receivers):
    service, *_ = receivers
    binding = service.authenticate(grant(receivers)["credential"])
    dispatcher = Dispatcher()

    async def unsafe(params):
        return {"changed": True}

    dispatcher.register("agents.register", unsafe)
    token = connection.bind_connection()
    try:
        connection.current_state()["mailbox_binding_id"] = binding.binding_id
        response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.register"})
        assert response.get("error", {}).get("message") == "receiver_method_forbidden"
    finally:
        connection.unbind_connection(token)


@pytest.fixture
def rpc(receivers, monkeypatch):
    from raven.rpc.methods.mailbox import register_mailbox_methods

    monkeypatch.setattr("raven.mailbox.delivery.time.time", lambda: NOW)
    service, refs, *_ = receivers
    dispatcher = Dispatcher()
    methods = register_mailbox_methods(dispatcher, receivers=service)
    binding = service.authenticate(grant(receivers)["credential"])
    return dispatcher, service, refs, binding, methods


async def call(dispatcher, method, params=None, *, binding=None, admin=False):
    token = connection.bind_connection()
    try:
        if binding:
            connection.current_state()["mailbox_binding_id"] = binding.binding_id
        if admin:
            connection.current_state()["mailbox_admin"] = True
        return await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    finally:
        connection.unbind_connection(token)


async def test_unbound_and_declared_surfaces_cannot_issue_credentials(rpc):
    dispatcher, _, refs, _, _ = rpc
    params = {
        "agent_name": "worker",
        "ref": refs[1].model_dump(),
        "scope": {"task_id": "task", "workspace_id": "workspace"},
        "request_id": str(uuid4()),
    }
    response = await call(dispatcher, "mailbox.enroll", params)
    assert response["error"]["data"]["code"] == "receiver_unauthorized"


async def test_schema_one_admin_reads_do_not_upgrade_but_task_creation_does(mailbox, tmp_path):
    from raven.agent.registry.identity import IdentityRegistry
    from raven.mailbox.receiver import ReceiverService
    from raven.rpc.methods.mailbox import register_mailbox_methods
    from tests.test_mailbox_store import SCOPE

    store, sender, _ = mailbox
    registry = IdentityRegistry(tmp_path / "registry.json", config_rows=lambda: [])
    dispatcher = Dispatcher()
    register_mailbox_methods(dispatcher, receivers=ReceiverService(store, registry, upgrade=False))
    for method, params in [("mailbox.bindings", {}), ("mailbox.overview", SCOPE)]:
        response = await call(dispatcher, method, params, admin=True)
        assert response["error"]["data"]["code"] == "receiver_capability_unavailable"
        with store.db.connection() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    response = await call(
        dispatcher,
        "mailbox.handoff.create",
        {**SCOPE, "owner_agent_id": sender.agent_id, "request_id": str(uuid4())},
        admin=True,
    )
    assert response["result"]["data"]["owner_agent_id"] == sender.agent_id
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize(
    "forged",
    [{"agent_id": str(uuid4())}, {"task_id": "other"}, {"instance_id": str(uuid4())}, {"binding_id": str(uuid4())}],
)
async def test_receiver_parameters_cannot_override_binding(rpc, forged):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4()), **forged}, binding=binding)
    assert "error" in response
    assert service.store.status(refs[1].agent_id, sent["message_id"])["phase"] == "pending"


async def test_poll_ack_and_receipt_reuse_r1_kernel(rpc):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4())}, binding=binding)
    claim = response["result"]["data"]["claims"][0]
    response = await call(
        dispatcher,
        "mailbox.ack",
        {"claim": claim, "result": {"outcome": "succeeded", "summary": "Reviewed", "evidence": []}},
        binding=binding,
    )
    assert response["result"]["data"]["phase"] == "completed"
    assert service.store.status(refs[1].agent_id, sent["message_id"])["outcome"] == "succeeded"


async def test_revoked_connected_receiver_cannot_poll(rpc):
    dispatcher, service, _, binding, _ = rpc
    service.revoke(binding.binding_id)
    response = await call(dispatcher, "mailbox.poll", {"request_id": str(uuid4())}, binding=binding)
    assert response["error"]["data"]["code"] == "receiver_revoked"


async def test_status_checks_persisted_message_scope(rpc):
    dispatcher, service, refs, binding, _ = rpc
    sent = service.store.send(wire(*refs, scope={"task_id": "other", "workspace_id": "workspace"}), refs[0], now=NOW)
    response = await call(dispatcher, "mailbox.status", {"message_id": sent["message_id"]}, binding=binding)
    assert response["error"]["data"]["code"] == "scope_denied"


async def test_receiver_websocket_auth_and_broadcast_isolation(rpc):
    import asyncio

    from raven.rpc.transports.ws import WsGateway

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    gateway = WsGateway()
    gateway.dispatcher = dispatcher
    app = web.Application()
    app.router.add_get("/rpc", gateway.handle_ws)
    app.router.add_post("/auth/nonce", gateway.handle_mint_nonce)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as client:
            headers = {"X-Raven-Receiver": issued["credential"]}
            async with client.post(url + "/auth/nonce", headers=headers) as denied:
                assert denied.status == 401
            async with client.ws_connect(url + "/rpc", headers=headers) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "mailbox.poll", "params": {"peek": True}})
                assert (await ws.receive_json())["result"]["data"] == {"messages": []}
                await gateway.broadcast(
                    {"method": "clarify.request", "params": {"conversation_id": "another-task", "secret": "private"}}
                )
                with pytest.raises(asyncio.TimeoutError):
                    await ws.receive(timeout=0.05)
                service.revoke(issued["binding"]["binding_id"])
                await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "mailbox.poll", "params": {"peek": True}})
                assert (await ws.receive_json())["error"]["data"]["code"] == "receiver_revoked"
    finally:
        await runner.cleanup()


async def test_receiver_socket_auth_and_notification_isolation(rpc):
    import asyncio

    from raven.rpc.server import RpcServer
    from tests.test_rpc_server_socket import _read_one_frame, _send, _wire_paired_socket

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    listener, client, accepted, temporary = await _wire_paired_socket()
    server = RpcServer(dispatcher, sock=accepted, auth_token="only-the-human-transport")
    task = asyncio.create_task(server.serve_forever())
    try:
        await server.started.wait()
        await asyncio.get_running_loop().sock_sendall(client, (issued["credential"] + "\n").encode())
        await _send(client, "mailbox.poll", {"peek": True}, 1)
        assert b'"messages": []' in await _read_one_frame(client)
        await server.send_frame({"jsonrpc": "2.0", "method": "clarify.request", "params": {"secret": "other-task"}})
        assert await _read_one_frame(client, timeout=0.05) == b""
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client.close()
        listener.close()
        (temporary / "sock").unlink()
        temporary.rmdir()


async def test_overview_shows_outgoing_accept_only_after_verified_read(handoff, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from raven.agent.registry.identity import IdentityRegistry
    from raven.contracts.terminal import TerminalRecord
    from raven.mailbox.receiver import ReceiverService
    from raven.rpc.methods.mailbox import register_mailbox_methods
    from tests.test_mailbox_handoff import acceptance
    from tests.test_mailbox_store import SCOPE

    monkeypatch.setattr("raven.mailbox.handoff.time.time", lambda: NOW)
    core, store, sender, receiver, offer, sha, _ = handoff
    record = TerminalRecord(worktree_id="workspace", worktree_path=str(tmp_path), liveness="live")
    registry = IdentityRegistry(
        tmp_path / "registry.json", config_rows=lambda: [{"name": "worker", "kind": "cli", "command": "codex"}]
    )
    registry.register("worker", kind_ref="worker", binding=record, task_ref="task")
    service = ReceiverService(store, registry, SimpleNamespace(show=lambda handle: record))
    service.grant("worker", receiver, SCOPE, request_id=str(uuid4()))
    accept_id = acceptance(store, sender, receiver, offer, sha)
    dispatcher = Dispatcher()
    register_mailbox_methods(dispatcher, receivers=service)
    first = await call(dispatcher, "mailbox.overview", SCOPE, admin=True)
    messages = first["result"]["data"]["messages"]
    assert accept_id in [row["message_id"] for row in messages]
    outgoing = next(row for row in messages if row["message_id"] == accept_id)
    assert outgoing["handoff"]["status"] == "accept_received"
    core.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    second = await call(dispatcher, "mailbox.overview", SCOPE, admin=True)
    outgoing = next(row for row in second["result"]["data"]["messages"] if row["message_id"] == accept_id)
    assert outgoing["handoff"]["status"] == "PROPOSED"
    assert outgoing["handoff"]["unresolved_items"] == ["Review output"]


@pytest.mark.parametrize("mode", ["large", "lost"])
async def test_receiver_cli_handles_large_response_and_lost_commit(mode):
    from raven.cli.mailbox_commands import _receiver_rpc
    from raven.contracts.mailbox import MailboxError

    async def receive(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        frame = await ws.receive_json()
        if mode == "large":
            await ws.send_json(
                {"jsonrpc": "2.0", "id": frame["id"], "result": {"data": {"content_base64": "A" * (6 * 1024 * 1024)}}}
            )
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/rpc", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        credential = {"url": f"http://127.0.0.1:{port}/rpc", "token": "s" * 43}
        if mode == "large":
            result = await _receiver_rpc(credential, "mailbox.artifact.read", {})
            assert len(result["content_base64"]) == 6 * 1024 * 1024
        else:
            with pytest.raises(MailboxError, match="commit_unknown"):
                await _receiver_rpc(credential, "mailbox.poll", {"request_id": str(uuid4())})
    finally:
        await runner.cleanup()


async def test_actual_receiver_cli_uses_only_scoped_credential(rpc, tmp_path):
    import asyncio
    import json
    import sys

    from raven.rpc.transports.ws import WsGateway

    dispatcher, service, refs, _, _ = rpc
    issued = service.grant("worker", refs[1], {"task_id": "task", "workspace_id": "workspace"}, request_id=str(uuid4()))
    gateway = WsGateway()
    gateway.dispatcher = dispatcher
    app = web.Application()
    app.router.add_get("/rpc", gateway.handle_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    credential = tmp_path / "receiver.json"
    credential.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/rpc", "token": issued["credential"]}))
    credential.chmod(0o600)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"peek": True}))
    params.chmod(0o600)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "raven",
            "mailbox",
            "rpc",
            "--credential-file",
            str(credential),
            "--params-file",
            str(params),
            "--method",
            "mailbox.poll",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, error.decode()
        assert json.loads(output)["result"] == {"messages": []}
        assert issued["credential"].encode() not in output + error
    finally:
        await runner.cleanup()
