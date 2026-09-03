"""What the job actually used, against what was asked for.

A config key can be written, echoed back, and still do nothing. Measured
2026-08-13 on the CFD divergence leg: the trial passed ``deltaT`` together with
``maxCo=0.5`` and ``maxAlphaCo=0.5``; the run script sets ``adjustTimeStep no``
whenever a ``deltaT`` arrives, and under fixed stepping the two Courant ceilings
take no part in the calculation. The job log's opening line echoed all three, so
the arm believed it had capped the Courant number at 0.5, reported a spot reading
as the run maximum, and delivered a conclusion built on a limit that never held.
The true maximum was 2.81, over 1 for 716 of 10000 steps.

The same shape had already been seen with keys nobody reads at all (``cores``,
``cells``, ``water_nu``, 2026-08-12) and with a partial submit silently falling
back to a job script's defaults -- including which dataset was evaluated
(2026-08-13, the ML line).

Nothing here interprets a key. Two dictionaries are compared and the differences
are named; whether a key mattered is the job's business, and a job that knows one
of its inputs is inert says so itself in ``ignored``. That split is deliberate:
the comparison is the same in every domain, and the semantics never are.
"""

from __future__ import annotations

from typing import Any

# Bookkeeping the caller adds to every payload; a diff that reported them would
# cry wolf on every trial.
_INJECTED = {
    "budget_gpu_minutes",
    "run_dir",
    "job_dir",
    # A counter that exists to vary the idem_key so a repeat is a new
    # trial. No job reads it, and reporting it as missing on every
    # trial would bury the differences that matter.
    "run",
}


def _same(a: Any, b: Any) -> bool:
    """Whether two config values name the same setting.

    Numbers when both read as numbers, so "1e-4" and 0.0001 are one value rather
    than a false difference; otherwise trimmed text, so 1 and "1" agree.
    """
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def compare_config(submitted: dict[str, Any] | None, effective: dict[str, Any] | None) -> list[str]:
    """One line per way the run differs from the request. Empty when they agree.

    A missing effective config is not a difference: most jobs do not write one,
    and inventing a complaint for them would train the reader to skip these lines.
    """
    if not submitted or not effective:
        return []
    out: list[str] = []
    for k, v in submitted.items():
        if k in _INJECTED:
            continue
        if k not in effective:
            out.append(f"{k}={v!r} was submitted but the run does not have it")
        elif not _same(v, effective[k]):
            out.append(f"{k}: submitted {v!r}, the run used {effective[k]!r}")
    return out


def ignored_keys(effective: dict[str, Any] | None) -> list[str]:
    """Keys the job itself says took no part, verbatim.

    Only the job can know this -- "maxCo does nothing once the time step is
    fixed" is solver semantics, not something a comparison can derive -- so this
    reads what the job reported and never reasons about it.
    """
    if not isinstance(effective, dict):
        return []
    raw = effective.get("ignored")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x) for x in raw if str(x).strip()]


def compare_against_declared(
    declared: dict[str, Any] | None, submitted: dict[str, Any] | None, effective: dict[str, Any] | None
) -> list[str]:
    """Keys the submit left out where the run disagrees with the campaign's list.

    Filling a missing key from a default is ordinary and is not reported. What is
    reported is narrower: the campaign declared a starting value, the submit did
    not repeat it, and the job's own default won instead. The agent never saw that
    default -- what it was shown is the campaign's declaration -- so it had every
    reason to believe the declared value still held.

    Measured live on M9, 2026-08-14: the campaign declared scoring on
    ``nfcorpus_dev``; the submit omitted ``eval_data``; the training script's
    default scored on a different dataset with no overlap. The number then came
    from one task and was compared against a baseline measured on another. One key
    out of eighteen, and nothing said so.
    """
    if not declared or not effective:
        return []
    submitted = submitted or {}
    out: list[str] = []
    for k, want in declared.items():
        if k in submitted or k in _INJECTED or k not in effective:
            continue
        if not _same(want, effective[k]):
            out.append(f"{k} was not submitted; this campaign declares {want!r}, the run used {effective[k]!r}")
    return out


def compare_submitted_to_declared(declared: dict[str, Any] | None, submitted: dict[str, Any] | None) -> list[str]:
    """Where this round departs from the campaign's declared start, as fact.

    The third of three pairings, and the one that was missing. The other two ask
    what the run did with what it was given -- submitted against effective, and
    declared against effective for keys the submit left out. Neither says anything
    when a round deliberately changes a declared value, because both are looking
    at the job's side of the exchange.

    Measured 2026-08-17 on the contact task. The campaign declared
    ``push: -0.002``, a prescribed downward displacement; seven rounds ran it; one
    round submitted ``+0.002``, which lifts the face instead and takes the two
    bodies out of contact entirely. That round converged in 135 seconds with no
    stiffness degradation, and was delivered as the cleanest result and the
    recommendation -- the cleanliness being the artefact of the sign. Nothing
    anywhere printed that the sign had moved, and the round's own reasoning never
    mentioned it.

    No judgement is offered: changing a declared value from round 1 on is the
    whole point of a campaign. What is stated is only that it moved, and from what
    to what, which is the part nobody had.
    """
    if not declared or not submitted:
        return []
    out: list[str] = []
    for k, want in declared.items():
        if k in _INJECTED or k not in submitted:
            continue
        if not _same(want, submitted[k]):
            out.append(f"{k}: this campaign declares {want!r}, this round submitted {submitted[k]!r}")
    return out


def describe(
    submitted: dict[str, Any] | None, effective: dict[str, Any] | None, declared: dict[str, Any] | None = None
) -> list[str]:
    """Every half, ready to print under a trial. Empty when there is nothing to say."""
    lines = compare_submitted_to_declared(declared, submitted)
    lines += compare_config(submitted, effective)
    lines += compare_against_declared(declared, submitted, effective)
    ignored = ignored_keys(effective)
    if ignored:
        why = str((effective or {}).get("ignored_reason") or "").strip()
        tail = f" ({why})" if why else ""
        lines.append("the job reports these took no part: " + ", ".join(ignored) + tail)
    return lines
