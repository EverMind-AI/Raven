"""End-to-end layer (E) of the agent package validator, against the ACP stub agent."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from evolver.validator.dispatch import collect_files, e2e_layer, run_cases
from evolver.validator.home import build_isolated_home
from evolver.validator.report import CaseRun
from evolver.validator.snapshot import take_snapshot
from evolver.validator.suite import Case, Criterion, Suite
from tests.evolver_validator_stub import make_stub_agent

FLASH = "deepseek/deepseek-flash"


def _home(tmp_path: Path, **stub):
    folder = make_stub_agent(tmp_path / "src", **stub)
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-test"}}}), encoding="utf-8")
    return build_isolated_home(tmp_path / "run", snap, host_config=host, model=FLASH)


def _suite(*cases: Case) -> Suite:
    return Suite(name="s", version="1", target=None, cases=cases, digest="0" * 64)


def _case(case_id: str = "a", *, files: dict[str, bytes] | None = None, timeout_s: int = 60) -> Case:
    return Case(
        id=case_id,
        task="Do the thing.",
        files=files or {},
        criteria=(Criterion("c1", "It is done."),),
        reference="",
        timeout_s=timeout_s,
    )


def _stub_pids(home: Path) -> set[str]:
    out = subprocess.run(["pgrep", "-f", "acp_stub_server.py"], capture_output=True, text=True).stdout
    mine = set()
    for pid in out.split():
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if f"RAVEN_HOME={home}".encode() in environ:
            mine.add(pid)
    return mine


def test_reply_and_files_are_collected(tmp_path):
    home = _home(tmp_path, mode="writes_file")
    (run,) = run_cases(home, agent_name="Stub-Agent", suite=_suite(_case()), run_dir=tmp_path / "run", run_id="r1")
    assert run.error is None
    assert run.reply == "wrote it"
    texts = {f.path: f.text for f in run.files}
    assert texts["out/result.txt"] == "made by the agent\n"
    assert texts["notes.md"] == "# notes\n"
    assert e2e_layer([run]).passed


def test_plain_reply_is_the_closing_message(tmp_path):
    home = _home(tmp_path, mode="ok", reply="pong")
    (run,) = run_cases(home, agent_name="Stub-Agent", suite=_suite(_case()), run_dir=tmp_path / "run", run_id="r1")
    assert run.reply == "pong"
    assert run.error is None


def test_input_files_unchanged_are_not_reported(tmp_path):
    home = _home(tmp_path, mode="writes_file")
    case = _case(files={"in.txt": b"hi", "data/table.csv": b"a,b\n"})
    (run,) = run_cases(home, agent_name="Stub-Agent", suite=_suite(case), run_dir=tmp_path / "run", run_id="r1")
    paths = {f.path for f in run.files}
    assert "in.txt" not in paths and "data/table.csv" not in paths
    assert (tmp_path / "run" / "cases" / "a" / "in.txt").read_bytes() == b"hi"


def test_empty_turn_fails_e1(tmp_path):
    home = _home(tmp_path, mode="ok", extra_env={"ACP_STUB_EMPTY_PROMPTS": "1"})
    (run,) = run_cases(home, agent_name="Stub-Agent", suite=_suite(_case()), run_dir=tmp_path / "run", run_id="r1")
    layer = e2e_layer([run])
    assert not layer.passed
    assert [r.rule for r in layer.results if r.status == "fail"] == ["E1"]


def test_timeout_fails_e1_and_leaves_no_process(tmp_path):
    home = _home(tmp_path, mode="ok", extra_env={"ACP_STUB_PROMPT_DELAY_S": "20"})
    (run,) = run_cases(
        home, agent_name="Stub-Agent", suite=_suite(_case(timeout_s=2)), run_dir=tmp_path / "run", run_id="r1"
    )
    assert run.error and "time" in run.error.lower()
    assert not e2e_layer([run]).passed
    assert not _stub_pids(home.path)


def test_cases_run_in_order_each_in_its_own_workspace(tmp_path):
    home = _home(tmp_path, mode="ok", reply="pong")
    runs = run_cases(
        home, agent_name="Stub-Agent", suite=_suite(_case("a"), _case("b")), run_dir=tmp_path / "run", run_id="r1"
    )
    assert [r.case_id for r in runs] == ["a", "b"]
    assert (tmp_path / "run" / "cases" / "a").is_dir() and (tmp_path / "run" / "cases" / "b").is_dir()


def test_large_text_file_is_truncated_and_hashed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    big = "x" * 70000
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00")
    records = {r.path: r for r in collect_files(workspace, before={})}
    assert records["big.txt"].truncated and len(records["big.txt"].text) == 65536
    assert records["big.txt"].sha256 == hashlib.sha256(big.encode()).hexdigest()
    assert records["blob.bin"].text is None and records["blob.bin"].size == 3


def test_e1_fails_on_error_or_blank_reply():
    ok = CaseRun("a", "done", "done", [], 1.0, None)
    blank = CaseRun("b", "  ", "", [], 1.0, None)
    broken = CaseRun("c", "", "", [], 1.0, "AcpEmptyTurnError: nothing")
    layer = e2e_layer([ok, blank, broken])
    assert [(r.where, r.status) for r in layer.results] == [("a", "pass"), ("b", "fail"), ("c", "fail")]


def test_raven_bookkeeping_is_not_a_delivered_file(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / ".raven" / "shadow.git").mkdir(parents=True)
    (workspace / ".raven" / "NOTICE.txt").write_text("created by Raven's run", encoding="utf-8")
    (workspace / ".raven" / "shadow.git" / "HEAD").write_text("ref", encoding="utf-8")
    (workspace / "out" / ".raven").mkdir(parents=True)
    (workspace / "out" / ".raven" / "kept.txt").write_text("agent wrote this", encoding="utf-8")
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    assert sorted(r.path for r in collect_files(workspace, before={})) == ["hello.txt", "out/.raven/kept.txt"]
