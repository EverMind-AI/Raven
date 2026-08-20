"""A key can be written, echoed back, and still do nothing.

The CFD divergence leg passed ``deltaT`` alongside ``maxCo=0.5`` and
``maxAlphaCo=0.5``. The run script sets ``adjustTimeStep no`` whenever a
``deltaT`` arrives, and under fixed stepping those two ceilings take no part --
but the job log echoed all three, so the arm believed the Courant number was
capped at 0.5 and delivered a conclusion on that basis. Measured maximum: 2.81,
above 1 for 716 of 10000 steps. It reported the limit it set, not the limit that
held.

Two mechanisms, split by who can know what:

  - comparing the request against the run is the same operation in every domain,
    so it lives here and interprets nothing;
  - which key was inert, and why, is solver semantics. Only the job knows, so the
    job says it and this layer prints it back word for word.
"""

from __future__ import annotations

from raven.ops.effective import compare_config, describe, ignored_keys


CFD_SUBMITTED = {"deltaT": "1e-4", "maxCo": 0.5, "maxAlphaCo": 0.5, "run": 2,
                 "budget_gpu_minutes": 139.05}


def test_agreeing_configs_say_nothing():
    assert compare_config({"lr": 2e-5, "epochs": 8}, {"lr": 2e-5, "epochs": 8}) == []


def test_a_key_the_run_does_not_have_is_named():
    """The ML shape: a partial submit, and fifteen settings fall back to the job
    script's own defaults -- including which dataset gets evaluated."""
    lines = compare_config({"lr": 2e-6, "epochs": 4}, {"lr": 2e-6})
    assert len(lines) == 1 and "epochs" in lines[0]


def test_a_value_the_run_changed_is_named():
    lines = compare_config({"max_len": 256}, {"max_len": 512})
    assert lines == ["max_len: submitted 256, the run used 512"]


def test_the_same_number_spelled_differently_is_not_a_difference():
    assert compare_config({"deltaT": "1e-4"}, {"deltaT": 0.0001}) == []
    assert compare_config({"run": 2}, {"run": "2"}) == []


def test_bookkeeping_the_caller_injects_is_not_a_difference():
    """budget_gpu_minutes rides on every payload; complaining about it on every
    trial would train the reader to skip these lines."""
    assert compare_config({"lr": 1e-5, "budget_gpu_minutes": 60.0}, {"lr": 1e-5}) == []


def test_no_effective_config_is_not_a_complaint():
    """Most jobs do not write one. Inventing a difference for them is noise."""
    assert compare_config({"lr": 1e-5}, None) == []
    assert compare_config({"lr": 1e-5}, {}) == []


def test_the_job_s_own_list_of_inert_keys_is_printed_verbatim():
    eff = {"deltaT": 1e-4, "adjustTimeStep": "no", "maxCo": 0.5,
           "ignored": ["maxCo", "maxAlphaCo"],
           "ignored_reason": "adjustTimeStep=no; Courant ceilings do not apply"}
    assert ignored_keys(eff) == ["maxCo", "maxAlphaCo"]
    line = describe({"deltaT": "1e-4", "maxCo": 0.5}, eff)[-1]
    assert "maxCo" in line and "maxAlphaCo" in line
    assert "adjustTimeStep=no" in line, "the reason is the job's words, kept"


def test_nothing_here_decides_a_key_is_inert():
    """maxCo is present and equal, so the comparison has nothing to say. Deriving
    "and therefore it did nothing" needs solver semantics this layer must not
    have -- which is why the job reports it instead."""
    eff = {"deltaT": 1e-4, "adjustTimeStep": "no", "maxCo": 0.5}
    assert describe({"deltaT": "1e-4", "maxCo": 0.5}, eff) == []


def test_the_cfd_leg_that_prompted_this():
    """With the run script reporting honestly, the arm would have been told."""
    eff = {"deltaT": 1e-4, "adjustTimeStep": "no", "maxCo": 0.5, "maxAlphaCo": 0.5,
           "run": 2, "ignored": ["maxCo", "maxAlphaCo"],
           "ignored_reason": "adjustTimeStep=no under a fixed deltaT"}
    lines = describe(CFD_SUBMITTED, eff)
    assert len(lines) == 1
    assert "took no part" in lines[0] and "maxCo" in lines[0]


# --------------------------------------------------------- reading it off a host

def test_one_remote_call_covers_the_whole_campaign():
    """Looking has to stay cheap: a connection per trial would make the status
    call expensive, and looking often is the behaviour the loop is built on."""
    from raven.agent.tools.ops import _effective_config_lines

    calls: list[str] = []

    def runner(cmd):
        calls.append(cmd)
        return 0, (
            '@@run1\n{"deltaT": "1e-4", "maxCo": 0.5}\n@@EFF\n'
            '{"deltaT": 0.0001, "adjustTimeStep": "no", "maxCo": 0.5,'
            ' "ignored": ["maxCo"], "ignored_reason": "adjustTimeStep=no"}\n'
            '@@run2\n{"lr": 2e-6, "epochs": 4}\n@@EFF\n{"lr": 2e-6}\n'
        )

    got = _effective_config_lines(runner, "/remote/ops", ["run1", "run2"])
    assert len(calls) == 1, "one call, every trial"
    assert "took no part" in got["run1"][0] and "maxCo" in got["run1"][0]
    assert "epochs" in got["run2"][0]


def test_a_host_that_cannot_answer_says_nothing():
    from raven.agent.tools.ops import _effective_config_lines

    def dead(cmd):
        raise OSError("connection refused")

    assert _effective_config_lines(dead, "/remote/ops", ["run1"]) == {}
    assert _effective_config_lines(lambda c: (1, ""), "/remote/ops", ["run1"]) == {}
    assert _effective_config_lines(None, "/remote/ops", ["run1"]) == {}


def test_a_job_with_no_effective_file_is_silent():
    """Most jobs write none, and a complaint per trial would be noise."""
    from raven.agent.tools.ops import _effective_config_lines

    runner = lambda cmd: (0, '@@run1\n{"lr": 1e-5}\n@@EFF\n\n')
    assert _effective_config_lines(runner, "/remote/ops", ["run1"]) == {}


def test_unparseable_json_does_not_break_the_status_call():
    from raven.agent.tools.ops import _effective_config_lines

    runner = lambda cmd: (0, '@@run1\n{"lr": 1e-5}\n@@EFF\nnot json at all\n')
    assert _effective_config_lines(runner, "/remote/ops", ["run1"]) == {}


def test_the_run_counter_is_not_a_difference():
    """It exists to vary the idem_key so a repeat is a new trial; no job reads it,
    and flagging it on every trial would bury the differences that matter."""
    assert compare_config({"deltaT": "1e-4", "run": 2}, {"deltaT": 0.0001}) == []


# ------------------------- 单子上写的 vs 实际用的(agent 没提交的那些键)

DECLARED = {"lr": 5e-06, "epochs": 8, "max_len": 256, "negatives": 3,
            "eval_data": "/Evermind/bj_share/lxt/oncall-eval/data/nfcorpus_dev"}


def test_a_key_left_out_that_the_job_defaulted_differently_is_named():
    """The campaign's list says score on one dataset; the submit left the key out;
    the training script filled it from its own default, which is a different
    dataset with no overlap. The score then comes from one task and is compared
    against a baseline measured on another, and nothing said so.

    Measured live on M9, 2026-08-14, while the run was in flight.
    """
    from raven.ops.effective import compare_against_declared

    submitted = {k: v for k, v in DECLARED.items() if k != "eval_data"}
    effective = dict(submitted,
                     eval_data="/Evermind/bj_share/lxt/oncall-embed/data/nfcorpus")
    lines = compare_against_declared(DECLARED, submitted, effective)
    assert len(lines) == 1
    assert "eval_data" in lines[0]
    assert "nfcorpus_dev" in lines[0] and "oncall-embed" in lines[0]


def test_a_default_that_agrees_with_the_list_is_silent():
    """Filling a missing key from a default is ordinary. Only a default that
    contradicts what the campaign declared is worth a line."""
    from raven.ops.effective import compare_against_declared

    submitted = {"lr": 5e-06}
    effective = dict(DECLARED)              # every default happens to match
    assert compare_against_declared(DECLARED, submitted, effective) == []


def test_a_key_the_submit_did_pass_is_not_reported_here():
    """That case is the other comparison's; reporting it twice would read as two
    problems."""
    from raven.ops.effective import compare_against_declared

    submitted = {"max_len": 512}
    effective = {"max_len": 512}
    assert compare_against_declared(DECLARED, submitted, effective) == []


def test_no_declared_list_means_nothing_to_check_against():
    from raven.ops.effective import compare_against_declared

    assert compare_against_declared(None, {"lr": 1e-5}, {"lr": 1e-5, "epochs": 4}) == []
    assert compare_against_declared({}, {"lr": 1e-5}, {"lr": 1e-5}) == []


def test_describe_carries_it_alongside_the_other_two():
    from raven.ops.effective import describe

    submitted = {k: v for k, v in DECLARED.items() if k != "eval_data"}
    effective = dict(submitted, eval_data="/somewhere/else", max_len=512)
    lines = describe(submitted, effective, declared=DECLARED)
    joined = " | ".join(lines)
    assert "max_len" in joined, "submitted but the run changed it"
    assert "eval_data" in joined, "not submitted and the run disagrees with the list"


def test_one_trial_without_the_file_does_not_silence_the_others():
    """The chain's exit status is its last command's, so a single missing
    config.effective.json made the whole batch return nothing -- and nothing
    reads exactly like "every trial agrees". Measured on M9's ten job dirs."""
    from raven.agent.tools.ops import _effective_config_lines

    def runner(cmd):
        return 1, (                       # rc from the last cat, which found no file
            '@@run1\n{"lr": 2e-6, "epochs": 4}\n@@EFF\n{"lr": 2e-6}\n'
            '@@run2\n{"lr": 1e-5}\n@@EFF\n\n'
        )

    got = _effective_config_lines(runner, "/remote/ops", ["run1", "run2"])
    assert "epochs" in got["run1"][0]
    assert "run2" not in got


# --- the third pairing: what this round changed about the declared start -------

def test_a_declared_value_this_round_moved_is_stated():
    """Measured 2026-08-17 on the contact task: the campaign declared
    push=-0.002, a prescribed downward displacement; one round submitted +0.002,
    which lifts the face and takes the bodies out of contact. That round
    converged in 135s with no stiffness degradation and was delivered as the
    cleanest result and the recommendation -- the cleanliness being the artefact
    of the sign. Nothing printed that the sign had moved."""
    from raven.ops.effective import compare_submitted_to_declared

    lines = compare_submitted_to_declared({"push": "-0.002", "K": "1e19"},
                                          {"push": "0.002", "K": "1.78e18"})
    assert any("push" in l and "-0.002" in l and "0.002" in l for l in lines)


def test_a_value_this_round_kept_is_not_mentioned():
    from raven.ops.effective import compare_submitted_to_declared

    assert compare_submitted_to_declared({"push": "-0.002"}, {"push": "-0.002"}) == []


def test_the_same_number_written_differently_is_not_a_departure():
    from raven.ops.effective import compare_submitted_to_declared

    assert compare_submitted_to_declared({"inc": "1e-4"}, {"inc": 0.0001}) == []


def test_a_key_this_round_left_out_is_the_other_comparisons_business():
    """Omitted keys are reported against what the run actually used, not here --
    otherwise every omission would be reported twice, in two different senses."""
    from raven.ops.effective import compare_submitted_to_declared

    assert compare_submitted_to_declared({"push": "-0.002", "ninc": 20},
                                         {"ninc": 20}) == []


def test_bookkeeping_the_caller_injects_is_not_a_departure():
    from raven.ops.effective import compare_submitted_to_declared

    assert compare_submitted_to_declared({"run": 1}, {"run": 2}) == []


def test_describe_leads_with_the_departure_from_the_declared_start():
    """It comes first because it is the one thing the round chose, and the round's
    own reasoning is what a reader is checking it against."""
    from raven.ops.effective import describe

    lines = describe({"push": "0.002"}, {"push": "0.002"}, {"push": "-0.002"})
    assert lines and "push" in lines[0] and "declares" in lines[0]
