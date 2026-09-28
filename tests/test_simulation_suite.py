"""The cultivation suite: spend from recorded calls, credential removal, reproducible commands and budget stops."""

import json
import random
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from experimental.simulation import suite
from experimental.simulation.agency import partition
from experimental.simulation.scenario import BUNDLED, Scenario


def span(cost, session=None):
    return json.dumps({"name": "llm.call", "attributes": {"session.id": session, "llm.usage.cost_total": str(cost)}})


def state_with(tmp_path, name, lines):
    logs = tmp_path / name / "traces" / "logs"
    logs.mkdir(parents=True)
    (logs / "audit-spans.log").write_text("\n".join([*lines, '{"name": "tool.call"}', "not json"]) + "\n")
    return tmp_path / name


def arguments(tmp_path, **overrides):
    values = {
        "home": tmp_path / "home",
        "rounds": 4,
        "turns": 14,
        "repeats": 1,
        "timeout": 3600,
        "curator_model": "z-ai/glm-5.3",
        "analyst_model": None,
        "simulation_model": "z-ai/glm-5.3",
        "traveller_model": "deepseek/deepseek-flash",
        "subagent_model": "z-ai/glm-5.3-flash",
        "cards": ["family", "student"],
        "scenario": BUNDLED / "travel_agency",
        "chains": [],
        "partitions": None,
        "random_partitions": 0,
        "seed": 7,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_spend_splits_recorded_calls_by_who_made_them(tmp_path):
    state = state_with(tmp_path, "run", [span(1.5), span(0.25, "curator:user"), span(2, "acp:20260924_1"), span(0.25)])
    assert suite.spend(state) == {"simulation": 1.75, "employee": 0.25, "subagents": 2.0, "total": 4.0}
    assert suite.spend(tmp_path / "missing")["total"] == 0
    child = tmp_path / "records" / "children" / "701d" / "native" / "traces" / "logs"
    child.mkdir(parents=True)
    (child / "audit-spans.log").write_text(span(0.5) + "\n" + span(0.25, "acp:x") + "\n")
    assert suite.spend(state, tmp_path / "records") == {
        "simulation": 1.75,
        "employee": 0.25,
        "subagents": 2.75,
        "total": 4.75,
    }
    replica = tmp_path / "records" / "replicas" / "1-student" / "children" / "701d" / "native" / "traces" / "logs"
    replica.mkdir(parents=True)
    (replica / "audit-spans.log").write_text(span(1) + "\n")
    assert suite.spend(state, tmp_path / "records")["subagents"] == 3.75


def test_a_call_its_provider_did_not_price_is_priced_from_its_tokens():
    usage = {"llm.usage.input_tokens": 1_000_000, "llm.usage.output_tokens": 500_000, "llm.usage.cache_read_tokens": 0}
    assert round(suite.call_cost({"llm.model": "deepseek/deepseek-flash", **usage}), 6) == 0.9
    cached = {**usage, "llm.usage.input_tokens": 0, "llm.usage.cache_read_tokens": 1_000_000}
    assert round(suite.call_cost({"llm.model": "deepseek-flash", **cached}), 6) == round(0.006 + 0.60, 6)
    assert suite.call_cost({"llm.model": "deepseek/deepseek-flash", "llm.usage.cost_total": 0.5, **usage}) == 0.5
    assert suite.call_cost({"llm.model": "unknown/model", **usage}) == 0


def test_scrub_redacts_every_credential_in_the_workers_configuration_copies(tmp_path):
    generation = tmp_path / "records" / "0f3a"
    generation.mkdir(parents=True)
    config = generation / "config.json"
    config.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"apiKey": "sk-or-v1-" + "a" * 40}},
                "tools": {"web": {"search": {"apiKey": "f" * 40}}},
                "subagents": {"agents": [{"env": {"PPT_API_KEY": "anything", "PPT_MODEL": "z-ai/glm-5.3-flash"}}]},
                "sentinel": {"evaluatorApiKeyEnv": "OPENAI_API_KEY"},
            }
        )
    )
    config.chmod(0o600)
    nested = tmp_path / "records" / "children" / "701d" / "versions" / "017f" / "deployment.json"
    nested.parent.mkdir(parents=True)
    nested.write_text(json.dumps({"config": {"providers": {"openrouter": {"apiKey": "sk-or-v1-" + "b" * 40}}}}))
    rendered = tmp_path / "records" / "children" / "701d" / "prepared" / "state" / ".config.rendered.42.json"
    rendered.parent.mkdir(parents=True)
    rendered.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-or-v1-" + "c" * 40}}}))
    assert suite.scrub(tmp_path / "records") == 5
    text = config.read_text()
    assert "sk-or-v1" not in text and "f" * 40 not in text and "anything" not in text
    assert "z-ai/glm-5.3-flash" in text and "OPENAI_API_KEY" in text and config.stat().st_mode & 0o777 == 0o600
    assert "sk-or-v1" not in nested.read_text() and "sk-or-v1" not in rendered.read_text()


def test_the_command_names_every_setting_so_a_run_can_be_reproduced(tmp_path):
    argv = suite.command(
        ["--chain", "documents"],
        arguments(tmp_path),
        config=tmp_path / "c.json",
        workdir=tmp_path / "w",
        records=tmp_path / "r",
    )
    assert argv[:3] == [sys.executable, "-m", "experimental.simulation"]
    flat = " ".join(argv[3:])
    assert "--chain documents" in flat and "--rounds 4" in flat and "--cards family student" in flat
    assert "--curator " not in flat and "--targets" not in flat
    assert "--subagent-model z-ai/glm-5.3-flash" in flat and "--analyst-model" not in flat
    assert "--traveller-model deepseek/deepseek-flash" in flat
    budgets = " ".join(
        suite.command(
            [],
            arguments(tmp_path, curator_calls=96, curator_queries=144, analysis="analyst"),
            config=tmp_path / "c.json",
            workdir=tmp_path / "w",
            records=tmp_path / "r",
        )
    )
    assert "--curator-calls 96 --curator-queries 144" in budgets
    assert "--analysis analyst" in budgets and "--analysis" not in flat
    efforts = " ".join(
        suite.command(
            ["--chain", "staged"],
            arguments(tmp_path, curator_effort="high", traveller_effort="low"),
            config=tmp_path / "c.json",
            workdir=tmp_path / "w",
            records=tmp_path / "r",
        )[3:]
    )
    assert "--curator-effort high" in efforts and "--traveller-effort low" in efforts
    assert "--concurrent-drills" not in efforts
    together = suite.command(
        [],
        arguments(tmp_path, concurrent_drills=3),
        config=tmp_path / "c.json",
        workdir=tmp_path / "w",
        records=tmp_path / "r",
    )
    assert " --concurrent-drills 3 " in " ".join(together) + " "


def test_the_budget_counts_only_runs_that_started(tmp_path):
    started = suite.Run("staged", [], "a", state_with(tmp_path, "a", [span(3)]), tmp_path / "ra", started=1.0)
    waiting = suite.Run("documents", [], "b", state_with(tmp_path, "b", [span(5)]), tmp_path / "rb")
    assert suite.over_budget([started, waiting], 3) and not suite.over_budget([started, waiting], 3.5)
    assert not suite.over_budget([started], None)


def test_a_stopped_run_is_marked_in_its_record_and_finished_with_its_evidence(tmp_path):
    records = tmp_path / "records"
    (records / "iteration").mkdir(parents=True)
    (records / "iteration" / "r.json").write_text(json.dumps({"status": "running", "rounds": [{}]}))
    (records / "gen").mkdir()
    (records / "gen" / "config.json").write_text(json.dumps({"providers": {"x": {"apiKey": "secret"}}}))
    state = state_with(tmp_path, "state", [span(0.5)])
    (state / "workdir" / "handover").mkdir(parents=True)
    (state / "workdir" / "handover" / "hold.md").write_text("ticket")
    (state / "run.log").write_text("started with sk-or-v1-" + "a" * 40 + ", Bearer tok.en\n")
    orphan = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", orphan], start_new_session=True)
    run = suite.Run("staged", ["--chain", "staged"], "staged-1", state, records, process=process, started=1.0)
    deadline = time.time() + 10
    while not suite.descendants(process.pid) and time.time() < deadline:
        time.sleep(0.1)
    below = suite.descendants(process.pid)
    suite.stop(run, "the suite reached its budget of $1")
    assert process.poll() is not None and below
    time.sleep(0.5)
    assert all(
        not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2] for pid in below
    )
    record = json.loads((records / "iteration" / "r.json").read_text())
    assert record["status"] == "error" and "budget" in record["error"]
    summary = suite.finish(run, SimpleNamespace())
    assert summary["status"] == "error" and summary["rounds"] == 1 and summary["stopped"].startswith("the suite")
    assert summary["credentials_redacted"] == 1 and summary["spend"]["total"] == 0.5
    assert (records / "traces" / "audit-spans.log").is_file()
    assert (records / "workdir" / "handover" / "hold.md").read_text() == "ticket"
    assert (records / "run.log").read_text() == "started with <redacted>, <redacted>\n"
    assert not (records / "config.json").exists()
    written = json.loads((records / suite.SUMMARY).read_text())
    assert written["name"] == "staged-1" and written["plan"] == ["--chain", "staged"] and "value" in written
    assert "secret" not in Path(records / "gen" / "config.json").read_text()


def test_random_partitions_cover_every_material_and_fit_before_the_last_round():
    materials = list(Scenario.load(BUNDLED / "travel_agency").materials)
    rng = random.Random(3)
    for _ in range(50):
        steps = suite.random_partition(materials, 4, rng)
        assert 2 <= len(steps) <= 4 and partition(steps, materials, rounds=4)
    core = [
        name for name in materials if name not in ("brand-design-guide", "consultation-scripts", "plan-deck-sample")
    ]
    for _ in range(50):
        steps = suite.random_partition(materials, 4, rng, core)
        assert set(core) <= set(steps[0]) and 2 <= len(steps) <= 4 and partition(steps, materials, rounds=4)
    with pytest.raises(ValueError, match="outside"):
        suite.random_partition(materials, 4, rng, materials)


def test_candidates_join_chains_given_partitions_and_seeded_random_ones(tmp_path):
    given = tmp_path / "partitions.json"
    given.write_text(json.dumps([[["service-sop"], ["price-list"]]]))
    args = arguments(tmp_path, chains=["staged", "by-stage"], partitions=given, random_partitions=2)
    found = suite.candidates(args)
    assert [label for label, _ in found] == ["staged", "by-stage", "p1", "r7-1", "r7-2"]
    assert found[2][1] == ["--partition", json.dumps([["service-sop"], ["price-list"]])]
    assert suite.candidates(args) == found
