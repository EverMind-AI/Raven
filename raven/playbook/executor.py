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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from loguru import logger
from pydantic import ValidationError

from raven.playbook.prompt import COMPOSE_TOOL_NAME, build_compose_prompt, compose_tool
from raven.playbook.types import NodeSpec, PlaybookSpec
from raven.playbook.validate import validate_graph_nodes

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}")
_NODE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.(output|output_path)\s*\}\}")


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


def _fill_params(spec: PlaybookSpec, params: dict[str, Any]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Resolve every declared param to a string value, or collect what is missing.

    Returns the missing params as ``(name, description)``: the description is the
    follow-up wording per the field definition, and the name is what the retry
    hint needs -- see :func:`_missing_params_reply`.
    """
    values: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for name, p in spec.params.items():
        if name in params and params[name] is not None:
            values[name] = _render_value(params[name])
        elif p.default is not None:
            values[name] = _render_value(p.default)
        elif p.required:
            missing.append((name, p.description))
        else:
            values[name] = ""
    return values, missing


def _missing_params_reply(spec: PlaybookSpec, missing: list[tuple[str, str]]) -> str:
    """Ask for the missing params, and show a phrasing that can come back.

    Matching is stateless: the answer re-enters at L1, where a bare reply like
    "OpenAI" carries no trigger word, wins no nomination, and the run is lost
    with the user believing they answered. So the ask carries an example built
    from this playbook's own vocabulary -- repeating a trigger is what makes the
    next message reach the gate at all.
    """
    asks = "\n".join(f"- {desc}" for _name, desc in missing)
    trigger = spec.triggers.keywords[0]
    slots = " ".join(f"{name}=<your answer>" for name, _desc in missing)
    return (
        f"Running '{spec.name}' still needs a few details:\n{asks}\n"
        f"Include a trigger word in your reply, e.g.: {trigger} {slots}"
    )


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _fill_param_refs(text: str, values: dict[str, str]) -> str:
    """Compile-time substitution of ``${params.x}``; ``{{ … }}`` passes
    through untouched for the runner."""
    return _PARAM_REF_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _namespace_run(spec_name: str, nodes: list[NodeSpec]) -> list[NodeSpec]:
    """Rewrite node ids to a run-unique form, all reference sites in step.

    The graph runner holds node ids unique per session (that is what makes a
    finished node's output addressable across runs), while playbook authors
    write plain stable ids like ``scan`` — so verbatim dispatch would reject
    the second run of the same playbook in one conversation. This rewrite is
    the bridge: id, ``dependsOn`` and the ``{{ id.output }}`` /
    ``{{ id.output_path }}`` placeholders move together, ``{{ ref:… }}``
    forms and unknown ids pass through untouched, and the mapping never
    leaves the executor, so the file format stays plain. A random tag rather
    than a run counter: a counter would restart with the process while the
    session's id registry outlives it.
    """
    tag = uuid.uuid4().hex[:6]
    # The composite must satisfy the runner's id charset even if the
    # playbook name does not.
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", spec_name)
    mapping = {n.id: f"{prefix}-{tag}-{n.id}" for n in nodes}

    def rewrite_refs(text: str) -> str:
        return _NODE_REF_RE.sub(
            lambda m: "{{ %s.%s }}" % (mapping.get(m.group(1), m.group(1)), m.group(2)),
            text,
        )

    return [
        n.model_copy(
            update={
                "id": mapping[n.id],
                "depends_on": [mapping.get(d, d) for d in n.depends_on],
                "prompt_template": rewrite_refs(n.prompt_template),
            }
        )
        for n in nodes
    ]


class PlaybookExecutor:
    """Turn a matched playbook into a running graph."""

    def __init__(
        self,
        *,
        backend_factory: RoleBackendFactory,
        dag_tool: Any = None,
        provider: "LLMProvider | None" = None,
        compose_model: str | None = None,
        background: bool = True,
    ) -> None:
        self._make_backend = backend_factory
        self._dag_tool = dag_tool
        self._provider = provider
        self._compose_model = compose_model
        #: False = wait for the graph and reply with its result instead of a
        #: dispatch receipt. The CLI's explicit run; in-conversation entries
        #: stay backgrounded so the turn is not held open by a long graph.
        self._background = background

    def set_context(self, *, channel: str | None, chat_id: str | None, session_key: str | None) -> None:
        """Address this turn's dispatch (progress + announce) like the loop
        does for registered tools; the executor's tool instance is private,
        so nobody else calls set_context on it."""
        if self._dag_tool is not None and hasattr(self._dag_tool, "set_context") and channel and chat_id:
            self._dag_tool.set_context(channel, chat_id, session_key)

    async def execute(self, spec: PlaybookSpec, params: dict[str, Any]) -> ExecutionPlan:
        values, missing = _fill_params(spec, params)
        if missing:
            return ExecutionPlan(kind="questions", reply=_missing_params_reply(spec, missing))
        if self._dag_tool is None:
            return ExecutionPlan(
                kind="questions",
                reply="No graph executor is wired up in this environment; describe the task directly and I will handle it ad hoc.",
            )

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
                    reply="Graph assembly from the template failed ("
                    + "; ".join(compose_errors[:3])
                    + "); describe the task directly and I will handle it ad hoc.",
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
                    "content": "The composed graph failed validation. Fix the errors and submit again through emit_graph:\n"
                    + "\n".join(errors or ["no nodes returned"]),
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
        # Notes and logs speak the author's plain ids; the runner gets the
        # namespaced ones.
        for node, run_node in zip(nodes, _namespace_run(spec.name, nodes)):
            # mcps degrades with a note rather than failing the graph: unlike a
            # confirm gate or a shared session (both refused in validate.py), a
            # missing tool costs capability, not correctness -- the node still
            # does its own step, just with less reach.
            if node.mcps:
                notes.append(
                    f"node {node.id} declares mcps ({', '.join(node.mcps)}) that sub-agents "
                    "cannot mount in this release; those capabilities are degraded"
                )
            key = f"pb-{run_node.id}"
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
                    "id": run_node.id,
                    "subagent": key,
                    "prompt_template": run_node.prompt_template,
                    "depends_on": list(run_node.depends_on),
                }
            )

        receipt = await self._dag_tool.run_with_roles(
            tool_nodes, roles=backends, role_capabilities=capabilities, background=self._background
        )
        text = str(getattr(receipt, "model_text", receipt))
        if text.startswith("Error"):
            return ExecutionPlan(kind="questions", reply=f"Failed to start the run: {text}", notes=notes)
        logger.info("playbook {} dispatched as a DAG run ({} nodes)", spec.name, len(tool_nodes))
        if self._background:
            reply = (
                f"Started '{spec.name}' ({len(tool_nodes)} steps); results will be delivered when the run completes."
            )
        else:
            reply = text
        if notes:
            reply += "\nNote: " + "; ".join(dict.fromkeys(notes))
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
