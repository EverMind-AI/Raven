"""Decision policy for the Ops loop (the minimal decision brain).

For now the only decision is retry-vs-escalate on a failed attempt, keyed on the
attempt count. Richer diagnosis (adjust the trial config, pick the next trial)
will extend this module; it is kept separate from Campaign so the policy can
evolve independently of the orchestration mechanics.

The decision-basis gate lives here too (hoisted from the fork's tool file so
the whole resubmit/kill chain lands as one mechanism layer): a wait, kill or
resubmit must cite a number an observation just made actually showed, and that
observation must be newer than the previous decision. The same check in all
three places on purpose -- friction on one side of a choice and none on the
others is a nudge toward the cheaper side.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry a failed attempt before escalating.

    ``max_retries`` counts retries *after* the first attempt, so the total
    number of attempts allowed is ``max_retries + 1``.
    """

    max_retries: int = 0

    def should_retry(self, failed_attempt: int) -> bool:
        """Given the 1-based attempt number that just failed, allow another?"""
        return failed_attempt <= self.max_retries


def from_meta(meta: dict[str, Any]) -> RetryPolicy:
    """The campaign's declared retry policy, or the default (no retries).

    Declared as ``meta["retry"] = {"max_retries": N}``. The fork constructed
    RetryPolicy in code only (default 0, survey retry slice); the key is this
    port's to define so the watcher and the part-2b tools read one declaration.
    An unreadable declaration is the default, not an error -- the policy gates
    escalation, and a typo must not turn every failure into silence.
    """
    declared = meta.get("retry")
    if not isinstance(declared, dict):
        return RetryPolicy()
    try:
        return RetryPolicy(max_retries=max(0, int(declared.get("max_retries", 0))))
    except (TypeError, ValueError):
        return RetryPolicy()


# ── Attempt keys (the fork's <trial>#a<N> ledger-key shape) ─────────
# Campaign keeps its private _attempt_key for byte-parity; these are the
# shared readers so the watcher and the tools parse the same shape.


def attempt_key(trial_id: str, attempt: int) -> str:
    """The ledger key of one attempt: attempt 1 keeps the trial id."""
    return trial_id if attempt == 1 else f"{trial_id}#a{attempt}"


def attempt_no(idem_key: str) -> int:
    """The 1-based attempt this ledger key represents."""
    base, sep, suffix = idem_key.rpartition("#a")
    if sep and base and suffix.isdigit():
        return max(1, int(suffix))
    return 1


def base_trial(idem_key: str) -> str:
    """The trial id an attempt key belongs to."""
    base, sep, suffix = idem_key.rpartition("#a")
    if sep and base and suffix.isdigit():
        return base
    return idem_key


# ── The decision-basis gate (fork ops.py:1740-1765) ─────────────────

BASIS_HELP = (
    "What you read that supports this decision -- cite the actual number you just "
    "observed. Required on every decision (wait, kill, or submit a further round) so "
    "that what a decision rested on is recorded, not inferred later."
)


def basis_refusal(cdir: Path, basis: str, action: str) -> str | None:
    """Refuse a decision whose basis does not rest on an observation just made.

    Applied to waiting, killing and re-submitting alike. Requiring it of only one
    of them would put friction on one side of a choice and none on the others,
    and that is a nudge toward the cheaper side -- which is why it is the same
    check in all three places.
    """
    from oncall_flow.instrument import log_event
    from oncall_flow.state_claims import basis_problems, read_decision_seq, read_facts

    facts = read_facts(cdir)
    problems = basis_problems(basis or "", facts, last_seq=read_decision_seq(cdir))
    if not problems:
        return None
    log_event(cdir, "basis_refused", action=action, reasons=problems)
    return "REFUSED: " + "; ".join(problems) + "."


def record_basis(cdir: Path, basis: str, action: str) -> None:
    from oncall_flow.instrument import log_event
    from oncall_flow.state_claims import read_facts, write_decision_seq

    write_decision_seq(cdir, read_facts(cdir).probe_seq)
    log_event(cdir, "basis_accepted", action=action, basis=(basis or "")[:400])
