"""A run's tool calls are scanned for paths outside its bounds; the shapes come from recorded runs."""

import json

from experimental.simulation.isolation import EXPERIMENTAL, scan


def tool_event(name, arguments, call_id):
    return {
        "kind": "runner.event",
        "event_type": "ToolEvent",
        "event": {"phase": "start", "name": name, "arguments": arguments, "tool_call_id": call_id},
    }


def request(*calls):
    history = [
        {"role": "assistant", "tool_calls": [{"id": f"c{i}", "function": {"name": n, "arguments": json.dumps(a)}}]}
        for i, (n, a) in enumerate(calls)
    ]
    return {"kind": "provider.request", "parameters": {"messages": [{"role": "system", "content": "x"}, *history]}}


def log(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_a_drill_may_use_its_own_replica_and_the_material_paths_relative_to_its_workdir(tmp_path):
    records = tmp_path / "runs" / "this-run"
    own = records / "replicas" / "1-family"
    log(
        own / "gen" / "observations.jsonl",
        tool_event("read_file", {"path": "uploads/price-list/price-list.md"}, "a"),
        tool_event("read_file", {"path": str(own / "workdir" / "uploads" / "service-sop" / "service-sop.md")}, "b"),
        tool_event("list_dir", {"path": str(own / "workdir")}, "c"),
        tool_event("read_file", {"path": "/usr/share/fonts/noto/NotoSerifSC.otf"}, "d"),
    )
    assert scan(records) == {"valid": True, "breaches": 0, "attempts": 0, "logs": 1, "calls": 4, "findings": []}


def test_the_recorded_breaches_are_each_named(tmp_path):
    runs, scratch = tmp_path / "runs", tmp_path / "scratch"
    records = runs / "this-run"
    own, other = records / "replicas" / "2-family", records / "replicas" / "2-student"
    other.mkdir(parents=True)
    child = {
        "kind": "child.execution",
        "harness": "Raven-PPT",
        "records": [
            request(
                # A child that could not find the template searches the whole machine.
                ("exec", {"command": 'find / -name "plan-deck-template.pptx" 2>/dev/null | head -5'}),
                (
                    "read_file",
                    {"path": str(scratch / "export-test" / "assets" / "scenario" / "personas" / "family.md")},
                ),
            )
        ],
    }
    log(
        own / "gen" / "observations.jsonl",
        child,
        tool_event("read_file", {"path": str(records / "curation" / "c1.json")}, "a"),
        tool_event("list_dir", {"path": str(other / "workdir")}, "b"),
        tool_event("read_file", {"path": str(EXPERIMENTAL / "simulation" / "BRIEF.md")}, "c"),
        tool_event("read_file", {"path": str(runs / "older-run" / "references.jsonl")}, "d"),
        tool_event("find", {"path": str(records), "pattern": "**/*.ppt*"}, "e"),
        tool_event("read_file", {"path": "../../../curation/c1.json"}, "f"),
    )
    result = scan(records, forbid=[scratch])
    kinds = {(finding["kind"], finding["tool"]) for finding in result["findings"]}
    assert not result["valid"]
    assert kinds == {
        ("broad_search", "exec"),
        ("forbidden", "read_file"),
        ("loop_records", "read_file"),
        ("other_drill", "list_dir"),
        ("simulation", "read_file"),
        ("other_run", "read_file"),
        ("broad_search", "find"),
    }
    assert any(finding["path"] == "/" for finding in result["findings"])


def test_each_tool_call_counts_once_across_repeated_requests(tmp_path):
    records = tmp_path / "run"
    repeated = request(("exec", {"command": "find / -name x"}))
    log(records / "children" / "abc" / "observations.jsonl", repeated, repeated)
    result = scan(records)
    assert result["breaches"] == 1 and result["findings"][0]["log"] == "children/abc/observations.jsonl"


def test_relative_paths_and_symlinks_are_resolved_and_a_refused_call_is_only_an_attempt(tmp_path):
    records = tmp_path / "run"
    own = records / "replicas" / "1-family"
    (own / "workdir").mkdir(parents=True)
    (records / "analysis").mkdir()
    (records / "analysis" / "a1.json").write_text("{}")
    (own / "workdir" / "notes").symlink_to(records / "analysis")
    log(
        own / "gen" / "observations.jsonl",
        tool_event("read_file", {"path": "../../../curation/c1.json"}, "a"),
        tool_event("read_file", {"path": "notes/a1.json"}, "b"),
    )
    result = scan(records)
    assert result["valid"] is False and {finding["kind"] for finding in result["findings"]} == {"loop_records"}
    assert any(finding["call"].startswith('{"path": "notes') for finding in result["findings"])
    refused = records.parent / "refused"
    log(
        refused / "replicas" / "1-family" / "gen" / "observations.jsonl",
        tool_event("read_file", {"path": str(refused / "curation" / "c1.json")}, "c"),
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "complete",
                "tool_call_id": "c",
                "ok": False,
                "result_preview": "Error: Path /x is outside allowed directories /y",
            },
        },
    )
    kept = scan(refused)
    assert (kept["valid"], kept["breaches"], kept["attempts"]) == (True, 0, 1)


def test_a_run_with_nothing_to_check_is_unverified_and_the_curator_must_stay_in_its_workspace(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan(empty)["valid"] is None
    records = tmp_path / "run"
    log(records / "gen" / "observations.jsonl", tool_event("read_file", {"path": "uploads/a.md"}, "a"))
    workspace = "/tmp/raven-curator-explore-abc"
    trace = {
        "trace": [
            {"event": "query", "tool": "read_file", "arguments": {"path": f"{workspace}/source/raven/x.py"}},
            {"event": "query", "tool": "list_dir", "arguments": {"path": "source/raven"}},
            # A glob segment must not be read as the absolute path /workdir.
            {
                "event": "query",
                "tool": "find",
                "arguments": {"pattern": "**/workdir*.py", "path": f"{workspace}/source"},
            },
            {
                "event": "query",
                "tool": "exec",
                "arguments": {"command": f"ls {records}/home"},
                "result": {"failed": True},
            },
            {
                "event": "query",
                "tool": "read_file",
                "arguments": {"path": str(EXPERIMENTAL / "simulation" / "BRIEF.md")},
            },
        ]
    }
    (records / "curation" / "scopes" / "s1").mkdir(parents=True)
    (records / "curation" / "scopes" / "s1" / "generation.json").write_text(json.dumps(trace))
    result = scan(records)
    outside = [(finding["path"], finding["refused"]) for finding in result["findings"]]
    assert result["valid"] is False and result["breaches"] == 1 and result["attempts"] == 1
    assert (str(EXPERIMENTAL / "simulation" / "BRIEF.md"), False) in outside


def test_a_name_the_file_system_refuses_is_scanned_not_fatal(tmp_path):
    records = tmp_path / "run"
    own = records / "replicas" / "1-family"
    (own / "workdir").mkdir(parents=True)
    log(own / "gen" / "observations.jsonl", tool_event("read_file", {"path": "x" * 300}, "a"))
    result = scan(records)
    assert (result["valid"], result["breaches"]) == (True, 0)
