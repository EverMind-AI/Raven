"""Real-model E2Es for unified Playbook decisions and persistence.

These tests use the current Codex OAuth session through a permission-restricted
temporary adapter. They never print or persist credentials in the repository.
Run explicitly:

    uv run pytest tests/integration/test_unified_playbook_real_llm.py -v -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import PlaybookConfig
from raven.playbook.agent_generator import WorkerTableGenerator
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry
from raven.playbook.store import PlaybookStore
from raven.playbook.unified import UnifiedPlaybookSpec
from raven.playbook.workflow_compiler import WorkflowCompiler
from raven.providers.openai_codex_provider import OpenAICodexProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

MODEL = "openai-codex/gpt-5.6-sol"
_CODEX_AUTH = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"

pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.slow,
    pytest.mark.skipif(not _CODEX_AUTH.is_file(), reason="no Codex OAuth credential"),
]


def _provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenAICodexProvider:
    raw = json.loads(_CODEX_AUTH.read_text(encoding="utf-8"))
    tokens = raw.get("tokens") or {}
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        pytest.skip("Codex OAuth file has no usable tokens")
    token_dir = tmp_path / "codex-oauth"
    token_dir.mkdir(mode=0o700)
    auth = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "id_token": tokens.get("id_token"),
        "account_id": tokens.get("account_id"),
    }
    path = token_dir / "auth.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))
    monkeypatch.setenv("CHATGPT_AUTH_FILE", "auth.json")
    return OpenAICodexProvider(default_model=MODEL)


@pytest.mark.asyncio
async def test_live_resolver_distinguishes_none_persona_and_saved_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    resolver = WorkerTableGenerator(provider, MODEL)
    roster = ["Raven"]
    notes = {"Raven": "general worker that can research, reason, and use local tools"}

    direct = await resolver.resolve(
        "Answer only this arithmetic question directly: what is 2 + 2? Do not create an agent or reusable process.",
        roster,
        ["spawn", "run_subagent_dag"],
        notes,
    )
    assert direct.disposition == "none"
    assert direct.table is None
    assert not direct.capture_workflow

    persona = await resolver.resolve(
        "Create and save a reusable digital persona named claim-auditor. "
        "It skeptically audits factual claims, requires primary sources, and flags uncertainty. "
        "Do not run research now and do not create a workflow.",
        roster,
        ["spawn", "run_subagent_dag"],
        notes,
    )
    assert persona.disposition == "artifact"
    assert persona.spec is not None and persona.spec.delegate
    assert persona.artifact_name
    assert not persona.capture_workflow

    reused = await resolver.resolve(
        "Run my claim audit on the launch announcement using the saved claim-auditor Playbook.",
        roster,
        ["spawn", "run_subagent_dag"],
        notes,
        {"claim-auditor": "Audits factual claims, requires primary sources, and flags uncertainty"},
    )
    assert reused.selected_playbook == "claim-auditor"
    assert reused.spec is None


@pytest.mark.asyncio
async def test_live_compiler_parameterizes_an_accepted_harness_dag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    harness = AgentPlaybookSpec(
        name="launch-evidence-brief",
        description="Evidence-first launch research",
        delegate=[
            DelegateEntry(
                **{
                    "as": "researcher",
                    "name": "Raven",
                    "brief": "Use primary sources and identify uncertainty",
                }
            )
        ],
    )
    dag = SubAgentDagSpec(
        taskSummary="Build launch evidence brief",
        confirm=False,
        nodes=[
            DagNodeSpec(
                id="research",
                subagent="researcher",
                nodeSummary="Research Acme launch",
                promptTemplate="Research Acme's launch claims using primary sources",
            ),
            DagNodeSpec(
                id="brief",
                subagent="researcher",
                nodeSummary="Write evidence brief",
                promptTemplate="Turn {{ research.output }} into a concise evidence brief",
                dependsOn=["research"],
            ),
        ],
    )

    compiled = await WorkflowCompiler(provider, MODEL).compile(
        query="Create a reusable evidence brief process for Acme launch claims",
        dag=dag,
        run_id="live-e2e-run",
        harness=harness,
        name_hint="launch-evidence-brief",
    )

    assert compiled.harness is not None and compiled.workflow is not None
    assert [node.id for node in compiled.workflow.nodes] == ["research", "brief"]
    assert compiled.workflow.nodes[1].depends_on == ["research"]
    assert compiled.metadata.source_run_id == "live-e2e-run"


@pytest.mark.asyncio
async def test_live_whole_turn_saves_a_persona_without_a_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    playbook_root = tmp_path / "playbooks"
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model=MODEL,
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    async def emit(*args, **kwargs) -> None:
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="live-persona", sender_id="user", chat_type=ChatType.DM),
            text=(
                "Create and save a reusable digital persona called source-skeptic. "
                "It challenges unsupported claims, demands primary evidence, and states uncertainty. "
                "Do not execute a task and do not create a workflow."
            ),
            conversation="test:live-persona",
        ),
        emit,
        lambda: [],
        stream=False,
    )

    names = [name for name in PlaybookStore(playbook_root).list_ids()]
    assert len(names) == 1
    saved = PlaybookStore(playbook_root).load(names[0])
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None and saved.workflow is None
    assert saved.harness.delegate
    assert list((playbook_root / ".runs").glob("*.json"))


@pytest.mark.asyncio
async def test_live_whole_turn_infers_a_travel_assistant_harness_from_user_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    playbook_root = tmp_path / "playbooks"
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model=MODEL,
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(plugin_tools=[LoadPlaybookTool()], restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    async def emit(*args, **kwargs) -> None:
        return None

    reply: dict[str, object] = {}
    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="live-travel-assistant", sender_id="user", chat_type=ChatType.DM),
            text=(
                "I travel independently several times a year and want a reusable travel assistant called "
                "travel-concierge. Later I should only need to provide a destination, dates, budget, and "
                "preferences. It should produce realistic daily routes that account for distance, transit, "
                "opening hours, reservations, and fatigue; research current local restrictions, customs, "
                "neighborhood safety, and changes; balance lodging, transport, food, and admission costs with "
                "alternatives at different price points; avoid tourist traps; and produce one clear plan I can "
                "actually follow. For now, create and save the assistant only. Do not plan a specific trip."
            ),
            conversation="test:live-travel-assistant",
        ),
        emit,
        lambda: [],
        stream=False,
        text_sink=reply,
    )

    names = PlaybookStore(playbook_root).list_ids()
    assert len(names) == 1
    saved = PlaybookStore(playbook_root).load(names[0])
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None and saved.workflow is None
    workers = saved.harness.delegate
    assert workers
    assert any(worker.label != worker.name for worker in workers)
    assert all("save the assistant" not in worker.brief.lower() for worker in workers)
    assert all("do not plan" not in worker.brief.lower() for worker in workers)
    assert list((playbook_root / ".runs").glob("*.json"))

    print(
        json.dumps(
            {
                "reply": reply.get("text"),
                "playbook": saved.name,
                "artifactKind": "harness",
                "workers": [
                    {"as": entry.label, "agent": entry.name, "brief": entry.brief} for entry in saved.harness.delegate
                ],
                "workflow": None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.asyncio
async def test_live_whole_turn_executes_and_saves_a_composite_playbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    playbook_root = tmp_path / "playbooks"
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model=MODEL,
        policy=TurnPolicy(max_iterations=5),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(
            runtime_config=RuntimeConfig(checkpoint=CheckpointConfig(policy="never")),
            playbook_config=PlaybookConfig(enabled=True, dir=str(playbook_root), agentHarness="generate"),
        ),
    )

    async def emit(*args, **kwargs) -> None:
        return None

    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="live-composite", sender_id="user", chat_type=ChatType.DM),
            text=(
                "Create, execute now, and save a reusable Playbook named launch-signal-brief. "
                "Use one complete run_subagent_dag graph for the reusable process. "
                "Step 1 extracts exactly three launch signals from this passage: "
                "'Northstar shipped offline mode, reduced cold-start latency by 35 percent, "
                "and opened an EU support hub.' "
                "Step 2 turns those signals into a concise executive brief. "
                "Generate task-specific workers when useful, and save both their Harness and "
                "the successful Workflow for later reuse."
            ),
            conversation="test:live-composite",
        ),
        emit,
        lambda: [],
        stream=False,
    )

    names = PlaybookStore(playbook_root).list_ids()
    assert len(names) == 1
    assert {path.name for path in (playbook_root / names[0]).iterdir()} == {"playbook.md"}
    saved = PlaybookStore(playbook_root).load(names[0])
    assert isinstance(saved, UnifiedPlaybookSpec)
    assert saved.harness is not None and saved.workflow is not None
    assert len(saved.workflow.nodes) >= 2
    assert {node.subagent for node in saved.workflow.nodes} <= {entry.label for entry in saved.harness.delegate}
    records = list((playbook_root / ".runs").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert len(record["dags"]) == 1
    assert record["savedPlaybook"] == names[0]
