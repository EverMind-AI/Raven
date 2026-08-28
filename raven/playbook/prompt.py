"""Prompt assembly for playbook generation — the tunable part of the module.

Everything the generation LLM is told lives here; ``generator.py`` holds no
prompt text. Three pieces:

- :data:`SYSTEM_PROMPT` — the static skeleton: the field definition's
  filling rules plus two compact few-shot sketches, one per mode.
- :func:`build_generation_prompt` — renders the live inventories (agent
  roster, retrieved skill candidates, mcps) and the user input.
- :func:`build_repair_prompt` — the validation-failure follow-up. Asks for
  a minimal edit, not a rewrite, so already-correct regions stay put.

The output is requested through a forced tool call whose parameter schema
is :func:`emit_tool` — derived from the pydantic contract at call time so
the two can never drift apart.
"""

from __future__ import annotations

import json
from typing import Any

from raven.playbook.agent_profiles import PlaybookAgentProfile
from raven.playbook.types import PlaybookSpec

EMIT_TOOL_NAME = "emit_playbook"

# Code-filled fields are stripped so the model cannot fight the generator.
# The model proposes a name, which the generator normalizes into a slug.
_CODE_FILLED_TOP = {"version"}


def _inline_local_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local ``$defs`` references for tool-schema compatibility."""
    definitions = schema.get("$defs") or {}

    def visit(value: Any, resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [visit(item, resolving) for item in value]
        if not isinstance(value, dict):
            return value
        if ref := value.get("$ref"):
            prefix = "#/$defs/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise ValueError(f"unsupported JSON Schema reference: {ref!r}")
            name = ref.removeprefix(prefix).replace("~1", "/").replace("~0", "~")
            if name not in definitions:
                raise ValueError(f"unknown JSON Schema reference: {ref!r}")
            if name in resolving:
                raise ValueError(f"recursive JSON Schema reference: {ref!r}")
            target = visit(definitions[name], (*resolving, name))
            siblings = visit({key: item for key, item in value.items() if key != "$ref"}, resolving)
            return {**target, **siblings}
        return {key: visit(item, resolving) for key, item in value.items() if key != "$defs"}

    return visit(schema)


def emit_tool() -> list[dict[str, Any]]:
    """The forced-call tool schema, derived from :class:`PlaybookSpec`.

    Two extra arrays ride the same call but never enter the machine block:
    the generator routes them into the body's review section (open
    questions and assumptions are for a human, not for the runtime)."""
    schema = _inline_local_refs(PlaybookSpec.model_json_schema(by_alias=True))
    for key in _CODE_FILLED_TOP:
        schema.get("properties", {}).pop(key, None)
    schema["required"] = [r for r in schema.get("required", []) if r not in _CODE_FILLED_TOP]
    props = schema.setdefault("properties", {})
    props["blockingQuestions"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Questions without which the playbook cannot be finalized.",
    }
    props["assumptions"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Defaults you adopted that the user may overturn.",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": EMIT_TOOL_NAME,
                "description": "Emit the generated playbook.",
                "parameters": schema,
            },
        }
    ]


SYSTEM_PROMPT = """\
You are the playbook generator. From one piece of user input, produce a
reusable task template (a playbook) and submit it through the emit_playbook
tool. A playbook describes how a family of tasks is carried out step by step
by sub-agents, so later tasks of the same family can reuse it. Fields are
camelCase.

# mode (the most important rule)

- The input spells out the steps ("first A, then B, finally C", a numbered
  list, an SOP) -> mode "dag"; the graph is written down in `nodes`. You
  translate faithfully: one node per user step, never adding, dropping or
  reordering steps. Your own additions are limited to identifying params and
  binding each node to an agent/skills.
- The input gives no steps -> mode "prompt"; do not guess a fixed graph on
  the user's behalf. Write `prompts`: assembly guidance a model uses at run
  time to compose the graph on the spot before executing it.
- dag never carries prompts; prompt never carries nodes. Once the graph
  exists the two modes run the exact same execution chain.

# name / description

- `name`: a lowercase hyphen slug (e.g. competitor-scan).
- `description`: <= 200 chars, "when to use me" written for retrieval, not a
  genre label.

# triggers

3-8 seed entries in `keywords` (words and phrases share the list): domain
proper nouns and action phrases ("post a tweet", "compare competitors").
Never mechanism words (playbook/workflow/template/automation) and never
everyday high-frequency words (article/report/data).

# params

Inputs that change per run become params (a map keyed by param name).
`description` is required: it doubles as the follow-up question when the
value is missing, so phrase it as a directly askable question. A param with
a default is never asked for. Templates reference values as
${params.<key>}. Anything fixable at generation time must not be a param.

# confirmation

`confirm` is graph-level. Set it to true when any step can make an
irreversible change (publish, file a ticket, send mail, write a database);
otherwise set it to false. Never put `confirm` on an individual node.

# nodes (dag mode)

- `subagent` must come from the available-agents list below.
- Agent capabilities are hard constraints: use `instance` only when
  `stateful=true`; use path placeholders only when `readsLocalFiles=true`;
  set `skills` only when `injectableSkills=true`; and set `mcps` only when
  `injectableMcps=true`. Otherwise leave that field unset or choose a capable
  agent.
- `nodeSummary` is this step's title, written before the prompt: the
  length of a chat title, under ten words -- not a sentence and not a
  summary of the prompt. It is the node's row while the run happens, so
  name the step; what it must do belongs in `promptTemplate`.
- Configuration hangs on the node, not the role: `skills`/`mcps` are what
  this step injects. Nodes using separate sessions may differ, but when nodes
  share an `instance`, only the node opening that session may set
  `skills`/`mcps`; continuation nodes must omit both fields. Skills may only
  reference names from the candidate list.
- `promptTemplate` is the step's work order: state the criteria, the
  prohibitions and the hard format constraints so the step's job is
  unambiguous. Reference upstream output with {{ <upstreamId>.output }}
  (content) or {{ <upstreamId>.output_path }} (file path); every referenced
  upstream must be listed in this node's dependsOn.
- `dependsOn` is the whole graph language: empty = a start node; several =
  a join; nodes with no dependency between them run in parallel.
- Consecutive steps by one agent that must keep memory (revising a draft
  has to remember the draft) -> the same `instance`, with a dependency
  between the nodes; a fresh perspective (review, critique) -> no shared
  instance.

# prompts (prompt mode)

Describe how to assemble the graph: how many layers, how many nodes per
layer, which agent with which skills/mcps, who dependsOn whom, how nodes
pass data with {{ }}, and what criteria each node's work order must state.
Never write run-time decision rules: the graph is fixed once assembled, so
loops and conditional fallbacks cannot be expressed — do not write them.

# Open items (report honestly)

- `blockingQuestions`: questions without which the playbook cannot be
  finalized. Prefer asking over silently inventing.
- `assumptions`: defaults you adopted that the user may overturn.
Both go to a human review section in the playbook body, not into machine
fields.

# Example (dag: the user gave steps — translate faithfully)

Input: "Weekly feedback analysis: first pull feedback from Slack and
Intercom, then cluster it into themes, finally write a weekly report for
the PM."
Key points: mode=dag; three nodes pull -> cluster -> report mirroring the
user's three steps (no more, no less); pull uses a data agent with data
skills, report uses a content agent; params: {week_of}; report's template
references {{ cluster.output }} and lists dependsOn: [cluster].

# Example (prompt: the user gave no steps)

Input: "Build me a due-diligence playbook."
Key points: mode=prompt; prompts holds the assembly guidance — "layer one:
a single research node doing a breadth scan producing three columns; layer
two: parallel nodes fanned out by focus, all dependsOn layer one, each
independent with no shared instance; layer three: one content node
summarizing, dependsOn all of layer two"; params: {target, focus};
blockingQuestions asks what the due diligence is for.
"""


def _render_skills(candidates: list[tuple[str, str]], user_pinned: list[str]) -> str:
    if not candidates and not user_pinned:
        return "(no candidates; leave skills empty)"
    lines = [f"- {name}: {desc}" for name, desc in candidates]
    if user_pinned:
        lines.insert(0, f"Pinned by the user (must be used): {', '.join(user_pinned)}")
    return "\n".join(lines)


def _render_agents(profiles: dict[str, PlaybookAgentProfile]) -> str:
    """Render only the safe capability view, using prompt-facing camelCase."""
    if not profiles:
        return "(agent table unavailable)"
    rows = {
        name: {
            "description": profile.description,
            "stateful": profile.stateful,
            "readsLocalFiles": profile.reads_local_files,
            "injectableSkills": profile.injectable_skills,
            "injectableMcps": profile.injectable_mcps,
        }
        for name, profile in profiles.items()
    }
    return json.dumps(rows, ensure_ascii=False, indent=2)


def build_generation_prompt(
    user_input: str,
    *,
    agent_profiles: dict[str, PlaybookAgentProfile],
    skill_candidates: list[tuple[str, str]],
    user_pinned_skills: list[str],
    known_mcp: list[str],
    inline_skill_docs: list[tuple[str, str]] | None = None,
) -> str:
    """Render the user message for one generation call.

    ``agent_profiles`` is the safe, runtime-effective capability view;
    ``skill_candidates`` are ``(name, one-line description)`` from the
    three-way retrieval; ``inline_skill_docs`` are ``(name, content)`` for
    skill *files* the user handed in directly.
    """
    parts = [
        "# Available agents (the only valid nodes[].subagent values)\n" + _render_agents(agent_profiles),
        "# Candidate skills (the only referencable names)\n" + _render_skills(skill_candidates, user_pinned_skills),
        "# Available mcp\n" + (", ".join(known_mcp) if known_mcp else "(none; leave mcps empty)"),
    ]
    for name, content in inline_skill_docs or []:
        parts.append(f"# Skill file provided by the user: {name}\n{content}")
    parts.append("# User input\n" + user_input.strip())
    return "\n\n".join(parts)


def build_repair_prompt(spec_json: dict[str, Any], errors: list[str]) -> str:
    """Follow-up message after a failed validation round."""
    numbered = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(errors))
    return (
        "Your previous playbook submission failed validation. Fix each error "
        "below and call emit_playbook again with the complete result. Change "
        "only what the errors point at; keep every other field as it was, do "
        "not rewrite.\n\n"
        f"# Validation errors\n{numbered}\n\n"
        f"# Your previous submission\n{json.dumps(spec_json, ensure_ascii=False, indent=2)}"
    )


def build_revise_prompt(
    current: PlaybookSpec,
    user_feedback: str,
    agent_profiles: dict[str, PlaybookAgentProfile],
) -> str:
    """User message for a revise round on an existing playbook."""
    return (
        "Below is an existing playbook and the user's feedback on it. Revise "
        "the playbook per the feedback and call emit_playbook again with the "
        "complete result.\n\n"
        f"# Available agents and runtime-effective capabilities\n{_render_agents(agent_profiles)}\n\n"
        f"# Current playbook\n{json.dumps(current.model_dump(by_alias=True), ensure_ascii=False, indent=2)}\n\n"
        f"# User feedback\n{user_feedback.strip()}"
    )


def _node_schema_of(schema: dict[str, Any]) -> dict[str, Any]:
    """The node definition a spec's ``nodes`` points at, whatever it is titled.

    Found by following the reference rather than by name: ``nodes`` is optional,
    so pydantic wraps it in ``anyOf`` and the ``$ref`` sits a level down, and the
    definition is titled after the class the alias points at. Both are shapes a
    name-and-one-level lookup gets wrong by returning ``{}`` -- silently, which
    is how an empty ``items`` reached the model.
    """

    def first_ref(node: Any) -> str:
        if isinstance(node, dict):
            if isinstance(node.get("$ref"), str):
                return node["$ref"]
            for v in node.values():
                if found := first_ref(v):
                    return found
        elif isinstance(node, list):
            for v in node:
                if found := first_ref(v):
                    return found
        return ""

    ref = first_ref((schema.get("properties") or {}).get("nodes"))
    return (schema.get("$defs") or {}).get(ref.rsplit("/", 1)[-1]) or {}


COMPOSE_TOOL_NAME = "emit_graph"


def compose_tool() -> list[dict[str, Any]]:
    """Forced-call schema for prompt-mode graph composition: a bare node list.

    The node schema is found through the model's own reference rather than by
    name. ``NodeSpec`` is an alias of ``DagNodeSpec`` since the two definitions
    merged, so pydantic titles the definition ``DagNodeSpec`` and a lookup by the
    alias returned ``{}`` -- ``items`` went out empty, leaving the prose in
    ``build_compose_prompt`` as the only field guidance the call had. That was
    survivable while the two agreed; a model following prose that names a field
    the model forbids gets one repair round holding nothing but "not permitted".
    """
    schema = PlaybookSpec.model_json_schema(by_alias=True)
    node_schema = _node_schema_of(schema)
    return [
        {
            "type": "function",
            "function": {
                "name": COMPOSE_TOOL_NAME,
                "description": "Submit the composed graph.",
                "parameters": {
                    "type": "object",
                    "properties": {"nodes": {"type": "array", "items": node_schema, "minItems": 1}},
                    "required": ["nodes"],
                },
            },
        }
    ]


def build_compose_prompt(
    prompts_filled: str,
    agent_profiles: dict[str, PlaybookAgentProfile],
    param_names: list[str],
) -> str:
    """The one-shot graph-composition request for a prompt-mode playbook."""
    return (
        "Assemble a task graph following the guidance below and submit the "
        "node list through emit_graph (camelCase fields:\n"
        "id / subagent / nodeSummary / promptTemplate / dependsOn / skills / mcps / inputs / instance).\n"
        "Rules: subagent must come from the available list; agent capabilities "
        "are hard constraints: use instance only when stateful=true, path placeholders "
        "only when readsLocalFiles=true, skills only when injectableSkills=true, and "
        "mcps only when injectableMcps=true; when nodes share an instance, only "
        "the node opening that session may set skills or mcps, and continuation "
        "nodes must omit both fields; every "
        "{{ <upstreamId>.output }} reference must have that upstream in the "
        "node's dependsOn;\n"
        "nodes with no dependency between them run in parallel; the graph is "
        "fixed once assembled, so design no run-time branches or loops.\n"
        f"Params are already substituted (original param names: "
        f"{', '.join(param_names) if param_names else 'none'}); "
        "no ${params.*} may appear in promptTemplate.\n\n"
        f"# Available agents\n{_render_agents(agent_profiles)}\n\n"
        f"# Assembly guidance\n{prompts_filled}"
    )
