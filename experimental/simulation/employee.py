"""The digital employee: the real Raven worker the agency trains, hired with the scenario's profile as its task.

Every drill runs on its own replica of the employee (see `replicate` and `Together`); drills of a round can run at once.
"""

import asyncio
import json
import os
import shlex
import shutil
from pathlib import Path

from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.vendored_agents import discover_product_rows, product_folder
from raven.config.schema import ThirdPartyAcpSubagentConfig

from ..curator.harness import Task
from ..curator.raven_adapter.baselines.prepare import prepare
from ..curator.raven_adapter.deployment import child_directory
from ..curator.raven_adapter.exploration import Withheld
from ..curator.raven_adapter.hosting.prepare import prepare_children
from ..curator.raven_adapter.targets import catalogue
from ..curator.raven_adapter.worker import Worker
from .scenario import Scenario

HOME = "home"
WORKDIR = "workdir"
REPLICAS = "replicas"
SUBAGENTS = "subagents"
HOUSED = {"Raven-Research": "RESEARCH_NG_ACP_HOME", "Raven-PPT": "PPT_ACP_HOME"}
SESSION_STATE = frozenset({"sessions", "memory"})
FORWARDED = ("PYTHONTZPATH",)
UNATTENDED = ("ask_user",)
# A product's shell reaches whatever its process can: the employee's is confined to its workspace, a product's is not,
# and its file tools must still reach the drill's workdir, so products run without a shell.
SHELLS = ("exec",)


def withheld(scenario: Scenario) -> Withheld:
    """What the employee's Curator may not read: the simulation, the loop that judges its rounds and the scenario,
    in their tests and in any repository file that names them."""
    return Withheld(
        paths=("tests/test_simulation_*", "tests/test_analyst_*", "tests/test_iteration_*"),
        markers=(
            b"experimental.simulation",
            b"experimental/simulation",
            b"experimental.analyst",
            b"experimental.iteration",
            scenario.root.name.encode(),
        ),
    )


def hire(
    scenario: Scenario,
    config: Path,
    *,
    workdir: Path,
    root: Path,
    home: Path | None = None,
    subagent_model: str | None = None,
    timeout=180,
) -> Worker:
    """A plain Raven baseline that knows only its job description; the Curator shapes everything else.

    The employee works from its own copy of the agent home under `root` (without past sessions), so the
    materials the agency hands over never touch the caller's home and two runs never share a skill pool.
    The Curator may author every declared Harness target.
    `subagent_model` runs the housed subagents on that model, through the employee's provider, instead of its own.
    """
    workspace = Path(root) / HOME
    if home is None:
        workspace.mkdir(parents=True)
    else:
        shutil.copytree(home, workspace, ignore=shutil.ignore_patterns("sessions", ".lock"))
    baseline = prepare(config, workdir=workdir, task=Task(text=scenario.profile), home=workspace)
    housed = house(baseline, model=subagent_model, deployment=Path(root) / "deployment")
    return Worker(
        baseline,
        root,
        timeout=timeout,
        children=lambda current: prepare_children(current, root, ["Raven", *housed]),
        withheld=withheld(scenario),
    )


def replicate(
    employee: Worker,
    config: Path,
    root: Path,
    *,
    subagent_model: str | None = None,
    timeout=180,
) -> Worker:
    """A replica of the started employee as it stands now, in a new `root`, with processes of its own.

    It gets a copy of the employee's home (without past sessions) and workdir, the state its strategies saved, and
    the revision installed in the employee and in each child harness, so it starts a drill where a drill played on
    the employee next would start. It keeps the employee's task, whose id the saved strategy state is bound to. The
    replica is not started; its revision is compared again once it is.
    """
    root = Path(root)
    workspace, workdir = root / HOME, root / WORKDIR
    shutil.copytree(
        employee.baseline.config.workspace_path, workspace, ignore=shutil.ignore_patterns("sessions", ".lock")
    )
    shutil.copytree(employee.baseline.workdir, workdir, symlinks=True)
    baseline = prepare(config, workdir=workdir, task=employee.baseline.task, home=workspace)
    housed = house(baseline, model=subagent_model, deployment=root / "deployment")
    children = prepare_children(baseline, root, ["Raven", *housed])
    if children.keys() != employee.children.keys():
        raise ValueError(f"a replica has child harnesses {sorted(children)}, the employee {sorted(employee.children)}")
    for name, child in children.items():
        child.artifact, child.plan = employee.children[name].artifact, employee.children[name].plan
    replica = Replica(
        employee.revision_id, baseline, root, timeout=timeout, children=children, withheld=employee.withheld
    )
    replica.artifact, replica.last_plan = employee.artifact, employee.last_plan
    saved = {f"{target.name.split('.')[0]}.json" for target in catalogue() if target.binding.endswith(".strategy")}
    folders = [
        (employee.root, root),
        *((child_directory(employee.root, name), child_directory(root, name)) for name in children),
    ]
    for source, target in folders:
        for name in saved:
            if (source / name).is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target / name)
    same_revision(employee, replica)
    return replica


class Replica(Worker):
    """A worker that reports the employee's revision id on its executions.

    `Worker.revision_id` also fingerprints each child's baseline, whose paths differ between replicas; the replica
    holds the same revision (checked by `same_revision`), so its drills must name the revision the employee names.
    """

    def __init__(self, revision_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee_revision_id = revision_id

    @property
    def revision_id(self):
        return self.employee_revision_id


def revision(worker: Worker) -> dict:
    """The installed revision of the root and each child harness, without the paths that differ between replicas."""
    return {
        "root": worker.artifact.model_dump(mode="json"),
        "children": {
            name: {"artifact": child.artifact.model_dump(mode="json"), "grants": child.grants}
            for name, child in worker.children.items()
        },
    }


def bound(worker: Worker) -> str | None:
    """The authored package the worker's running generation bound, as its folder name."""
    packages = [row["package"] for row in worker.records() if row.get("kind") == "runtime.bound" and row.get("package")]
    return Path(str(packages[-1])).name if packages else None


def same_revision(employee: Worker, replica: Worker, *, running=False):
    """Refuse a replica that would play a drill on another revision than the employee's."""
    if revision(replica) != revision(employee):
        raise ValueError(f"replica {replica.root} does not hold the employee's installed revision")
    if running and bound(replica) != bound(employee):
        raise ValueError(f"replica {replica.root} bound {bound(replica)}, the employee {bound(employee)}")


def house(baseline, housed=HOUSED, *, model=None, deployment=None) -> list[str]:
    """Point each named external subagent's own Agent home into the employee's `subagents/` folder.

    Their homes then sit in the same tree as the rest of the employee's Harness, where the Curator can author
    them. A product's children do not inherit this process's environment, so the row also forwards what they
    need from it; so does a row for the built-in Raven child, whose hosted launch keeps that row's environment.
    Subagents the configuration disables or that no product folder provides are left alone. Tools that need a
    person on the other end (`UNATTENDED`) are switched off: nobody answers them in a drill.
    With `model`, each housed subagent launches on the employee's provider and key with that model (see `pin`).
    """
    tools = baseline.config.tools
    tools.disabled_tools = [*tools.disabled_tools, *(name for name in UNATTENDED if name not in tools.disabled_tools)]
    rows = baseline.config.subagents.agents
    configured = {row.name: row for row in rows}
    extra = {name: os.environ[name] for name in FORWARDED if os.environ.get(name)}
    if extra and GENERIC_AGENT not in configured:
        rows = [
            *rows,
            ThirdPartyAcpSubagentConfig(
                name=GENERIC_AGENT, command="hosted", env=extra, description=f"Task Harness for {GENERIC_AGENT}"
            ),
        ]
    done = []
    for row in discover_product_rows():
        if row.name not in housed or not row.enabled or not getattr(configured.get(row.name), "enabled", True):
            continue
        home = baseline.config.workspace_path / SUBAGENTS / row.name
        home.mkdir(parents=True, exist_ok=True)
        env = {**(row.env or {}), **extra, housed[row.name]: str(home)}
        update = {"env": env}
        if model:
            pinned_env, command = pin(row, model, baseline.config, Path(deployment))
            update = {"env": {**env, **pinned_env}, "command": command}
        rows = [item for item in rows if item.name != row.name] + [row.model_copy(update=update)]
        done.append(row.name)
    baseline.config.subagents.agents = rows
    return done


def pin(row, model: str, config, deployment: Path) -> tuple[dict[str, str], str]:
    """Environment and command that launch a product on `model` through the employee's own provider and key.

    Each product's configuration is copied with that provider (its key in the copy, which is private to the run),
    the model, the employee's reasoning effort and its pinned context window in place, so a subagent never runs on
    a default effort or window of its own. A product whose sessions start in a tier of its own catalogue starts in
    the employee's tier instead when the catalogue has one of that name: a tier carries its own effort, which would
    otherwise override the pinned one (a shipped `high` default tier runs every call at high effort). The copy also
    carries the employee's permission settings: an unattended subagent has no one to answer an approval prompt, so it
    works under the same rules as the employee. Products run without a shell (`SHELLS`), and Raven-Research's
    end-of-turn verify gate runs without thinking: it asks for its verdict as a forced tool call, which a thinking
    DeepSeek model refuses (HTTP 400), and the gate then lets every draft through unreviewed.
    """
    defaults = config.agents.defaults
    provider = defaults.provider
    settings = getattr(config.providers, provider, None)
    key = getattr(settings, "api_key", None)
    if not key:
        raise ValueError(f"pinning a subagent model needs the employee's {provider} key")
    if not defaults.reasoning_effort:
        raise ValueError("pinning a subagent model needs the employee's reasoning effort set explicitly")
    permissions = config.permissions.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    secret = {"Raven-PPT": "PPT_API_KEY", "Raven-Research": "RESEARCH_API_KEY"}.get(row.name)
    if secret is None:
        raise ValueError(f"no known way to pin the model of {row.name}")
    product = json.loads((product_folder(row.name) / "config.json").read_text())
    product.setdefault("agents", {}).setdefault("defaults", {}).update(
        {
            "model": model,
            "provider": provider,
            "reasoningEffort": defaults.reasoning_effort,
            **({"contextWindowTokens": defaults.context_window_tokens} if defaults.context_window_tokens else {}),
        }
    )
    product.setdefault("providers", {})[provider] = {"apiKey": key}
    tier = config.acp.default_mode
    if tier and tier in ((product.get("acp") or {}).get("modes") or {}):
        product["acp"]["defaultMode"] = tier
    disabled = product.setdefault("tools", {}).setdefault("disabledTools", [])
    disabled.extend(name for name in (*UNATTENDED, *SHELLS) if name not in disabled)
    verify = (((product.get("plugins") or {}).get("config") or {}).get("research-flow") or {}).get("verify")
    if isinstance(verify, dict) and str(model).startswith("deepseek/"):
        verify["reasoningEffort"] = "none"
    if permissions:
        product["permissions"] = {**product.get("permissions", {}), **permissions}
    path = deployment / f"{row.name.lower()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(product, ensure_ascii=False, indent=2))
    path.chmod(0o600)
    return {secret: key}, f"{row.command} --config {shlex.quote(str(path))}"


def housed_files(home: Path) -> dict[str, bytes]:
    """The housed subagent homes' files, keyed by their path under `subagents/`, without per-session logs."""
    root = Path(home) / SUBAGENTS
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not SESSION_STATE & set(path.relative_to(root).parts[1:2])
    }


def refresh(home: Path, seed: dict[str, bytes], authored: dict[str, str]) -> list[str]:
    """Rewrite every file of `seed`, overlaid with the revision's `authored` files, that no longer matches it.

    A subagent writes its own memory files as it works; this returns them to what the employee was hired with.
    """
    root = Path(home) / SUBAGENTS
    wanted = {**seed, **{relative: text.encode() for relative, text in authored.items()}}
    changed = []
    for relative, content in wanted.items():
        path = root / relative
        if not path.is_file() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            changed.append(relative)
    return changed


class Fresh:
    """A trial that starts with each housed subagent home as hired, plus what the installed revision authored.

    Without it, what a subagent remembered from one drill would carry into the next drill and the next round,
    and a change in results could not be told apart from a change the Curator made.
    """

    def __init__(self, trial, seed: dict[str, bytes]):
        self.trial, self.seed = trial, seed

    async def run(self, worker):
        from ..curator.harness import Declaration
        from ..curator.raven_adapter.materialize import content_updates
        from ..curator.raven_adapter.targets import catalogue

        home = worker.baseline.config.workspace_path
        authored = {}
        declaration = Declaration("trial-content", catalogue())
        for child in getattr(worker, "children", {}).values():
            for path, text in content_updates(
                child.baseline.config.workspace_path, child.artifact, declaration
            ).items():
                if path.is_relative_to(home / SUBAGENTS):
                    authored[path.relative_to(home / SUBAGENTS).as_posix()] = text
        refresh(home, self.seed, authored)
        return await self.trial.run(worker)


class Together:
    """Trials each played on its own replica of the employee, made from it as the trials begin (see `replicate`)
    and closed when they end; trials of one group run at once.

    The employee itself plays no trial, so every drill starts from its installed revision with a clean workdir and
    home: no drill reads another's files, and nothing a drill wrote carries into the next one or the next round.
    `trials` maps each trial's name to it; `spawn(employee, label)` makes an unstarted replica. `placed` is filled
    with the workdir and home each named trial played in, for readers of the files a drill wrote.
    """

    def __init__(self, trials: dict, spawn, placed: dict | None = None):
        if not trials:
            raise ValueError("playing trials together needs at least one trial")
        self.trials, self.spawn, self.placed, self.played = dict(trials), spawn, placed, 0

    async def run(self, worker):
        self.played += 1
        replicas = {name: self.spawn(worker, f"{self.played}-{name}") for name in self.trials}
        if self.placed is not None:
            self.placed.update(
                {
                    name: {"workdir": replica.baseline.workdir, "home": replica.baseline.config.workspace_path}
                    for name, replica in replicas.items()
                }
            )
        tasks = []
        try:
            await asyncio.gather(*(replica.start() for replica in replicas.values()))
            for replica in replicas.values():
                same_revision(worker, replica, running=True)
            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(trial.run(replicas[name])) for name, trial in self.trials.items()]
            except ExceptionGroup as failed:
                raise failed.exceptions[0] from failed
        finally:
            await asyncio.gather(*(replica.close() for replica in replicas.values()), return_exceptions=True)
        sessions = {}
        for task in tasks:
            produced = task.result()
            if sessions.keys() & produced.keys():
                raise ValueError(f"trials produced the same session key: {sorted(sessions.keys() & produced.keys())}")
            sessions.update(produced)
        return sessions


def skills(worker: Worker) -> Path:
    """The employee's skill pool: materials the agency puts here become sources it can read."""
    return worker.baseline.config.workspace_path / "skills"
