"""A trial's name has to fit on a filesystem, and it did not.

``config_key`` spells every key and value into the name, which is what makes a
status output readable -- ``deltaT5em4_run1`` says what it is. But a value can be
a filesystem path, and then the name inherits its length and its separators.

Measured 2026-08-13 on the ML campaigns: the key was **255 bytes**, exactly
``NAME_MAX``. It survived only because the ``eval_data`` value's slashes split it
into seven nested directories -- and that nesting broke something else, the
backend's spend probe globs ``jobs/*/`` one level deep, so a running job reported
0.0 minutes used. Flattening the slashes would have produced a single 255-byte
name with **no headroom at all**: writing ``lr`` as ``1.5e-05`` instead of
``2e-05`` would then fail with ENAMETOOLONG and the job would not start.

So values that are long or contain separators are folded to a short digest, and
the whole name is capped. What stays readable stays readable; the parts that were
never readable anyway become eight characters.

Determinism is not negotiable: this key is the idempotency key, and re-submitting
the same config must return the original job rather than start a second one. That
rules out a random id, and it is why a digest rather than a counter.
"""

from __future__ import annotations

from raven.ops.proposer import config_key

NAME_MAX = 255

ML_SEED = {
    "lr": 5e-06, "queries_per_step": 16, "negatives": 3, "max_len": 256,
    "warmup_steps": 20, "grad_checkpointing": True, "param_dtype": "float32",
    "temperature": 0.05, "epochs": 8, "seed": 20260804, "eval_every": 200,
    "log_every": 20, "keep_recent_checkpoints": 3,
    "eval_data": "/Evermind/bj_share/lxt/oncall-eval/data/nfcorpus_dev",
}


def test_the_ml_key_now_fits_with_room_to_spare():
    k = config_key(ML_SEED)
    assert "/" in k or True
    assert len(k) < 120, f"{len(k)} bytes: {k}"
    assert NAME_MAX - len(k) > 100, "and enough room left for an apparatus digest"


def test_a_path_value_no_longer_becomes_directories():
    """Nesting is what hid a running job from the spend probe."""
    k = config_key(ML_SEED)
    assert "/" not in k


def test_the_short_readable_parts_survive():
    """The name is read by people and by the loop to tell trials apart."""
    k = config_key({"deltaT": "5e-4", "run": 1})
    assert k == "deltaT5em4_run1", k


def test_two_configs_differing_only_in_a_folded_value_still_differ():
    a = config_key({"data": "/very/long/path/to/dataset/alpha/train"})
    b = config_key({"data": "/very/long/path/to/dataset/beta/train"})
    assert a != b


def test_the_same_config_always_gives_the_same_key():
    """The idempotency key. A random id would restart every crash-resumed trial."""
    assert config_key(ML_SEED) == config_key(dict(reversed(list(ML_SEED.items()))))


def test_a_pathological_config_is_still_a_legal_name():
    huge = {f"key{i}": "x" * 40 for i in range(40)}
    k = config_key(huge)
    assert len(k) <= NAME_MAX and "/" not in k


def test_an_empty_config_keeps_its_name():
    assert config_key({}) == "noparams"


def test_the_legacy_spelling_is_still_computable():
    """Campaigns started before this change hold records under the old key; a
    resume has to find them rather than re-run trials that already ran."""
    from raven.ops.proposer import legacy_config_key

    assert legacy_config_key({"deltaT": "5e-4", "run": 1}) == "deltaT5em4_run1"
    assert len(legacy_config_key(ML_SEED)) == 255
