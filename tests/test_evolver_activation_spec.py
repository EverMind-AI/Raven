"""Activation-spec validation: a zero/negative threshold must not reach evaluation."""

from __future__ import annotations

import pytest

from evolver.activation.spec import ActivationSpec, evaluate_spec

THRESHOLD_KINDS = (
    "consecutive_repeat",
    "short_content_run",
    "empty_run",
    "repeated_failure_run",
    "min_iterations",
)


@pytest.mark.parametrize("kind", THRESHOLD_KINDS)
@pytest.mark.parametrize("threshold", [0, -1])
def test_threshold_must_be_positive(kind: str, threshold: int):
    with pytest.raises(ValueError, match="must be >= 1"):
        ActivationSpec.from_dict({"kind": kind, "threshold": threshold})


def test_threshold_one_is_accepted():
    for kind in THRESHOLD_KINDS:
        spec = ActivationSpec.from_dict({"kind": kind, "threshold": 1})
        assert spec.threshold == 1


def test_negative_max_chars_is_rejected():
    with pytest.raises(ValueError, match="max_chars must be >= 0"):
        ActivationSpec.from_dict({"kind": "short_content_run", "threshold": 1, "max_chars": -1})


def test_min_iterations_does_not_count_an_empty_trajectory():
    spec = ActivationSpec.from_dict({"kind": "min_iterations", "threshold": 1})
    assert evaluate_spec(spec, [[]]) == 0
    assert evaluate_spec(spec, [[{"role": "assistant", "content": "hi"}]]) == 1
