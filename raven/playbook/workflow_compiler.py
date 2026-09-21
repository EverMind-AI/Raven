"""Compile an accepted run DAG into a durable, parameterized v2 Workflow."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import ValidationError

from raven.playbook.agent_spec import AgentPlaybookSpec
from raven.playbook.prompt import _inline_local_refs
from raven.playbook.types import ParamSpec, slugify
from raven.playbook.unified import (
    InputSchema,
    PlaybookMatch,
    PlaybookMetadata,
    UnifiedPlaybookSpec,
    WorkflowSpec,
)

if TYPE_CHECKING:
    from raven.agent.subagent.dag_graph import SubAgentDagSpec
    from raven.providers.base import LLMProvider

EMIT_WORKFLOW = "emit_reusable_workflow"

SYSTEM_PROMPT = """\
You compile one accepted execution DAG into a reusable Playbook Workflow.
Preserve its nodes, dependencies, worker aliases, skills, MCP names, inputs and
instance continuity. Replace only concrete values inside promptTemplate that
are expected to change between runs with ${params.<name>} references and declare those inputs.
Every declared input must carry the replaced concrete value as its default so
the accepted prompt can be reconstructed exactly. Do not invent extra steps
and do not emit a prompt-mode template. Retrieval keywords
describe the user's domain and action, never the words playbook/workflow.
The Harness is supplied separately and must not be rewritten here.
"""
_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z_][A-Za-z0-9_-]*)\}")


def _tool() -> list[dict[str, Any]]:
    from raven.agent.subagent.dag_graph import SubAgentDagSpec

    dag = _inline_local_refs(SubAgentDagSpec.model_json_schema(by_alias=True))
    node_items = dag["properties"]["nodes"]["items"]
    param = _inline_local_refs(ParamSpec.model_json_schema(by_alias=True))
    return [
        {
            "type": "function",
            "function": {
                "name": EMIT_WORKFLOW,
                "description": "Emit the reusable Workflow compiled from the accepted DAG.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                        "description": {"type": "string", "maxLength": 200},
                        "match": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            },
                            "required": ["summary", "keywords"],
                            "additionalProperties": False,
                        },
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["object"]},
                                "properties": {"type": "object", "additionalProperties": param},
                                "required": {"type": "array", "items": {"type": "string"}},
                                "additionalProperties": {"type": "boolean", "enum": [False]},
                            },
                            "required": ["type", "properties", "required", "additionalProperties"],
                            "additionalProperties": False,
                        },
                        "workflow": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "confirm": {"type": "boolean"},
                                "nodes": {"type": "array", "items": node_items, "minItems": 1},
                            },
                            "required": ["summary", "confirm", "nodes"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["name", "description", "match", "inputSchema", "workflow"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _args(response: Any) -> dict[str, Any] | None:
    for call in getattr(response, "tool_calls", None) or []:
        if isinstance(call, dict):
            fn = call.get("function") or {}
            name, raw = fn.get("name"), fn.get("arguments")
        else:
            name, raw = getattr(call, "name", None), getattr(call, "arguments", None)
        if name != EMIT_WORKFLOW:
            continue
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _prompt_is_preserved(source: str, compiled: str, artifact: UnifiedPlaybookSpec) -> bool:
    """Whether declared defaults restore the exact accepted instruction."""
    if source == compiled:
        return True
    missing = False

    def restore(match: re.Match[str]) -> str:
        nonlocal missing
        param = artifact.input_schema.properties.get(match.group(1))
        if param is None or param.default is None:
            missing = True
            return ""
        return str(param.default)

    restored, references = _PARAM_REF_RE.subn(restore, compiled)
    return references > 0 and not missing and restored == source


def _preservation_errors(accepted: "SubAgentDagSpec", artifact: UnifiedPlaybookSpec) -> list[str]:
    """Ensure compilation parameterizes prompts without rewriting the proven graph."""
    if artifact.workflow is None:
        return ["compiled artifact has no Workflow"]
    expected_ids = [node.id for node in accepted.nodes]
    actual_ids = [node.id for node in artifact.workflow.nodes]
    errors: list[str] = []
    if artifact.workflow.confirm != accepted.confirm:
        errors.append("workflow confirm changed during compilation")
    if actual_ids != expected_ids:
        errors.append(f"workflow node ids/order changed: expected {expected_ids}, got {actual_ids}")
        return errors
    actual = {node.id: node for node in artifact.workflow.nodes}
    for source in accepted.nodes:
        compiled = actual[source.id]
        for field in ("subagent", "node_summary", "depends_on", "skills", "mcps", "inputs", "instance"):
            if getattr(compiled, field) != getattr(source, field):
                errors.append(f"node {source.id!r}: {field} changed during compilation")
        if not _prompt_is_preserved(source.prompt_template, compiled.prompt_template, artifact):
            errors.append(f"node {source.id!r}: prompt_template changed beyond declared parameter defaults")
    return errors


class WorkflowCompiler:
    """One compile call plus one repair, with a lossless deterministic fallback."""

    def __init__(self, provider: "LLMProvider", model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    async def compile(
        self,
        *,
        query: str,
        dag: "SubAgentDagSpec",
        run_id: str,
        harness: AgentPlaybookSpec | None = None,
        name_hint: str | None = None,
        description_hint: str | None = None,
    ) -> UnifiedPlaybookSpec:
        payload = {
            "query": query,
            "acceptedDag": dag.model_dump(by_alias=True, exclude_none=True),
            "workerAliases": [entry.label for entry in harness.delegate] if harness else [],
            "nameHint": name_hint,
            "descriptionHint": description_hint,
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ]
        for _ in range(2):
            try:
                response = await self._provider.chat_with_retry(
                    messages=messages,
                    tools=_tool(),
                    model=self._model or None,
                    tool_choice={"type": "function", "function": {"name": EMIT_WORKFLOW}},
                )
                data = _args(response)
                if data is None:
                    raise ValueError(f"model did not call {EMIT_WORKFLOW}")
                data["schemaVersion"] = 2
                data["state"] = "ready"
                data["harness"] = harness.model_dump(by_alias=True, exclude_none=True) if harness else None
                data["metadata"] = PlaybookMetadata(source_run_id=run_id).model_dump(by_alias=True)
                artifact = UnifiedPlaybookSpec.model_validate(data)
                if errors := _preservation_errors(dag, artifact):
                    raise ValueError("; ".join(errors))
                from raven.playbook.runtime import validation_errors

                allowed_agents = {node.subagent for node in dag.nodes}
                if harness:
                    allowed_agents.update(entry.name for entry in harness.delegate)
                    allowed_agents.update(entry.label for entry in harness.delegate)
                if errors := validation_errors(artifact, sorted(allowed_agents)):
                    raise ValueError("; ".join(errors))
                return artifact
            except (ValidationError, ValueError, TypeError) as exc:
                messages.append(
                    {
                        "role": "user",
                        "content": f"The compiled Workflow was invalid: {exc}. Emit the complete corrected artifact.",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - compilation has a safe fallback
                logger.warning("playbook workflow compiler failed: {}", exc)
                break
        return self._fallback(query, dag, run_id, harness, name_hint, description_hint)

    @staticmethod
    def _fallback(
        query: str,
        dag: "SubAgentDagSpec",
        run_id: str,
        harness: AgentPlaybookSpec | None,
        name_hint: str | None,
        description_hint: str | None,
    ) -> UnifiedPlaybookSpec:
        words = [word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", query)[:8]]
        summary = (description_hint or query.strip().splitlines()[0] or dag.task_summary)[:200]
        name = slugify(name_hint or dag.task_summary or summary)
        return UnifiedPlaybookSpec(
            name=name,
            description=summary,
            match=PlaybookMatch(summary=summary, keywords=words or [summary[:80]]),
            input_schema=InputSchema(),
            harness=harness,
            workflow=WorkflowSpec(summary=dag.task_summary, confirm=dag.confirm, nodes=dag.nodes),
            metadata=PlaybookMetadata(source_run_id=run_id),
        )


__all__ = ["EMIT_WORKFLOW", "WorkflowCompiler"]
