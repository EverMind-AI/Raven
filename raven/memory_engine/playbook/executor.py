"""PlaybookExecutor — from a matched spec plus extracted params to a running
graph.

Both modes end at the same dispatch: a node list handed to the
SubAgentDagTool's ``run_with_roles`` entry (scheduling, file passing,
progress and the background announce are all the tool's existing
behaviour). ``dag`` ships its nodes; ``prompt`` first spends one LLM call
composing them from the spec's assembly guidance, and the composed graph
passes the same validation — no escape hatch.

Configuration hangs on nodes, so backends are built per node (the same
agent may run several steps with different skills); each node dispatches to
its own backend under a synthetic name.

Stateless by design: a plan either runs, or comes back as questions the
caller relays to the user. The user's next message re-enters matching from
scratch, which is what lets v1 ship without a confirm-state machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from loguru import logger
from pydantic import ValidationError

from raven.memory_engine.playbook.prompt import COMPOSE_TOOL_NAME, build_compose_prompt, compose_tool
from raven.memory_engine.playbook.types import NodeSpec, PlaybookSpec
from raven.memory_engine.playbook.validate import validate_graph_nodes

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}")


@dataclass(frozen=True)
class RoleBuildSpec:
    """What the backend factory needs for one node's backend. Duck-typed by
    ``SubagentManager.build_role_backend`` (tools_allow / skills_allow).

    Deliberately narrow: in v1 the capability base behind ``node.agent`` only
    steers *generation* (which name the casting LLM picks and which skills it
    binds); the built backends differ solely in ``skills_allow``. Carrying the
    base name here would imply a per-role prompt or model that nothing
    downstream applies -- when role charters reach the sub-agent prompt, the
    field returns together with its consumer."""

    name: str
    tools_allow: list[str] | None
    skills_allow: list[str] | None


RoleBackendFactory = Callable[[RoleBuildSpec], Any]


@dataclass(frozen=True)
class ExecutionPlan:
    """The executor's verdict on one matched spec + extracted params."""

    kind: Literal["dag", "questions"]
    reply: str = ""
    """dag: the dispatch receipt; questions: what to ask the user."""

    notes: list[str] = field(default_factory=list)
    """Degradations worth surfacing (stripped instances, unsupported mcps)."""


def _fill_params(spec: PlaybookSpec, params: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Resolve every declared param to a string value, or collect questions.

    A param's ``description`` doubles as the follow-up wording, per the
    field definition."""
    values: dict[str, str] = {}
    questions: list[str] = []
    for name, p in spec.params.items():
        if name in params and params[name] is not None:
            values[name] = _render_value(params[name])
        elif p.default is not None:
            values[name] = _render_value(p.default)
        elif p.required:
            questions.append(p.description)
        else:
            values[name] = ""
    return values, questions


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _fill_param_refs(text: str, values: dict[str, str]) -> str:
    """Compile-time substitution of ``${params.x}``; ``{{ … }}`` passes
    through untouched for the runner."""
    return _PARAM_REF_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


class PlaybookExecutor:
    """Turn a matched playbook into a running graph."""

    def __init__(
        self,
        *,
        backend_factory: RoleBackendFactory,
        dag_tool: Any = None,
        provider: "LLMProvider | None" = None,
        compose_model: str | None = None,
    ) -> None:
        self._make_backend = backend_factory
        self._dag_tool = dag_tool
        self._provider = provider
        self._compose_model = compose_model

    def set_context(self, *, channel: str | None, chat_id: str | None, session_key: str | None) -> None:
        """Address this turn's dispatch (progress + announce) like the loop
        does for registered tools; the executor's tool instance is private,
        so nobody else calls set_context on it."""
        if self._dag_tool is not None and hasattr(self._dag_tool, "set_context") and channel and chat_id:
            self._dag_tool.set_context(channel, chat_id, session_key)

    async def execute(self, spec: PlaybookSpec, params: dict[str, Any]) -> ExecutionPlan:
        values, questions = _fill_params(spec, params)
        if questions:
            asks = "\n".join(f"- {q}" for q in questions)
            return ExecutionPlan(
                kind="questions",
                reply=f"要跑「{spec.name}」还差几个信息：\n{asks}\n补充后我就开始。",
            )
        if self._dag_tool is None:
            return ExecutionPlan(kind="questions", reply="当前环境没有接好图执行器，请直接描述任务由我即兴处理。")

        if spec.mode == "dag":
            nodes = [
                n.model_copy(update={"prompt_template": _fill_param_refs(n.prompt_template, values)})
                for n in spec.nodes or []
            ]
        else:
            nodes, compose_errors = await self._compose(spec, values)
            if nodes is None:
                return ExecutionPlan(
                    kind="questions",
                    reply="按模板组图失败（" + "; ".join(compose_errors[:3]) + "），请直接描述任务由我即兴处理。",
                )
        return await self._dispatch(spec, nodes)

    async def _compose(self, spec: PlaybookSpec, values: dict[str, str]) -> tuple[list[NodeSpec] | None, list[str]]:
        """prompt mode: one LLM call assembles the graph; same validation,
        one repair round, then give up gracefully."""
        if self._provider is None:
            return None, ["no provider wired for graph composition"]
        prompts_filled = _fill_param_refs(spec.prompts or "", values)
        roster = getattr(self, "_roster_cache", None) or {}
        messages = [{"role": "user", "content": build_compose_prompt(prompts_filled, roster, list(spec.params))}]
        errors: list[str] = []
        for _ in range(2):
            try:
                response = await self._provider.chat_with_retry(
                    messages=messages,
                    tools=compose_tool(),
                    model=self._compose_model,
                    tool_choice={"type": "function", "function": {"name": COMPOSE_TOOL_NAME}},
                )
            except Exception as exc:  # noqa: BLE001 - composition failure degrades, never raises
                return None, [str(exc)]
            nodes, errors = _parse_nodes(response)
            if nodes is not None and not errors:
                errors = validate_graph_nodes(nodes, set())
                if not errors:
                    return nodes, []
            messages.append(
                {
                    "role": "user",
                    "content": "组出的图未通过校验，修复后重新提交 emit_graph：\n"
                    + "\n".join(errors or ["未返回 nodes"]),
                }
            )
        return None, errors or ["composition failed"]

    def set_roster(self, roster: dict[str, str]) -> None:
        """Agent name -> description, for the composition prompt."""
        self._roster_cache = roster

    async def _dispatch(self, spec: PlaybookSpec, nodes: list[NodeSpec]) -> ExecutionPlan:
        notes: list[str] = []
        backends: dict[str, Any] = {}
        capabilities: dict[str, Any] = {}
        from raven.agent.subagent_dag import AgentCapabilities

        tool_nodes: list[dict[str, Any]] = []
        for node in nodes:
            if node.mcps:
                notes.append(f"节点 {node.id} 声明的 mcps（{', '.join(node.mcps)}）本期子代理暂不支持，相关能力降级")
            if node.instance:
                notes.append(f"节点 {node.id} 的会话续接（instance={node.instance}）暂不支持，已按独立会话执行")
            if node.confirm:
                notes.append(f"节点 {node.id} 声明了确认闸，本期确认机制未接入，已直接执行")
            key = f"pb-{node.id}"
            backends[key] = self._make_backend(
                RoleBuildSpec(
                    name=key,
                    tools_allow=None,
                    skills_allow=node.skills or None,
                )
            )
            capabilities[key] = AgentCapabilities(stateful=False, reads_local_files=True)
            tool_nodes.append(
                {
                    "id": node.id,
                    "subagent": key,
                    "prompt_template": node.prompt_template,
                    "depends_on": list(node.depends_on),
                }
            )

        receipt = await self._dag_tool.run_with_roles(tool_nodes, roles=backends, role_capabilities=capabilities)
        text = str(getattr(receipt, "model_text", receipt))
        if text.startswith("Error"):
            return ExecutionPlan(kind="questions", reply=f"流程启动失败：{text}", notes=notes)
        logger.info("playbook {} dispatched as a DAG run ({} nodes)", spec.name, len(tool_nodes))
        reply = f"已按「{spec.name}」启动流程（{len(tool_nodes)} 步），跑完我会把结果发回来。"
        if notes:
            reply += "\n注：" + "；".join(dict.fromkeys(notes))
        return ExecutionPlan(kind="dag", reply=reply, notes=notes)


def _parse_nodes(response: Any) -> tuple[list[NodeSpec] | None, list[str]]:
    if not getattr(response, "has_tool_calls", False):
        return None, ["no emit_graph tool call in the response"]
    args = response.tool_calls[0].arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None, ["unparseable emit_graph arguments"]
    raw = (args or {}).get("nodes") if isinstance(args, dict) else None
    if not isinstance(raw, list) or not raw:
        return None, ["emit_graph returned no nodes"]
    nodes: list[NodeSpec] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        try:
            nodes.append(NodeSpec.model_validate(item))
        except ValidationError as exc:
            errors.extend(f"nodes[{i}].{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return (nodes if not errors else None), errors
