"""Write the worker table for the question that just arrived.

What the model is asked for is deliberately small. With no graph there are no
nodes to name, no edges to keep acyclic and no per-node agent assignment --
which is where a graph-shaped generator spends both its tokens and its failure
modes. What is left is: which of these agents does this question need, and what
is each one's brief.

The candidate lists are handed in rather than described. A roster the host
renders into the tool's own ``enum`` cannot be hallucinated, so "an agent that
does not exist" stops being a repair round and becomes a shape the request
could not express. Only the judgements that remain -- which agents, how many,
what brief -- can be wrong, and those are what a repair round can fix.

One repair round, then the turn runs unconfigured. A turn that dies because its
configuration step failed is strictly worse than a turn that runs without one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from raven.agent.harness_capabilities import function_enabled, parameter_enabled
from raven.agent.subagent.delegate import DelegateTable, Worker
from raven.playbook.agent_spec import AgentPlaybookSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from raven.playbook.agent_spec import SubPlaybook

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

MAX_CODE_CHARS = 8000
"""The cap the worker's own reader applies, stated here too: a source over it
would be cut on arrival, and cutting Python mid-statement turns a judgement
the author wrote into one the gate refuses for a reason they cannot see."""

MAX_REPAIR_ROUNDS = 1
EMIT_TOOL = "emit_worker_table"

HarnessDisposition = Literal["none", "runtime", "artifact", "runtime_and_artifact"]


@dataclass(frozen=True)
class HarnessResolution:
    """The single pre-turn decision: reuse, generate, persist, or do nothing."""

    disposition: HarnessDisposition = "none"
    spec: AgentPlaybookSpec | None = None
    table: DelegateTable | None = None
    selected_playbook: str | None = None
    artifact_name: str | None = None
    capture_workflow: bool = False
    active: bool = False
    description: str = ""


SYSTEM_PROMPT = (
    "You prepare the workers for one task before the agent that will run it starts.\n\n"
    "Given the task, decide which of the offered sub-agents it needs and write each one a brief. "
    "Two entries may name the same agent with different briefs -- that is how one task gets a "
    "worker per subject. Give a worker a distinct `as` label when you do that.\n\n"
    "Write a brief only where it changes what the worker would do: what to cover, what to leave "
    "alone, where to put its output, what counts as done. Do not restate the agent's own job -- it "
    "already knows that. If the task needs no sub-agents, emit an empty list.\n\n"
    "First consider the offered saved playbooks. Select one only when it directly matches the task; "
    "then emit no workers because its durable Harness will be loaded. Otherwise set disposition: "
    "none for no Harness, runtime for temporary workers, artifact when the user asks to create a "
    "reusable agent/persona without running it, or runtime_and_artifact when that created Harness "
    "must also work now. artifactName is required for either artifact disposition. Set "
    "captureWorkflow true only for a multi-step execution whose successful DAG would be useful "
    "again. When true, the running agent must express the complete reusable process in one "
    "run_subagent_dag call; a fragmented or failed run is not saved. Never set it merely because "
    "the user asked a question. A digital-person/agent creation request is Harness-only unless "
    "the user also asks to save a process.\n\n"
    "For artifact and runtime_and_artifact, the workers are the durable Harness itself -- not "
    "authors tasked with defining or saving that Harness. Give them meaningful role aliases and "
    "write briefs, system prompts, and stop conditions for how they should behave when the Harness "
    "is used later (and now for runtime_and_artifact). Do not carry over creation-time meta commands "
    "such as create/save the assistant, do not execute now, or plan only on future runs. A named "
    "single persona normally becomes one specialized worker; infer additional complementary workers "
    "only when the user's needs genuinely justify a team.\n\n"
    "Be sparing. Every worker you name is a separate process the agent has to wait for."
)


_RULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "The tool this rule is about."},
        "when": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Narrows the rule to calls whose arguments match these values.",
        },
        "pathPrefix": {"type": "string", "description": "The path argument must start with this."},
        "forbid": {"type": "string", "description": "A fragment no argument of the call may contain."},
        "requiresPrior": {"type": "string", "description": "A tool that must already have run successfully."},
        "matchParam": {
            "type": "string",
            "description": "With requiresPrior: the argument the earlier call must have matched on.",
        },
        "message": {"type": "string", "description": "What the worker is told when this rule refuses."},
    },
    "required": ["tool"],
    "additionalProperties": False,
}
"""One rule, in the worker's own vocabulary.

Spelled out here rather than derived from :class:`CheckRule`: a schema the
model reads wants a sentence per field saying when to write it, and a dump of
the dataclass would carry the field names without the reason for any of them.
The two are held together by :func:`_spec_from_args`, which validates what
comes back against the real model.
"""

_PARTICIPANT_FUNCTIONS: tuple[tuple[str, str, str], ...] = (
    ("memory", "intake", "Define intake(text, step). Return a dict with optional text, reply, and note keys, or None."),
    ("planning", "advise", "Define advise(step). Return guidance text for the next model call, or None."),
    ("action", "salvage", "Define salvage(step). Return a final reply string when the turn has no answer, or None."),
)


def roster_note(meta: Any) -> str:
    """One line on what an agent is for, in the terms a choice between two turns on.

    Prose first, because ``owns`` is the sharpest thing the registry holds and
    is what the identity prompt's Delegation section renders; ``description``
    when a row declares no ownership. Then the advertised capabilities, because
    a roster may carry neither -- and two agents with blank descriptions are
    still not interchangeable if only one of them can read the local files the
    task is about. ``spawn`` gates on exactly these, which is why the model
    choosing a worker is shown the same ones.
    """
    prose = (getattr(meta, "owns", "") or getattr(meta, "description", "") or "").strip().rstrip(".")
    tags = [
        label
        for attr, label in (
            ("reads_local_files", "reads local files"),
            ("stateful", "resumable across dispatches"),
            ("live_progress", "reports progress while it runs"),
            ("owns_watched_work", "owns watched work"),
        )
        if getattr(meta, attr, False)
    ]
    if prose and tags:
        return f"{prose} ({'; '.join(tags)})"
    return prose or "; ".join(tags)


def _roster_description(agent_names: list[str], agent_notes: "Mapping[str, str] | None") -> str:
    """What each name on the roster is for, or the bare instruction without them.

    An enum of names alone is only selectable when the names say what they are.
    The shipped roster reads that way; a deployment's own does not -- ``alpha``
    and ``beta`` are indistinguishable to the one party that has to choose
    between them. The registry already carries the answer (``owns``, which
    drives the identity prompt's Delegation section, and ``description``), and
    ``spawn`` renders it for exactly this reason, so the generating model is
    given the same thing rather than a shorter list.
    """
    head = "Which sub-agent this worker is."
    lines = [
        f"- {name}: {agent_notes[name].strip()}" for name in agent_names if (agent_notes or {}).get(name, "").strip()
    ]
    return head if not lines else head + " What each one is for:\n" + "\n".join(lines)


def emit_tool(
    agent_names: list[str],
    tool_names: list[str],
    agent_notes: "Mapping[str, str] | None" = None,
    playbook_candidates: "Mapping[str, str] | None" = None,
) -> list[dict[str, Any]]:
    """The one tool the generator may call, with the install's own enums.

    The roster is an ``enum`` and the tool list is an ``enum`` for the same
    reason: a name the host cannot resolve is worth refusing at the boundary
    rather than diagnosing after.
    """
    worker: dict[str, Any] = {
        "type": "object",
        "properties": {
            "as": {
                "type": "string",
                "description": (
                    "Optional short label for this worker (e.g. 'research-a'). Give one only when "
                    "the same agent appears more than once; otherwise omit it."
                ),
            },
            "name": {
                "type": "string",
                "enum": agent_names,
                "description": _roster_description(agent_names, agent_notes),
            },
            "brief": {
                "type": "string",
                "description": "One line on what this worker is for, read by the agent that dispatches it.",
            },
            "systemPrompt": {
                "type": "string",
                "description": "Task-specific instructions appended to the worker's existing identity. Omit when the brief is enough.",
            },
            "stopWhen": {"type": "string", "description": "Optional: what, once obtained, means this worker is done."},
            "tools": {
                "type": "array",
                "items": {"type": "string", "enum": tool_names},
                "description": "Optional: the tools this job calls for. Guidance, not a permission.",
            },
            "checks": {
                "type": "array",
                "items": _RULE_SCHEMA,
                "description": (
                    "Optional: rules judged before each of this worker's tool calls. A rule that "
                    "refuses replaces the call with its message, so the worker can try again "
                    "correctly. Write one only where the job has a boundary the brief alone "
                    "cannot hold -- 'stay under this directory', 'read it before writing it'."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Optional: a judgement the rules above cannot state, as Python. Define "
                    "judge(name, params, prior) returning the refusal sentences for one call "
                    "(an empty list allows it); prior is [(tool_name, params), ...] of what "
                    "already ran successfully this turn. An allow-listed subset only: no "
                    "imports, no loops, no attribute starting with '_', and no calls beyond "
                    "len/str/int/bool/any/all/sorted/set/list/dict/tuple/min/max/abs and the "
                    "string and dict methods. Comprehensions are allowed and are how you walk "
                    "prior. Source outside the subset is dropped, so keep it to one judgement."
                ),
            },
            "timeoutSeconds": {
                "type": "integer",
                "description": (
                    "Optional: a deadline for this worker, in seconds. Tightening only -- it "
                    "cannot lengthen a limit an operator set. Give one only when the job is "
                    "genuinely bounded; a long job is a long job."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    properties = worker["properties"]
    for module, name, field in (
        ("memory", "systemPrompt", "systemPrompt"),
        ("memory", "stopWhen", "stopWhen"),
        ("capability", "tools", "tools"),
        ("action", "checks", "checks"),
    ):
        if not parameter_enabled(module, name):
            properties.pop(field, None)
    if not function_enabled("action", "participant", "judge"):
        properties.pop("code", None)
    function_properties = {
        name: {
            "type": "string",
            "description": description
            + " The step argument is a read-only JSON-like dict with session_key, iteration, response, transcript, history, turn_base, question, rollbacks, mode, mode_overlay, phase, tools, window, max_iterations, and tools_ran; use only the allow-listed Python subset.",
        }
        for module, name, description in _PARTICIPANT_FUNCTIONS
        if function_enabled(module, "participant", name)
    }
    if function_properties:
        properties["functions"] = {
            "type": "object",
            "properties": function_properties,
            "additionalProperties": False,
            "description": "Optional generated participant functions, keyed by their loop verb.",
        }
    decision_properties: dict[str, Any] = {
        "description": {"type": "string", "description": "One sentence explaining this setup."},
        "disposition": {
            "type": "string",
            "enum": ["none", "runtime", "artifact", "runtime_and_artifact"],
            "description": "How newly generated workers are used this turn.",
        },
        "artifactName": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9-]*$",
            "description": "Durable kebab-case name, required for artifact dispositions.",
        },
        "captureWorkflow": {
            "type": "boolean",
            "description": "Whether a successful DAG from this run should be compiled and saved.",
        },
        "workers": {"type": "array", "items": worker},
    }
    if playbook_candidates:
        decision_properties["selectedPlaybook"] = {
            "type": "string",
            "enum": sorted(playbook_candidates),
            "description": "An existing directly applicable Playbook; omit when none fits.",
        }
    return [
        {
            "type": "function",
            "function": {
                "name": EMIT_TOOL,
                "description": "Resolve a saved Playbook or emit the workers this task needs.",
                "parameters": {
                    "type": "object",
                    "properties": decision_properties,
                    "required": ["disposition", "workers"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def render_charter(brief: str, system_prompt: str, stop_when: str, tools: list[str] | None) -> str:
    """The preamble one worker's task carries.

    Assembled once, at generation time: two dispatches to one label are the
    same worker, so re-deriving this per dispatch would only invite the two to
    drift.
    """
    lines: list[str] = []
    if brief.strip():
        lines.append(brief.strip())
    if parameter_enabled("memory", "systemPrompt") and system_prompt.strip():
        lines.append(system_prompt.strip())
    if parameter_enabled("capability", "tools") and tools:
        lines.append(f"Tools this job calls for: {', '.join(tools)}.")
    if parameter_enabled("memory", "stopWhen") and stop_when.strip():
        lines.append(f"Done when: {stop_when.strip()}")
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"Your brief for this task:\n{body}\n\nThe task follows.\n\n"


def build_payload(brief: str, spec: "SubPlaybook | None") -> dict[str, Any] | None:
    """The charter in the shape a Raven worker binds, or ``None`` for none.

    The same brief the preamble renders, minus the rendering: a worker that can
    hold its turn to it reads the fields, and one that cannot gets only the
    preamble. Both are built from one source so the two can never say different
    things.
    """
    instruction_addendum = (
        (spec.memory.system_prompt if spec else "") if parameter_enabled("memory", "systemPrompt") else ""
    )
    tools = spec.capability.tools if spec and parameter_enabled("capability", "tools") else None
    checks = spec.action.checks if spec and spec.action.checks else None
    stop_when = spec.stop_when if spec and parameter_enabled("memory", "stopWhen") else ""
    payload: dict[str, Any] = {}
    if brief.strip():
        payload["brief"] = brief.strip()
    if instruction_addendum.strip():
        payload["instructionAddendum"] = instruction_addendum.strip()
    legacy_prompt = "\n\n".join(part for part in (brief.strip(), instruction_addendum.strip()) if part)
    if legacy_prompt:
        payload["prompt"] = legacy_prompt
    if tools is not None:
        payload["tools"] = list(tools)
    if stop_when.strip():
        payload["stopWhen"] = stop_when.strip()
    if parameter_enabled("action", "checks") and checks and checks.rules:
        payload["checks"] = [rule.model_dump(by_alias=True, exclude_defaults=True) for rule in checks.rules]
    # Carried whether or not there are rules beside it: a judgement some jobs
    # can only state as code is the reason the field exists, and one written
    # without any declarative rule would otherwise be dropped on the way out.
    if function_enabled("action", "participant", "judge") and checks and checks.code.strip():
        payload["code"] = checks.code
    generated_functions: dict[str, str] = {}
    if spec:
        for module, functions in (
            ("memory", spec.memory.functions),
            ("planning", spec.planning.functions),
            ("action", spec.action.functions),
        ):
            for name, source in functions.items():
                if function_enabled(module, "participant", name) and source.strip():
                    generated_functions[name] = source
    if generated_functions:
        payload["functions"] = generated_functions
    if spec and spec.timeout_seconds:
        payload["timeoutSeconds"] = spec.timeout_seconds
    return payload or None


def build_table(spec: AgentPlaybookSpec, briefs: dict[str, str]) -> DelegateTable:
    """Turn a validated spec into the table a turn dispatches through."""
    workers: dict[str, Worker] = {}
    for entry in spec.delegate:
        pb = entry.playbook
        brief = briefs.get(entry.label, entry.brief)
        charter = render_charter(
            brief,
            pb.memory.system_prompt if pb else "",
            pb.stop_when if pb else "",
            (pb.capability.tools if pb else None),
        )
        workers[entry.label] = Worker(
            label=entry.label,
            agent=entry.name,
            brief=brief,
            charter=charter,
            payload=build_payload(brief, pb),
        )
    return DelegateTable(workers=workers)


def _spec_from_args(args: dict[str, Any], roster: set[str]) -> tuple[AgentPlaybookSpec, dict[str, str]]:
    """Build a spec from the emitted arguments, or raise with what to repair."""
    rows = args.get("workers")
    if not isinstance(rows, list):
        raise ValueError("workers must be a list")
    delegate: list[dict[str, Any]] = []
    briefs: dict[str, str] = {}
    errors: list[str] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"{index}. a worker must be an object")
            continue
        name = str(row.get("name") or "")
        if name not in roster:
            # Dropped rather than repaired: the roster was an enum, so a name
            # outside it is a shape the request could not express, and spending
            # a round on it teaches the model nothing it was not already told.
            logger.info("agent playbook: dropping worker {!r} -- not on the roster", name)
            continue
        label = str(row.get("as") or "") or name
        switches = (
            ("systemPrompt", parameter_enabled("memory", "systemPrompt")),
            ("stopWhen", parameter_enabled("memory", "stopWhen")),
            ("tools", parameter_enabled("capability", "tools")),
            ("checks", parameter_enabled("action", "checks")),
            ("code", function_enabled("action", "participant", "judge")),
        )
        disabled = [field for field, enabled in switches if field in row and not enabled]
        if disabled:
            errors.append(f"{index}. disabled harness field(s): {', '.join(disabled)}")
            continue
        raw_functions = row.get("functions")
        if "functions" in row and not isinstance(raw_functions, dict):
            errors.append(f"{index}. functions must be an object")
            continue
        function_modules = {function_name: module for module, function_name, _ in _PARTICIPANT_FUNCTIONS}
        generated_functions: dict[str, tuple[str, str]] = {}
        for function_name, source in (raw_functions or {}).items():
            module = function_modules.get(function_name)
            if module is None:
                errors.append(f"{index}. unknown generated participant function: {function_name}")
                continue
            if not function_enabled(module, "participant", function_name):
                errors.append(f"{index}. disabled harness field(s): functions.{function_name}")
                continue
            if not isinstance(source, str) or not source.strip():
                errors.append(f"{index}. functions.{function_name} must be non-empty Python source")
                continue
            if len(source) > MAX_CODE_CHARS:
                errors.append(f"{index}. functions.{function_name} exceeds {MAX_CODE_CHARS} characters")
                continue
            try:
                from raven.agent.subagent.charter_code import compile_function

                compile_function(source, function_name)
            except Exception as exc:
                errors.append(f"{index}. functions.{function_name} was refused: {exc}")
                continue
            generated_functions[function_name] = (module, source)
        if any(message.startswith(f"{index}.") for message in errors):
            continue
        sub: dict[str, Any] = {}
        if row.get("systemPrompt"):
            sub.setdefault("memory", {})["systemPrompt"] = str(row["systemPrompt"])
        if row.get("stopWhen"):
            sub["stopWhen"] = str(row["stopWhen"])
        for function_name, (module, source) in generated_functions.items():
            sub.setdefault(module, {}).setdefault("functions", {})[function_name] = source
        if isinstance(row.get("tools"), list):
            sub.setdefault("capability", {})["tools"] = [str(x) for x in row["tools"]]
        # Both halves of the judgement seat land under one key, because a
        # worker with code and no rules is as ordinary as one with rules and no
        # code -- the two are alternatives, not a base and an extension.
        checks: dict[str, Any] = {}
        if isinstance(row.get("checks"), list) and row["checks"]:
            checks["rules"] = row["checks"]
        if row.get("code"):
            checks["code"] = str(row["code"])[:MAX_CODE_CHARS]
        if checks:
            sub.setdefault("action", {})["checks"] = checks
        timeout = row.get("timeoutSeconds")
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
            sub["timeoutSeconds"] = timeout
        delegate.append({"as": label, "name": name, "brief": str(row.get("brief") or ""), "playbook": sub or None})
        briefs[label] = str(row.get("brief") or "")
    if errors:
        raise ValueError("; ".join(errors))
    payload = {"delegate": delegate}
    if args.get("description"):
        payload["description"] = str(args["description"])[:200]
    return AgentPlaybookSpec.model_validate(payload), briefs


class WorkerTableGenerator:
    """One model call that writes this turn's worker table."""

    def __init__(self, provider: "LLMProvider", model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    async def resolve(
        self,
        query: str,
        agent_names: list[str],
        tool_names: list[str],
        agent_notes: "Mapping[str, str] | None" = None,
        playbook_candidates: "Mapping[str, str] | None" = None,
    ) -> HarnessResolution:
        """Resolve reuse and generation in the one setup call a turn pays for."""
        if not query.strip() or not agent_names:
            return HarnessResolution()
        candidates = dict(playbook_candidates or {})
        candidate_text = "\n".join(f"- {name}: {detail}" for name, detail in candidates.items())
        user = f"Task:\n{query}"
        if candidate_text:
            user += f"\n\nSaved Playbook candidates:\n{candidate_text}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        tools = emit_tool(sorted(agent_names), sorted(tool_names), agent_notes, candidates)
        roster = set(agent_names)
        for _ in range(1 + MAX_REPAIR_ROUNDS):
            try:
                response = await self._provider.chat_with_retry(
                    messages=messages,
                    tools=tools,
                    model=self._model or None,
                    tool_choice={"type": "function", "function": {"name": EMIT_TOOL}},
                )
            except Exception as exc:  # noqa: BLE001 - setup failure must not cost the turn
                logger.warning("agent playbook: resolution call failed ({}); running unconfigured", exc)
                return HarnessResolution()
            args = _emitted_args(response)
            if args is None:
                messages.append({"role": "user", "content": f"You emitted no decision. Call {EMIT_TOOL}."})
                continue
            selected = args.get("selectedPlaybook")
            if isinstance(selected, str) and selected in candidates:
                return HarnessResolution(
                    active=True,
                    selected_playbook=selected,
                    capture_workflow=False,
                    description=str(args.get("description") or "")[:200],
                )
            try:
                spec, briefs = _spec_from_args(args, roster)
                raw = args.get("disposition")
                disposition: HarnessDisposition = (
                    raw
                    if raw in {"none", "runtime", "artifact", "runtime_and_artifact"}
                    else ("runtime" if spec.delegate else "none")
                )
                if disposition in {"artifact", "runtime_and_artifact"} and not spec.delegate:
                    raise ValueError("artifact disposition requires at least one worker")
                if disposition == "none" and spec.delegate:
                    raise ValueError("disposition none requires an empty worker list")
                artifact_name = None
                if disposition in {"artifact", "runtime_and_artifact"}:
                    from raven.playbook.types import slugify

                    artifact_name = slugify(str(args.get("artifactName") or spec.description or query))
                    spec = spec.model_copy(update={"name": artifact_name})
                table = build_table(spec, briefs) or None
            except Exception as exc:  # noqa: BLE001 - the message is the repair prompt
                logger.info("agent playbook: rejected, repairing once ({})", exc)
                messages.append(
                    {"role": "user", "content": f"That decision was rejected: {exc}\nEmit a corrected one."}
                )
                continue
            return HarnessResolution(
                disposition=disposition,
                active=True,
                spec=spec if spec.delegate else None,
                table=table,
                artifact_name=artifact_name,
                capture_workflow=bool(args.get("captureWorkflow", False)),
                description=str(args.get("description") or "")[:200],
            )
        logger.warning("agent playbook: still invalid after one repair; running unconfigured")
        return HarnessResolution()

    async def generate(
        self,
        query: str,
        agent_names: list[str],
        tool_names: list[str],
        agent_notes: "Mapping[str, str] | None" = None,
    ) -> DelegateTable | None:
        """Backward-compatible table-only face used by direct callers."""
        return (await self.resolve(query, agent_names, tool_names, agent_notes)).table


def _emitted_args(response: Any) -> dict[str, Any] | None:
    """The emitted arguments, from either shape a provider hands back.

    ``LLMResponse.tool_calls`` carries :class:`ToolCallRequest` objects on the
    paths this repo owns and raw OpenAI-shaped dicts on the ones it adapts, so
    both are read rather than one being assumed -- the object form is what the
    in-process providers produce, and testing only the dict form would make
    this work solely against the vendors.
    """
    for call in getattr(response, "tool_calls", None) or []:
        if isinstance(call, dict):
            fn = call.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            raw = fn.get("arguments") if isinstance(fn, dict) else None
        else:
            name = getattr(call, "name", None)
            raw = getattr(call, "arguments", None)
        if name != EMIT_TOOL:
            continue
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


__all__ = [
    "EMIT_TOOL",
    "MAX_REPAIR_ROUNDS",
    "WorkerTableGenerator",
    "build_payload",
    "build_table",
    "emit_tool",
    "roster_note",
    "render_charter",
]
