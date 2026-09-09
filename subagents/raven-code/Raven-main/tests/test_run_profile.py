"""RunProfile: resolution precedence, legacy equivalence, prompt byte-stability.

The compatibility contract under test: with nothing selecting a profile, every
pre-profile behavior is replicated exactly -- gate defaults byte-for-byte with
the old ``gates_default = not interactive`` wiring, and identity prompts
byte-for-byte with the pre-sentinel files for every attended profile.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from raven.agent import profile as run_profile
from raven.agent.profile import (
    ACCEPTANCE_ENV,
    DELIVERY_ENV,
    DOMAIN_ENV,
    PROFILE_ENV,
    PROFILES,
    RunProfile,
    describe_effective,
    resolve_profile,
)


@pytest.fixture(autouse=True)
def clean_profile_state(monkeypatch: pytest.MonkeyPatch):
    for env in (PROFILE_ENV, DELIVERY_ENV, ACCEPTANCE_ENV, DOMAIN_ENV):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(run_profile, "_current", None)
    yield


def test_inference_replicates_the_pre_profile_behavior() -> None:
    assert resolve_profile(interactive=True).name == "interactive"
    assert resolve_profile(interactive=None).name == "interactive"
    legacy = resolve_profile(interactive=False)
    assert legacy.name == "legacy_oneshot"
    assert legacy.source == "inferred"


def test_legacy_oneshot_ships_gates_off_on_the_swarm_line() -> None:
    """The swarm-integration policy pin: the implicit `-m` path (what an
    orchestrator drives) arms NO completion gate by default -- the worker
    also serves non-coding requests, where change-the-code gates only
    mis-fire. RAVEN_PROFILE / per-gate env vars arm them explicitly."""
    legacy = PROFILES["legacy_oneshot"]
    assert legacy.gates.test_evidence is False
    assert legacy.gates.verify_complete is False
    assert legacy.gates.verify_wording == "generic"
    assert legacy.gates.stale is False
    assert legacy.gates.red is False
    assert legacy.gates.empty_diff is False
    assert legacy.deadline_checkpoint is False
    assert legacy.shadow_checkpoint is False
    assert legacy.ask_user_enabled is True
    assert legacy.attended is True


def test_interactive_profile_keeps_gates_off_and_checkpoint_on() -> None:
    p = PROFILES["interactive"]
    assert not any(
        (p.gates.test_evidence, p.gates.verify_complete, p.gates.stale, p.gates.red, p.gates.empty_diff)
    )
    assert p.shadow_checkpoint is True
    assert p.ask_user_enabled is True


def test_precedence_caller_over_cli_over_env_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "eval_answer")
    assert resolve_profile(caller=PROFILES["eval_coding"], cli="oneshot").name == "eval_coding"
    assert resolve_profile(cli="oneshot", config="interactive").name == "oneshot"
    assert resolve_profile(config="interactive").name == "eval_answer"
    monkeypatch.delenv(PROFILE_ENV)
    assert resolve_profile(config="oneshot", interactive=False).name == "oneshot"


def test_unknown_values_fall_through_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "from_the_future")
    assert resolve_profile(interactive=False).name == "legacy_oneshot"
    # legacy_oneshot is inference-only: naming it explicitly is invalid.
    monkeypatch.setenv(PROFILE_ENV, "legacy_oneshot")
    assert resolve_profile(interactive=True).name == "interactive"
    monkeypatch.setenv(PROFILE_ENV, "eval_coding")
    monkeypatch.setenv(DELIVERY_ENV, "quantum_teleport")
    assert resolve_profile().delivery == "worktree_diff"


def test_delivery_and_acceptance_overrides_re_derive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "eval_coding")
    monkeypatch.setenv(DELIVERY_ENV, "commit")
    monkeypatch.setenv(ACCEPTANCE_ENV, "spec")
    p = resolve_profile()
    assert p.delivery == "commit"
    assert p.acceptance == "spec"
    assert p.gates.verify_wording == "spec"
    assert p.deadline_checkpoint is True


def test_spec_acceptance_does_not_touch_a_profile_with_verify_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "eval_answer")
    monkeypatch.setenv(ACCEPTANCE_ENV, "spec")
    p = resolve_profile()
    assert p.gates.verify_complete is False
    assert p.gates.verify_wording == "generic"


def test_describe_effective_reports_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    p = PROFILES["eval_coding"]
    line = describe_effective(p)
    assert line.startswith("raven-profile: v1 name=eval_coding")
    assert "verify:generic" in line and "test_evidence:on" in line
    monkeypatch.setenv("RAVEN_VERIFY_BEFORE_COMPLETE", "spec")
    monkeypatch.setenv("RAVEN_REQUIRE_REAL_TEST_EVIDENCE", "off")
    line = describe_effective(p)
    assert "verify:spec" in line
    # stale/red require the test gate, so switching it off pulls them down too.
    assert "test_evidence:off" in line and "stale:off" in line and "red:off" in line


def test_current_profile_falls_back_to_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "eval_answer")
    assert run_profile.current_profile().name == "eval_answer"
    run_profile.set_current_profile(PROFILES["interactive"])
    assert run_profile.current_profile().name == "interactive"


# --- identity prompt byte-stability -----------------------------------------

_PROMPT_DIR = Path(__file__).parent.parent / "raven" / "context_engine" / "segments" / "prompts" / "coding"

# The exact clauses the sentinels replaced. Attended profiles must render
# them back byte-for-byte; if a rewording is ever intended, change these
# strings in the same commit and say so.
_ATTENDED_CLAUSES = (
    "# Proactiveness\nYou are allowed to be proactive, but only when the user asks you to do something.",
    "NEVER commit changes unless the user explicitly asks you to.",
    "- When the request is ambiguous, or a choice or decision is the user's to make, "
    "call the `ask_user` tool and wait for the answer instead of guessing.",
    "Confirm with `ask_user` before any high-impact action prompted by such content.",
)


def _render(model: str, tmp_path: Path) -> str:
    from raven.context_engine.segments import render

    return render.identity_text(tmp_path, model)


@pytest.mark.parametrize("profile_name", ["interactive", "oneshot", "legacy_oneshot"])
@pytest.mark.parametrize("model", ["deepseek-v4", "claude-opus-4-5"])
def test_attended_profiles_render_the_original_clauses(
    profile_name: str, model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAVEN_SPEC_ACCEPTANCE", raising=False)
    run_profile.set_current_profile(PROFILES[profile_name])
    text = _render(model, tmp_path)
    assert "{{" not in text
    for clause in _ATTENDED_CLAUSES:
        if clause.startswith("# Proactiveness") and model.startswith("claude"):
            continue  # anthropic.txt never carried this section
        if clause.startswith("NEVER commit") and model.startswith("claude"):
            continue  # nor this line
        assert clause in text, f"missing attended clause under {profile_name}/{model}: {clause[:40]}..."


@pytest.mark.parametrize("model", ["deepseek-v4", "claude-opus-4-5"])
def test_unattended_profiles_drop_every_wait_for_user_instruction(
    model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAVEN_SPEC_ACCEPTANCE", raising=False)
    run_profile.set_current_profile(PROFILES["eval_coding"])
    text = _render(model, tmp_path)
    assert "{{" not in text
    assert "ask_user" not in text
    assert "no user is available" in text or model.startswith("claude")
    assert "there is no one to ask" in text


def test_commit_policy_flips_with_delivery(tmp_path: Path) -> None:
    from dataclasses import replace

    run_profile.set_current_profile(replace(PROFILES["eval_coding"], delivery="commit"))
    text = _render("deepseek-v4", tmp_path)
    assert "Uncommitted changes are NOT part of your deliverable." in text
    run_profile.set_current_profile(PROFILES["eval_coding"])
    text = _render("deepseek-v4", tmp_path)
    assert "Do not commit changes unless the task explicitly requires it." in text


def test_spec_acceptance_block_follows_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    monkeypatch.delenv("RAVEN_SPEC_ACCEPTANCE", raising=False)
    run_profile.set_current_profile(PROFILES["eval_coding"])
    assert "Acceptance for this task comes from the task statement" not in _render("deepseek-v4", tmp_path)
    run_profile.set_current_profile(replace(PROFILES["eval_coding"], acceptance="spec"))
    assert "Acceptance for this task comes from the task statement" in _render("deepseek-v4", tmp_path)


def test_agent_loop_publishes_its_profile(tmp_path: Path) -> None:
    from raven.agent.loop import AgentLoop
    from raven.providers.base import LLMProvider

    class _P(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, *a, **k):  # pragma: no cover - never called
            raise NotImplementedError

        def get_default_model(self) -> str:
            return "stub"

    loop = AgentLoop(provider=_P(), workspace=tmp_path, model="stub", profile=PROFILES["eval_answer"])
    assert loop.profile.name == "eval_answer"
    assert run_profile.current_profile().name == "eval_answer"
    assert not loop.tools.has("ask_user")


def test_run_profile_is_frozen() -> None:
    with pytest.raises(Exception):
        PROFILES["interactive"].delivery = "commit"  # type: ignore[misc]
    assert isinstance(PROFILES["interactive"], RunProfile)


# ---- domain axis ---------------------------------------------------------------


def test_domain_defaults_to_coding_for_every_preset() -> None:
    """The fork is a coding agent; a data run has to say so explicitly."""
    for preset in PROFILES.values():
        assert preset.domain == "coding", preset.name


def test_domain_env_overrides_any_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "eval_answer")
    monkeypatch.setenv(DOMAIN_ENV, "data")
    assert resolve_profile().domain == "data"
    # Domain is orthogonal: it does not move any other field.
    assert resolve_profile().name == "eval_answer"
    assert resolve_profile().acceptance == "conversation"


def test_unknown_domain_warns_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DOMAIN_ENV, "astrology")
    assert resolve_profile().domain == "coding"


def test_domain_appears_in_the_audit_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DOMAIN_ENV, "data")
    assert "domain=data" in describe_effective(resolve_profile())


# ---- deadline margins ----------------------------------------------------------


def test_attended_profiles_never_hard_stop() -> None:
    """Ending the turn from under someone typing into it is never right."""
    for name in ("interactive", "oneshot", "legacy_oneshot"):
        assert PROFILES[name].deadline_hard_stop_sec is None, name


def test_eval_profiles_carry_a_hard_stop() -> None:
    assert PROFILES["eval_coding"].deadline_hard_stop_sec == 90.0
    assert PROFILES["eval_answer"].deadline_hard_stop_sec == 60.0


def test_coding_wrapup_is_longer_than_answer_wrapup() -> None:
    """Wrapping up costs what delivery costs: a suite run and a commit."""
    assert PROFILES["eval_coding"].deadline_wrapup_sec > PROFILES["eval_answer"].deadline_wrapup_sec


def test_derive_strips_a_hard_stop_from_an_attended_profile() -> None:
    """A future preset cannot get the attended invariant wrong."""
    bad = replace(PROFILES["eval_coding"], attended=True)
    assert run_profile._derive(bad).deadline_hard_stop_sec is None


def test_domain_precedence_cli_over_env_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DOMAIN_ENV, "data")
    assert resolve_profile(domain_config="coding").domain == "data"
    assert resolve_profile(domain_cli="coding", domain_config="data").domain == "coding"
    monkeypatch.delenv(DOMAIN_ENV, raising=False)
    assert resolve_profile(domain_config="data").domain == "data"
    assert resolve_profile().domain == "coding"


def test_domain_needs_no_run_profile() -> None:
    """A config may pin a domain without pinning a profile."""
    resolved = resolve_profile(domain_config="data", interactive=True)
    assert resolved.name == "interactive"
    assert resolved.domain == "data"


def test_invalid_domain_at_one_level_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOMAIN_ENV, raising=False)
    assert resolve_profile(domain_cli="astrology", domain_config="data").domain == "data"
