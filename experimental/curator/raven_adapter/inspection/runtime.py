"""Collect the effective runtime facts and derive host reachability from one assembly."""

import json
from hashlib import sha256
from inspect import getdoc, iscoroutinefunction
from pathlib import Path
from typing import Any

from raven.core.runtime import RavenRuntime

from ...composition.requirements import read_requirements
from ...harness import Artifact, Declaration
from ..baselines import Baseline
from ..targets import catalogue
from . import mechanisms
from .sources import file_source


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def playbook_library(loop):
    """The AgentLoop's loaded playbook library, which Raven keeps without a public accessor; None when disabled."""
    return getattr(loop, "_playbooks", None)


def playbook_nodes(runtime, baseline):
    """Every loaded playbook node with the requirements file beside it in the agent home."""
    library = playbook_library(runtime.loop)
    nodes = {}
    if library is not None:
        for name in library.names():
            spec = library.store.load(name)
            for node in spec.nodes:
                key = f"{name}/{node.id}"
                path = baseline.config.workspace_path / "playbooks" / name / "nodes" / node.id / "requirements.json"
                nodes[key] = {
                    "playbook": name,
                    "node": node.model_dump(mode="json"),
                    "requirements": read_requirements(path.read_text()) if path.exists() else [],
                }
    return nodes


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        hidden = {"apikey", "password", "secret", "clientsecret", "accesstoken", "refreshtoken", "authorization"}
        result = {}
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in hidden:
                result[key] = "<configured>" if item else item
            elif normalized in {"env", "headers"} and isinstance(item, dict):
                result[key] = {name: "<configured>" for name in item}
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def declaration_for(identity, unavailable, *, names=None, fields=None, phases=None) -> Declaration:
    """Apply native reachability and host grants identically in inspection and assembly."""
    targets = tuple(target for target in catalogue() if target.name not in unavailable)
    declaration = Declaration(identity, targets)
    return declaration.restrict(
        names if names is not None else (target.name for target in targets), fields=fields, phases=phases
    )


def describe_runtime(runtime: RavenRuntime, baseline: Baseline, authored: Artifact, records, sources) -> dict:
    from raven.context_engine.segments.render import BOOTSTRAP_FILES

    loop = runtime.loop
    sources = {name: dict(entry) for name, entry in sources.items()}
    for name, entry in sources.items():
        if sha256(Path(entry["path"]).read_bytes()).hexdigest() != entry["digest"]:
            raise ValueError(f"loaded source changed; restart the worker before curation: {name}")
    bootstrap = {
        name: (baseline.config.workspace_path / name).read_text()
        for name in BOOTSTRAP_FILES
        if (baseline.config.workspace_path / name).is_file()
    }
    for name in bootstrap:
        sources[f"bootstrap.{name}"] = file_source(baseline.config.workspace_path / name)
    registry = runtime.plugin_registry
    skills = loop.context.skills.list_skills(filter_unavailable=False)
    available = {row["path"] for row in loop.context.skills.list_skills()}
    for row in skills:
        row["registry_available"] = row["path"] in available
        path = Path(row["path"])
        if path.is_file():
            sources[f"skill.{row['source']}/{row['name']}"] = {**file_source(path), "root": str(path.parent)}
    for path in sorted((baseline.config.workspace_path / "playbooks").glob("*/playbook.md")):
        sources[f"playbook.{path.parent.name}"] = {**file_source(path), "root": str(path.parent)}
    uploads = baseline.config.workspace_path / "uploads"
    uploaded = sorted(path for path in uploads.rglob("*") if path.is_file()) if uploads.is_dir() else []
    for path in uploaded:
        try:
            sources[f"upload.{path.relative_to(uploads).as_posix()}"] = {**file_source(path), "root": str(uploads)}
        except UnicodeDecodeError:
            continue
    library = playbook_library(loop)
    facts = {
        "backend": "AgentLoop",
        "hosting": baseline.hosting,
        "allow_delegation": baseline.allow_delegation,
        "playbooks": library.names() if library is not None else [],
        "uploads": [str(path) for path in uploaded],
        "source_roots": [str(path) for path in baseline.source_roots],
        "mode": baseline.mode,
        "origin": baseline.origin.value,
        "resident": baseline.resident,
        "workdir": str(baseline.workdir),
        "agent_home": str(baseline.config.workspace_path),
        "native_modules": {
            name: f"{type(getattr(loop.harness, name)).__module__}:{type(getattr(loop.harness, name)).__qualname__}"
            for name in ("memory", "planning", "capability", "action")
        },
        "hooks": [
            {"name": hook.name, "type": f"{type(hook).__module__}:{type(hook).__qualname__}"} for hook in loop.hooks
        ],
        "tools": loop.tools.get_definitions(),
        "plugins": [registry.manifest_for(name).model_dump(mode="json") for name in registry.activated_ids()],
        "mcp": loop.mcp_manager_if_started.status() if loop.mcp_manager_if_started is not None else [],
        "memory_backend": type(runtime.backend).__name__ if runtime.backend is not None else None,
        "context_engine": type(loop.context_engine).__name__,
        "skills": skills,
        "bootstrap": bootstrap,
        "configuration": redact(baseline.export()),
        "authored": redact(authored.model_dump(mode="json")),
        "assembly_evidence": records,
    }
    if baseline.mode is not None:
        from dataclasses import asdict

        from raven.config.mode_catalogue import build_mode_catalogue

        profile = build_mode_catalogue(baseline.config).get(baseline.mode)
        if profile is None:
            raise ValueError(f"configured worker mode is unavailable: {baseline.mode}")
        facts["turn_profile"] = asdict(profile)
    unavailable = unavailable_targets(baseline)
    from raven.contracts.loop_hooks import AgentHook

    from ..targets import action, capability, memory, planning

    facts["loop_contract"] = {
        "roles": mechanisms.roles(),
        "native_modules": {
            name: {
                "responsibility": (getdoc(module.CONTRACT) or "").split("\n\n")[0],
                "source": f"native.strategy.{name}",
            }
            for name, module in (
                ("memory", memory),
                ("planning", planning),
                ("capability", capability),
                ("action", action),
            )
        },
        "hook_contracts": {
            name: getdoc(method) for name, method in vars(AgentHook).items() if iscoroutinefunction(method)
        },
        "execution_source": "loop.execution",
        "composition_source": "loop.participant_composition",
    }
    facts["unavailable"] = unavailable
    declaration = declaration_for("view", unavailable)
    facts["mechanisms"] = [item.model_dump(mode="json") for item in mechanisms.describe(facts, sources, declaration)]
    identity = fingerprint(
        {
            "baseline": baseline.export(),
            "authored": authored.model_dump(mode="json"),
            "sources": sources,
            "bootstrap": bootstrap,
            "mechanisms": facts["mechanisms"],
            "assembly": {"plugins": facts["plugins"], "strategies": facts["native_modules"]},
        }
    )
    return {"identity": identity, "facts": facts, "sources": sources, "unavailable": unavailable}


def describe_bound(bound, records=None):
    """Inspect the actual assembled runtime, including its generated owners and current state."""
    rows = bound.recorder.rows if records is None else records
    end = next((index + 1 for index, row in enumerate(rows) if row["kind"] == "runtime.bound"), len(rows))
    data = describe_runtime(bound.runtime, bound.baseline, bound.artifact, rows[:end], bound.sources)
    data["facts"]["task"] = bound.baseline.task.model_dump() if bound.baseline.task else None
    data["facts"]["planning"] = bound.planning.facts() if bound.planning else None
    strategies = {name: strategy.facts() for name, strategy in bound.strategies.items()}
    data["facts"].update(strategies)
    data["facts"]["prompts"] = bound.prompts
    data["identity"] = fingerprint({"baseline": data["identity"], "planning": data["facts"]["planning"], **strategies})
    return data


def unavailable_targets(baseline: Baseline) -> dict[str, str]:
    from raven.agent.loop.turn_path import _SKIP_AFTER_SEND_ORIGINS, _SKIP_USER_INBOUND_ORIGINS

    result = {}
    if not baseline.allow_delegation:
        result["planning.playbooks"] = "this host is a leaf Harness and cannot delegate further"
    if baseline.task is None:
        for target in catalogue():
            if target.binding.endswith(".strategy"):
                result[target.name] = "Bind an explicit current task before generating a task strategy."
    if baseline.origin in _SKIP_USER_INBOUND_ORIGINS:
        result["memory.intake"] = "This origin skips the generic inbound hook."
    if baseline.origin in _SKIP_AFTER_SEND_ORIGINS:
        result["memory.archive"] = "This origin skips after_send."
    if not baseline.resident:
        result["action.services"] = "Services require a resident host."
        result["memory.session_observers"] = "Session observers require a resident host."
    return result
