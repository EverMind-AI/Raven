"""Run profile: the single in-process answer to "who is this run for".

Mode-dependent behavior used to be inferred independently by each consumer
from the ``interactive`` flag (completion gates, shadow-git checkpoint, the
identity prompt), which coupled unrelated decisions: benchmarks passing
``interactive=False`` to skip the checkpoint also turned every completion
gate on. A :class:`RunProfile` is resolved once per process and read
everywhere, splitting that flag into the three orthogonal facts it conflated:
whether anyone can answer (``attended``), how the work is collected
(``delivery``), and what defines done (``acceptance``).

Fields are named for task properties, never for benchmarks: a profile encodes
the grading contract (``delivery="commit"``), not the suite that happens to
grade that way.

Cross-repo contract (harness -> raven) is environment-only: ``RAVEN_PROFILE``,
``RAVEN_DELIVERY`` and ``RAVEN_ACCEPTANCE``. Raven's own JSON config is
``extra='forbid'``, so an old raven given a config with new keys refuses to
start; old ravens ignore unknown env instead. Unknown env VALUES are ignored
with a warning rather than raised — an old value-set raven handed a newer
enum member must keep running.

When nothing selects a profile, inference reproduces today's behavior exactly:
``interactive=True`` maps to ``interactive`` and ``interactive=False`` to
``legacy_oneshot``, whose field values replicate the historical
``gates_default = not interactive`` wiring byte for byte. ``legacy_oneshot``
is deliberately absent from the env value set — it exists only as an inference
result, so explicit configuration always states intent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from loguru import logger

PROFILE_ENV = "RAVEN_PROFILE"
DELIVERY_ENV = "RAVEN_DELIVERY"
ACCEPTANCE_ENV = "RAVEN_ACCEPTANCE"
DOMAIN_ENV = "RAVEN_DOMAIN"

DELIVERIES = ("none", "live_env", "worktree_diff", "commit")
ACCEPTANCES = ("conversation", "existing_tests", "spec")
# Subject-matter domain of the task, selecting the identity prompt directory and
# the discipline block inside it. Deliberately NOT derived from the preset: one
# preset serves several domains (eval_answer runs pinchbench, clawbench,
# appworld and DataAgentBench, of which only the last is a data task), so the
# harness states the domain separately. Values match the prompt directory names
# under context_engine/segments/prompts/ so there is no mapping table to keep
# in sync.
DOMAINS = ("coding", "data")


@dataclass(frozen=True)
class GateDefaults:
    """Default on/off per completion gate; single-gate env vars still override."""

    test_evidence: bool
    verify_complete: bool
    verify_wording: str  # "generic" | "spec"
    stale: bool
    red: bool
    empty_diff: bool


@dataclass(frozen=True)
class RunProfile:
    name: str
    source: str  # "caller" | "cli" | "env" | "config" | "inferred"
    attended: bool
    delivery: str
    acceptance: str
    gates: GateDefaults
    deadline_checkpoint: bool
    shadow_checkpoint: bool
    ask_user_enabled: bool
    domain: str = "coding"
    # Absolute-seconds margins handed to TimeBudgetReminder. Wrapping up costs
    # what delivery costs: a coding task has to run the suite and commit, an
    # answer task only has to write the answer. hard_stop=None disables the
    # stop entirely, which is the only correct setting for an attended run --
    # ending the turn from under the person typing into it is never right.
    deadline_wrapup_sec: float = 420.0
    deadline_hard_stop_sec: float | None = 60.0


_GATES_OFF = GateDefaults(
    test_evidence=False,
    verify_complete=False,
    verify_wording="generic",
    stale=False,
    red=False,
    empty_diff=False,
)
_GATES_ON = GateDefaults(
    test_evidence=True,
    verify_complete=True,
    verify_wording="generic",
    stale=True,
    red=True,
    empty_diff=True,
)

PROFILES: dict[str, RunProfile] = {
    "interactive": RunProfile(
        name="interactive",
        source="preset",
        attended=True,
        delivery="none",
        acceptance="conversation",
        gates=_GATES_OFF,
        deadline_checkpoint=False,
        shadow_checkpoint=True,
        ask_user_enabled=True,
        deadline_wrapup_sec=420.0,
        deadline_hard_stop_sec=None,
    ),
    # The intended shape of an attended one-shot (`-m` quick question):
    # quieter than legacy_oneshot. Nothing maps here implicitly yet — turning
    # inference over to it is a product decision, tracked separately.
    "oneshot": RunProfile(
        name="oneshot",
        source="preset",
        attended=True,
        delivery="none",
        acceptance="conversation",
        gates=replace(_GATES_OFF, verify_complete=True),
        deadline_checkpoint=False,
        shadow_checkpoint=False,
        ask_user_enabled=True,
        deadline_wrapup_sec=420.0,
        deadline_hard_stop_sec=None,
    ),
    # Inference fallback for `-m` (and any third-party caller passing
    # interactive=False) without an explicit profile. Not accepted via
    # RAVEN_PROFILE. On the swarm-integration line this path ships gates
    # OFF: an orchestrated worker also serves non-coding requests, where
    # change-the-code gates only mis-fire; RAVEN_PROFILE=eval_coding or the
    # per-gate env vars arm them for coding-style runs.
    "legacy_oneshot": RunProfile(
        name="legacy_oneshot",
        source="preset",
        attended=True,
        delivery="none",
        acceptance="conversation",
        gates=_GATES_OFF,
        deadline_checkpoint=False,
        shadow_checkpoint=False,
        ask_user_enabled=True,
        deadline_wrapup_sec=420.0,
        deadline_hard_stop_sec=None,
    ),
    "eval_coding": RunProfile(
        name="eval_coding",
        source="preset",
        attended=False,
        delivery="worktree_diff",
        acceptance="existing_tests",
        gates=_GATES_ON,
        deadline_checkpoint=False,
        shadow_checkpoint=False,
        ask_user_enabled=False,
        deadline_wrapup_sec=600.0,
        deadline_hard_stop_sec=90.0,
    ),
    "eval_answer": RunProfile(
        name="eval_answer",
        source="preset",
        attended=False,
        delivery="none",
        acceptance="conversation",
        gates=_GATES_OFF,
        deadline_checkpoint=False,
        shadow_checkpoint=False,
        ask_user_enabled=False,
        deadline_wrapup_sec=420.0,
        deadline_hard_stop_sec=60.0,
    ),
}

# Values accepted from RAVEN_PROFILE / --profile / config. legacy_oneshot is
# excluded on purpose (see module docstring).
SELECTABLE = ("interactive", "oneshot", "eval_coding", "eval_answer")


def _valid_or_warn(value: str | None, allowed: tuple[str, ...], what: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in allowed:
        logger.warning("unknown {} {!r}; ignoring (accepted: {})", what, value, ", ".join(allowed))
        return None
    return cleaned


def _derive(profile: RunProfile) -> RunProfile:
    """Re-derive the fields that follow from delivery/acceptance.

    acceptance="spec" selects the statement-audit verify wording (when the
    verify gate is on at all); delivery="commit" turns the deadline
    checkpoint ask on — uncommitted work is exactly the work a commit-graded
    deadline destroys. An attended run never hard-stops, whatever its preset
    says: enforcing that here means a new preset cannot get it wrong.
    """
    gates = profile.gates
    if profile.acceptance == "spec" and gates.verify_complete:
        gates = replace(gates, verify_wording="spec")
    deadline_checkpoint = profile.deadline_checkpoint or profile.delivery == "commit"
    hard_stop = None if profile.attended else profile.deadline_hard_stop_sec
    return replace(
        profile,
        gates=gates,
        deadline_checkpoint=deadline_checkpoint,
        deadline_hard_stop_sec=hard_stop,
    )


def resolve_profile(
    *,
    caller: RunProfile | str | None = None,
    cli: str | None = None,
    config: str | None = None,
    interactive: bool | None = None,
    domain_cli: str | None = None,
    domain_config: str | None = None,
) -> RunProfile:
    """Resolve the process's RunProfile. Called once, at loop construction.

    Precedence: caller > cli > env RAVEN_PROFILE > config > inference from
    the deprecated ``interactive`` flag. An invalid value at one level falls
    through to the next (with a warning), never raises — a raven older than
    a value must keep running, and so must a raven newer than one.

    Domain has its own chain (cli > env RAVEN_DOMAIN > config > preset) because
    it is orthogonal to the profile: one profile serves several domains, so a
    config that pins a domain must not also have to pin a profile.
    """
    base: RunProfile | None = None
    source = "inferred"
    if isinstance(caller, RunProfile):
        base, source = caller, "caller"
    elif isinstance(caller, str) and (name := _valid_or_warn(caller, SELECTABLE, "profile")):
        base, source = PROFILES[name], "caller"
    if base is None and (name := _valid_or_warn(cli, SELECTABLE, "profile")):
        base, source = PROFILES[name], "cli"
    if base is None and (name := _valid_or_warn(os.environ.get(PROFILE_ENV), SELECTABLE, "profile")):
        base, source = PROFILES[name], "env"
    if base is None and (name := _valid_or_warn(config, SELECTABLE, "profile")):
        base, source = PROFILES[name], "config"
    if base is None:
        base = PROFILES["interactive"] if interactive in (True, None) else PROFILES["legacy_oneshot"]

    if delivery := _valid_or_warn(os.environ.get(DELIVERY_ENV), DELIVERIES, "delivery"):
        base = replace(base, delivery=delivery)
    if acceptance := _valid_or_warn(os.environ.get(ACCEPTANCE_ENV), ACCEPTANCES, "acceptance"):
        base = replace(base, acceptance=acceptance)
    for candidate in (domain_cli, os.environ.get(DOMAIN_ENV), domain_config):
        if domain := _valid_or_warn(candidate, DOMAINS, "domain"):
            base = replace(base, domain=domain)
            break
    return _derive(replace(base, source=source))


def describe_effective(profile: RunProfile) -> str:
    """One machine-readable audit line: the profile AFTER single-gate env overrides.

    This is the feature-detection and audit contract (grep target for
    harnesses): opt-in switches have already produced one silent-no-op run
    whose conclusion was void, so the line reports what will actually be in
    force this turn, not what the preset says.
    """
    from raven.agent.loop import completion_gates, time_budget

    g = profile.gates
    test_evidence = completion_gates.gate_enabled("RAVEN_REQUIRE_REAL_TEST_EVIDENCE", g.test_evidence)
    verify = completion_gates.gate_enabled("RAVEN_VERIFY_BEFORE_COMPLETE", g.verify_complete)
    env_wording = completion_gates.gate_variant("RAVEN_VERIFY_BEFORE_COMPLETE")
    wording = ("spec" if env_wording == "spec" else "generic") if env_wording else g.verify_wording
    stale = test_evidence and completion_gates.gate_enabled("RAVEN_GATE_STALE", g.stale)
    red = test_evidence and completion_gates.gate_enabled("RAVEN_GATE_RED", g.red)
    empty = completion_gates.gate_enabled(completion_gates.EMPTY_DIFF_ENV, g.empty_diff)
    deadline = bool(os.environ.get(time_budget.CHECKPOINT_ENV)) or profile.deadline_checkpoint

    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

    return (
        f"raven-profile: v1 name={profile.name} source={profile.source} "
        f"delivery={profile.delivery} acceptance={profile.acceptance} "
        f"domain={profile.domain} "
        f"gates=test_evidence:{onoff(test_evidence)},"
        f"verify:{wording if verify else 'off'},"
        f"stale:{onoff(stale)},red:{onoff(red)},empty_diff:{onoff(empty)} "
        f"checkpoint=deadline:{onoff(deadline)},shadow:{onoff(profile.shadow_checkpoint)} "
        f"ask_user={onoff(profile.ask_user_enabled)}"
    )


# Process-wide holder so prompt rendering (which has no AgentLoop reference)
# reads the same resolution the loop uses. One process hosts one loop; the
# fallback covers contexts that render prompts without ever building a loop.
_current: RunProfile | None = None


def set_current_profile(profile: RunProfile) -> None:
    global _current
    _current = profile


def current_profile() -> RunProfile:
    return _current if _current is not None else resolve_profile()
