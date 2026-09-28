"""Generate local strategy candidates for a root and its assigned children before one activation."""

import json
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ..generation.run import Generated, GenerationError, GenerationInterruptedError, GenerationPausedError
from ..harness import Validation
from ..raven_adapter.deployment import child_directory
from ..raven_adapter.inspection import Inspection, file_source, fingerprint
from ..raven_adapter.materialize import _write, extend_artifact
from ..raven_adapter.observe import plain
from ..workflow import CANDIDATE, PENDING, propose
from .context import merge_sources
from .requirements import feedback_for
from .state import Progress


def _save(path, state):
    _write(path, state.model_dump_json(indent=2).encode())


def _cleanup(directory):
    for path in Path(directory).glob("*/curation/*.json"):
        if path.name not in {CANDIDATE, PENDING}:
            continue
        record = json.loads(path.read_text())
        if workspace := record.get("workspace"):
            shutil.rmtree(workspace, ignore_errors=True)


def _groups(nodes, available):
    """Select children with requirements, then include all their known playbook uses."""
    groups = {}
    for key, node in nodes.items():
        if not node["requirements"]:
            continue
        name = node["node"]["subagent"]
        if name not in available:
            raise ValueError(f"node {key} requests an unprepared child Harness: {name}")
        groups.setdefault(name, {})
    for key, node in nodes.items():
        name = node["node"]["subagent"]
        if name in groups:
            groups[name][key] = node
    return groups


async def improve(worker, provider, *, feedback, model, limits, probe, resume):
    from ..raven_adapter.worker import Worker, WorkerError

    actual_children = {name: await worker.agent_state(name) for name in worker.children}
    inspection = await worker.inspect()
    path = worker.root / "curation/composition.json"
    state = Progress.model_validate_json(path.read_text()) if path.exists() else None
    if state is not None and not resume:
        _cleanup(state.directory)
        state = None
    if state is not None:
        feedback = state.feedback if feedback is None else feedback
        model = state.model if model is None else model
    identity = fingerprint(
        {
            "baseline": inspection.declaration.baseline,
            "children": {
                name: Inspection.restore(actual["inspection"]).declaration.baseline
                for name, actual in actual_children.items()
            },
            "feedback": feedback,
            "model": model,
        }
    )
    if state is not None and state.input_id != identity:
        raise ValueError(
            "composite inputs changed; the prior investigation was retained, explicitly restart to replace it"
        )
    state = state or Progress(
        input_id=identity, directory=str(worker.root / "curation/scopes" / uuid4().hex), feedback=feedback, model=model
    )
    if state.error:
        raise GenerationError(f"previous composite curation failed: {state.error}; explicitly restart to replace it")
    _save(path, state)
    current_nodes = inspection.facts["composition"]["nodes"]
    sources = dict(inspection.sources)
    child_views = {}
    for name, actual in actual_children.items():
        viewed = Inspection.restore(actual["inspection"])
        prefix = f"child.{name}"
        evidence = Path(state.directory) / "root" / f"{fingerprint(name)[:16]}-inspection.json"
        if not evidence.exists():
            _write(evidence, json.dumps(actual["inspection"], ensure_ascii=False).encode())
        sources[f"{prefix}.facts"] = file_source(evidence)
        aliases = merge_sources(sources, viewed.sources, prefix)
        child_views[name] = {
            "baseline": viewed.declaration.baseline,
            "targets": [target.name for target in viewed.declaration.targets],
            "hosting": viewed.facts["hosting"],
            "backend": viewed.facts["backend"],
            "allow_delegation": viewed.facts["allow_delegation"],
            "mechanisms": [
                item.model_copy(update={"sources": tuple(aliases[name] for name in item.sources)}).model_dump(
                    mode="json"
                )
                for item in viewed.mechanisms
            ],
            "artifact": worker.children[name].artifact.model_dump(mode="json"),
            "facts_source": f"{prefix}.facts",
        }
    inspection = Inspection(
        inspection.declaration,
        {**inspection.facts, "composition": {"nodes": current_nodes, "children": child_views}},
        sources,
    )

    async def root_inspection():
        return inspection

    async def root_check(candidate, *, probe=None):
        checked = await worker.check(candidate, probe=probe)
        if not checked.passed:
            return checked
        try:
            nodes = await worker.preview_nodes(candidate)
            if set(candidate.plan.node_reasons) != set(current_nodes) | set(nodes):
                raise ValueError("root node reasons must cover current, added and retired playbook nodes")
            _groups(nodes, worker.children)
            return checked
        except ValueError as exc:
            return Validation([str(exc)], checked.observations)

    root = SimpleNamespace(
        baseline=worker.baseline,
        artifact=worker.artifact,
        root=Path(state.directory) / "root",
        last_plan=worker.last_plan,
        last_execution=worker.last_execution,
        inspect=root_inspection,
        check=root_check,
        withheld=worker.withheld,
    )
    record = {
        "task_id": worker.baseline.task.id,
        "feedback": feedback,
        "turn_id": worker.last_execution.turn_id if worker.last_execution else None,
    }
    try:
        while True:
            if state.root is None or state.repair is not None:
                result = await propose(
                    root,
                    provider,
                    feedback=feedback,
                    model=model,
                    limits=state.limits_for("root", limits),
                    probe=probe,
                    repair=state.repair,
                )
                state.root, state.repair = result, None
                _save(path, state)
            nodes = await worker.preview_nodes(state.root.candidate)
            groups = _groups(nodes, worker.children)
            failure_scope = {"scope": "composition"}
            try:
                selected = {}
                parent_artifact = extend_artifact(worker.artifact, state.root.candidate.artifact)
                parent_materials = {
                    target.name: parent_artifact.values[target.name]
                    for target in inspection.declaration.targets
                    if target.binding in {"bootstrap_files", "skill_files"} and target.name in parent_artifact.values
                }
                for name, uses in groups.items():
                    child = worker.children[name]
                    actual = await worker.agent_state(name)
                    view = Inspection.restore(actual["inspection"])
                    failure_scope = {
                        "scope": f"child/{name}",
                        "baseline": view.declaration.baseline,
                        "nodes": list(uses),
                    }
                    relevant = {
                        "parent_materials": parent_materials,
                        "requirements": {
                            key: node["requirements"] for key, node in uses.items() if node["requirements"]
                        },
                        "feedback": feedback_for(name, feedback),
                    }
                    child_id = fingerprint(
                        {
                            "baseline": view.declaration.baseline,
                            "uses": uses,
                            "feedback": relevant,
                            "materials": {
                                key: value["digest"]
                                for key, value in inspection.sources.items()
                                if key.startswith(("upload.", "skill.", "bootstrap."))
                            },
                        }
                    )
                    if name in state.children and state.inputs.get(name) == child_id:
                        selected[name] = state.children[name].candidate
                        continue
                    scope = state.scopes.get(name)
                    if (
                        scope is None
                        or state.inputs.get(name) != child_id
                        or not (Path(state.directory) / scope / "curation" / PENDING).exists()
                    ):
                        scope = f"child-{fingerprint(name)[:12]}-{uuid4().hex[:12]}"
                        state.scopes[name] = scope
                    state.inputs[name] = child_id
                    directory = Path(state.directory) / scope
                    evidence = directory / "execution.json"
                    if not evidence.exists():
                        _write(evidence, json.dumps(actual["records"], ensure_ascii=False).encode())
                    observed = json.loads(evidence.read_text())
                    local_view = Inspection(
                        view.declaration,
                        {**view.facts, "scope": {"kind": "child", "name": name}, "assigned_nodes": uses},
                        {
                            **view.sources,
                            **{
                                f"parent.{key}": value
                                for key, value in inspection.sources.items()
                                if not key.startswith("child.")
                            },
                            "execution.current": file_source(evidence),
                        },
                    )
                    validator = Worker(
                        child.baseline, child_directory(worker.root, name), timeout=worker.timeout, **child.grants
                    )
                    validator.artifact = child.artifact
                    validator._content_baseline = worker._content_baseline

                    async def inspect(current=local_view):
                        return current

                    async def check(candidate, *, probe=None, owner=validator, current=view):
                        return await owner._check(candidate, probe=probe, inspection=current)

                    subject = SimpleNamespace(
                        baseline=child.baseline,
                        root=directory,
                        artifact=child.artifact,
                        last_plan=child.plan,
                        last_execution=None,
                        inspect=inspect,
                        check=check,
                        withheld=worker.withheld,
                    )
                    _save(path, state)
                    result = await propose(
                        subject,
                        provider,
                        feedback=relevant,
                        model=model,
                        limits=state.limits_for(scope, limits),
                        observations=(
                            {
                                "kind": "child.execution",
                                "revision": actual["revision"],
                                "source": "execution.current",
                                "record_kinds": dict(Counter(row["kind"] for row in observed)),
                            },
                        ),
                    )
                    state.children[name] = result
                    state.inputs[name] = child_id
                    selected[name] = result.candidate
                    _save(path, state)
                withdrawn = []
                for name in _groups(current_nodes, worker.children).keys() - groups.keys():
                    child = worker.children[name]
                    if child.artifact == child.original:
                        continue
                    selected[name] = None
                    withdrawn.append(name)
                failure_scope = {"scope": "composition"}
                checked = await worker.check_composition(state.root.candidate, selected, probe=probe)
                if not checked.passed:
                    raise GenerationError("composite validation failed: " + "; ".join(checked.errors))
                before = worker.revision_id
                await worker.install(state.root.candidate, children=selected)
                record.update(
                    generated=plain(state.root),
                    changed=worker.revision_id != before,
                    child_changes={
                        name: plain(state.children[name])
                        for name in selected
                        if name in state.children and name not in withdrawn
                    },
                    withdrawn_children=withdrawn,
                    revision={
                        "root": worker.artifact.model_dump(mode="json"),
                        "children": {
                            name: {
                                "artifact": child.artifact.model_dump(mode="json"),
                                "plan": child.plan.model_dump(mode="json") if child.plan else None,
                            }
                            for name, child in worker.children.items()
                        },
                    },
                )
                record["composition"] = state.model_dump(mode="json")
                combined = Generated(
                    state.root.candidate,
                    Validation(
                        [],
                        [
                            *state.root.validation.observations,
                            *checked.observations,
                            *[
                                row
                                for name in selected
                                if name in state.children and name not in withdrawn
                                for row in state.children[name].validation.observations
                            ],
                            {
                                "kind": "composition.activated",
                                "revision": worker.revision_id,
                                "children": list(selected),
                            },
                        ],
                    ),
                    state.root.trace,
                )
                record["generated"] = plain(combined)
                _cleanup(state.directory)
                path.unlink()
                return combined
            except GenerationInterruptedError:
                raise
            except (ValueError, GenerationError, WorkerError) as exc:
                if state.totals()["repairs"] >= limits.max_repairs:
                    raise GenerationError(f"composite repair budget exhausted: {exc}") from exc
                state.repair = Validation(
                    [str(exc)],
                    [
                        {
                            "kind": "composition.failure",
                            **failure_scope,
                            "evidence": [
                                row
                                for row in getattr(exc, "trace", ())
                                if row.get("event") in {"report_gap", "validation", "output.rejected"}
                            ],
                        },
                        *getattr(exc, "records", ()),
                    ],
                )
                _save(path, state)
    except GenerationInterruptedError as exc:
        record["paused"] = {"checkpoint": str(path), "reason": str(exc), **state.totals()}
        _save(path, state)
        interrupted = state.paused(exc.state.stage)
        if isinstance(exc, GenerationPausedError):
            raise interrupted
        raise GenerationInterruptedError(str(exc), interrupted.state) from exc
    except Exception as exc:
        state.error = record["error"] = str(exc)
        _save(path, state)
        raise
    finally:
        record["active_artifact_id"] = worker.revision_id
        _write(worker.root / "curation" / f"{uuid4().hex}.json", json.dumps(record, ensure_ascii=False).encode())
