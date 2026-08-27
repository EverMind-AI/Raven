"""A graph whose work has nowhere to run is refused before anything is spawned.

The shape this exists for: "train me a model and watch it" becomes
`coding -> on-call`, and the on-call half needs a machine. Without this the
coding node runs to completion first and the registry turns out to be empty
afterwards, with the code already written -- and written for an imagined
machine, since how much device memory there is decides the batch size and
whether gradient checkpointing has to be on.

The other half of what these pin is the silence. The check leaves this process
to ask, so most of what can go wrong is not evidence of anything: an unbuilt
venv, an agent that has nothing to do with machines, a timeout. Every one of
those has to leave the graph exactly as it was.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from raven.agent.subagent_dag import _machines


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """An agent whose checkout has a built venv, ready to be asked."""
    binary = tmp_path / ".venv" / "bin" / "raven"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(_machines, "_raven_in", lambda agent: str(binary))
    return binary


def answers(monkeypatch, stdout: str, *, code: int = 0):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, code, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake)


def test_a_graph_with_nowhere_to_run_is_refused(installed, monkeypatch):
    answers(monkeypatch, json.dumps({"state": "ok", "listed": 0, "usable": 0, "blocking": []}))

    stranded = _machines.machineless(["Raven-Oncall"])

    assert stranded is not None and stranded.agent == "Raven-Oncall"


def test_the_refusal_names_what_the_owner_has_to_supply(installed, monkeypatch):
    """The loop cannot fill any of it in: the address and the key are outside
    what it can see, by design."""
    answers(monkeypatch, json.dumps({"state": "ok", "listed": 1, "usable": 0, "blocking": ["gpu: 'key' is missing"]}))

    text = _machines.refusal(_machines.machineless(["Raven-Oncall"]))

    assert "Nothing was dispatched" in text
    assert "'key' is missing" in text, "say which machine failed and why"
    for asked in ("port", "private key", "installed", "budget", "at once"):
        assert asked in text, asked
    assert "raven ops connection add" in text


def test_a_machine_that_is_there_lets_the_graph_through(installed, monkeypatch):
    answers(monkeypatch, json.dumps({"state": "ok", "listed": 2, "usable": 1, "blocking": []}))

    assert _machines.machineless(["Raven-Oncall"]) is None


def test_an_agent_with_nothing_to_do_with_machines_is_not_asked_twice(monkeypatch):
    """`_raven_in` returns None for a non-vendored agent, and that is the end of it."""
    monkeypatch.setattr(_machines, "_raven_in", lambda agent: None)

    assert _machines.ask("Raven-Code") is None
    assert _machines.machineless(["Raven-Code", "Raven-Ppt"]) is None


def test_a_checkout_whose_command_does_not_exist_refuses_nothing(installed, monkeypatch):
    """An agent that has no `ops connection` surface says so by failing to parse.
    That is not evidence the owner has no machines."""
    answers(monkeypatch, "No such command 'connection'.", code=2)

    assert _machines.machineless(["Raven-Code"]) is None


def test_a_timeout_refuses_nothing(installed, monkeypatch):
    def explode(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20.0)

    monkeypatch.setattr(subprocess, "run", explode)

    assert _machines.machineless(["Raven-Oncall"]) is None


def test_output_that_will_not_parse_refuses_nothing(installed, monkeypatch):
    answers(monkeypatch, "usable: none\n")

    assert _machines.machineless(["Raven-Oncall"]) is None


def test_a_payload_missing_the_field_it_promises_refuses_nothing(installed, monkeypatch):
    answers(monkeypatch, json.dumps({"state": "ok"}))

    assert _machines.machineless(["Raven-Oncall"]) is None


def test_the_registry_it_asks_about_is_the_owners(installed, monkeypatch):
    """Asked with no --config, so the sub-agent's reader resolves the host's raven
    home -- the one file `raven ops connection add` writes. A launcher may have
    pointed this process elsewhere, so that pointer is cleared."""
    seen = {}

    def fake(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"listed": 1, "usable": 1}), "")

    monkeypatch.setenv("RAVEN_CONNECTIONS", "/somewhere/else/connections.json")
    monkeypatch.setattr(subprocess, "run", fake)

    _machines.ask("Raven-Oncall")

    assert "--config" not in seen["argv"]
    assert seen["argv"][1:] == ["ops", "connection", "doctor", "--json"]
    assert "RAVEN_CONNECTIONS" not in seen["env"]


def test_only_one_agent_is_reported_even_when_two_are_stranded(installed, monkeypatch):
    """The owner has to be asked either way; twice is the same question twice."""
    answers(monkeypatch, json.dumps({"listed": 0, "usable": 0}))

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
