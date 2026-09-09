"""Campaign metrics: the eval plan's efficiency/reliability numbers, computed
from a campaign's durable trail (events.jsonl + ledger.json).

These are the raw ingredients for the on-call eval report -- wake overhead,
event-vs-timer wake mix, decision sequence, trial outcomes -- always reported as
a paired difference against a baseline run, never as absolute scores.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from raven.ops.instrument import read_events
from raven.ops.ledger import Ledger


def campaign_metrics(campaign_dir: str | Path) -> dict[str, Any]:
    """Summarize one campaign's trail. Missing files yield zeros, not errors."""
    d = Path(campaign_dir).expanduser()
    events = read_events(d)
    kinds = Counter(e.get("kind") for e in events)

    trials: Counter = Counter()
    ledger_path = d / "ledger.json"
    if ledger_path.exists():
        for rec in Ledger(ledger_path).all():
            trials[rec.status.value] += 1

    rounds = [e.get("round") for e in events if e.get("kind") == "submit" and isinstance(e.get("round"), int)]
    timestamps = [e.get("ts") for e in events if e.get("ts")]

    # Cost family: attributed per wake turn by the wake handler. Never read these
    # apart from the outcome fields below -- a loop that gives up spends almost no
    # tokens, which looks like peak efficiency and is actually nothing done. Pair
    # cost with succeeded trials (or with the campaign's own success criterion).
    #
    # Two calibers, and mixing them is the failure this shape exists to prevent.
    # ``prompt_tokens`` and friends are each wake's LAST model call: that is what
    # the field has always held and what published numbers came from. ``total_*``
    # is the sum over a wake's calls, and only a run recorded after the fix has
    # it. A campaign with any wake missing the totals gets ``None`` for the
    # derived per-trial figure rather than a number summed over a partial set.
    wake_turns = [e for e in events if e.get("kind") == "wake_turn"]

    def _tokens(field: str) -> int:
        return sum(int(e.get(field, 0) or 0) for e in wake_turns)

    wakes_with_totals = [e for e in wake_turns if e.get("usage_calls") is not None]
    totals_complete = bool(wake_turns) and len(wakes_with_totals) == len(wake_turns)

    def _totals(field: str) -> int | None:
        return sum(int(e.get(field, 0) or 0) for e in wakes_with_totals) if totals_complete else None

    summed_tokens = (
        None
        if not totals_complete
        else _totals("total_prompt_tokens") + _totals("total_completion_tokens")  # type: ignore[operator]
    )

    return {
        "trials": dict(trials),
        "trials_total": sum(trials.values()),
        "trials_succeeded": trials.get("succeeded", 0),
        "rounds": (max(rounds) + 1) if rounds else 0,
        "submits": kinds.get("submit", 0),
        "wakes_scheduled": kinds.get("wake_scheduled", 0),
        "checks_rescheduled": kinds.get("check_later", 0),
        "event_wake_advances": kinds.get("event_wake_advanced", 0),
        "kills": kinds.get("kill", 0),
        "notes": kinds.get("note", 0),
        "concluded": kinds.get("concluded", 0) > 0 or (d / "concluded.json").exists(),
        "wake_turns": len(wake_turns),
        # Each wake's LAST model call, summed over wakes. Unchanged meaning and
        # unchanged values: published numbers came from these three.
        "prompt_tokens": _tokens("prompt_tokens"),
        "completion_tokens": _tokens("completion_tokens"),
        "total_tokens": _tokens("total_tokens"),
        # The turn spend, summed at the provider boundary. None unless every wake
        # in this campaign recorded it -- a partial sum over the wakes that happen
        # to have it is not a campaign total.
        "total_prompt_tokens": _totals("total_prompt_tokens"),
        "total_completion_tokens": _totals("total_completion_tokens"),
        "total_tokens_summed": summed_tokens,
        "usage_calls": _totals("usage_calls"),
        "wakes_with_summed_usage": len(wakes_with_totals),
        "wakes_total": len(wake_turns),
        # Refused rather than approximated. Computed off the last-call caliber it
        # under-reports by roughly the call count while reading like a price, and
        # that is the number this campaign's cost claims were built on.
        "tokens_per_succeeded_trial": (
            summed_tokens / trials["succeeded"] if summed_tokens is not None and trials.get("succeeded") else None
        ),
        "tokens_per_succeeded_trial_unavailable_reason": (
            None
            if summed_tokens is not None and trials.get("succeeded")
            else (
                f"summed usage on {len(wakes_with_totals)}/{len(wake_turns)} wake turns"
                if not totals_complete
                else "no succeeded trial"
            )
        ),
        "first_event_ts": timestamps[0] if timestamps else None,
        "last_event_ts": timestamps[-1] if timestamps else None,
    }
