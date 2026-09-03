"""The fork's wake vocabulary, said through the host's WakeScheduler grant.

The fork scheduled its looks by writing the CronService directly
(``_schedule_ops_wake``); the plugin holds a namespaced
:class:`~raven.contracts.scheduling.WakeScheduler` instead (paper:
raven/contracts/scheduling.py) and this module is the whole translation:

- **key = the campaign name.** The fork named jobs ``ops:<campaign>:<leg>``
  (``r<N>`` / ``recheck`` / ``after-act``) and enforced at-most-one pending
  wake per campaign by scanning and removing the prefix. The grant's replace
  semantics are per key, so the campaign IS the key and the leg detail lives
  in the message -- that is what makes the grant's one-key-one-wake exactly
  the fork's one-campaign-one-wake invariant.
- **eta -> at_ms.** The model chooses ``eta_seconds`` per look; the floor of
  one second is the fork's clamp (``max(1, eta)``, no upper bound: the pace
  is the caller's to choose).
- **replace is silent in the grant**, so the "this replaced the previous
  pending wake" half of the fork's note is recovered by reading the pending
  wake before scheduling.
- **the route is a campaign fact.** The fork addressed a wake by the
  scheduling window's live channel/chat_id (plus the owner-window store the
  seam ruling retired); a resident watcher has no session, so where a
  campaign's wakes land is recorded in its own declaration (``wake_route``
  in meta) -- ``channel``+``to``, or ``direct_agent``, the D9 rebuilt
  addressing.

Every function takes the scheduler explicitly and answers in the fork's own
note text when it cannot schedule: the caller (a tool, the watcher) owes the
model an actionable sentence, not an exception.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.contracts.scheduling import WakeScheduler

NO_SCHEDULER_NOTE = "No scheduler available; check ops_tune_status manually when results are due."
NO_CONTEXT_NOTE = "No session context to schedule a wake; check ops_tune_status manually when results are due."

_ROUTE_KEYS = ("channel", "to", "direct_agent", "direct_handle")


def wake_route(meta: dict[str, Any]) -> dict[str, str]:
    """The campaign's declared wake route, as ``schedule_next_look`` kwargs.

    Empty when the declaration names no usable route -- usable means
    ``channel`` + ``to``, or ``direct_agent`` (the same guard the scheduling
    verb applies). The scheduling tools (part 2b) record the route whenever
    they schedule from a live session, which is what lets a cold resident
    watcher raise a wake for a campaign whose window is long gone.
    """
    declared = meta.get("wake_route")
    if not isinstance(declared, dict):
        return {}
    route = {k: str(v) for k, v in declared.items() if k in _ROUTE_KEYS and v}
    if (route.get("channel") and route.get("to")) or route.get("direct_agent"):
        return route
    return {}


def schedule_next_look(
    scheduler: "WakeScheduler | None",
    *,
    campaign: str,
    eta_seconds: int,
    message: str,
    channel: str | None = None,
    to: str | None = None,
    direct_agent: str | None = None,
    direct_handle: str | None = None,
) -> str:
    """Schedule, or replace, the campaign's one pending wake; answer in prose.

    Returns a human note describing what was scheduled, or why not -- the
    fork's ``_schedule_ops_wake`` contract, minus the owner-window store
    machinery the seam ruling retired (quiet hours and the interruption
    contract come back tool-side in part two).
    """
    if scheduler is None:
        return NO_SCHEDULER_NOTE
    if not (channel and to) and not direct_agent:
        return NO_CONTEXT_NOTE
    at = datetime.now() + timedelta(seconds=max(1, int(eta_seconds)))
    replaced = pending_look(scheduler, campaign) is not None
    try:
        job = scheduler.schedule_wake(
            campaign,
            int(at.timestamp() * 1000),
            message,
            channel=channel,
            to=to,
            direct_agent=direct_agent,
            direct_handle=direct_handle,
        )
    except ValueError as exc:
        return f"Scheduling the wake failed ({exc}); check ops_tune_status manually."
    note = f"Scheduled a wake at ~{at.isoformat(timespec='seconds')} (job {job.id})."
    if replaced:
        # Say it moved rather than added: "Scheduled a wake" alone reads as
        # "now there are two", which is what a loop scheduling in a tight
        # sequence would have to assume.
        note += " This replaced the campaign's previous pending wake; one is pending."
    return note


def advance_look(scheduler: "WakeScheduler | None", campaign: str) -> bool:
    """Pull the campaign's pending wake to now; False when none is pending."""
    if scheduler is None:
        return False
    return bool(scheduler.advance_wake_to_now(campaign))


def cancel_look(scheduler: "WakeScheduler | None", campaign: str) -> bool:
    """Stand the campaign's pending wake down (a concluded campaign must not
    come back); False when none existed."""
    if scheduler is None:
        return False
    return bool(scheduler.cancel_wake(campaign))


def pending_look(scheduler: "WakeScheduler | None", campaign: str) -> Any | None:
    """The campaign's one pending wake, or None.

    ``pending_wakes`` narrows by prefix, so the id is re-checked for an exact
    key match ("c1" must not answer for "c10"). The footnote and the
    one-pending invariant both read through here.
    """
    if scheduler is None:
        return None
    for job in scheduler.pending_wakes(campaign):
        if job.id.endswith(f":{campaign}"):
            return job
    return None


# ── Wake message composition (fork ops.py text, verbatim) ──────────
# A wake turn is a cold start whose whole context is this message, so every
# branch names the tool that performs it -- the fork measured what happens
# when a verb has no tool behind it (re-armed to the cap, report never
# handed back).


def round_due_message(
    *,
    campaign: str,
    round_no: int,
    where: str,
    objective: str,
    ledger: str,
    max_rounds: Any,
) -> str:
    """The ops_submit wake: results should be ready, decide the next round."""
    next_round = round_no + 1
    return (
        f"[Ops campaign '{campaign}' round {round_no} due] Jobs on {where} for objective "
        f"'{objective}' should be ready. Call ops_tune_status(ledger='{ledger}') "
        f"to read results, then decide: ops_submit the next config(s) with round={next_round} "
        f"exactly (always increment the round, never reuse a previous number; max_rounds={max_rounds}) "
        f"if ready, ops_check_later if still running, ops_finish to end it and hand the result "
        f"back, or ops_ask_owner if the decision is genuinely the owner's. When a trial FAILED, "
        f"decide from what the failure text actually states: a named exception or a traceback is a "
        f"stated cause, and if that cause is a code error in the trial script, do NOT rewrite the "
        f"trial code yourself -- triage it, ops_finish with outcome='failed' to hand it off. Output "
        f"that merely stops -- progress bars, library warnings, an ordinary last line, no traceback "
        f"-- states no cause at all: the job was ended from outside and the reason is not in its "
        f"artifacts, so report it as unexplained rather than naming a cause, and with budget left "
        f"resubmit before handing it back."
    )


def recheck_message(*, campaign: str, ledger: str) -> str:
    """The ops_check_later wake: woke too early, look again."""
    return (
        f"[Ops campaign '{campaign}' re-check] Call ops_tune_status(ledger='{ledger}') "
        f"again. If results are ready, decide the next round: ops_submit to run another, "
        f"ops_finish to end it and hand the result back, or ops_ask_owner if the decision "
        f"is genuinely the owner's. If still running, ops_check_later again."
    )


def after_action_message(*, campaign: str, name: str, at: str, key: str) -> str:
    """The after-action wake: the action is in the ledger, decide what it changed."""
    return (
        f"[Ops campaign '{campaign}'] You did {name!r} at {at}, recorded as {key}. "
        f"It is in the ledger, so it must not be done again -- a repeat of a harmful "
        f"action is refused, and this wake is not an instruction to retry.\n"
        f"Read the campaign with ops_tune_status and decide what the action changed: "
        f"ops_finish to report what was done and end it, ops_check_later if this "
        f"campaign is still watching for something, or ops_submit for another action "
        f"the situation now calls for."
    )


def escalation_message(*, campaign: str, trial: str, error: str, attempt: int, ledger: str) -> str:
    """The escalation wake: a trial failed and its retry budget is exhausted.

    Raised at most once per trial (the ledger's ``escalated`` flag); the fork
    fired its escalation handler from Campaign under the same flag, and this
    is that handler said as a wake -- the agent decides, and interrupting a
    person stays behind ops_ask_owner's contract guard (D4).
    """
    return (
        f"[Ops campaign '{campaign}' escalation] Trial '{trial}' FAILED and its retry "
        f"budget is exhausted (attempt {attempt} was the last allowed; error: "
        f"{error or 'unrecorded'}). This escalation is raised at most once for this trial. "
        f"Call ops_tune_status(ledger='{ledger}') to read the campaign, then decide from "
        f"what the failure text actually states: ops_submit a changed config as the next "
        f"round if the budget allows and the cause is addressable, ops_finish with "
        f"outcome='failed' to hand it back, or ops_ask_owner if the decision is genuinely "
        f"the owner's."
    )


__all__ = [
    "NO_CONTEXT_NOTE",
    "NO_SCHEDULER_NOTE",
    "advance_look",
    "after_action_message",
    "cancel_look",
    "escalation_message",
    "pending_look",
    "recheck_message",
    "round_due_message",
    "schedule_next_look",
    "wake_route",
]
