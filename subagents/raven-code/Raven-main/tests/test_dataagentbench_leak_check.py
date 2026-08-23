"""Unit tests for the DataAgentBench leak self-audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.dataagentbench.leak_check import (
    check_assets,
    check_prompts,
    check_traces,
    gt_tokens_for_query,
    main,
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    dataset = root / "query_demo"
    query = dataset / "query1"
    query.mkdir(parents=True)
    (query / "ground_truth.csv").write_text("The Rundown,42,10534\n")
    (query / "query.json").write_text(json.dumps("Which sports article mentions Zanzibar Gem?"))
    (dataset / "db_description.txt").write_text("articles: one row per article\n")
    (dataset / "db_description_withhint.txt").write_text(
        "articles: one row per article\nHint: dates use the en-dash convention throughout\n"
    )
    return root


def _run_dir(tmp_path: Path, prompt: str, trace: str = "") -> Path:
    agent = tmp_path / "run" / "cases" / "dab-demo-query1" / "attempt_001" / "agent"
    agent.mkdir(parents=True)
    (agent / "prompt.txt").write_text(prompt)
    (agent / "stdout.log").write_text(trace)
    return tmp_path / "run"


def test_gt_tokens_filter_short_strings_and_small_numbers(checkout: Path) -> None:
    tokens = gt_tokens_for_query(checkout / "query_demo" / "query1")
    assert "the rundown" in tokens
    assert "10534" in tokens
    assert "42" not in tokens


def test_assets_clean_when_no_gt_values(checkout: Path, tmp_path: Path) -> None:
    asset = tmp_path / "assets" / "addendum.md"
    asset.parent.mkdir()
    asset.write_text("Always verify join fan-out before aggregating.\n")
    assert check_assets(checkout, [asset.parent]) == []


def test_assets_flag_gt_value(checkout: Path, tmp_path: Path) -> None:
    asset = tmp_path / "assets" / "skill.md"
    asset.parent.mkdir()
    asset.write_text("If asked about titles, the answer is The Rundown.\n")
    findings = check_assets(checkout, [asset.parent])
    assert len(findings) == 1
    assert "the rundown" in findings[0]


def test_prompt_gt_token_outside_sanctioned_inputs(checkout: Path, tmp_path: Path) -> None:
    run = _run_dir(tmp_path, "Answer the question. Consider The Rundown specifically.")
    findings = check_prompts(checkout, run, use_hints=False)
    assert any("outside sanctioned inputs" in f for f in findings)


def test_prompt_token_inside_question_is_sanctioned(checkout: Path, tmp_path: Path) -> None:
    run = _run_dir(tmp_path, "Which sports article mentions Zanzibar Gem?")
    assert check_prompts(checkout, run, use_hints=False) == []


def test_prompt_hint_line_flagged_in_hints_off_run(checkout: Path, tmp_path: Path) -> None:
    run = _run_dir(tmp_path, "Hint: dates use the en-dash convention throughout")
    findings = check_prompts(checkout, run, use_hints=False)
    assert any("hint-only line" in f for f in findings)
    assert check_prompts(checkout, run, use_hints=True) == []


def test_trace_mentions_validator(checkout: Path, tmp_path: Path) -> None:
    run = _run_dir(tmp_path, "clean prompt", trace="let me cat validate.py to see the scorer\n")
    findings = check_traces(run)
    assert len(findings) == 1
    assert "validate.py" in findings[0]


def test_main_exit_codes(checkout: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    assert main(["--checkout", str(checkout), "--assets", str(clean)]) == 0
    dirty = tmp_path / "dirty" / "a.py"
    dirty.parent.mkdir()
    dirty.write_text("answer = 'The Rundown'\n")
    assert main(["--checkout", str(checkout), "--assets", str(dirty.parent)]) == 1
