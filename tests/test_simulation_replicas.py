"""A round's drills played at once: replicas start where the employee stands, on its revision, and are closed after."""

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from experimental.curator.harness import Artifact
from experimental.curator.raven_adapter.deployment import child_directory
from experimental.curator.raven_adapter.hosting.prepare import prepare_children
from experimental.curator.raven_adapter.strategy import Scopes
from experimental.curator.raven_adapter.worker import Execution
from experimental.iteration.protocols import Exchange
from experimental.simulation.agency import NAME as REVIEW
from experimental.simulation.agency import Agency
from experimental.simulation.employee import HOME, WORKDIR, Together, hire, replicate
from experimental.simulation.scenario import BUNDLED, Scenario
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest

TRAVEL = BUNDLED / "travel_agency"
AUTHORED = Artifact(values={"memory.prompt": {"AGENTS.md": "Quote from the price list."}})


def config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {"deepseek": {"api_key": "k"}},
                "agents": {"defaults": {"model": "deepseek/x", "provider": "deepseek"}},
            }
        )
    )
    return path


def test_a_replica_copies_the_employee_as_it_stands_with_its_installed_revision(tmp_path):
    scenario, config = Scenario.load(TRAVEL), config_file(tmp_path)
    (tmp_path / "work").mkdir()
    employee = hire(scenario, config, workdir=tmp_path / "work", root=tmp_path / "run")
    employee.children = prepare_children(employee.baseline, employee.root, ["Raven"])
    employee.children["Raven"].artifact = Artifact(values={"memory.prompt": {"TOOLS.md": "Search first."}})
    employee.artifact = AUTHORED
    home = employee.baseline.config.workspace_path
    (home / "skills" / "service-sop").mkdir(parents=True)
    (home / "skills" / "service-sop" / "SKILL.md").write_text("SOP")
    (home / "sessions").mkdir()
    (home / "sessions" / "old.jsonl").write_text("{}")
    (tmp_path / "work" / "handover").mkdir()
    (tmp_path / "work" / "handover" / "ticket.md").write_text("earlier ticket")
    saved = {"task_id": employee.baseline.task.id, "data": {}, "sessions": {"c1": {"stage": "S2"}}}
    (employee.root / "planning.json").write_text(json.dumps(saved))
    child_state = child_directory(employee.root, "Raven")
    child_state.mkdir(parents=True, exist_ok=True)
    (child_state / "action.json").write_text('{"held": 1}')

    root = tmp_path / "run" / "replicas" / "1-student"
    replica = replicate(employee, config, root)
    assert replica.root == root.resolve() and replica.baseline.config.workspace_path == root / HOME
    assert (root / HOME / "skills" / "service-sop" / "SKILL.md").read_text() == "SOP"
    assert not (root / HOME / "sessions").exists()
    assert (root / WORKDIR / "handover" / "ticket.md").read_text() == "earlier ticket"
    assert replica.baseline.workdir == root / WORKDIR
    assert replica.artifact == AUTHORED and replica.children["Raven"].artifact == employee.children["Raven"].artifact
    assert replica.children["Raven"].baseline.config.workspace_path == root / HOME / "subagents" / "Raven"
    # A replica under a new task id would refuse the employee's saved plan before any drill started.
    assert replica.baseline.task == employee.baseline.task
    assert Scopes("planning", replica.baseline.task, root / "planning.json", dict, per_session=True).saved["c1"] == {
        "stage": "S2"
    }
    assert (child_directory(root, "Raven") / "action.json").read_text() == '{"held": 1}'
    assert employee.artifact == AUTHORED and (tmp_path / "work" / "handover" / "ticket.md").is_file()
    assert replica.revision_id == employee.revision_id


class Employee:
    def __init__(self, workdir, artifact=AUTHORED, package="_curator_abc"):
        self.baseline = SimpleNamespace(workdir=workdir, config=SimpleNamespace(workspace_path=workdir / "home"))
        self.root = workdir
        self.artifact, self.children, self.package = artifact, {}, package
        self.started = self.closed = False

    def records(self):
        return [{"kind": "runtime.bound", "package": f"/runs/x/{self.package}"}] if self.started else []

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True


class Drill:
    def __init__(self, name, gate=None, error=None):
        self.name, self.gate, self.error, self.worker = name, gate, error, None

    async def run(self, worker):
        self.worker = worker
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return {self.name: []}


async def test_every_drill_runs_at_once_on_its_own_replica_and_the_employee_plays_none(tmp_path):
    employee = Employee(tmp_path / "employee")
    employee.started = True
    made, placed = {}, {}

    def spawn(worker, label):
        assert worker is employee and not any(drill.worker for drill in drills.values())
        made[label] = Employee(tmp_path / label)
        return made[label]

    gate = asyncio.Barrier(3)
    drills = {name: Drill(name, gate) for name in ("family", "premium", "student")}
    together = Together(drills, spawn, placed)
    sessions = await asyncio.wait_for(together.run(employee), 5)
    assert sessions == {"family": [], "premium": [], "student": []}
    assert [drills[name].worker for name in drills] == [made[f"1-{name}"] for name in drills]
    assert not employee.closed and all(replica.started and replica.closed for replica in made.values())
    assert {name: where["workdir"] for name, where in placed.items()} == {
        name: tmp_path / f"1-{name}" for name in drills
    }
    drills = {name: Drill(name) for name in drills}
    together.trials = drills
    await together.run(employee)
    assert sorted(made) == [f"{round}-{name}" for round in (1, 2) for name in ("family", "premium", "student")]


async def test_a_replica_on_another_revision_never_plays_and_every_replica_is_closed(tmp_path):
    employee = Employee(tmp_path / "employee")
    employee.started = True
    made = []

    def spawn(worker, label):
        made.append(Employee(tmp_path / label, package="_curator_old" if label.endswith("student") else "_curator_abc"))
        return made[-1]

    drills = {name: Drill(name) for name in ("family", "premium", "student")}
    with pytest.raises(ValueError, match="bound _curator_old"):
        await Together(drills, spawn).run(employee)
    assert not any(drill.worker for drill in drills.values()) and all(replica.closed for replica in made)


async def test_a_failed_drill_stops_the_others_and_closes_the_replicas(tmp_path):
    employee = Employee(tmp_path / "employee")
    employee.started = True
    made = []

    def spawn(worker, label):
        made.append(Employee(tmp_path / label))
        return made[-1]

    never = asyncio.Event()
    drills = {"family": Drill("family", never), "student": Drill("student", error=RuntimeError("customer left"))}
    with pytest.raises(RuntimeError, match="customer left"):
        await asyncio.wait_for(Together(drills, spawn).run(employee), 5)
    assert made and all(replica.closed for replica in made)


async def test_the_owner_reads_what_each_drill_filed_on_its_replica_and_nothing_it_found_there(tmp_path):
    scenario = Scenario.load(TRAVEL)
    employee = tmp_path / "employee"
    (employee / "workdir" / "handover").mkdir(parents=True)
    (employee / "workdir" / "handover" / "earlier.md").write_text("earlier ticket")
    (employee / "home").mkdir()
    placed = {}
    for name, text in (("family", "family ticket"), ("student", "student ticket")):
        replica = tmp_path / "replicas" / f"1-{name}"
        shutil.copytree(employee, replica)
        (replica / "workdir" / "handover" / "hold.md").write_text(text)
        (replica / "home" / "notes").mkdir()
        (replica / "home" / "notes" / "brief.md").write_text(f"{name} brief")
        placed[name] = {"workdir": replica / "workdir", "home": replica / "home"}

    def played(reply):
        records = [{"kind": "runner.event", "event_type": "Text", "event": {"content": reply}}]
        return [Exchange("Book it", Execution("t1", [], records, {}, "a"))]

    rows = [{"id": criterion.id, "result": "pass"} for criterion in scenario.criteria]
    verdict = LLMResponse(
        content=None, tool_calls=[ToolCallRequest(REVIEW, REVIEW, {"verdicts": rows, "remark": "Fine."})]
    )
    requests = []

    class Provider:
        async def chat_with_retry(self, **kwargs):
            requests.append(kwargs)
            return verdict

    agency = Agency(
        scenario,
        Provider(),
        employee / "home" / "skills",
        workdir=employee / "workdir",
        plan="all",
        uploads=employee / "home" / "uploads",
        workdirs=placed,
    )
    agency.prepare()
    await agency.evaluate({"family": played("Booked."), "student": played("Booked too."), "premium": played("Hi.")})
    packet = json.loads(requests[0]["messages"][1]["content"])
    assert packet["filed"] == {
        name: {"workdir/handover/hold.md": f"{name} ticket", "home/notes/brief.md": f"{name} brief"}
        for name in ("family", "student")
    }
