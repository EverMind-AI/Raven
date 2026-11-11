"""The agent package validator end to end on a real model: raven-code on deepseek-flash.

Opt-in (``-m integration``). The suite has one case the agent can satisfy and one whose
criterion the task never asks for, so a working check reports one pass and one fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolver.validator.runner import ValidateOptions, validate
from raven.home import get_config_path

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "tests" / "fixtures" / "evolver_validator" / "raven_code_suite"
FLASH = "deepseek/deepseek-flash"


# Resolved at import: tests/conftest.py gives each test its own temporary HOME, and the
# credentials live in the operator's config.
HOST_CONFIG = get_config_path()


def _has_providers() -> bool:
    try:
        return bool(json.loads(HOST_CONFIG.read_text(encoding="utf-8")).get("providers"))
    except (OSError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(not _has_providers(), reason="no providers in the active raven config"),
]


def test_raven_code_one_pass_one_fail_on_deepseek_flash(tmp_path):
    report = validate(
        ValidateOptions(
            folder=REPO / "agents" / "raven-code",
            suite=SUITE,
            model=FLASH,
            host_config=HOST_CONFIG,
            judge_model=FLASH,
            min_pass_rate=1.0,
            run_dir=tmp_path / "run",
        )
    )
    assert report.rendered["model"] == FLASH and report.rendered["memory_backend"] is None
    assert report.rendered["provider"] == "deepseek"
    assert [run.error for run in report.cases] == [None, None]
    assert [v.passed for v in report.verdicts] == [True, False], [v.criteria for v in report.verdicts]
    assert report.outcome == "FAIL"
    assert not (tmp_path / "run" / "home").exists()
