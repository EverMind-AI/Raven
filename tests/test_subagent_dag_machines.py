"""A graph whose work has nowhere to run is refused before anything is spawned.

The shape this exists for: "train me a model and watch it" becomes
`coding -> on-call`, and the on-call half needs a machine. Without this the
coding node runs to completion first and the registry turns out to be empty
afterwards, with the code already written -- and written for an imagined
machine, since how much device memory there is decides the batch size and
whether gradient checkpointing has to be on.

The other half of what these pin is the silence. An agent whose manifest does
not claim machines, a folder that cannot be found, a manifest that will not
parse: none of those are evidence of anything, and every one has to leave the
graph exactly as it was.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.subagent import dag_machines as _machines


@pytest.fixture
def oncall_flagged(monkeypatch):
    """Raven-Oncall's manifest claims machines; nobody else's does."""
    monkeypatch.setattr(_machines, "runs_on_machines", lambda agent: agent == "Raven-Oncall")


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A writable registry file the reader resolves through RAVEN_CONNECTIONS."""
    path = tmp_path / "connections.json"
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))

    def write(rows):
        path.write_text(json.dumps({"connections": rows}), encoding="utf-8")

    return write


_GOOD_ROW = {
    "id": "cpu-box",
    "display_name": "CPU box",
    "transport": "ssh",
    "host": "10.0.0.1",
    "port": 22,
    "user": "root",
    "key": "~/.ssh/id_rsa",
    "software": "ccx",
    "budget_unit": "minute",
    "concurrency": 1,
}


def test_a_graph_with_nowhere_to_run_is_refused(oncall_flagged, registry):
    registry([])

    stranded = _machines.machineless(["Raven-Oncall"])

    assert stranded is not None and stranded.agent == "Raven-Oncall"


def test_a_missing_registry_is_also_nowhere_to_run(oncall_flagged, monkeypatch, tmp_path):
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(tmp_path / "never-written.json"))

    assert _machines.machineless(["Raven-Oncall"]) is not None


def test_the_refusal_names_what_the_owner_has_to_supply(oncall_flagged, registry):
    """The loop cannot fill any of it in: the address and the key are outside
    what it can see, by design."""
    registry([{"id": "gpu", "transport": "ssh", "host": "10.0.0.2"}])

    text = _machines.refusal(_machines.machineless(["Raven-Oncall"]))

    assert "Nothing was dispatched" in text
    assert "'port' is missing" in text, "say which machine failed and why"
    for asked in ("port", "private key", "installed", "budget", "at once"):
        assert asked in text, asked
    assert "raven ops connection add" in text


def test_a_machine_that_is_there_lets_the_graph_through(oncall_flagged, registry):
    registry([_GOOD_ROW])

    assert _machines.machineless(["Raven-Oncall"]) is None
    verdict = _machines.verdict_for(["Raven-Oncall"])
    assert verdict.usable == 1 and verdict.machines[0]["id"] == "cpu-box"


def test_what_the_verdict_carries_is_never_the_way_onto_the_machine(oncall_flagged, registry):
    registry([_GOOD_ROW])

    machine = _machines.verdict_for(["Raven-Oncall"]).machines[0]

    assert "host" not in machine and "key" not in machine and "user" not in machine


def test_an_agent_whose_manifest_claims_nothing_refuses_nothing(oncall_flagged, registry):
    registry([])

    assert _machines.ask("Raven-Code") is None
    assert _machines.machineless(["Raven-Code", "Raven-Ppt"]) is None


def test_a_manifest_that_cannot_be_read_refuses_nothing(monkeypatch, registry):
    from raven.agent.subagent import vendored_agents

    monkeypatch.setattr(vendored_agents, "vendored_folder", lambda agent: None)
    registry([])

    assert _machines.machineless(["Raven-Oncall"]) is None


@pytest.mark.parametrize("spelling", ["runsOnMachines", "runs_on_machines"])
def test_the_flag_is_read_off_the_manifest_in_either_spelling(monkeypatch, tmp_path, spelling):
    """conftest points ``subagents_root`` at nothing, so the folder is stubbed;
    what this pins is the read itself -- both manifest spellings count."""
    folder = tmp_path / "raven-oncall"
    folder.mkdir()
    (folder / "subagent.json").write_text(json.dumps({"name": "Raven-Oncall", spelling: True}), encoding="utf-8")
    monkeypatch.setattr("raven.agent.subagent.vendored_agents.vendored_folder", lambda agent, root=None: folder)

    assert _machines.runs_on_machines("Raven-Oncall") is True


def test_a_manifest_without_the_flag_claims_nothing(monkeypatch, tmp_path):
    folder = tmp_path / "raven-code"
    folder.mkdir()
    (folder / "subagent.json").write_text(json.dumps({"name": "Raven-Code"}), encoding="utf-8")
    monkeypatch.setattr("raven.agent.subagent.vendored_agents.vendored_folder", lambda agent, root=None: folder)

    assert _machines.runs_on_machines("Raven-Code") is False


def test_only_one_agent_is_reported_even_when_two_are_stranded(monkeypatch, registry):
    """The owner has to be asked either way; twice is the same question twice."""
    monkeypatch.setattr(_machines, "runs_on_machines", lambda agent: True)
    registry([])

    stranded = _machines.machineless(["Raven-Oncall", "Raven-Sim"])

    assert stranded.agent == "Raven-Oncall"


# --- which machine, and who is told ------------------------------------------


class _Node:
    """Enough of a DagNodeSpec for the naming and broadcast rules."""

    def __init__(self, node_id, subagent, template="do the thing", inputs=None, depends_on=()):
        self.id, self.subagent, self.prompt_template = node_id, subagent, template
        self.inputs, self.depends_on = dict(inputs or {}), list(depends_on)

    def model_copy(self, *, update):
        clone = _Node(self.id, self.subagent, self.prompt_template, self.inputs, self.depends_on)
        for key, value in update.items():
            setattr(clone, key, value)
        return clone


GPU = {
    "id": "conn_gpu",
    "display_name": "GPU机器",
    "kind": "gpu",
    "device": "2 x A800-80GB",
    "memory": "463 GB",
    "software": "PyTorch 2.12 + CUDA",
}
CPU = {"id": "conn_cpu", "display_name": "CPU box", "kind": "cpu", "cores": 32}


def verdict(*machines):
    return _machines.Verdict("Raven-Oncall", usable=len(machines), listed=len(machines), machines=tuple(machines))


def test_a_node_that_runs_work_on_a_machine_has_to_say_which(monkeypatch):
    """Refused rather than defaulted, even with one machine: the choice is the
    owner's, and a default picked here would appear in the ledger as a decision
    somebody made."""
    nodes = [_Node("code", "Raven-Code"), _Node("watch", "Raven-Oncall")]

    problem = _machines.unnamed_machine(nodes, verdict(GPU))

    assert "does not say which" in problem
    assert "conn_gpu" in problem, "the candidates have to be in front of it"
    assert "'machine'" in problem


def test_a_machine_that_is_not_on_the_list_is_refused(monkeypatch):
    nodes = [_Node("watch", "Raven-Oncall", inputs={"machine": "conn_typo"})]

    problem = _machines.unnamed_machine(nodes, verdict(GPU))

    assert "conn_typo" in problem and "not one this installation can use" in problem


def test_a_named_machine_passes(monkeypatch):
    nodes = [_Node("watch", "Raven-Oncall", inputs={"machine": "conn_gpu"})]

    assert _machines.unnamed_machine(nodes, verdict(GPU)) == ""


def test_every_node_is_told_not_just_the_ones_before_the_work(monkeypatch):
    """The node writing the training script needs the device memory; the node
    reporting afterwards has to say which box the numbers came from. Which of
    them needs it is not something this code gets to see."""
    nodes = [
        _Node("code", "Raven-Code"),
        _Node("watch", "Raven-Oncall", inputs={"machine": "conn_gpu"}, depends_on=["code"]),
        _Node("report", "Raven-Research", depends_on=["watch"]),
    ]

    told = _machines.with_facts(nodes, verdict(GPU))

    for node in told:
        assert "A800" in node.prompt_template, f"{node.id} was not told"
        assert "463 GB" in node.prompt_template


def test_what_travels_is_what_the_machine_is_never_the_way_onto_it(monkeypatch):
    """The far side projects this; nothing here should be able to leak an address
    into a prompt even if the payload carried one."""
    nodes = [_Node("watch", "Raven-Oncall", inputs={"machine": "conn_gpu"})]

    told = _machines.with_facts(nodes, verdict(GPU))

    assert "id" not in told[0].prompt_template.split("---")[1].split("\n")[1]
    assert "not reachable from here" in told[0].prompt_template


def test_a_graph_that_names_no_machine_is_left_alone(monkeypatch):
    nodes = [_Node("a", "Raven-Code"), _Node("b", "Raven-Research")]

    assert _machines.with_facts(nodes, verdict(GPU)) is nodes


def test_two_machines_in_one_graph_are_both_told_to_everyone(monkeypatch):
    """A node reading the wrong one is the failure this prevents, and showing it
    less does not fix that."""
    nodes = [
        _Node("gpu_work", "Raven-Oncall", inputs={"machine": "conn_gpu"}),
        _Node("cpu_work", "Raven-Oncall", inputs={"machine": "conn_cpu"}),
        _Node("report", "Raven-Research"),
    ]

    told = _machines.with_facts(nodes, verdict(GPU, CPU))

    for node in told:
        assert "GPU机器" in node.prompt_template and "CPU box" in node.prompt_template


def test_two_rows_behind_one_id_strand_the_graph(monkeypatch, registry):
    """Reproduced in review: two individually valid rows sharing an id kept
    usable=2 while problems() reported a blocking defect, so dispatch went
    ahead and Verdict.machine silently picked whichever row came first. The
    id is what a campaign stores; an ambiguous one is nowhere to run."""
    monkeypatch.setattr(_machines, "runs_on_machines", lambda agent: True)
    registry([_GOOD_ROW, {**_GOOD_ROW, "display_name": "same id, other box", "host": "10.0.0.9"}])

    stranded = _machines.machineless(["Raven-Oncall"])

    assert stranded is not None and stranded.usable == 0


def test_whitespace_equivalent_duplicate_ids_also_strand_the_graph(monkeypatch, registry):
    """problems() strips ids before duplicate detection; usable() must strip the
    same way, or `cpu` and ` cpu ` produce the blocking diagnostic while both
    rows stay usable (reproduced in review)."""
    monkeypatch.setattr(_machines, "runs_on_machines", lambda agent: True)
    registry([_GOOD_ROW, {**_GOOD_ROW, "id": f" {_GOOD_ROW['id']} ", "host": "10.0.0.9"}])

    stranded = _machines.machineless(["Raven-Oncall"])

    assert stranded is not None and stranded.usable == 0
