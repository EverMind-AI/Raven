"""Activation-spec validation: a zero/negative threshold must not reach evaluation."""

from __future__ import annotations

import dataclasses

import pytest

from evolver.activation.spec import _THRESHOLD_KINDS, ActivationSpec, evaluate_spec


@pytest.mark.parametrize("kind", sorted(_THRESHOLD_KINDS))
@pytest.mark.parametrize("threshold", [0, -1])
def test_threshold_must_be_positive(kind: str, threshold: int):
    with pytest.raises(ValueError, match="must be >= 1"):
        ActivationSpec.from_dict({"kind": kind, "threshold": threshold})


def test_threshold_one_is_accepted():
    for kind in _THRESHOLD_KINDS:
        spec = ActivationSpec.from_dict({"kind": kind, "threshold": 1})
        assert spec.threshold == 1


def test_missing_threshold_names_the_field():
    with pytest.raises(ValueError, match="requires 'threshold'"):
        ActivationSpec.from_dict({"kind": "min_iterations"})


@pytest.mark.parametrize("max_chars", [0, -1])
def test_non_positive_max_chars_is_rejected(max_chars: int):
    with pytest.raises(ValueError, match="max_chars must be >= 1"):
        ActivationSpec.from_dict({"kind": "short_content_run", "threshold": 1, "max_chars": max_chars})


def test_max_chars_one_is_accepted():
    spec = ActivationSpec.from_dict({"kind": "short_content_run", "threshold": 1, "max_chars": 1})
    assert spec.max_chars == 1


@pytest.mark.parametrize("pattern", ["  ", None, 3])
def test_empty_or_non_string_trajectory_regex_is_rejected(pattern):
    with pytest.raises(ValueError, match="non-empty string"):
        ActivationSpec.from_dict({"kind": "trajectory_regex", "pattern": pattern})


@pytest.mark.parametrize("value", [True, 1.9, float("inf")])
def test_non_integer_threshold_is_rejected(value):
    with pytest.raises(ValueError, match="must be an int"):
        ActivationSpec.from_dict({"kind": "min_iterations", "threshold": value})


def test_min_iterations_does_not_count_an_empty_trajectory():
    spec = ActivationSpec.from_dict({"kind": "min_iterations", "threshold": 1})
    assert evaluate_spec(spec, [[]]) == 0
    assert evaluate_spec(spec, [[{"role": "assistant", "content": "hi"}]]) == 1


def test_evaluate_spec_rechecks_a_hand_built_or_mutated_spec():
    hand_built = ActivationSpec(kind="min_iterations", raw={"threshold": 0})
    with pytest.raises(ValueError, match="must be >= 1"):
        evaluate_spec(hand_built, [[]])

    parsed = ActivationSpec.from_dict({"kind": "min_iterations", "threshold": 1})
    mutated = dataclasses.replace(parsed, raw={"threshold": 0})
    with pytest.raises(ValueError, match="must be >= 1"):
        evaluate_spec(mutated, [[]])
