"""Generate either a minimal Task Harness or a rich Persona Harness.

The two modes intentionally have separate prompts, structured inputs, and tool
schemas. Task selects existing agents with only a reusable prompt and suggested
tools; Persona may use the complete enabled Harness surface, including generated
participant functions. Neither generator emits a Workflow. One repair round is
allowed, after which the ordinary unconfigured turn continues.
"""

from __future__ import annotations

import json
import textwrap
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
TASK_TOOL = "create_task_playbook"
PERSONA_TOOL = "create_persona_playbook"
# Compatibility alias for callers of the original task-only generator.
EMIT_TOOL = TASK_TOOL

HarnessDisposition = Literal["none", "runtime", "artifact", "runtime_and_artifact"]
GenerationMode = Literal["task", "persona"]


@dataclass(frozen=True)
class HarnessResolution:
    """A validated generated Harness ready for binding and persistence."""

    disposition: HarnessDisposition = "none"
    spec: AgentPlaybookSpec | None = None
    table: DelegateTable | None = None
    selected_playbook: str | None = None
    artifact_name: str | None = None
    capture_workflow: bool = False
    active: bool = False
    description: str = ""
    generation_mode: GenerationMode | None = None
    persisted: bool = False


TASK_SYSTEM_PROMPT = (
    "Create a reusable task Playbook Harness for the request. Call create_task_playbook and emit no prose.\n\n"
    "Treat the user text as one task instance, not a persona specification. Select registered agents by their "
    "declared ownership and capabilities; a generic agent must not absorb work explicitly owned by a specialist. "
    "Each worker has exactly one reusable prompt plus optional suggested tools. Keep run-specific companies, "
    "dates, destinations, and other values out of that durable prompt: the actual task is injected separately "
    "when spawn or a DAG node dispatches the worker. Do not invent a DAG here. The main agent plans and executes "
    "normally, and the host later captures a complete successful DAG as the Workflow. Use no fields beyond the "
    "offered schema."
)

PERSONA_SYSTEM_PROMPT = (
    "Create a durable digital-person Harness. Call create_persona_playbook and emit no prose.\n\n"
    "Treat the user text as requirements for the persona's future capabilities, behavior, style, and constraints. "
    "Workers are operating components of the persona, not temporary authors asked to design or save it. Write "
    "every brief, system prompt, stop condition, check, and function for future runtime behavior; remove creation-"
    "time commands. Choose distinct specialist owners for materially different responsibilities, and never let a "
    "generic agent absorb work a specialist explicitly owns. Do not design a Workflow or invent a DAG for the act "
    "of creating the persona. Generate participant functions only for concrete long-lived runtime rules that "
    "ordinary instructions cannot enforce, following the supplied contracts and participantFunctionSyntax exactly. "
    "Never use for/while statements, try/except, imports, append, or an unlisted call in generated functions."
)

# Compatibility for direct imports that historically meant the task generator.
SYSTEM_PROMPT = TASK_SYSTEM_PROMPT

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
The two are held together by :func:`_persona_spec_from_args`, which validates what
comes back against the real model.
"""

FUNCTION_REFERENCE_IMPLEMENTATIONS: dict[str, str] = {
    "intake": "def intake(text, step):\n    return None",
    "advise": "def advise(step):\n    return None",
    "judge": "def judge(name, params, prior):\n    return []",
    "salvage": "def salvage(step):\n    return None",
}
"""The generated, synchronous equivalents of AgentParticipant's default no-ops."""


_STEP_FIELDS = (
    "session_key, iteration, response, transcript, history, turn_base, question, "
    "rollbacks, mode, mode_overlay, phase, tools, window, max_iterations, tools_ran"
)


_FUNCTION_CONTRACTS: dict[str, dict[str, str]] = {
    "intake": {
        "when": "Runs once on inbound user text, before any model call.",
        "returns": (
            "None for no opinion; otherwise a dict. text replaces the inbound text, reply ends the turn "
            "before a model call, and note is diagnostic. Across participants, text changes are threaded "
            "in order and the first non-null reply stops intake."
        ),
    },
    "advise": {
        "when": "Runs around model calls with the current read-only step.",
        "returns": (
            "None for no opinion or a guidance string. Guidance from all participants is joined in order "
            "with a blank line."
        ),
    },
    "judge": {
        "when": "Runs synchronously before each worker tool call.",
        "returns": (
            "An empty list to allow the call, or refusal sentences returned to the worker so it can retry. "
            "The first participant with a non-empty refusal decides."
        ),
    },
    "salvage": {
        "when": "Runs only when a turn otherwise ended without a final answer.",
        "returns": "None for no opinion or a final reply string. The first non-empty string decides.",
    },
}


def participant_function_guide() -> dict[str, dict[str, str]]:
    """The exact runtime contract and executable no-op each generated hook extends."""
    guide: dict[str, dict[str, str]] = {}
    for module, name in (
        ("memory", "intake"),
        ("planning", "advise"),
        ("action", "judge"),
        ("action", "salvage"),
    ):
        if not function_enabled(module, "participant", name):
            continue
        guide[name] = {
            "signature": FUNCTION_REFERENCE_IMPLEMENTATIONS[name].splitlines()[0],
            "defaultImplementation": FUNCTION_REFERENCE_IMPLEMENTATIONS[name],
            "when": _FUNCTION_CONTRACTS[name]["when"],
            "returnsAndComposition": _FUNCTION_CONTRACTS[name]["returns"],
            "availableInputs": (
                "step is a read-only JSON-like dict with " + _STEP_FIELDS
                if name != "judge"
                else "name is the tool name; params is its argument dict; prior is [(tool_name, params), ...]."
            ),
        }
    return guide


def participant_function_syntax_guide() -> dict[str, Any]:
    """The executable subset enforced by charter_code, in model-facing terms."""
    return {
        "shape": "Exactly one synchronous def with the supplied signature; no statements outside it.",
        "allowedStatements": ["assignment", "if", "return"],
        "allowedIteration": "Use a list or generator comprehension; for and while statements are forbidden.",
        "allowedCalls": [
            "len",
            "str",
            "int",
            "float",
            "bool",
            "isinstance",
            "any",
            "all",
            "sorted",
            "set",
            "list",
            "dict",
            "tuple",
            "min",
            "max",
            "abs",
        ],
        "allowedMethods": [
            "startswith",
            "endswith",
            "lower",
            "upper",
            "strip",
            "split",
            "get",
            "items",
            "keys",
            "values",
            "count",
            "join",
        ],
        "forbidden": [
            "for or while statements",
            "try/except",
            "imports",
            "with",
            "raise",
            "lambda",
            "async/await",
            "append or any unlisted call or method",
        ],
    }


def _choice_profile(choice: Any) -> dict[str, str]:
    """A mode/model choice without transport-owned or secret configuration."""
    read = choice.get if isinstance(choice, dict) else lambda key, default="": getattr(choice, key, default)
    profile = {
        "id": str(read("id", "") or read("value", "") or ""),
        "name": str(read("name", "") or read("label", "") or ""),
        "description": str(read("description", "") or ""),
    }
    return {key: value for key, value in profile.items() if value}


def task_roster_profile(meta: Any) -> dict[str, Any]:
    """Only the safe AgentMeta facts the task selector can act on."""
    return {
        "description": str(getattr(meta, "description", "") or ""),
        "owns": str(getattr(meta, "owns", "") or ""),
        "stateful": bool(getattr(meta, "stateful", False)),
        "readsLocalFiles": bool(getattr(meta, "reads_local_files", False)),
        "liveProgress": bool(getattr(meta, "live_progress", False)),
        "ownsWatchedWork": bool(getattr(meta, "owns_watched_work", False)),
    }


def persona_roster_profile(meta: Any) -> dict[str, Any]:
    """Every safe AgentMeta fact that can shape a durable persona."""
    return {
        "description": str(getattr(meta, "description", "") or ""),
        "owns": str(getattr(meta, "owns", "") or ""),
        "stateful": bool(getattr(meta, "stateful", False)),
        "readsLocalFiles": bool(getattr(meta, "reads_local_files", False)),
        "liveProgress": bool(getattr(meta, "live_progress", False)),
        "ownsWatchedWork": bool(getattr(meta, "owns_watched_work", False)),
        "modes": [profile for choice in (getattr(meta, "modes", ()) or ()) if (profile := _choice_profile(choice))],
        "modelChoices": [
            profile for choice in (getattr(meta, "model_choices", ()) or ()) if (profile := _choice_profile(choice))
        ],
    }


# Compatibility for callers that asked for the formerly single rich profile.
roster_profile = persona_roster_profile


def _profile_summary(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    prose = str(value.get("owns") or value.get("description") or "").strip().rstrip(".")
    nested = value.get("capabilities") if isinstance(value.get("capabilities"), dict) else {}
    caps = {
        **nested,
        **{
            key: value[key]
            for key in ("readsLocalFiles", "stateful", "liveProgress", "ownsWatchedWork")
            if key in value
        },
    }
    tags = [
        label
        for key, label in (
            ("readsLocalFiles", "reads local files"),
            ("stateful", "resumable"),
            ("liveProgress", "reports live progress"),
            ("ownsWatchedWork", "owns watched work"),
        )
        if caps.get(key)
    ]
    if prose and tags:
        return f"{prose} ({'; '.join(tags)})"
    return prose or "; ".join(tags)


def _profile_payload(value: Any) -> dict[str, Any]:
    """Normalize the structured profile while tolerating the old prose form."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        return {"description": value.strip()}
    return {}


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


def _roster_description(agent_names: list[str], agent_notes: "Mapping[str, Any] | None") -> str:
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
        f"- {name}: {summary}" for name in agent_names if (summary := _profile_summary((agent_notes or {}).get(name)))
    ]
    return head if not lines else head + " What each one is for:\n" + "\n".join(lines)


def task_tool(agent_names: list[str], tool_names: list[str]) -> list[dict[str, Any]]:
    """The deliberately small task selector: agent, reusable prompt, and tools."""
    worker = {
        "type": "object",
        "properties": {
            "as": {
                "type": "string",
                "minLength": 1,
                "description": "Optional local label; omit when the registered agent name is sufficient.",
            },
            "agent": {
                "type": "string",
                "enum": agent_names,
                "description": "The registered sub-agent that owns this part of the task.",
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Reusable responsibilities and instructions for this worker. Do not include values "
                    "specific to this run; the dispatch supplies the actual task separately."
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string", "enum": tool_names},
                "description": "Optional tools this worker's role calls for; guidance, not permission.",
            },
        },
        "required": ["agent", "prompt"],
        "additionalProperties": False,
    }
    if not parameter_enabled("capability", "tools"):
        worker["properties"].pop("tools")
    return [
        {
            "type": "function",
            "function": {
                "name": TASK_TOOL,
                "description": (
                    "Create and save the minimal worker selection for a reusable task. Do not emit a DAG "
                    "or any Persona-only Harness field."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifactName": {
                            "type": "string",
                            "pattern": "^[a-z0-9][a-z0-9-]*$",
                            "description": "Durable kebab-case Playbook name.",
                        },
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "One reusable sentence describing the task Playbook.",
                        },
                        "workers": {"type": "array", "items": worker, "minItems": 1},
                    },
                    "required": ["artifactName", "description", "workers"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def persona_tool(
    agent_names: list[str],
    tool_names: list[str],
    agent_notes: "Mapping[str, Any] | None" = None,
) -> list[dict[str, Any]]:
    """The rich Persona Harness tool, with the install's own enums.

    The roster is an ``enum`` and the tool list is an ``enum`` for the same
    reason: a name the host cannot resolve is worth refusing at the boundary
    rather than diagnosing after.
    """
    worker: dict[str, Any] = {
        "type": "object",
        "properties": {
            "as": {
                "type": "string",
                "minLength": 1,
                "description": ("Short stable label for this persona component (for example 'research-a')."),
            },
            "agent": {
                "type": "string",
                "enum": agent_names,
                "description": _roster_description(agent_names, agent_notes),
            },
            "brief": {
                "type": "string",
                "minLength": 1,
                "description": "One line on what this worker is for, read by the agent that dispatches it.",
            },
            "systemPrompt": {
                "type": "string",
                "description": "Durable persona instructions appended to the worker's existing identity. Omit when the brief is enough.",
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
            "timeoutSeconds": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional: a deadline for this worker, in seconds. Tightening only -- it "
                    "cannot lengthen a limit an operator set. Give one only when the job is "
                    "genuinely bounded; a long job is a long job."
                ),
            },
        },
        "required": ["as", "agent", "brief"],
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
    function_properties = {
        name: {
            "type": "string",
            "description": participant_function_guide()[name]["returnsAndComposition"]
            + " Use the exact signature in the supplied participantFunctions input and the allow-listed Python subset.",
        }
        for module, name in (
            ("memory", "intake"),
            ("planning", "advise"),
            ("action", "judge"),
            ("action", "salvage"),
        )
        if function_enabled(module, "participant", name)
    }
    if function_properties:
        properties["functions"] = {
            "type": "object",
            "properties": function_properties,
            "additionalProperties": False,
            "description": "Optional generated participant functions, keyed by their loop verb.",
        }
    description = {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "description": "One sentence explaining the generated setup.",
    }
    artifact_name = {
        "type": "string",
        "pattern": "^[a-z0-9][a-z0-9-]*$",
        "description": "Durable kebab-case Playbook name.",
    }

    def decision_tool(
        name: str,
        summary: str,
        schema_properties: dict[str, Any],
        required: list[str],
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": summary,
                "parameters": {
                    "type": "object",
                    "properties": schema_properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    return [
        decision_tool(
            PERSONA_TOOL,
            (
                "Create a durable digital person, assistant, or agent Harness. Workers are the persona's "
                "future operating parts, not authors asked to create it. Write future-facing briefs, system "
                "prompts, tools, functions, checks, and stop conditions; remove creation-time commands such "
                "as save it, do not run now, or only plan later. A single named persona may still use several "
                "workers when different offered specialists own materially different behavior. Use intake "
                "for an enforceable missing-input gate, not merely to repeat requirements. This branch never "
                "captures a Workflow."
            ),
            {
                "description": description,
                "artifactName": artifact_name,
                "workers": {"type": "array", "items": worker, "minItems": 1},
            },
            ["artifactName", "description", "workers"],
        ),
    ]


def emit_tool(
    agent_names: list[str],
    tool_names: list[str],
    agent_notes: "Mapping[str, Any] | None" = None,
    playbook_candidates: "Mapping[str, str] | None" = None,
) -> list[dict[str, Any]]:
    """Backward-compatible name for the task-only tool schema."""
    del agent_notes, playbook_candidates
    return task_tool(agent_names, tool_names)


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


def _short_description(value: Any) -> str:
    """Keep generated index prose readable when a provider misses maxLength."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= 200 else textwrap.shorten(text, width=200, placeholder="…")


def _task_spec_from_args(
    args: dict[str, Any],
    roster: set[str],
    available_tools: set[str] | None = None,
) -> tuple[AgentPlaybookSpec, dict[str, str]]:
    """Build the minimal Task Harness and reject every Persona-only field."""
    rows = args.get("workers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("workers must be a non-empty list")
    delegate: list[dict[str, Any]] = []
    briefs: dict[str, str] = {}
    errors: list[str] = []
    allowed = {"as", "agent", "prompt", "tools"}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"{index}. a worker must be an object")
            continue
        unexpected = sorted(set(row) - allowed)
        if unexpected:
            errors.append(f"{index}. Task worker field(s) not allowed: {', '.join(unexpected)}")
            continue
        name = str(row.get("agent") or "")
        prompt = str(row.get("prompt") or "").strip()
        if name not in roster:
            errors.append(f"{index}. unknown agent: {name or '<empty>'}")
            continue
        if not prompt:
            errors.append(f"{index}. prompt must be non-empty")
            continue
        tools = row.get("tools")
        if tools is not None and not isinstance(tools, list):
            errors.append(f"{index}. tools must be an array")
            continue
        if tools is not None and not parameter_enabled("capability", "tools"):
            errors.append(f"{index}. disabled harness field(s): tools")
            continue
        unknown_tools = sorted({str(tool) for tool in tools or []} - (available_tools or set()))
        if available_tools is not None and unknown_tools:
            errors.append(f"{index}. unknown tool(s): {', '.join(unknown_tools)}")
            continue
        label = str(row.get("as") or "") or name
        sub = {"capability": {"tools": [str(tool) for tool in tools]}} if tools is not None else None
        delegate.append({"as": label, "name": name, "brief": prompt, "playbook": sub})
        briefs[label] = prompt
    if errors:
        raise ValueError("; ".join(errors))
    payload = {
        "description": _short_description(args.get("description")),
        "delegate": delegate,
    }
    return AgentPlaybookSpec.model_validate(payload), briefs


def _persona_spec_from_args(
    args: dict[str, Any],
    roster: set[str],
    available_tools: set[str] | None = None,
) -> tuple[AgentPlaybookSpec, dict[str, str]]:
    """Build a rich Persona Harness, or raise with what to repair."""
    rows = args.get("workers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("workers must be a non-empty list")
    delegate: list[dict[str, Any]] = []
    briefs: dict[str, str] = {}
    errors: list[str] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"{index}. a worker must be an object")
            continue
        allowed = {
            "as",
            "agent",
            "brief",
            "systemPrompt",
            "stopWhen",
            "tools",
            "checks",
            "functions",
            "timeoutSeconds",
        }
        unexpected = sorted(set(row) - allowed)
        if unexpected:
            errors.append(f"{index}. Persona worker field(s) not allowed: {', '.join(unexpected)}")
            continue
        name = str(row.get("agent") or "")
        if name not in roster:
            # Dropped rather than repaired: the roster was an enum, so a name
            # outside it is a shape the request could not express, and spending
            # a round on it teaches the model nothing it was not already told.
            logger.info("agent playbook: dropping worker {!r} -- not on the roster", name)
            continue
        label = str(row.get("as") or "").strip()
        brief = str(row.get("brief") or "").strip()
        if not label:
            errors.append(f"{index}. as must be non-empty")
        if not brief:
            errors.append(f"{index}. brief must be non-empty")
        switches = (
            ("systemPrompt", parameter_enabled("memory", "systemPrompt")),
            ("stopWhen", parameter_enabled("memory", "stopWhen")),
            ("tools", parameter_enabled("capability", "tools")),
            ("checks", parameter_enabled("action", "checks")),
        )
        disabled = [field for field, enabled in switches if field in row and not enabled]
        if disabled:
            errors.append(f"{index}. disabled harness field(s): {', '.join(disabled)}")
            continue
        raw_functions = row.get("functions")
        if "functions" in row and not isinstance(raw_functions, dict):
            errors.append(f"{index}. functions must be an object")
            continue
        function_modules = {
            "intake": "memory",
            "advise": "planning",
            "judge": "action",
            "salvage": "action",
        }
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
            if function_name == "judge":
                sub.setdefault("action", {}).setdefault("checks", {})["code"] = source
            else:
                sub.setdefault(module, {}).setdefault("functions", {})[function_name] = source
        raw_tools = row.get("tools")
        if "tools" in row and not isinstance(raw_tools, list):
            errors.append(f"{index}. tools must be an array")
            continue
        if isinstance(raw_tools, list):
            requested_tools = [str(x) for x in raw_tools]
            unknown_tools = sorted(set(requested_tools) - (available_tools or set()))
            if available_tools is not None and unknown_tools:
                errors.append(f"{index}. unknown tool(s): {', '.join(unknown_tools)}")
                continue
            sub.setdefault("capability", {})["tools"] = requested_tools
        # Both halves of the judgement seat land under one key, because a
        # worker with code and no rules is as ordinary as one with rules and no
        # code -- the two are alternatives, not a base and an extension.
        checks: dict[str, Any] = dict(sub.get("action", {}).get("checks", {}))
        raw_checks = row.get("checks")
        if "checks" in row and not isinstance(raw_checks, list):
            errors.append(f"{index}. checks must be an array")
            continue
        if isinstance(raw_checks, list) and raw_checks:
            checks["rules"] = raw_checks
        if checks:
            sub.setdefault("action", {})["checks"] = checks
        timeout = row.get("timeoutSeconds")
        if "timeoutSeconds" in row:
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                errors.append(f"{index}. timeoutSeconds must be a positive integer")
                continue
            sub["timeoutSeconds"] = timeout
        delegate.append({"as": label, "name": name, "brief": brief, "playbook": sub or None})
        briefs[label] = brief
    if errors:
        raise ValueError("; ".join(errors))
    payload = {"delegate": delegate}
    if args.get("description"):
        payload["description"] = _short_description(args["description"])
    return AgentPlaybookSpec.model_validate(payload), briefs


def _tool_inventory(tool_catalog: list[Any]) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for item in tool_catalog:
        if isinstance(item, str):
            name, description = item, ""
        elif isinstance(item, dict):
            function = item.get("function", item)
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            description = str(function.get("description") or "")
        else:
            continue
        if name:
            inventory.append({"name": name, "description": description})
    return sorted(inventory, key=lambda item: item["name"])


def persona_parameter_guide() -> dict[str, str]:
    """The Persona-only Harness parameters and their runtime meaning."""
    guide = {
        "brief": "The worker's durable responsibility.",
        "systemPrompt": "Long-lived behavior and style appended to the worker identity.",
        "stopWhen": "What means this worker is finished.",
        "tools": "Suggested tools for this role; guidance, not permission.",
        "checks": "Declarative rules evaluated before worker tool calls.",
        "functions": "Generated Participant runtime functions.",
        "timeoutSeconds": "A tightening-only dispatch deadline.",
    }
    switches = {
        "systemPrompt": parameter_enabled("memory", "systemPrompt"),
        "stopWhen": parameter_enabled("memory", "stopWhen"),
        "tools": parameter_enabled("capability", "tools"),
        "checks": parameter_enabled("action", "checks"),
    }
    return {name: detail for name, detail in guide.items() if switches.get(name, True)}


class _PlaybookGenerator:
    mode: GenerationMode
    prompt: str
    tool_name: str

    def __init__(self, provider: "LLMProvider", model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    async def _resolve(
        self,
        *,
        query: str,
        agent_names: list[str],
        user_payload: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> HarnessResolution:
        if not query.strip() or not agent_names:
            return HarnessResolution()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ]
        roster = set(agent_names)
        for _ in range(1 + MAX_REPAIR_ROUNDS):
            try:
                response = await self._provider.chat_with_retry(
                    messages=messages,
                    tools=tools,
                    model=self._model or None,
                    tool_choice={"type": "function", "function": {"name": self.tool_name}},
                )
            except Exception as exc:  # noqa: BLE001 - setup failure must not cost the turn
                logger.warning("agent playbook: {} generation failed ({}); running unconfigured", self.mode, exc)
                return HarnessResolution()
            args = _emitted_args(response, self.tool_name)
            if args is None:
                messages.append({"role": "user", "content": f"Call {self.tool_name} with a valid payload."})
                continue
            try:
                if self.mode == "task":
                    spec, briefs = _task_spec_from_args(
                        args, roster, {item["name"] for item in user_payload["availableTools"]}
                    )
                else:
                    spec, briefs = _persona_spec_from_args(
                        args,
                        roster,
                        {item["name"] for item in user_payload["availableTools"]},
                    )
                from raven.playbook.types import slugify

                description = _short_description(args.get("description") or spec.description or query.splitlines()[0])
                if not description:
                    raise ValueError("description must be non-empty")
                artifact_name = slugify(str(args.get("artifactName") or description))
                spec = spec.model_copy(update={"name": artifact_name, "description": description})
                table = build_table(spec, briefs)
            except Exception as exc:  # noqa: BLE001 - the message is the repair prompt
                logger.info("agent playbook: rejected {} Harness, repairing once ({})", self.mode, exc)
                messages.append({"role": "user", "content": f"That Harness was rejected: {exc}\nEmit a corrected one."})
                continue
            return HarnessResolution(
                disposition="runtime_and_artifact" if self.mode == "task" else "artifact",
                active=True,
                spec=spec,
                table=table,
                artifact_name=artifact_name,
                capture_workflow=self.mode == "task",
                description=description,
                generation_mode=self.mode,
            )
        logger.warning("agent playbook: {} Harness still invalid after one repair; running unconfigured", self.mode)
        return HarnessResolution()


class TaskPlaybookGenerator(_PlaybookGenerator):
    """Select existing agents with only a reusable prompt and suggested tools."""

    mode: GenerationMode = "task"
    prompt = TASK_SYSTEM_PROMPT
    tool_name = TASK_TOOL

    async def resolve(
        self,
        query: str,
        agent_names: list[str],
        tool_catalog: list[Any],
        agent_profiles: "Mapping[str, Any] | None" = None,
        playbook_candidates: "Mapping[str, str] | None" = None,
    ) -> HarnessResolution:
        del playbook_candidates  # Retrieval is explicitly outside this version.
        inventory = _tool_inventory(tool_catalog)
        payload = {
            "task": query,
            "agents": [
                {"name": name, **_profile_payload((agent_profiles or {}).get(name))} for name in sorted(agent_names)
            ],
            "availableTools": inventory,
        }
        return await self._resolve(
            query=query,
            agent_names=agent_names,
            user_payload=payload,
            tools=task_tool(sorted(agent_names), [item["name"] for item in inventory]),
        )

    async def generate(
        self,
        query: str,
        agent_names: list[str],
        tool_catalog: list[Any],
        agent_profiles: "Mapping[str, Any] | None" = None,
    ) -> DelegateTable | None:
        return (await self.resolve(query, agent_names, tool_catalog, agent_profiles)).table


class PersonaPlaybookGenerator(_PlaybookGenerator):
    """Generate the full durable Harness surface for a digital person."""

    mode: GenerationMode = "persona"
    prompt = PERSONA_SYSTEM_PROMPT
    tool_name = PERSONA_TOOL

    async def resolve(
        self,
        query: str,
        agent_names: list[str],
        tool_catalog: list[Any],
        agent_profiles: "Mapping[str, Any] | None" = None,
        existing_artifact_names: list[str] | None = None,
    ) -> HarnessResolution:
        inventory = _tool_inventory(tool_catalog)
        profiles = {name: _profile_payload((agent_profiles or {}).get(name)) for name in sorted(agent_names)}
        payload = {
            "personaRequirements": query,
            "agentProfiles": profiles,
            "availableTools": inventory,
            "harnessParameters": persona_parameter_guide(),
            "participantFunctions": participant_function_guide(),
            "participantFunctionSyntax": participant_function_syntax_guide(),
            "existingArtifactNames": sorted(existing_artifact_names or []),
        }
        return await self._resolve(
            query=query,
            agent_names=agent_names,
            user_payload=payload,
            tools=persona_tool(
                sorted(agent_names),
                [item["name"] for item in inventory],
                profiles,
            ),
        )


class WorkerTableGenerator(TaskPlaybookGenerator):
    """Backward-compatible task-only generator name."""


def _emitted_args(response: Any, expected_tool: str = EMIT_TOOL) -> dict[str, Any] | None:
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
        if name != expected_tool:
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
    "FUNCTION_REFERENCE_IMPLEMENTATIONS",
    "MAX_REPAIR_ROUNDS",
    "PERSONA_SYSTEM_PROMPT",
    "PERSONA_TOOL",
    "PersonaPlaybookGenerator",
    "TASK_SYSTEM_PROMPT",
    "TASK_TOOL",
    "TaskPlaybookGenerator",
    "WorkerTableGenerator",
    "build_payload",
    "build_table",
    "emit_tool",
    "participant_function_guide",
    "participant_function_syntax_guide",
    "persona_parameter_guide",
    "persona_roster_profile",
    "persona_tool",
    "roster_note",
    "roster_profile",
    "render_charter",
    "task_roster_profile",
    "task_tool",
]
