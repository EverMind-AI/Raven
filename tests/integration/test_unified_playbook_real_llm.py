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
from raven.agent.loop.bundles import EngineWiring, SubagentWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec
from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.config.raven import CheckpointConfig, RuntimeConfig
from raven.config.schema import BuiltinAgentConfig, PlaybookConfig
from raven.playbook.agent_generator import PersonaPlaybookGenerator, TaskPlaybookGenerator
from raven.playbook.agent_spec import AgentPlaybookSpec, DelegateEntry
from raven.playbook.store import PlaybookStore
from raven.playbook.unified import UnifiedPlaybookSpec
from raven.playbook.workflow_compiler import WorkflowCompiler
from raven.providers.openai_codex_provider import OpenAICodexProvider
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

MODEL = "openai-codex/gpt-5.6-sol"
_CODEX_AUTH = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
TRAVEL_PERSONA_PROMPT = (
    "我每年会独立旅行几次，请为我创建并保存一个名为 travel-concierge 的可复用旅游助手。"
    "以后我只想提供目的地、日期、总预算、同行人和旅行节奏偏好，它就能给出真正可以照着走的方案。"
    "它需要调查最新的当地限制、习俗、街区安全、营业时间和预约变化；规划考虑距离、公共交通、"
    "开放时间、预约和疲劳程度的每日路线；平衡住宿、交通、饮食和门票费用，提供不同价位的替代方案，"
    "并主动避开游客陷阱。最终交付必须是排版清楚的六部分旅行简报，每天的安排不超过180个中文字。"
    "这份简报会直接发给同行人，最终交付必须由独立的内容编排与视觉表达职责统一格式和信息层级，"
    "不能由路线规划职责顺手兼任。"
    "我有膝盖旧伤，每日步行不得超过12000步，连续步行不得超过30分钟；不要安排红眼航班，任何换乘"
    "不得少于90分钟。这些是硬性限制，不能只当作建议。缺少目的地、日期、总预算、同行人或节奏偏好"
    "中的任何一项时，必须先说明缺少什么，不得开始规划。未经我明确批准，不得预订或付款；任何预订"
    "或付款操作必须携带 approved=true，且单笔人民币金额不得超过2800元，否则必须拒绝。行程批准后"
    "可以监控预订截止时间和余位变化，但非紧急提醒只能在我的当地时间18:00到21:00发送。如果查询"
    "失败或信息不足，必须返回已经确认的事实、仍缺少的信息和下一步，不得编造。现在只创建并保存"
    "这个助手，不要规划任何具体旅行，也不要执行工作流。"
)

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
async def test_live_generators_keep_task_and_persona_contracts_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    roster = ["Raven"]
    profiles = {"Raven": {"description": "general worker that can research, reason, and use local tools"}}

    task = await TaskPlaybookGenerator(provider, MODEL).resolve(
        "Create a reusable primary-source due-diligence task setup for evaluating a company.",
        roster,
        ["spawn", "run_subagent_dag"],
        profiles,
    )
    assert task.disposition == "runtime_and_artifact"
    assert task.spec is not None and task.spec.delegate
    assert task.capture_workflow
    assert all(entry.playbook is None or not entry.playbook.memory.system_prompt for entry in task.spec.delegate)

    persona = await PersonaPlaybookGenerator(provider, MODEL).resolve(
        "Create and save a reusable digital persona named claim-auditor. "
        "It skeptically audits factual claims, requires primary sources, and flags uncertainty. "
        "Do not run research now and do not create a workflow.",
        roster,
        ["spawn", "run_subagent_dag"],
        profiles,
    )
    assert persona.disposition == "artifact"
    assert persona.spec is not None and persona.spec.delegate
    assert persona.artifact_name
    assert not persona.capture_workflow


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
            playbook_mode="persona",
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
    assert not list(tmp_path.glob("skills/**/SKILL.md")), "Persona mode must not duplicate the Harness as a skill"


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
        subagents=SubagentWiring(
            agents=[
                BuiltinAgentConfig(
                    name="Raven-Research",
                    description="Researches current external facts and verifies primary sources.",
                    owns="live web research, source verification, restrictions, safety, prices, and opening hours",
                ),
                BuiltinAgentConfig(
                    name="Raven-Design",
                    description="Turns approved content into polished, structured visual deliverables.",
                    owns="visual hierarchy, document structure, concise layout, and presentation quality",
                ),
                BuiltinAgentConfig(
                    name="Raven-OnCall",
                    description="Monitors changing conditions and sends time-sensitive updates.",
                    owns="watched work, availability changes, deadlines, and scheduled reminders",
                ),
            ]
        ),
    )

    async def emit(*args, **kwargs) -> None:
        return None

    reply: dict[str, object] = {}
    await loop.run_turn(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="live-travel-assistant", sender_id="user", chat_type=ChatType.DM),
            text=TRAVEL_PERSONA_PROMPT,
            conversation="test:live-travel-assistant",
            playbook_mode="persona",
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
    assert {"Raven-Research", "Raven-Design", "Raven-OnCall"} <= {worker.name for worker in workers}
    assert any(worker.label != worker.name for worker in workers)
    assert any(worker.playbook and worker.playbook.memory.functions.get("intake") for worker in workers)
    assert any(
        worker.playbook and worker.playbook.action.checks and worker.playbook.action.checks.code for worker in workers
    )
    assert all("save the assistant" not in worker.brief.lower() for worker in workers)
    assert all("do not plan a specific trip" not in worker.brief.lower() for worker in workers)
    assert all("创建并保存" not in worker.brief for worker in workers)
    assert all("不要规划任何具体旅行" not in worker.brief for worker in workers)
    assert list((playbook_root / ".runs").glob("*.json"))
    assert not list(tmp_path.glob("skills/**/SKILL.md")), "Persona mode must not duplicate the Harness as a skill"

    print(
        json.dumps(
            {
                "reply": reply.get("text"),
                "playbook": saved.name,
                "artifactKind": "harness",
                "harness": saved.harness.model_dump(by_alias=True, exclude_none=True),
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
