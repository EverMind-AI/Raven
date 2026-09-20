"""Live two-model-call E2E for generated worker harnesses."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.agent.subagent.charter import parse
from raven.playbook.agent_generator import WorkerTableGenerator
from raven.providers.litellm_provider import LiteLLMProvider


def _config_key() -> str:
    path = Path.home() / ".raven" / "config.json"
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get("providers", {}).get("openrouter") or {}
    except Exception:
        return ""
    return section.get("apiKey") or section.get("api_key") or ""


OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY") or _config_key()
MODEL = "openrouter/anthropic/claude-fable-5"
MARKER = "HARNESS_E2E_OK"

pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.slow,
    pytest.mark.skipif(not OPENROUTER_KEY, reason="no OpenRouter credential (env or ~/.raven)"),
]


@pytest.mark.asyncio
async def test_generated_harness_reaches_a_real_worker_model() -> None:
    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    table = await WorkerTableGenerator(provider, model=MODEL).generate(
        (
            "Delegate exactly one Raven-Research worker. Its job is to return the exact marker "
            f"{MARKER}. Give it a system prompt requiring that exact marker, allow only web_search, "
            "and set its completion condition to returning the marker."
        ),
        ["Raven-Research"],
        ["web_search", "web_fetch"],
        {"Raven-Research": "researches a bounded question and reports the answer"},
    )

    assert table is not None
    worker = table.get("Raven-Research")
    assert worker is not None and worker.payload is not None
    assert worker.payload.get("tools") == ["web_search"]
    assert MARKER in worker.payload.get("instructionAddendum", "")
    assert MARKER in worker.payload.get("stopWhen", "")

    charter = parse(worker.payload)
    assert charter is not None
    response = await provider.chat_with_retry(
        messages=[
            {"role": "system", "content": charter.task_brief},
            {"role": "user", "content": "Return the required marker now."},
        ],
        model=MODEL,
    )
    assert MARKER in (response.content or "")


@pytest.mark.asyncio
async def test_real_model_generated_checks_and_judge_execute(tmp_path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import ToolWiring
    from raven.agent.subagent.charter import charter_scope, judge

    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    table = await WorkerTableGenerator(provider, model=MODEL).generate(
        (
            "Delegate exactly one Raven-Code worker. Allow only write_file. Add a declarative check requiring "
            "write_file paths to start with out/. Also write a Python judge that refuses write_file when its "
            "content contains SECRET, returning the sentence 'secret content forbidden'."
        ),
        ["Raven-Code"],
        ["read_file", "write_file"],
        {"Raven-Code": "edits local code and files within explicit boundaries"},
    )

    assert table is not None
    worker = table.get("Raven-Code")
    assert worker is not None and worker.payload is not None
    assert worker.payload.get("checks")
    assert worker.payload.get("code")
    charter = parse(worker.payload)
    assert charter is not None
    with charter_scope(charter):
        assert judge("write_file", {"path": "elsewhere/a.txt", "content": "safe"}, ())
        assert judge("write_file", {"path": "out/a.txt", "content": "SECRET"}, ()) == ["secret content forbidden"]
        assert judge("write_file", {"path": "out/a.txt", "content": "safe"}, ()) == []

    loop = AgentLoop(provider=provider, workspace=tmp_path, model=MODEL, tools=ToolWiring(restrict_to_workspace=True))
    (tmp_path / "out").mkdir()
    with charter_scope(charter):
        refused = await loop.tools.execute("write_file", {"path": "out/blocked.txt", "content": "SECRET"})
    assert "secret content forbidden" in str(refused)
    assert not (tmp_path / "out" / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_real_model_cannot_emit_disabled_harness_fields(tmp_path, monkeypatch) -> None:
    from raven.contracts import harness_capabilities

    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    root = document["harnessGeneration"]
    for module, name in (
        ("memory", "systemPrompt"),
        ("memory", "stopWhen"),
        ("capability", "tools"),
        ("action", "checks"),
    ):
        root[module]["parameters"]["items"][name]["enabled"] = False
    for module, name in (("memory", "intake"), ("planning", "advise"), ("action", "judge"), ("action", "salvage")):
        root[module]["functions"]["participant"]["items"][name]["enabled"] = False
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    table = await WorkerTableGenerator(provider, model=MODEL).generate(
        "Delegate exactly one Raven-Research worker to return a short answer about the task.",
        ["Raven-Research"],
        ["web_search", "web_fetch"],
        {"Raven-Research": "researches a bounded question and reports the answer"},
    )

    assert table is not None
    worker = table.get("Raven-Research")
    assert worker is not None
    assert set(worker.payload or {}) <= {"brief", "prompt", "timeoutSeconds"}


@pytest.mark.asyncio
async def test_real_harness_crosses_acp_process_and_blocks_worker_tool(tmp_path) -> None:
    import sys

    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool
    from raven.agent.subagent.delegate import dispatch_charter

    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    table = await WorkerTableGenerator(provider, model=MODEL).generate(
        (
            "Delegate exactly one Raven-ACP worker. Allow only write_file. Give it instructions to attempt writing "
            "out/blocked.txt with content SECRET, then return HARNESS_ACP_BLOCKED after the refusal. Add a Python "
            "judge that refuses write_file when content contains SECRET with reason 'secret content forbidden'."
        ),
        ["Raven-ACP"],
        ["write_file"],
        {"Raven-ACP": "runs a Raven agent loop in a separate ACP process"},
    )
    assert table is not None
    worker = table.get("Raven-ACP")
    assert worker is not None and worker.payload is not None
    assert worker.payload.get("code")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "worker-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiKey": OPENROUTER_KEY}},
                "agents": {
                    "defaults": {
                        "provider": "openrouter",
                        "model": MODEL,
                        "maxToolIterations": 4,
                        "requestTimeoutSeconds": 120,
                    }
                },
                "memory": {"backend": None},
                "playbooks": {"enabled": False},
                "sessionTitle": {"enabled": False},
                "tools": {"restrictToWorkspace": True},
            }
        ),
        encoding="utf-8",
    )
    binary = Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")
    assert binary.exists()
    backend = AcpAgentBackend(
        name=f"harness-e2e-{tmp_path.name}",
        command=f"{binary} acp",
        env={"RAVEN_HOME": str(home)},
        ready_timeout_ms=120_000,
        timeout=180,
    )
    try:
        with dispatch_charter(worker.payload):
            reply = await backend.run(
                "Follow the dispatch brief and return its required marker after the tool result.",
                task_id="harness-live-e2e",
                workspace=workspace,
                executor=None,
                session_key="test:harness-live-e2e",
                provider=provider,
                model=MODEL,
            )
    finally:
        await close_pool()

    assert "HARNESS_ACP_BLOCKED" in reply
    frames = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "acp-frames").rglob("*.jsonl"))
    assert "write_file" in frames and "out/blocked.txt" in frames
    assert "secret content forbidden" in frames
    assert '"status": "failed"' in frames
    assert not (workspace / "out" / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_real_model_generates_binds_and_executes_three_participant_functions() -> None:
    from raven.agent.harness.participants import compose_advice, compose_intake, compose_salvage
    from raven.agent.subagent.charter import charter_participants, charter_scope
    from raven.contracts.participant import StepView

    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    table = await WorkerTableGenerator(provider, model=MODEL).generate(
        (
            "Delegate exactly one Raven-Code worker and emit all three optional generated functions. "
            "Under functions, write Python source for intake(text, step) returning "
            "{'text': 'INTAKE_OK'}, advise(step) returning exactly 'ADVISE_OK', and salvage(step) "
            "returning exactly 'SALVAGE_OK'. Keep every function to a single return statement."
        ),
        ["Raven-Code"],
        ["read_file"],
        {"Raven-Code": "edits local code and files"},
    )

    assert table is not None
    worker = table.get("Raven-Code")
    assert worker is not None and worker.payload is not None
    assert set(worker.payload.get("functions", {})) == {"intake", "advise", "salvage"}
    charter = parse(worker.payload)
    assert charter is not None
    step = StepView(
        session_key="live",
        iteration=1,
        response=None,
        transcript=(),
        history=(),
        turn_base=0,
        question="test generated functions",
        rollbacks=0,
        mode=None,
        mode_overlay=None,
        phase="user_inbound",
    )
    with charter_scope(charter):
        participants = charter_participants()
        intake = await compose_intake("ignored", step, participants)
        assert intake is not None and intake.text == "INTAKE_OK"
        assert await compose_advice(step, participants) == "ADVISE_OK"
        assert await compose_salvage(step, participants) == "SALVAGE_OK"


@pytest.mark.asyncio
async def test_generated_intake_executes_inside_the_forked_acp_worker(tmp_path) -> None:
    import sys

    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool
    from raven.agent.subagent.delegate import dispatch_charter

    marker = "FORKED_INTAKE_SHORT_CIRCUIT_OK"
    payload = {
        "functions": {
            "intake": (f"def intake(text, step):\n    return {{'reply': '{marker}', 'note': step.get('phase')}}")
        }
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "worker-home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiKey": OPENROUTER_KEY}},
                "agents": {"defaults": {"provider": "openrouter", "model": MODEL}},
                "memory": {"backend": None},
                "playbooks": {"enabled": False},
                "sessionTitle": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    binary = Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")
    backend = AcpAgentBackend(
        name=f"harness-functions-{tmp_path.name}",
        command=f"{binary} acp",
        env={"RAVEN_HOME": str(home)},
        ready_timeout_ms=120_000,
        timeout=120,
    )
    try:
        with dispatch_charter(payload):
            reply = await backend.run(
                "This text must never reach the worker model.",
                task_id="harness-function-acp",
                workspace=workspace,
                executor=None,
                session_key="test:harness-function-acp",
            )
    finally:
        await close_pool()

    assert reply == marker
