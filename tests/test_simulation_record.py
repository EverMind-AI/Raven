"""The cultivation record: parsing a run, linking requirements and changes to criteria, the ledger and the export."""

import hashlib
import json
import shlex
from pathlib import Path

import pytest

from experimental.simulation import record
from experimental.simulation.record import build_record, export_record, facet, ledger, link, markers, redact

T1 = "a1" * 16
T2 = "b2" * 16
KEY = "sk-or-v1-" + "a" * 40
JINA = "jina_" + "x" * 25
HEX40 = "0123456789abcdef0123456789abcdef01234567"
QUOTE = "the pair total lands around 2,020 pounds"


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def scenario_dir(tmp_path: Path) -> Path:
    root = tmp_path / "scenario"
    write(root / "profile.md", "A small agency selling short trips.")
    write(root / "onboarding.md", "Here are my files:\n{files}")
    write(root / "materials" / "sop" / "SKILL.md", "---\nname: sop\n---\n# Service procedure\n\nGreet, then ask.\n")
    write(root / "materials" / "price-list" / "SKILL.md", "---\nname: price-list\n---\n# Price list\n\nComfort 3900.\n")
    write(root / "materials" / "brand" / "SKILL.md", "---\nname: brand\n---\n# Brand guide\n")
    write(root / "materials" / "brand" / "template.pptx", b"deck-template")
    write(root / "personas" / "family.md", "A family of four.")
    write(root / "personas" / "student.md", "A student.")
    write(
        root / "scenario.json",
        {
            "initial": ["sop"],
            "criteria": [
                {
                    "id": "ai-identity",
                    "check": "G1: the first message discloses the AI consultant.",
                    "severity": "red_line",
                },
                {"id": "question-limit", "check": "G2: at most two questions per message."},
                {"id": "quote-correct", "check": "S4.2: every figure matches the price list.", "severity": "red_line"},
                {"id": "deck-structure", "check": "The deck has the required pages."},
            ],
        },
    )
    return root


def text_row(turn, content):
    return {"kind": "runner.event", "turn_id": turn, "event_type": "Text", "event": {"content": content}}


def round_one_rows(run: Path) -> list[dict]:
    out_file = write(
        run / "home" / "sessions" / "curator" / "family_1" / "subagents" / "nodes" / "n1.out.md", "Found 3 trains."
    )
    return [
        {"kind": "provider.request", "turn_id": T1, "parameters": {"messages": []}},
        {"kind": "participant.call", "turn_id": T1, "target": "capability.select_tools", "phase": "iteration"},
        {
            "kind": "participant.result",
            "turn_id": T1,
            "target": "capability.select_tools",
            "result": [{"type": "function", "function": {"name": "read_file"}}],
        },
        {"kind": "planning.result", "turn_id": T1, "operation": "view", "result": {"stage": "intake"}},
        {"kind": "planning.observation", "turn_id": T1, "observation": {"messages": []}},
        {
            "kind": "runner.event",
            "turn_id": T1,
            "event_type": "ToolEvent",
            "event": {"phase": "start", "tool_call_id": "c1", "name": "write_file", "arguments": {"path": "x.md"}},
        },
        {
            "kind": "runner.event",
            "turn_id": T1,
            "event_type": "ToolEvent",
            "event": {
                "phase": "complete",
                "tool_call_id": "c1",
                "ok": False,
                "result_preview": "Error: blocked by the handover gate: no ticket has been filed yet",
            },
        },
        {"kind": "loop.control", "turn_id": T1, "rollbacks": 1, "rollbacks_refused": 0},
        {
            "kind": "dag.progress",
            "turn_id": T1,
            "name": "dag_run_started",
            "payload": {"run_id": "dag-1", "nodes": [{"id": "n1", "subagent": "Raven-Research"}]},
        },
        {
            "kind": "dag.progress",
            "turn_id": T1,
            "name": "dag_run_completed",
            "payload": {
                "run_id": "dag-1",
                "manifest": {
                    "files": [
                        {
                            "node": "n1",
                            "subagent": "Raven-Research",
                            "status": "completed",
                            "started_at": 1000,
                            "ended_at": 13500,
                            "output_file": f"/somewhere/else/home/{out_file.relative_to(run / 'home').as_posix()}",
                        }
                    ]
                },
            },
        },
        text_row(T1, f"Hello, I am the AI consultant. So {QUOTE}. Bearer abc.def.ghi"),
    ]


def make_run(tmp_path: Path, **overrides) -> tuple[Path, Path]:
    """A finished two-round run with an onboarding curation, one curation after round 1 and a handover in the review."""
    scenario = scenario_dir(tmp_path)
    run = tmp_path / "runs" / "staged-1"
    deck = write(run / "deliverables" / T1 / "deck.pptx", b"pptx-bytes")
    write(run / "deliverables" / T1 / "deck.pptx.thumbs" / "page-01.png", b"png-1")
    write(run / "deliverables" / T1 / "notes.md", f"notes with {KEY}")
    write(run / "gen1" / "config.json", {"providers": {"openrouter": {"apiKey": KEY}}})
    write(
        run / "gen1" / "observations.jsonl",
        json.dumps({"kind": "runtime.bound", "turn_id": None, "package": "/x/_curator_" + "c" * 20}) + "\n",
    )
    write(run / "home" / "uploads" / "sop" / "sop.md", "sop")
    write(
        run / "settings.json",
        {
            "chain": "staged",
            "scenario": "scenario",
            "deliver": "dialog",
            "disclose": "staged",
            "teach": "documents",
            "curator": "improve",
            "targets": "all",
            "judge": "round",
            "rounds": 2,
            "turns": 4,
            "repeats": 1,
            "cards": ["family"],
            "models": {"employee": "m-e", "curator": "m-c", "analyst": "m-a", "simulation": "m-s", "subagents": None},
            "baseline": {"skills/x/SKILL.md": "ab" * 32},
            "started": 1790000000.0,
        },
    )
    write(
        run / "curation" / "c0.json",
        {
            "feedback": {"signals": []},
            "generated": {
                "candidate": {
                    "plan": {
                        "understanding": f"Plain employee; the owner gave an SOP. {JINA}",
                        "design": "Put the rules in the prompt.",
                        "changes": [
                            {
                                "target": "memory.prompt",
                                "reason": "Quote format per S4.1 to S4.3 and identity per G1.",
                                "expected": "Rules visible.",
                            }
                        ],
                    },
                    "artifact": {
                        "values": {"memory.prompt": {"agent.md": "# Rules\nQuote from the list.\n"}},
                        "files": {},
                        "remove": [],
                    },
                },
                "validation": {"errors": [], "observations": [{"kind": "runtime.bound", "targets": ["memory.prompt"]}]},
            },
            "active_artifact_id": "A" * 64,
        },
    )
    write(
        run / "curation" / "c1.json",
        {
            "feedback": {"decision": "curate", "reason": "Quotes drift."},
            "generated": {
                "candidate": {
                    "plan": {
                        "understanding": "Quotes and questions keep failing.",
                        "design": "A reviewer gates quotes; planning counts questions.",
                        "changes": [
                            {
                                "target": "action.review",
                                "reason": "Gate every quote before it is sent.",
                                "expected": "Resamples.",
                            },
                            {
                                "target": "planning.strategy",
                                "reason": "Count questions for question-limit.",
                                "expected": "Fewer.",
                            },
                        ],
                    },
                    "artifact": {
                        "values": {"action.review": "gate:create", "planning.strategy": {"factory": "gate:plan"}},
                        "files": {"gate.py": "def create():\n    return None\n"},
                    },
                },
                "validation": {"errors": [], "observations": []},
            },
            "active_artifact_id": "B" * 64,
        },
    )
    write(run / "analysis" / "a1.json", {"materials": {"skills": ["skill.builtin/weather", "skill.workspace/sop"]}})
    write(
        run / "analysis" / "a2.json", {"materials": {"skills": ["skill.workspace/sop", "skill.workspace/price-list"]}}
    )
    price_head = "# Price list\n\nComfort 3900."
    requirements = [
        {
            "behavior": "Ask at most two questions (`question-limit`).",
            "observed": "Four questions at once.",
            "evidence": ["the family drill"],
            "expectation": "new",
            "acceptance": "Two at most.",
            "strength": "must_hold",
            "recurrence": 0,
        },
        {
            "behavior": "State only listed prices.",
            "observed": "An invented total.",
            "evidence": [f'turn {T1[:12]} assistant: "{QUOTE}"'],
            "expectation": "new",
            "acceptance": "Every figure is on the list.",
            "strength": "must_hold",
            "recurrence": 0,
        },
        {
            "behavior": "Sound warm.",
            "observed": "Curt replies.",
            "evidence": ["overall tone"],
            "expectation": "new",
            "acceptance": "Warm.",
            "strength": "should",
            "recurrence": 0,
        },
    ]
    iteration = {
        "task_id": "task-1",
        "task": "Sell short trips.",
        "status": "finished",
        "stop": "rounds exhausted",
        "curator": "improve",
        "opening": [
            {
                "source": "agency",
                "text": "Here are my files:\n- uploads/sop/sop.md",
                "attachments": ["uploads/sop/sop.md"],
            }
        ],
        "initial_curation": ["c0.json"],
        "rounds": [
            {
                "sessions": {
                    "family": [
                        {
                            "user": f"How much for two? my key is {KEY}",
                            "execution": {
                                "turn_id": T1,
                                "events": [],
                                "records": round_one_rows(run),
                                "outcome": {},
                                "artifact_id": "A" * 64,
                                "deliverables": [str(Path("/old/place/deliverables") / T1 / "deck.pptx")],
                            },
                        }
                    ]
                },
                "signals": [
                    {
                        "source": "agency",
                        "text": f"Quotes were wrong.\n\n---\n\n{price_head}",
                        "items": [
                            {"id": "ai-identity", "result": "pass", "session": "family"},
                            {
                                "id": "question-limit",
                                "result": "fail",
                                "session": "family",
                                "actual": "asked four questions",
                            },
                            {
                                "id": "quote-correct",
                                "result": "fail",
                                "session": "family",
                                "actual": f'said "{QUOTE}"',
                                "note": HEX40,
                            },
                        ],
                        "metrics": {},
                        "satisfied": False,
                    }
                ],
                "feedback": {
                    "decision": "curate",
                    "reason": "Quotes drift.",
                    "requirements": requirements,
                    "filtered": [],
                    "task_updates": [],
                },
                "curated": True,
                "analysis": ["a1.json"],
                "curation": ["c1.json"],
                "unexpected": {"extra": True},
            },
            {
                "sessions": {
                    "family": [
                        {
                            "user": "How much for two?",
                            "execution": {
                                "turn_id": T2,
                                "records": [
                                    {
                                        "kind": "participant.result",
                                        "turn_id": T2,
                                        "target": "action.review",
                                        "result": {"verdict": "resample", "reason": "price not on the list"},
                                    },
                                    {
                                        "kind": "participant.result",
                                        "turn_id": T2,
                                        "target": "action.review",
                                        "result": {"verdict": "accept"},
                                    },
                                    text_row(T2, "It is 4100 per person."),
                                ],
                                "artifact_id": "B" * 64,
                                "deliverables": [],
                            },
                        }
                    ]
                },
                "signals": [
                    {
                        "source": "agency",
                        "text": "Better quotes.",
                        "items": [
                            {"id": "ai-identity", "result": "pass", "session": "family"},
                            {"id": "question-limit", "result": "fail", "session": "family"},
                            {"id": "quote-correct", "result": "pass", "session": "family"},
                        ],
                    }
                ],
                "feedback": {"decision": "continue", "reason": "Improving."},
                "analysis": ["a2.json"],
                "curation": [],
            },
        ],
        **overrides,
    }
    write(run / "iteration" / "r1.json", iteration)
    assert deck.is_file()
    return run, scenario


def by_id(entries, key="criterion"):
    return {entry[key]: entry for entry in entries}


def test_a_run_with_onboarding_and_two_rounds_reads_as_one_record(tmp_path):
    run, scenario = make_run(tmp_path)
    data = build_record(run, scenario)
    assert data["schema"] == 1
    assert data["run"]["name"] == "staged-1" and data["run"]["record_id"] == "r1" and data["run"]["chain"] == "staged"
    assert "baseline" not in data["run"]["settings"] and data["inputs"]["baseline"] == {"skills/x/SKILL.md": "ab" * 32}
    assert data["run"]["started"] == "2026-09-21T14:13:20Z"
    assert data["run"]["revisions"] == ["c" * 20, "A" * 64, "B" * 64]
    assert "--chain staged" in data["run"]["reproduce"] and "--cards family" in data["run"]["reproduce"]
    assert "--analysis owner" in data["run"]["reproduce"] and "--judge" not in data["run"]["reproduce"]
    assert [(c["round"], c["outcome"]) for c in data["curations"]] == [(0, "installed"), (1, "installed")]
    onboarding, after = data["curations"]
    assert [(d["target"], d["path"], d["change"], d["lines_added"]) for d in onboarding["artifact_diff"]] == [
        ("memory.prompt", "agent.md", "added", 2)
    ]
    assert {(d["target"], d["path"]) for d in after["artifact_diff"]} == {
        ("action.review", "(value)"),
        ("planning.strategy", "factory"),
        ("files", "gate.py"),
    }
    assert by_id(after["changes"], "target")["action.review"]["paths"] == ["(value)", "gate.py"]
    first, second = data["rounds"]
    assert (first["revision"], second["revision"], first["curation"], second["curation"]) == (
        "A" * 64,
        "B" * 64,
        "c1",
        None,
    )
    exchange = first["drills"][0]["exchanges"][0]
    assert exchange["delivered"] == [
        {
            "name": "deck.pptx",
            "path": f"deliverables/{T1}/deck.pptx",
            "sha256": hashlib.sha256(b"pptx-bytes").hexdigest(),
            "pages": [f"deliverables/{T1}/deck.pptx.thumbs/page-01.png"],
        }
    ]
    node = exchange["playbook_runs"][0]["nodes"][0]
    assert (node["subagent"], node["status"], node["seconds"], node["summary"]) == (
        "Raven-Research",
        "completed",
        12.5,
        "Found 3 trains.",
    )
    decisions = {(row["kind"], row["target"], row["decision"]): row for row in first["mechanism_evidence"]}
    assert decisions[("tool.refused", "tool:write_file", "refused")]["intervention"]
    assert not decisions[("loop.control", "loop", "rollback")]["intervention"]
    assert ("planning.result", "planning.strategy", "view") in decisions
    assert "1 entries: read_file" in decisions[("participant.result", "capability.select_tools", "result")]["summary"]
    assert not any(row["kind"] == "planning.observation" for row in first["mechanism_evidence"])
    materials = by_id(data["inputs"]["materials"], "name")
    assert (materials["sop"]["given"], materials["price-list"]["given"], materials["price-list"]["round"]) == (
        "opening",
        "handed_over",
        1,
    )
    assert materials["brand"]["given"] == "withheld" and set(materials["brand"]["files"]) == {"template.pptx"}
    assert (
        by_id(data["inputs"]["cards"], "name")["family"]["played"]
        and not by_id(data["inputs"]["cards"], "name")["student"]["played"]
    )


def test_the_reproduce_command_is_the_stored_command_line_with_the_readers_own_paths():
    argv = ["--config", "<config.json with your own keys>", "--home", "/h", "--state-dir=/runs/r", "--workdir", "/w"]
    settings = {"argv": [*argv, "--partition", '[["sop"], ["price-list"]]', "--timeout", "9000", "--seed=3"]}
    command = record._command({**settings, "seed": 3, "analysis": "analyst"})
    assert shlex.split(command) == [
        *record.COMMAND,
        "--config",
        "<config.json with your own keys>",
        "--home",
        "<a home matching inputs.baseline>",
        "--state-dir=<a new empty directory>",
        "--workdir",
        "<a new empty directory>",
        "--partition",
        '[["sop"], ["price-list"]]',
        "--timeout",
        "9000",
        "--seed=3",
        "--analysis",
        "analyst",
    ]
    drawn = shlex.split(record._command({"argv": ["--chain", "documents"], "seed": 5, "analysis": "owner"}))
    assert drawn[-6:] == ["--chain", "documents", "--seed", "5", "--analysis", "owner"]


def test_an_older_runs_command_is_rebuilt_from_its_settings_as_it_ran():
    old = {
        "chain": "partition",
        "disclose": [["sop"], ["price-list"]],
        "scenario": "travel_agency",
        "rounds": 3,
        "curator": "improve",
        "judge": "round",
        "models": {
            "employee": "e",
            "curator": "c",
            "analyst": "c",
            "simulation": "c",
            "traveller": "e",
            "subagents": "e",
        },
        "efforts": {"curator": "high", "traveller": "low"},
        "curator_budget": {"calls": 96, "queries": None},
        "seed": 11,
    }
    argv = shlex.split(record._command(old))
    assert "--chain" not in argv and argv[argv.index("--partition") + 1] == json.dumps([["sop"], ["price-list"]])
    assert argv[argv.index("--analysis") + 1] == "owner" and "--judge" not in argv
    assert [flag for flag in argv if flag.endswith("-model")] == [
        "--curator-model",
        "--traveller-model",
        "--subagent-model",
    ]
    assert argv[argv.index("--curator-calls") + 1] == "96" and "--curator-queries" not in argv
    assert argv[argv.index("--curator-effort") + 1] == "high" and argv[argv.index("--seed") + 1] == "11"
    assert "--analysis analyst" in record._command({**old, "analysis": "analyst"})
    assert "--chain documents" in record._command({**old, "chain": "documents", "disclose": "all"})


def test_requirements_link_explicitly_by_id_by_cited_failing_session_or_not_at_all(tmp_path):
    run, scenario = make_run(tmp_path)
    requirements = build_record(run, scenario)["rounds"][0]["analysis"]["requirements"]
    assert [(r["criteria"], r["link"]) for r in requirements] == [
        (["question-limit"], "explicit"),
        (["quote-correct"], "inferred"),
        ([], "none"),
    ]
    items = [
        {"criterion": "quote-correct", "result": "fail", "session": "premium", "actual": "x"},
        {"criterion": "question-limit", "result": "fail", "session": "premium", "actual": "y"},
    ]
    turns = {"premium": ["c3" * 16]}
    both = link(
        {"behavior": "b", "evidence": ["In the premium conversation it rushed."]}, ["quote-correct"], items, turns
    )
    assert both == (["quote-correct", "question-limit"], "inferred")
    assert link({"behavior": "Recommend the Premium package only after intake."}, [], items, turns) == ([], "none")
    assert link({"behavior": "b", "evidence": [f"turn {'c3' * 4} said so"]}, [], items, turns)[1] == "inferred"


def test_the_ledger_shows_a_failure_held_after_a_sedimented_change(tmp_path):
    run, scenario = make_run(tmp_path)
    entries = by_id(build_record(run, scenario)["ledger"])
    quote = entries["quote-correct"]
    assert quote["severity"] == "red_line" and quote["rule"].startswith("S4.2")
    assert quote["status"] == "held_since_round_2"
    assert [(m["round"], m["result"], m["fails"], m["passes"]) for m in quote["timeline"]] == [
        (0, "unknown", 0, 0),
        (1, "fail", 1, 0),
        (2, "pass", 0, 1),
    ]
    assert [(c["target"], c["link"]) for c in quote["timeline"][0]["changes"]] == [("memory.prompt", "marker")]
    assert quote["timeline"][1]["requirements"] == [1]
    assert [(c["target"], c["link"]) for c in quote["timeline"][1]["changes"]] == [("action.review", "round")]
    assert quote["timeline"][2]["evidence"] == {"participant.result/accept": 1, "participant.result/resample": 1}
    assert [(s["round"], s["target"], s["facet"]) for s in quote["sedimented_in"]] == [
        (0, "memory.prompt", "memory"),
        (1, "action.review", "action"),
    ]
    questions = entries["question-limit"]
    assert questions["status"] == "still_failing"
    assert [(c["target"], c["link"]) for c in questions["timeline"][1]["changes"]] == [
        ("action.review", "round"),
        ("planning.strategy", "named"),
    ]
    assert entries["ai-identity"]["status"] == "never_failed"
    assert [c["target"] for c in entries["ai-identity"]["timeline"][0]["changes"]] == ["memory.prompt"]
    assert entries["deck-structure"]["status"] == "not_exercised" and entries["deck-structure"]["sedimented_in"] == []


def test_status_follows_the_final_run_of_passing_rounds():
    def rounds_with(*results):
        return [
            {
                "number": number,
                "evaluation": {"items": [{"criterion": "c", "result": result, "session": None} for result in results_]},
                "analysis": {"requirements": []},
                "mechanism_evidence": [],
            }
            for number, results_ in enumerate(results, start=1)
        ]

    def status(*results):
        return ledger([{"id": "c", "check": ""}], rounds_with(*results), [])[0]["status"]

    assert status(["fail"], ["pass", "fail"], ["pass"], ["pass"]) == "held_since_round_3"
    assert status(["pass"], ["fail"]) == "still_failing"
    assert status(["pass"], ["pass", "fail"]) == "still_failing"
    assert status(["unknown"], []) == "not_exercised"
    assert status(["pass"], ["unknown"]) == "never_failed"
    assert (
        ledger([{"id": "c", "check": ""}], rounds_with(["fail"], ["pass", "fail"]), [])[0]["timeline"][1]["result"]
        == "mixed"
    )


def test_facets_and_section_markers():
    assert [
        facet(target)
        for target in (
            "memory.prompt",
            "prompt.resources",
            "planning.playbooks",
            "capability.tools",
            "capability.select_tools",
            "action.review",
            "files",
            "hosting",
        )
    ] == ["memory", "memory", "planning", "capability", "capability", "action", "other", "other"]
    assert markers("S4.1 to S4.3, G4/G5 and B3") == {"S4.1", "S4.2", "S4.3", "G4", "G5", "B3"}
    assert markers("rules G1" + chr(0x2013) + "G3 and stages S1-S2") == {"G1", "G2", "G3", "S1", "S2"}
    assert markers("HL-SOP-01 and AWS S3") == {"S3"}


def test_scopes_children_and_unknown_fields_are_tolerated(tmp_path):
    run, scenario = make_run(tmp_path)
    child = {
        "scope": {"node": "research", "harness": "Raven-Research"},
        "surprise": [1, 2],
        "generated": {
            "candidate": {
                "plan": {"understanding": "u", "changes": [{"target": "memory.prompt", "reason": "r"}]},
                "artifact": {"values": {"memory.prompt": {"AGENTS.md": "x"}}},
            }
        },
    }
    data = json.loads((run / "curation" / "c1.json").read_text())
    write(run / "curation" / "c1.json", {**data, "scope": "root", "children": [child], "future": {"a": 1}})
    iteration = json.loads((run / "iteration" / "r1.json").read_text())
    iteration["rounds"][1]["sessions"]["family"].append({"user": "no execution here"})
    iteration["rounds"].append({"odd": True})
    write(run / "iteration" / "r1.json", iteration)
    built = build_record(run, scenario)
    assert [(c["round"], c["scope"]) for c in built["curations"]] == [(0, "root"), (1, "root"), (1, "research")]
    assert built["curations"][2]["artifact_diff"][0]["path"] == "AGENTS.md"
    assert built["rounds"][2]["drills"] == [] and built["rounds"][2]["analysis"]["decision"] is None
    minimal = tmp_path / "minimal"
    write(minimal / "iteration" / "r.json", {"status": "running", "rounds": [{}]})
    out = export_record(minimal, minimal / "record", scenario)
    assert json.loads((out / "record.json").read_text())["run"]["steps"][-1] == {
        "step": "end",
        "state": "running",
        "note": None,
    }


def test_a_composed_curations_child_changes_and_child_process_rows_carry_the_child_scope(tmp_path):
    run, scenario = make_run(tmp_path)
    data = json.loads((run / "curation" / "c1.json").read_text())
    child = {
        "candidate": {
            "plan": {
                "understanding": "Decks repeat quotes.",
                "changes": [{"target": "action.review", "reason": "Check deck figures against S4.2 before delivery."}],
            },
            "artifact": {"values": {"action.review": "deck_gate:create"}, "files": {"deck_gate.py": "x = 1\n"}},
        },
        "validation": {"errors": [], "observations": []},
        "trace": [],
    }
    write(run / "curation" / "c1.json", {**data, "child_changes": {"Raven-PPT": child}, "withdrawn_children": []})
    iteration = json.loads((run / "iteration" / "r1.json").read_text())
    records = iteration["rounds"][1]["sessions"]["family"][0]["execution"]["records"]
    records.append(
        {
            "kind": "child.execution",
            "harness": "Raven-PPT",
            "revision": "c" * 16,
            "records": [{"kind": "participant.result", "target": "action.review", "result": {"verdict": "end"}}],
        }
    )
    write(run / "iteration" / "r1.json", iteration)
    built = build_record(run, scenario)
    ppt = next(c for c in built["curations"] if c["scope"] == "Raven-PPT")
    assert (ppt["id"], ppt["round"], ppt["outcome"]) == ("c1.Raven-PPT", 1, "installed")
    assert [d["path"] for d in ppt["artifact_diff"]] == ["(value)", "deck_gate.py"]
    rows = {(r["scope"], r["target"], r["decision"]) for r in built["rounds"][1]["mechanism_evidence"]}
    assert {("root", "action.review", "resample"), ("Raven-PPT", "action.review", "end")} <= rows
    quote = by_id(built["ledger"])["quote-correct"]
    assert (1, "Raven-PPT", "action.review") in {(s["round"], s["scope"], s["target"]) for s in quote["sedimented_in"]}
    assert quote["timeline"][2]["evidence"]["participant.result/end"] == 1
    assert built["value"]["run"]["name"] == "staged-1" and built["value"]["verdict"] in ("partial", "none")
    out = export_record(run, run / "record", scenario)
    text = (out / "transcript.md").read_text()
    assert "Raven-PPT: action.review" in text and "Value verdict" in text


def test_secret_looking_strings_never_leave_the_record(tmp_path):
    run, scenario = make_run(tmp_path)
    assert redact({"a": [f"x {KEY} y", f"Bearer {HEX40}"], JINA: 1}) == {
        "a": ["x <redacted> y", "<redacted>"],
        "<redacted>": 1,
    }
    out = export_record(run, tmp_path / "bundle", scenario)
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".md"):
            text = path.read_text()
            for secret in (KEY, JINA, HEX40, "Bearer abc"):
                assert secret not in text, (path, secret)
    manifest = json.loads((out / "manifest.json").read_text())
    assert by_id(manifest["files"], "path")[f"assets/deliverables/{T1}/notes.md"]["redacted"] is True


def test_export_writes_a_manifest_with_true_hashes_and_never_copies_configuration(tmp_path):
    run, scenario = make_run(tmp_path)
    out = export_record(run, run / "record", scenario)
    assert {"record.json", "ledger.json", "transcript.md", "manifest.json"} <= {path.name for path in out.iterdir()}
    assert not list(out.rglob("config.json")) and not list(out.rglob("credentials"))
    manifest = json.loads((out / "manifest.json").read_text())
    listed = {entry["path"]: entry for entry in manifest["files"]}
    on_disk = {
        path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file() and path.name != "manifest.json"
    }
    assert set(listed) == on_disk
    for relative, entry in listed.items():
        content = (out / relative).read_bytes()
        assert entry["sha256"] == hashlib.sha256(content).hexdigest() and entry["size"] == len(content)
        assert entry["modified"].endswith("Z")
    saved = json.loads((out / "record.json").read_text())
    delivered = saved["rounds"][0]["drills"][0]["exchanges"][0]["delivered"][0]
    assert (
        delivered["path"] == f"assets/deliverables/{T1}/deck.pptx"
        and (out / delivered["path"]).read_bytes() == b"pptx-bytes"
    )
    assert (out / delivered["pages"][0]).read_bytes() == b"png-1"
    assert [entry["path"] for entry in saved["unlinked_deliverables"]] == [f"assets/deliverables/{T1}/notes.md"]
    assert json.loads((out / "ledger.json").read_text()) == saved["ledger"]
    assert saved["inputs"]["materials"][0]["copy"].startswith("assets/scenario/materials/")
    transcript = (out / "transcript.md").read_text()
    for heading in (
        "## Inputs",
        "## Onboarding curation",
        "## Round 1 on v1",
        "### Deliverable pages",
        "## Knowledge-sedimentation ledger",
        "## Cost and reproduction",
    ):
        assert heading in transcript
    assert f"![deck.pptx page 1](assets/deliverables/{T1}/deck.pptx.thumbs/page-01.png)" in transcript
    assert export_record(run, run / "record", scenario) == out
    write(tmp_path / "busy" / "keep.txt", "mine")
    with pytest.raises(ValueError, match="not empty"):
        export_record(run, tmp_path / "busy", scenario)


def test_an_unfinished_trial_becomes_a_partial_round_from_the_observations(tmp_path):
    run, scenario = make_run(tmp_path, rounds=[], status="error", error="stopped")
    turn, follow = "d4" * 16, "e5" * 16
    context = "[Runtime Context - metadata only]\nChat ID: student:0123456789abcdef\n\n"
    rows = [
        {"kind": "runtime.bound", "turn_id": None, "package": "/x/_curator_" + "A" * 20},
        {
            "kind": "provider.request",
            "turn_id": turn,
            "parameters": {"messages": [{"role": "user", "content": "internal context call"}]},
        },
        {
            "kind": "provider.request",
            "turn_id": turn,
            "parameters": {"messages": [{"role": "user", "content": context + "Two nights in June?"}]},
        },
        text_row(turn, "Happy to help."),
        {
            "kind": "provider.request",
            "turn_id": follow,
            "parameters": {
                "messages": [
                    {
                        "role": "user",
                        "content": context + "[BEGIN UNTRUSTED subagent #1]\ndone\n[END UNTRUSTED subagent #1]",
                    }
                ]
            },
        },
        text_row(follow, "Your deck is ready."),
    ]
    write(run / "gen2" / "observations.jsonl", "\n".join(json.dumps(row) for row in rows) + "\n")
    write(run / "deliverables" / follow / "deck.pptx", b"late-deck")
    data = build_record(run, scenario)
    (partial,) = data["rounds"]
    assert partial["partial"] and partial["revision"] == "A" * 64
    exchanges = partial["drills"][0]["exchanges"]
    assert partial["drills"][0]["session"] == "student"
    assert [(e["customer"], e["assistant"], e["follow_up"]) for e in exchanges] == [
        ("Two nights in June?", "Happy to help.", False),
        ("", "Your deck is ready.", True),
    ]
    assert exchanges[1]["delivered"][0]["path"] == f"deliverables/{follow}/deck.pptx"
    assert [(s["step"], s["state"]) for s in data["run"]["steps"]][-2:] == [("trial", "unfinished"), ("end", "error")]


def test_a_drill_played_on_a_replica_is_read_from_the_replicas_folder(tmp_path):
    run, scenario = make_run(tmp_path, rounds=[], status="error", error="stopped")
    turn = "f6" * 16
    context = "[Runtime Context - metadata only]\nChat ID: student:0123456789abcdef\n\n"
    rows = [
        {"kind": "runtime.bound", "turn_id": None, "package": "/x/_curator_" + "A" * 20},
        {
            "kind": "provider.request",
            "turn_id": turn,
            "parameters": {"messages": [{"role": "user", "content": context + "Five nights?"}]},
        },
        text_row(turn, "Here is your deck."),
    ]
    replica = run / "replicas" / "1-student"
    write(replica / "gen1" / "observations.jsonl", "\n".join(json.dumps(row) for row in rows) + "\n")
    write(replica / "deliverables" / turn / "deck.pptx", b"replica-deck")
    (partial,) = build_record(run, scenario)["rounds"]
    (drill,) = partial["drills"]
    assert drill["session"] == "student" and drill["exchanges"][0]["assistant"] == "Here is your deck."
    assert drill["exchanges"][0]["delivered"][0]["path"] == f"replicas/1-student/deliverables/{turn}/deck.pptx"
    moved = write(replica / "home" / "skills" / "sop" / "SKILL.md", "SOP")
    assert record._local("/elsewhere/replicas/1-student/home/skills/sop/SKILL.md", run) == moved.resolve()


def test_cost_prefers_the_suite_summary_then_audit_spans(tmp_path):
    run, scenario = make_run(tmp_path)
    assert build_record(run, scenario)["cost"] is None
    span = json.dumps({"name": "llm.call", "attributes": {"session.id": None, "llm.usage.cost_total": "0.5"}})
    employee = json.dumps(
        {"name": "llm.call", "attributes": {"session.id": "curator:family", "llm.usage.cost_total": "0.25"}}
    )
    write(run / "traces" / "audit-spans.log", f"{span}\n{employee}\nnot json\n")
    child = json.dumps({"name": "llm.call", "attributes": {"session.id": "x", "llm.usage.cost_total": "1.0"}})
    write(run / "replicas" / "1-family" / "children" / "c1" / "native" / "traces" / "logs" / "audit-spans.log", child)
    local = build_record(run, scenario)["cost"]
    assert (local["total"], local["by_part"]) == (1.75, {"simulation": 0.5, "employee": 0.25, "subagents": 1.0})
    write(tmp_path / "state" / run.name / "traces" / "logs" / "audit-spans.log", span + "\n")
    assert build_record(run, scenario, state_root=tmp_path / "state")["cost"]["total"] == 1.5
    write(
        run / "suite-summary.json",
        {"spend": {"simulation": 1.0, "employee": 2.0, "subagents": 3.0, "total": 6.0}, "exit": 0},
    )
    summary = build_record(run, scenario, state_root=tmp_path / "state")
    assert summary["cost"] == {
        "total": 6.0,
        "by_part": {"simulation": 1.0, "employee": 2.0, "subagents": 3.0},
        "source": "suite-summary.json",
    }
    assert summary["run"]["suite"] == {"exit": 0}


def test_a_scenario_may_relabel_the_transcript_and_the_cli_exports(tmp_path, capsys):
    run, scenario = make_run(tmp_path)
    write(scenario / "record.md", "# Labels\n\n- ledger: Ledger of what stuck\n- round: Lap {number}\n")
    out = record.main([str(run), "--out", str(tmp_path / "cli"), "--scenario", str(scenario)])
    transcript = (out / "transcript.md").read_text()
    assert "## Ledger of what stuck" in transcript and "## Lap 2 on v2" in transcript
    assert capsys.readouterr().out.strip() == str(out)


def test_each_drill_carries_the_card_it_played_and_the_figures_computed_for_its_round(tmp_path):
    run, scenario = make_run(tmp_path)
    session = build_record(run, scenario)["rounds"][0]["drills"][0]["session"]
    figures = {"prices": {"persons": 4, "by_product": {"value": {"party_total": 4000}}}}
    rows = [
        {"evaluation": 1, "drill": session, "card": "Card one.", "trip": {"start": "10-22"}, "references": figures},
        {"evaluation": 2, "drill": session, "card": "Card two.", "trip": {"start": "10-25"}, "references": {}},
    ]
    write(run / "references.jsonl", "\n".join(json.dumps(row) for row in rows) + "\n")
    first, second = build_record(run, scenario)["rounds"]
    assert first["drills"][0]["drawn"] == {"start": "10-22"} and first["drills"][0]["references"] == figures
    assert second["drills"][0]["card_text"] == "Card two." and second["drills"][0]["references"] == {}
    text = "\n".join(record._drills_md(first, record.LABELS))
    assert "> Card one." in text and '"party_total": 4000' in text


def test_host_guards_and_argument_errors_are_not_mechanism_refusals():
    from experimental.analyst.activity import classify

    def refusal(preview):
        tools = {}
        classify(
            {
                "kind": "runner.event",
                "event_type": "ToolEvent",
                "event": {"phase": "start", "tool_call_id": "c", "name": "read_file"},
            },
            tools,
        )
        row = {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {"phase": "complete", "tool_call_id": "c", "ok": False, "result_preview": preview},
        }
        return classify(row, tools)

    assert refusal("Error: blocked by the quote gate: price not in the list")[2] == "refused"
    for preview in (
        "Error: This call requires user approval",
        "Error: Path /x is outside allowed directories /w",
        "Error: path outside working dir",
        "1 validation error: Extra inputs are not permitted",
        "Error: ask_user not configured (no question broker)",
    ):
        assert refusal(preview) is None


def test_the_owners_scorecard_is_read_from_the_analysis_record_when_its_signal_carries_only_words():
    from experimental.simulation.record import _analysis, _evaluation

    scorecard = {
        "source": "agency",
        "text": "It guessed a price.",
        "items": [{"id": "quote-sheet-correct", "result": "fail", "session": "student", "actual": "300"}],
        "metrics": {},
        "satisfied": False,
    }
    item = {
        "signals": [{"source": "agency", "text": "It guessed a price.", "items": [], "satisfied": False}],
        "analysis": [
            {"source": "agency", "scorecard": scorecard, "waiting_on_material": {"deck-aesthetics": "brand"}},
            {"materials": {}, "feedback": {"decision": "continue", "reason": "r"}},
        ],
        "feedback": {"decision": "continue", "reason": "r", "requirements": []},
    }
    evaluation = _evaluation(item, {"quote-sheet-correct": "standard"})
    assert evaluation["remark"] == "It guessed a price."
    assert [(row["criterion"], row["result"], row["session"]) for row in evaluation["items"]] == [
        ("quote-sheet-correct", "fail", "student")
    ]
    assert _analysis(item, ["quote-sheet-correct"], evaluation["items"], {})["waiting_on_material"] == [
        "deck-aesthetics"
    ]
