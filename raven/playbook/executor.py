"""PlaybookExecutor — from a matched spec plus extracted params to a running
graph.

``dag`` mode ends at the same dispatch a model-composed graph goes through: the
filled node list handed to ``SubAgentDagTool.execute`` -- same validation, same
scheduler, same billing, same re-injection.

``prompt`` mode has two answers depending on who is asking, and that is the point
of the mode rather than a wart. In a conversation the caller *is* a model, so the
filled guidance is returned to it and it composes the graph itself, with the whole
conversation in hand and ordinary tool errors to correct against. On the CLI there
is no model in the room, so ``_compose`` spends one call to turn the guidance into
a graph. Deleting that path would have quietly removed the CLI's ability to run a
prompt-mode playbook at all.

This module builds no backends and resolves no agent names: each node names an
agent on the shared table and the graph tool resolves it.

Stateless by design: a plan either runs, or comes back as questions the
caller relays to the user, and the caller decides what to do with the answer --
it holds the conversation and calls again. Nothing here waits, which is what
lets this ship without a confirm-state machine. (It used to say the next
message re-entered matching: there is no matching left to re-enter. The passive
funnel that judged one message before the turn is gone, and trigger words now
decide which playbooks get *described* to the model, never which one runs.)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger
from pydantic import ValidationError

from raven.playbook.agent_profiles import AgentProfileSource, validate_node_capabilities
from raven.playbook.llm_result import ProviderResponseError, RequiredToolError, required_tool_arguments
from raven.playbook.mcp import playbook_mcp_servers
from raven.playbook.params import fill_param_refs_without_secrets, resolve_params, secret_param_names
from raven.playbook.prompt import COMPOSE_TOOL_NAME, build_compose_prompt, compose_tool
from raven.playbook.types import NodeSpec, PlaybookSpec
from raven.playbook.validate import validate_graph_nodes

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

_NODE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.(output|output_path)\s*\}\}")


def _fill_text(text: str, spec: PlaybookSpec, values: Mapping[str, str], where: str) -> str:
    """Fill one piece of playbook text, minus any secret, and say what was withheld.

    Every path that turns playbook text into something a model or a sub-agent
    reads goes through here, and there are three: the guidance reply, the compose
    input, and a node's prompt template. They had a redaction each and a log line
    between two of them, so the third changed a work order and said nothing --
    leaving an operator to debug a graph that could not do what the file asked,
    with no trace of why.

    ``where`` names the piece, because "a secret was withheld" is not actionable
    without it: the fix is to move the reference into an ``mcpServers`` ``env`` or
    ``headers`` entry, and the author has to know which text to edit.
    """
    filled, withheld = fill_param_refs_without_secrets(text or "", values, secret_param_names(spec))
    if withheld:
        logger.warning(
            "playbook {}: withheld secret param(s) {} referenced from {} -- a secret may only be referenced "
            "from an mcpServers env or headers value, where the host substitutes it and nothing downstream "
            "sees it",
            spec.name,
            sorted(set(withheld)),
            where,
        )
    return filled


#: Node fields a playbook may leave blank for the caller to fill, and that a node
#: cannot run without. Deliberately short: ``skills`` absent means "this agent's
#: own menu", which is a finished answer, so counting it as a gap would put a
#: question in front of every well-formed playbook.
FILLABLE_REQUIRED = ("subagent", "prompt_template", "node_summary")


@dataclass(frozen=True)
class ExecutionPlan:
    """What loading one playbook produced."""

    kind: Literal["dag", "guidance", "gaps", "questions"]
    """``dag``: dispatched, ``reply`` is the receipt. ``guidance``: prompt-mode
    composition instructions for the caller to build a graph from. ``gaps``: the
    playbook is missing values only the caller can supply and *nothing was
    dispatched*. ``questions``: it cannot proceed, and ``reply`` says why."""

    reply: str = ""

    notes: list[str] = field(default_factory=list)
    """Degradations worth surfacing (unsupported mcps, unfillable requests)."""


def _gap_reply(spec: PlaybookSpec, missing: list[tuple[str, str]], blanks: list[tuple[str, str]]) -> str:
    """What is still needed, addressed to the caller that can supply it.

    The reader here is the model, and it used to be the user: the old wording
    ended "include a trigger word in your reply, e.g. ..." because the answer had
    to re-enter through a stateless keyword funnel to be seen at all. There is no
    funnel now -- the caller holds the conversation and calls again -- so the reply
    names the exact arguments instead of coaching someone into re-triggering a
    match.

    Both kinds of gap in one message, and named precisely (``params`` by key,
    blanks by ``node.field``), because a caller that has to guess which argument a
    complaint refers to will guess wrong and spend another round.
    """
    lines = [f"'{spec.name}' was not run -- it still needs:"]
    for name, desc in missing:
        lines.append(f"- params.{name}: {desc}")
    for node_id, field_name in blanks:
        lines.append(f"- fills[{node_id!r}][{_wire_name(field_name)!r}]: this playbook leaves it for you to write")
    lines.append(
        "Call load_playbook again with those filled in. Values the playbook already specifies are "
        "not yours to change and will be refused."
    )
    return "\n".join(lines)


def _wire_name(field_name: str) -> str:
    """A node field as a playbook author spells it (camelCase).

    The caller is told to write ``fills[...]["promptTemplate"]`` rather than the
    python attribute name, because camelCase is what it will have read in the
    file and in the tool's own field list.
    """
    head, *rest = field_name.split("_")
    return head + "".join(part.title() for part in rest)


def _guidance_reply(spec: PlaybookSpec, values: dict[str, str]) -> str:
    """prompt mode's answer to a caller that can compose: the filled guidance.

    Returned rather than composed here. The caller has the conversation, so the
    graph it builds can answer *this* request; a private composition call sees only
    the guidance text and a cached roster, and its output then has to be reconciled
    with a conversation it never read. Flexibility is the whole reason an author
    picks prompt mode -- turning it into a worse ``dag`` mode by pinning the graph
    at load time would remove the only thing it offers.
    """
    # Without secrets: this text is returned to the caller and, in compose mode,
    # sent to a provider. A secret exists so the playbook can name a credential
    # without carrying it; substituting one here would carry it further than the
    # file ever did.
    filled = _fill_text(spec.prompts or "", spec, values, "'prompts'")
    return (
        f"'{spec.name}' is a guidance playbook: it describes how to build the graph rather than "
        "shipping one. Compose it yourself and submit it with run_subagent_dag.\n\n"
        f"{filled}"
    )


def _apply_fills(
    nodes: list[NodeSpec],
    fills: dict[str, dict[str, Any]],
) -> tuple[list[NodeSpec], list[str]]:
    """Apply the caller's fills, refusing any that target a field already written.

    **This check is what makes "the playbook's own values are not negotiable"
    true**, rather than a claim about a shape. Without it ``fills`` is a
    general-purpose field editor: the caller could rewrite any node's prompt,
    repoint it at another agent, or drop its skills, and the file in git would stop
    describing what ran. With it, the only thing expressible is filling a blank --
    which is exactly what the author asked for by leaving one.

    Keys are the author's plain node ids. The run prefix is added afterwards, and
    the caller has never seen it, so accepting a prefixed id would only ever be
    accepting a coincidence.
    """
    by_id = {n.id: n for n in nodes}
    errors: list[str] = []
    updates: dict[str, dict[str, Any]] = {}
    for node_id, patch in (fills or {}).items():
        node = by_id.get(node_id)
        if node is None:
            errors.append(f"fills names node {node_id!r}, which this playbook does not have (nodes: {sorted(by_id)}).")
            continue
        if not isinstance(patch, dict):
            errors.append(f"fills[{node_id!r}] must be an object of field -> value.")
            continue
        for raw_field, value in patch.items():
            attr = _FILL_ALIASES.get(raw_field, raw_field)
            if attr not in _FILLABLE:
                errors.append(
                    f"fills[{node_id!r}] names {raw_field!r}, which is not fillable "
                    f"(fillable: {sorted(_wire_name(f) for f in _FILLABLE)})."
                )
                continue
            if not _is_blank(getattr(node, attr, None)):
                errors.append(
                    f"node {node_id!r} already specifies {_wire_name(attr)!r}, so it cannot be changed -- "
                    f"a playbook's own values are fixed; only what it left blank can be filled."
                )
                continue
            updates.setdefault(node_id, {})[attr] = value
    if errors:
        return nodes, errors
    return [n.model_copy(update=updates[n.id]) if n.id in updates else n for n in nodes], []


def _is_blank(value: Any) -> bool:
    """Whether a node field was left for someone else to write.

    ``None`` and an empty string are blank. An empty *list* is not: ``skills: []``
    is a written instruction meaning "no skills at all", and treating it as an
    invitation would let a caller quietly widen what a step may reach.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


#: Fields ``fills`` may write. The three required ones plus the three optional
#: per-node knobs, so a caller can add narrowing the author left open -- the
#: blank-only rule is what keeps that from becoming an edit.
_FILLABLE = (*FILLABLE_REQUIRED, "skills", "mcps", "instance")

#: The wire spellings a caller might use, mapped to attribute names. Both are
#: accepted for the same reason the node model accepts both.
_FILL_ALIASES = {"promptTemplate": "prompt_template", "dependsOn": "depends_on", "nodeSummary": "node_summary"}


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

    ``instance`` is namespaced with the same tag, so an author writing
    ``instance: researcher`` on three steps gets one session shared by those
    three steps *of this run* rather than one shared by every run of the
    playbook ever. Sharing across runs is a thing a handle can do, but it is not
    what this declaration means: two runs of the same playbook are two separate
    pieces of work, and pouring both into one session mixes their contexts. Two
    concurrent runs would additionally queue against each other on that handle
    (see ``hold_handle``) for no reason.
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

    def rewrite_inputs(inputs: dict[str, object]) -> dict[str, object]:
        rewritten: dict[str, object] = {}
        for key, value in inputs.items():
            if isinstance(value, dict) and "node" in value:
                target = str(value["node"])
                rewritten[key] = {**value, "node": mapping.get(target, target)}
            else:
                rewritten[key] = value
        return rewritten

    return [
        n.model_copy(
            update={
                "id": mapping[n.id],
                "depends_on": [mapping.get(d, d) for d in n.depends_on],
                "prompt_template": rewrite_refs(n.prompt_template),
                "inputs": rewrite_inputs(n.inputs),
                "instance": f"{prefix}-{tag}-{n.instance}" if n.instance else None,
            }
        )
        for n in nodes
    ]


_RUN_ID_IN_RECEIPT = re.compile(r"^DAG\s+(?:run\s+)?(\S+?)[:\s]")


def _run_id_of(receipt: str) -> str | None:
    """The run id out of the graph tool's receipt, or None if it is not one.

    Read from the text rather than threaded through as a field because the tool
    hands back one object whose model-facing string is the contract both this
    and the clients already parse; a second channel for the same fact is a
    second thing to keep in step.
    """
    m = _RUN_ID_IN_RECEIPT.match(receipt or "")
    return m.group(1) if m else None


class PlaybookExecutor:
    """Turn a matched playbook into a running graph."""

    def __init__(
        self,
        *,
        dag_tool: Any = None,
        provider: "LLMProvider | None" = None,
        compose_model: str | None = None,
        background: bool = True,
        compose_prompt_mode: bool = False,
    ) -> None:
        self._dag_tool = dag_tool
        self._provider = provider
        self._compose_model = compose_model
        #: False = wait for the graph and reply with its result instead of a
        #: dispatch receipt. The CLI's explicit run; in-conversation entries
        #: stay backgrounded so the turn is not held open by a long graph.
        self._background = background
        #: Whether *this* executor composes a prompt-mode graph itself. True on
        #: the CLI, which has no model in the room and would otherwise have no way
        #: to run a prompt-mode playbook. False in a conversation, where the
        #: caller is a model: it gets the guidance and composes with the whole
        #: conversation in hand, and a bad graph comes back as an ordinary tool
        #: error it can fix, rather than through a private two-round repair loop
        #: working from a cached roster and no history.
        self._compose_prompt_mode = compose_prompt_mode

    @property
    def dag_tool(self) -> Any:
        """The private graph tool this executor dispatches through, if any.

        Exposed so the host can treat it as one more live instance rather than a
        hidden one. Everything a consumer asks a graph tool -- is this run live,
        cancel it, send its progress somewhere -- has to answer the same for a
        run a playbook started as for one the model composed; reaching only for
        the registered instance answers "not happening" to all three.
        """
        return self._dag_tool

    def set_context(self, *, channel: str | None, chat_id: str | None, session_key: str | None) -> None:
        """Address this turn's dispatch (progress + announce) like the loop
        does for registered tools; the executor's tool instance is private,
        so nobody else calls set_context on it."""
        if self._dag_tool is not None and hasattr(self._dag_tool, "set_context") and channel and chat_id:
            self._dag_tool.set_context(channel, chat_id, session_key)

    async def execute(
        self,
        spec: PlaybookSpec,
        params: dict[str, Any],
        *,
        fills: dict[str, dict[str, Any]] | None = None,
        confirmed: bool = False,
    ) -> ExecutionPlan:
        """Fill the spec and act on it. The mode decides which of those happens.

        ``fills`` supplies the node fields the author deliberately left blank,
        keyed by the *author's* plain node id -- the ids in the file, not the
        run-prefixed ones the runner sees, which the caller has never been shown.

        ``confirmed`` says the caller already put this run to the user, so the
        graph-level gate should not ask a second time. Nothing in the conversation
        path sets it now that no funnel asks ahead of the turn; it stays because
        an entry point that *does* ask must be able to say so.
        """
        values, missing = resolve_params(spec, params)
        if self._dag_tool is None:
            return ExecutionPlan(
                kind="questions",
                reply="No graph executor is wired up in this environment; describe the task directly and I will handle it ad hoc.",
            )

        if spec.mode == "prompt":
            # Nothing to fill node-wise: there are no nodes yet. A missing param
            # still stops it, because the guidance is written against those values.
            if missing:
                return ExecutionPlan(kind="gaps", reply=_gap_reply(spec, missing, []))
            if not self._compose_prompt_mode:
                return ExecutionPlan(kind="guidance", reply=_guidance_reply(spec, values))
            nodes, compose_errors = await self._compose(spec, values)
            if nodes is None:
                return ExecutionPlan(
                    kind="questions",
                    reply="Graph assembly from the template failed ("
                    + "; ".join(compose_errors[:3])
                    + "); describe the task directly and I will handle it ad hoc.",
                )
            return await self._dispatch(spec, nodes, confirmed=confirmed, values=values)

        nodes, fill_errors = _apply_fills(spec.nodes or [], fills or {})
        if fill_errors:
            # A refused fill is the caller's mistake, not a gap: telling it "still
            # missing X" would invite the same wrong call again.
            return ExecutionPlan(kind="questions", reply="Error: " + " ".join(fill_errors))
        nodes = [
            n.model_copy(update={"prompt_template": _fill_text(n.prompt_template, spec, values, f"node {n.id!r}")})
            for n in nodes
        ]
        blanks = [(n.id, f) for n in nodes for f in FILLABLE_REQUIRED if not str(getattr(n, f, "") or "").strip()]
        if missing or blanks:
            # Reported before anything is dispatched, and reported *together*: a
            # caller told about the params, asked again, and then told about the
            # blank fields would spend two round trips learning one thing.
            return ExecutionPlan(kind="gaps", reply=_gap_reply(spec, missing, blanks))
        return await self._dispatch(spec, nodes, confirmed=confirmed, values=values)

    async def _compose(self, spec: PlaybookSpec, values: dict[str, str]) -> tuple[list[NodeSpec] | None, list[str]]:
        """prompt mode: one LLM call assembles the graph; same validation,
        one repair round, then give up gracefully."""
        if self._provider is None:
            return None, ["no provider wired for graph composition"]
        prompts_filled = _fill_text(spec.prompts or "", spec, values, "'prompts' (graph composition)")
        profile_source = getattr(self, "_agent_profile_source", None)
        profiles = profile_source() if profile_source is not None else {}
        messages = [{"role": "user", "content": build_compose_prompt(prompts_filled, profiles, list(spec.params))}]
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
            try:
                args = required_tool_arguments(response, COMPOSE_TOOL_NAME)
            except (ProviderResponseError, RequiredToolError) as exc:
                return None, [str(exc)]
            nodes, errors = _parse_nodes(args)
            if nodes is not None and not errors:
                errors = validate_graph_nodes(nodes, set(), known_agents=profiles.keys() or None)
                errors += validate_node_capabilities(nodes, profiles)
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

    def set_agent_profiles(self, source: AgentProfileSource) -> None:
        """Set the live safe capability view used by prompt-mode composition."""
        self._agent_profile_source = source

    async def _dispatch(
        self,
        spec: PlaybookSpec,
        nodes: list[NodeSpec],
        *,
        confirmed: bool = False,
        values: dict[str, str] | None = None,
    ) -> ExecutionPlan:
        """Hand the filled nodes to the DAG tool's own entry.

        Nothing here resolves an agent name or builds a backend any more. Each
        node carries the agent it wants, the graph tool looks it up on the shared
        table, and a playbook step is dispatched by exactly the code path a
        model-composed node is. What that removed: a per-node backend built here, a
        hardcoded ``stateful=False`` that contradicted the mechanism, and the
        synthetic ``pb-<node>`` agent name every step ran under -- which is why a
        playbook's steps were unattributable in a trace and why an ``instance``
        could not work.

        ``values`` is here for the ``mcpServers`` section only: the spec's own
        server definitions reference secret params, so they can only be filled
        once the params are resolved, and the resolution happens in
        :meth:`execute`. They travel to the graph tool as a run-scoped overlay
        rather than being merged into the host's configuration -- see
        ``raven.agent.subagent.dag_mcp_scope``. Without this hand-off a
        conversation running the same playbook resolved every server it shipped
        to ``not_configured``, while ``raven playbook run`` resolved them fine,
        because only the CLI wired a source that had heard of them.

        **The hand-off is only reachable from here**, which is why a prompt-mode
        playbook may not carry the section at all: in a conversation
        :meth:`execute` returns guidance and the model dispatches the graph in a
        later turn, through a public tool with no such parameter and no scope
        left to read. ``validate._prompt_mode_cannot_carry_servers`` refuses that
        combination when the file is validated, rather than letting it load and
        drop the definitions here.
        """
        tool_nodes: list[dict[str, Any]] = []
        for run_node in _namespace_run(spec.name, nodes):
            tool_nodes.append(
                {
                    "id": run_node.id,
                    "subagent": run_node.subagent,
                    "node_summary": run_node.node_summary,
                    "prompt_template": run_node.prompt_template,
                    "depends_on": list(run_node.depends_on),
                    **({"skills": list(run_node.skills)} if run_node.skills is not None else {}),
                    **({"inputs": dict(run_node.inputs)} if run_node.inputs else {}),
                    **({"mcps": run_node.mcps} if run_node.mcps is not None else {}),
                    **({"instance": run_node.instance} if run_node.instance else {}),
                }
            )
        receipt = await self._dag_tool.execute(
            tool_nodes,
            task_summary=spec.task_summary,
            background=self._background,
            mcp_servers=playbook_mcp_servers(spec, values or {}),
            # The gate. With the passive funnel gone, nothing asks ahead of this,
            # so a playbook's ``confirm: true`` lands here or nowhere -- which is
            # why the graph-level parameter had to exist before the funnel could
            # be removed. ``confirmed`` is for an entry point that already asked.
            confirm=bool(spec.confirm and not confirmed),
        )
        text = str(getattr(receipt, "model_text", receipt))
        if text.startswith("Error"):
            return ExecutionPlan(kind="questions", reply=f"Failed to start the run: {text}")
        logger.info("playbook {} dispatched as a DAG run ({} nodes)", spec.name, len(tool_nodes))
        if self._background:
            # Leads with the run id in the graph tool's own shape. A client
            # restoring this card from history has no events to replay and
            # recovers the run from the result line, so a receipt that names only
            # the playbook left a reopened conversation with a dispatch it could
            # not connect to any run -- no node chips, no way back to the graph.
            # The model gets a handle on the run out of the same change.
            run = _run_id_of(text)
            lead = f"DAG {run}: " if run else ""
            reply = (
                f"{lead}started '{spec.name}' ({len(tool_nodes)} steps); "
                f"results will be delivered when the run completes."
            )
        else:
            reply = text
        return ExecutionPlan(kind="dag", reply=reply)


def _parse_nodes(args: dict[str, Any]) -> tuple[list[NodeSpec] | None, list[str]]:
    raw = args.get("nodes")
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
