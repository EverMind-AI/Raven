"""Host-owned child baselines, immutable launch definitions and native registration bindings."""

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from raven.config.schema import ThirdPartyAcpSubagentConfig

from ..harness import Artifact, Plan
from .baselines import Baseline
from .inspection import fingerprint
from .materialize import _write


@dataclass
class Child:
    """One existing child Harness and the current implementation owned by its deployment."""

    baseline: Baseline
    artifact: Artifact = field(default_factory=lambda: Artifact(values={}))
    plan: Plan | None = None
    grants: dict = field(default_factory=dict)
    original: Artifact | None = None

    def __post_init__(self):
        if set(self.grants) - {"names", "fields", "phases"}:
            raise ValueError("unknown child authoring grant")
        self.original = (self.artifact if self.original is None else self.original).model_copy(deep=True)
        self.baseline = Baseline.restore(self.baseline.export())
        self.baseline.hosting = "acp"
        self.baseline.allow_delegation = False

    def export(self):
        return {
            "baseline": self.baseline.export(),
            "artifact": self.artifact.model_dump(mode="json"),
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "grants": self.grants,
            "original": self.original.model_dump(mode="json"),
        }

    @classmethod
    def restore(cls, value):
        return cls(
            Baseline.restore(value["baseline"]),
            Artifact.model_validate(value["artifact"]),
            Plan.model_validate(value["plan"]) if value.get("plan") else None,
            value.get("grants", {}),
            Artifact.model_validate(value["original"]) if "original" in value else None,
        )


def child_directory(root, name):
    return Path(root) / "children" / fingerprint(name)[:16]


def bind_children(baseline, children, root):
    """Bind existing roster names to their checked full-Harness launch definition."""
    if not children:
        return baseline
    bound = Baseline.restore(baseline.export())
    rows = {row.name: row for row in bound.config.subagents.agents}
    repository = Path(__file__).resolve().parents[3]
    for name, child in children.items():
        directory = child_directory(root, name)
        content = {
            "baseline": child.baseline.export(),
            "artifact": child.artifact.model_dump(mode="json"),
            "state_dir": str(directory),
            "grants": child.grants,
        }
        definition = directory / "versions" / fingerprint(content) / "deployment.json"
        encoded = json.dumps(content, ensure_ascii=False, indent=2).encode()
        if definition.exists() and definition.read_bytes() != encoded:
            raise ValueError("a versioned child definition changed after preparation")
        if not definition.exists():
            _write(definition, encoded)
        previous = rows.get(name)
        inherited = dict(getattr(previous, "env", None) or {})
        inherited["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repository), inherited.get("PYTHONPATH"))))
        inherited["RAVEN_HOME"] = str(directory / "native")
        command = shlex.join(
            [sys.executable, "-m", "experimental.curator.raven_adapter.hosting.acp", "--deployment", str(definition)]
        )
        update = {"command": command, "cwd": str(child.baseline.workdir), "env": inherited}
        rows[name] = (
            previous.model_copy(update=update)
            if isinstance(previous, ThirdPartyAcpSubagentConfig)
            else ThirdPartyAcpSubagentConfig(
                name=name,
                **update,
                enabled=getattr(previous, "enabled", True),
                description=getattr(previous, "description", "") or f"Task Harness for {name}",
            )
        )
    bound.config.subagents.agents = list(rows.values())
    return bound


async def activate(worker, candidate, children=None):
    """Activate one checked set at a single root execution boundary, restoring all owned state on failure."""
    import asyncio

    from .inspection import Inspection
    from .materialize import extend_artifact, restore_content, retired_content, save_content
    from .targets import catalogue

    async with worker._lock:
        if not await worker._exchange({"operation": "idle"}):
            raise ValueError("Harness activation requires an idle root and completed child calls")
        current = await worker._inspect()
        candidate = worker._accept(candidate, current)
        proposed = worker._release_edited(
            candidate.artifact, extend_artifact(worker.artifact, candidate.artifact), current.declaration
        )
        previous = {name: Child.restore(child.export()) for name, child in worker.children.items()}
        revised = {name: Child.restore(child.export()) for name, child in worker.children.items()}
        saved = {
            **save_content(worker.baseline.config.workspace_path, worker.artifact, current.declaration),
            **save_content(worker.baseline.config.workspace_path, proposed, current.declaration),
        }
        saved_children, retired_children = {}, {}
        if set(children or {}) - set(revised):
            raise ValueError("candidate names a child Harness outside this deployment")
        for name, child in revised.items():
            report = await worker._exchange({"operation": "inspect_agent", "agent": name})
            inspection = Inspection.restore(report["inspection"])
            submitted = (children or {}).get(name)
            restoring = name in (children or {}) and submitted is None
            home = child.baseline.config.workspace_path
            effective = child_artifact(child, submitted, inspection, restoring=restoring)
            saved_children[name] = {
                **save_content(home, child.artifact, inspection.declaration),
                **save_content(home, effective, inspection.declaration),
            }
            retired_children[name] = retired_content(
                home, child.artifact, effective, inspection.declaration, worker._content_baseline
            )
            child.artifact = effective
            if restoring:
                child.plan = None
            elif submitted is not None:
                child.plan = submitted.plan
        check_content_owners(worker.baseline, proposed, revised)
        if proposed == worker.artifact and all(
            revised[name].artifact == child.artifact for name, child in previous.items()
        ):
            return current
        retired = worker._retired_content(proposed, current.declaration)
        directories = [worker.root, *(child_directory(worker.root, name) for name in worker.children)]
        checkpoints = {}
        old = worker.artifact
        try:
            try:
                await worker._close()
            finally:
                for directory in directories:
                    for target in catalogue():
                        if target.binding.endswith(".strategy"):
                            path = directory / f"{target.name.split('.')[0]}.json"
                            checkpoints[path] = path.read_bytes() if path.exists() else None
            worker.artifact, worker.children = proposed, revised
            restore_content(worker.baseline.config.workspace_path, retired)
            for name, content in retired_children.items():
                restore_content(revised[name].baseline.config.workspace_path, content)
            await worker._start()
            installed = await worker._inspect()
            for name, child in worker.children.items():
                report = await worker._exchange({"operation": "inspect_agent", "agent": name})
                if report["revision"] != fingerprint(child.artifact.model_dump(mode="json")):
                    raise ValueError(f"child {name} did not activate the expected strategy artifact")
        except BaseException as exc:
            try:
                await worker._close()
            finally:
                restore_content(worker.baseline.config.workspace_path, saved)
                for name, content in saved_children.items():
                    restore_content(previous[name].baseline.config.workspace_path, content)
                worker.artifact, worker.children = old, previous
                for path, content in checkpoints.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write(path, content)
                if not isinstance(exc, asyncio.CancelledError):
                    await worker._start()
            raise
        for path, value in saved.items():
            worker._content_baseline.setdefault(path, value)
        for content in saved_children.values():
            for path, value in content.items():
                worker._content_baseline.setdefault(path, value)
        worker.last_plan = candidate.plan
        return installed


def check_content_owners(baseline, artifact, children):
    """Reject simultaneous writers using the same content mapping used by materialization."""
    from ..harness import Declaration
    from .materialize import content_updates
    from .targets import catalogue

    declaration = Declaration("ownership", catalogue())
    root_paths = content_updates(baseline.config.workspace_path, artifact, declaration)
    for name, child in children.items():
        home = child.baseline.config.workspace_path.resolve()
        for path in root_paths:
            if path.resolve().is_relative_to(home):
                raise ValueError(f"root content overlaps the managed child {name}: {path}")
    owners = {}
    for name, child in children.items():
        for path in content_updates(child.baseline.config.workspace_path, child.artifact, declaration):
            key = path.resolve()
            if key in owners:
                raise ValueError(f"children {owners[key]} and {name} both own {path}")
            owners[key] = name


def child_artifact(child, candidate, inspection, *, restoring=False):
    """Resolve one proposed child version for both copied validation and real activation."""
    from .materialize import extend_artifact, release_edited

    if candidate is not None:
        inspection.declaration.validate(candidate)
    delta = child.original if restoring else (candidate.artifact if candidate else Artifact(values={}))
    proposed = child.original if restoring else extend_artifact(child.artifact, delta)
    return release_edited(
        child.baseline.config.workspace_path,
        child.artifact,
        delta,
        proposed,
        inspection.declaration,
    )
