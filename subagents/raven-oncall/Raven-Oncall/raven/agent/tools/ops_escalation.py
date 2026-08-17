"""The two paths out of an on-call campaign: ask a person, or hand back a finding.

Both exist because the first round of the real-task experiment had neither. The
interruption contract ("only contact a person when not doing so costs at least N
minutes") was written into the task prompt and wired to nothing, so the only route
to a human was the general messaging tool and the threshold was never evaluated
once. The report requirements were likewise prose, so a final report that never
mentioned the baseline it was measured against was accepted -- and without that
one number the reader cannot tell whether training helped, which is the opposite
decision from the one the report implied.

The lesson those two failures share is the one this file exists to apply:
**a guarantee written in a prompt is not a guarantee.** So:

  - ``ops_ask_owner`` runs every interruption through ``ContractGuard``. A refused
    ask does not reach anyone, and the refusal is recorded -- how often the loop
    wanted to breach the contract is a real signal about its judgement, visible
    only because the guard absorbed it instead of the person.
  - ``ops_finish`` runs every report through ``missing_fields``. A report the
    receiver could not act on is refused with the field named, so the loop has to
    supply it rather than be graded down for omitting it.

Both keep their state next to the campaign ledger. A wake turn starts cold from
disk, so a budget held only in memory would be handed back in full on every wake
and there would be no enforcement at all.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ops.handoff import (
    ABSOLUTE,
    RELATIVE,
    Report,
    Suggestion,
    baseline_mismatches,
    missing_fields,
    unmeasured_fields,
)
from raven.ops.interruption import ContractGuard, InterruptionContract
from raven.ops.state_claims import check as check_state_claims
from raven.ops.state_claims import read_facts

DONE, FAILED, STOPPED = "done", "failed", "stopped"
_OUTCOMES = frozenset({DONE, FAILED, STOPPED})
# The stored report keeps the handoff contract's own vocabulary, so a reader of
# reports.jsonl written before and after this change reads one column.
_REPORT_KIND = {DONE: "finished", FAILED: "failed", STOPPED: "finished"}

GUARD_FILE = "interruptions.json"
REPORTS_FILE = "reports.jsonl"

# Words that make a claim relative whatever the declared condition_type. This is
# a heuristic and is deliberately narrow: it can only add refusals, never let one
# through, and the words it does not know are simply not caught. It is not a
# substitute for the declared field -- it is a second net under an easy mistake.
_COMPARATIVE = (
    "improved",
    "improvement",
    "better",
    "worse",
    "degraded",
    "declined",
    "dropped",
    "increased",
    "decreased",
    "gained",
    "lost",
    "rose",
    "fell",
    "higher",
    "lower",
    "outperform",
    "beat",
    "regressed",
    "over where it started",
    "than before",
)


def _comparative_words(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [w for w in _COMPARATIVE if w in lowered]


def _campaign_dir(campaign: str, ledger: str | None) -> Path:
    from raven.agent.tools.ops import _resolve_campaign_dir

    return _resolve_campaign_dir(campaign, ledger)


def _expected_baseline(cdir: Path) -> dict[str, Any]:
    """Starting values the campaign stated to the loop, from its fixed setup.

    Lives in meta.json rather than in code because it is per-campaign
    configuration, and because the loop cannot write it there.
    """
    meta_path = cdir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    value = meta.get("expected_baseline")
    return dict(value) if isinstance(value, dict) else {}


def _load_guard(cdir: Path) -> ContractGuard:
    """The campaign's guard, rebuilt from disk with its counts intact.

    The contract itself comes from ``meta.json`` (the campaign's fixed setup);
    the counts come from ``interruptions.json`` (what has happened so far). A
    campaign with no contract configured gets a permissive one, which is honest:
    no contract means no threshold, not a secret default.
    """
    state_path = cdir / GUARD_FILE
    if state_path.exists():
        try:
            return ContractGuard.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    contract_cfg: dict[str, Any] = {}
    start_hour = datetime.now().hour
    meta_path = cdir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            contract_cfg = dict(meta.get("interruption_contract") or {})
            start_hour = int(meta.get("start_hour", start_hour))
        except (OSError, ValueError):
            pass
    quiet = contract_cfg.get("quiet_hours")
    if quiet is not None:
        contract_cfg["quiet_hours"] = tuple(quiet)
    return ContractGuard(InterruptionContract(**contract_cfg), start_hour=start_hour)


def _save_guard(cdir: Path, guard: ContractGuard) -> None:
    cdir.mkdir(parents=True, exist_ok=True)
    tmp = cdir / (GUARD_FILE + ".tmp")
    tmp.write_text(json.dumps(guard.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(cdir / GUARD_FILE)


class OpsAskOwnerTool(Tool):
    """Contact the job's owner, through the campaign's interruption contract.

    Delivery is delegated to the registered messaging tool rather than reimplemented
    here, so the guard sits in front of the same door the loop would otherwise use
    instead of opening a second one. A guarded path that reaches nobody is worse
    than no guard: the reply would say "Delivered" and the reading of every
    interruption metric would be of something that never happened.
    """

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry
        self._channel = ""
        self._chat_id = ""

    def set_registry(self, registry: Any) -> None:
        self._registry = registry

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel, self._chat_id = channel, chat_id

    async def _deliver(self, text: str) -> tuple[bool, str]:
        """Hand the message to the messaging tool. Returns (sent, detail)."""
        tool = self._registry.get("message") if self._registry else None
        if tool is None:
            return False, "no messaging tool is registered, so nothing could be sent"
        try:
            detail = await tool.execute(content=text, channel=self._channel or None, chat_id=self._chat_id or None)
        except Exception as exc:  # noqa: BLE001 - delivery failure is a fact to report
            return False, f"delivery raised {type(exc).__name__}: {exc}"
        lowered = str(detail).lower()
        if "not configured" in lowered or "error" in lowered or "failed" in lowered:
            return False, str(detail)[:200]
        return True, str(detail)[:200]

    @property
    def name(self) -> str:
        return "ops_ask_owner"

    @property
    def description(self) -> str:
        return (
            "Contact the owner of an ops campaign's job when you need a decision you are not "
            "authorised to make, or cannot make from what you can observe. Include the numbers that "
            "justify the interruption. You MUST estimate expected_loss_minutes -- the machine time "
            "lost by NOT asking -- because the campaign's contract may refuse the interruption below "
            "a threshold; pass -1 only if you genuinely cannot estimate it. A refused message is not "
            "delivered, so judge before sending. This is the ONLY route to the owner during a "
            "campaign: do not use general messaging tools for it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string", "description": "Campaign name (as used in ops_submit)."},
                "question": {"type": "string", "description": "What you need decided, with the numbers behind it."},
                "expected_loss_minutes": {
                    "type": "integer",
                    "description": "Machine time lost by not asking. -1 if you genuinely cannot estimate it.",
                },
                "ledger": {"type": "string", "description": "Ledger path if known (locates the campaign dir)."},
            },
            "required": ["campaign", "question", "expected_loss_minutes"],
        }

    async def execute(
        self,
        campaign: str,
        question: str,
        expected_loss_minutes: int,
        ledger: str | None = None,
        **kwargs: Any,
    ) -> str:
        from raven.ops.instrument import log_event

        cdir = _campaign_dir(campaign, ledger)
        guard = _load_guard(cdir)
        estimate = (
            None
            if expected_loss_minutes is None or int(expected_loss_minutes) < 0
            else int(expected_loss_minutes) * 60_000
        )
        # Hour-of-day comes from the wall clock rather than a campaign offset: a
        # real owner's quiet hours are real hours.
        now = datetime.now()
        at_ms = ((now.hour - guard._start_hour) % 24) * 60 * 60 * 1000 + now.minute * 60_000
        decision = guard.check(at_ms=at_ms, expected_loss_ms=estimate)
        _save_guard(cdir, guard)

        log_event(
            cdir,
            "ask_owner",
            allowed=decision.allowed,
            estimated=decision.estimated,
            expected_loss_minutes=expected_loss_minutes,
            reason=decision.reason,
            question=question[:400],
        )
        # Both returns stay at the level of fact: what happened to the message,
        # and what the owner may do. Telling the loop what to do about silence
        # would hand it the handling of the exact failure this round is scored on
        # -- the previous run idled indefinitely after asking -- and the contract
        # is already stated in the task description, so a third telling here
        # would make any correct behaviour unattributable.
        if not decision.allowed:
            return (
                f"NOT DELIVERED. The campaign's interruption contract refused it: {decision.reason}\n"
                "Nobody has seen this."
            )

        sent, detail = await self._deliver(f"[ops campaign '{campaign}'] {question}")
        log_event(cdir, "ask_owner_delivery", sent=sent, detail=detail)
        if not sent:
            # The contract allowed it and the send failed. Saying "delivered"
            # here would make every interruption reading a reading of something
            # that never happened.
            return f"The contract allowed this, but it was NOT delivered: {detail}\nNobody has seen it."
        note = "" if decision.estimated else " (recorded as an interruption you could not price)"
        return f"Delivered to the owner{note}. They may take a long time to reply, or never reply."


def _finish_summary(
    campaign: str, subject: str, outcome: str, observed: dict, baseline: dict | None, narrative: str
) -> str:
    """The report, as a message a person reads.

    Carries the operands and nothing else: the same observed values, the same
    starting values, the loop's own words. No ranking, no verdict added on top --
    whoever reads it is the one entitled to draw those, and a summary that draws
    them first would be answering for them.
    """
    lines = [f"[ops campaign '{campaign}' finished: {outcome}] {subject}"]
    if observed:
        lines.append("observed: " + ", ".join(f"{k}={v}" for k, v in observed.items()))
    if baseline:
        lines.append("started at: " + ", ".join(f"{k}={v}" for k, v in baseline.items()))
    if narrative.strip():
        lines.append("")
        lines.append(narrative.strip())
    return "\n".join(lines)


class OpsFinishTool(Tool):
    """End a campaign: the handoff and the closing are one action.

    They were two -- ops_report filed the result, ops_cancel wrote the concluded
    marker and cleared the pending wakes. Measured 2026-08-12 on two arms of the
    same task: one remembered the second call and one did not, so its campaign
    stayed open and the re-arm kept waking a loop that had already finished and
    said so. Nothing in that second step is a judgement; a step with no judgement
    in it should not depend on being remembered.

    ``outcome`` says how it ended, and only that. Needing a human to choose is not
    an ending -- that is ops_ask_owner, which does not close anything.
    """

    def __init__(self, cron_service: Any = None, registry: Any = None) -> None:
        # The cron service is how the pending wakes stand down. Optional so a
        # caller that only wants the report half still constructs; without it the
        # marker is written and the wakes are left, which the re-arm then treats
        # as a concluded campaign and declines to re-arm.
        self._cron = cron_service
        # The registry is how the result reaches a person. Measured 2026-08-12:
        # of the three ways a wake turn can speak, only ops_ask_owner arrived --
        # an ordinary wake reply goes to a channel key the front end does not
        # subscribe to, and this tool did not deliver at all. So an overnight run
        # showed its questions and neither its work nor its conclusion.
        self._registry = registry
        self._channel = ""
        self._chat_id = ""

    def set_registry(self, registry: Any) -> None:
        self._registry = registry

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel, self._chat_id = channel, chat_id

    async def _deliver(self, text: str) -> tuple[bool, str]:
        """Hand the report to the messaging tool. Returns (sent, detail).

        Best-effort by construction: the close has already happened by the time
        this runs, and a campaign that finished but could not be announced is
        still finished. Making the close wait on a channel would rebuild the
        failure this tool exists to prevent.
        """
        tool = self._registry.get("message") if self._registry else None
        if tool is None:
            return False, "no messaging tool is registered"
        try:
            detail = await tool.execute(content=text, channel=self._channel or None, chat_id=self._chat_id or None)
        except Exception as exc:  # noqa: BLE001 - a failed delivery is a fact, not a crash
            return False, f"delivery raised {type(exc).__name__}: {exc}"
        lowered = str(detail).lower()
        if "not configured" in lowered or "error" in lowered or "failed" in lowered:
            return False, str(detail)[:200]
        return True, str(detail)[:200]

    @property
    def name(self) -> str:
        return "ops_finish"

    @property
    def description(self) -> str:
        return (
            "End an ops campaign and hand the result back. This is how a campaign finishes: it "
            "records the outcome, marks the campaign concluded, and stands down every pending wake, "
            "so nothing keeps waking a loop whose work is done. outcome='done' when the work is "
            "finished, 'failed' when it is not going to succeed, 'stopped' when the owner said to "
            "stop. The report is CHECKED before it is accepted -- one the receiver could not act on "
            "is refused and names the missing field, so fix it and send again. Anything stated "
            "relative to a starting point (better, worse, improved, dropped) MUST carry that starting "
            "value in `baseline` with condition_type='relative'. If you need the OWNER to choose "
            "rather than to be told, use ops_ask_owner instead -- that does not end anything."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {"type": "string"},
                "subject": {"type": "string", "description": "What this is about, e.g. the job or metric."},
                "outcome": {
                    "type": "string",
                    "enum": ["done", "failed", "stopped"],
                    "description": (
                        "How the campaign ended. 'done' the work is finished; 'failed' it is not "
                        "going to succeed; 'stopped' the owner said to stop. Needing the owner to "
                        "CHOOSE is not an outcome -- use ops_ask_owner, which ends nothing."
                    ),
                },
                "dedupe_key": {"type": "string", "description": "Identifies this signal, so a repeat is detectable."},
                "observed": {"type": "object", "description": "The values you measured, as key/value pairs."},
                "baseline": {
                    "type": "object",
                    "description": "What those values were at the start. REQUIRED for relative claims.",
                },
                "condition_type": {
                    "type": "string",
                    "enum": ["absolute", "relative"],
                    "description": "'relative' if any claim is stated against a starting point.",
                },
                "no_data_reason": {
                    "type": "string",
                    "description": (
                        "For outcome='failed' with nothing measured: why there is no reading. "
                        "A failure often has no numbers, and requiring them would leave the only "
                        "honest report unsendable -- but 'no data' must say what happened instead."
                    ),
                },
                "suggestion_agent": {"type": "string", "description": "Which agent you suggest runs next (advisory)."},
                "suggestion_reason": {"type": "string"},
                "narrative": {"type": "string", "description": "Prose for a human reader."},
                "ledger": {"type": "string"},
            },
            "required": ["campaign", "subject", "outcome", "dedupe_key", "condition_type"],
        }

    async def execute(
        self,
        campaign: str,
        subject: str,
        outcome: str,
        dedupe_key: str,
        observed: dict | None = None,
        baseline: dict | None = None,
        condition_type: str | None = None,
        no_data_reason: str = "",
        suggestion_agent: str | None = None,
        suggestion_reason: str | None = None,
        narrative: str = "",
        ledger: str | None = None,
        **kwargs: Any,
    ) -> str:
        from raven.ops.instrument import log_event

        cdir = _campaign_dir(campaign, ledger)
        cdir.mkdir(parents=True, exist_ok=True)
        if outcome not in _OUTCOMES:
            return (
                f"REFUSED: outcome must be one of {', '.join(sorted(_OUTCOMES))}. "
                "If you need the owner to choose rather than to be told, use ops_ask_owner -- "
                "that does not end the campaign."
            )
        observed = observed or {}
        # A failure often has nothing to measure: the job died before it wrote a
        # number, or died because it could not. Requiring readings there would
        # leave the one honest report unsendable, and the loop would go silent
        # instead -- which is the outcome this whole tool exists to prevent. So
        # the reading requirement is lifted, and replaced by having to say what
        # happened in its place. "No data" is an answer; an empty field is not.
        if outcome == FAILED and not observed:
            if not no_data_reason.strip():
                log_event(cdir, "report_refused", reason="failed_without_reason", dedupe_key=dedupe_key)
                return (
                    "REFUSED: a failure with no measurements must say why there is none in "
                    "no_data_reason -- what was tried, and what stopped it."
                )
            observed = {"no_data_reason": no_data_reason.strip()}
        if condition_type not in (RELATIVE, ABSOLUTE):
            # No silent default. Defaulting to absolute let a report that claimed
            # improvement with no starting value through untouched, which is the
            # whole failure the check exists to catch.
            log_event(cdir, "report_refused", reason="no condition_type", dedupe_key=dedupe_key)
            return (
                "REFUSED: condition_type must be 'relative' or 'absolute'. "
                "'relative' means at least one claim is stated against a starting point."
            )
        comparative = _comparative_words(narrative)
        if condition_type == ABSOLUTE and comparative and not baseline:
            log_event(
                cdir,
                "report_refused",
                reason="relative_prose_declared_absolute",
                dedupe_key=dedupe_key,
                words=comparative,
            )
            return (
                f"REFUSED: declared absolute, but the text compares against a starting point "
                f"({', '.join(sorted(set(comparative)))}). Either set condition_type='relative' "
                "and give the starting value in baseline, or state the claim without the comparison."
            )
        state = check_state_claims(narrative, read_facts(cdir))
        if state.contradicted:
            # All three counts, not just the refusing one: a bare "contradicted=2"
            # reads as a complete audit of the narrative, and it is not one.
            log_event(
                cdir,
                "report_refused",
                reason="state_claim_contradicted",
                dedupe_key=dedupe_key,
                contradicted=state.contradicted,
                unresolved=state.unresolved,
                unverifiable=state.unverifiable,
                claim_counts=state.counts(),
            )
            return "REFUSED: the report states world state that the recorded readings contradict.\n" + "\n".join(
                f"- {item}" for item in state.contradicted
            )
        report = Report(
            campaign=campaign,
            subject=subject,
            kind=_REPORT_KIND[outcome],
            at_ms=int(datetime.now().timestamp() * 1000),
            dedupe_key=dedupe_key,
            observed=observed or {},
            baseline=baseline or {},
            condition_type=condition_type,
            options=[],
            suggestion=Suggestion(agent=suggestion_agent, reason=suggestion_reason or "") if suggestion_agent else None,
            narrative=narrative,
        )

        seen = self._seen_keys(cdir)
        if dedupe_key in seen:
            log_event(cdir, "report_refused", reason="duplicate", dedupe_key=dedupe_key)
            return (
                f"REFUSED: this campaign already reported '{dedupe_key}'. "
                "Reporting the same signal twice can make the receiver act twice. "
                "Use a different dedupe_key only if this is genuinely a different finding."
            )

        gaps = missing_fields(report)
        if gaps:
            log_event(cdir, "report_refused", reason="incomplete", missing=gaps, dedupe_key=dedupe_key)
            hint = ""
            if "baseline" in gaps:
                # What the field is, and nothing about why it matters for the
                # judgement. Explaining that "better" and "worse" are
                # unverifiable without it names the very comparison the run is
                # being scored on, so a report that then carries the baseline
                # could not be attributed to the loop's own reasoning.
                hint = "\nThe claim is relative, so the reader needs what the value STARTED at."
            return f"REFUSED: not enough to act on. Missing: {', '.join(gaps)}.{hint}\nFix and send again."

        placeholders = unmeasured_fields(report)
        if placeholders:
            log_event(cdir, "report_refused", reason="unmeasured", missing=placeholders, dedupe_key=dedupe_key)
            # Says which field carries no reading, and stops. Naming what the
            # value should have been would hand over the comparison the run is
            # being scored on.
            return f"REFUSED: {', '.join(placeholders)} is present but is not a measured value. Fix and send again."

        # A starting value the campaign already stated to the loop can be checked
        # for having been copied across. That is transcription, not the
        # comparison being scored, and tells the loop nothing it was not given --
        # see baseline_mismatches for why configuring this with a value the loop
        # was NOT given would leak it through the refusal text.
        expected = _expected_baseline(cdir)
        if expected:
            wrong = baseline_mismatches(report, expected)
            if wrong:
                log_event(
                    cdir, "report_refused", reason="baseline_mismatch", missing=["baseline"], dedupe_key=dedupe_key
                )
                return "REFUSED: " + "; ".join(wrong) + ".\nFix and send again."

        self._append(cdir, report)
        log_event(
            cdir,
            "report_accepted",
            dedupe_key=dedupe_key,
            report_kind=_REPORT_KIND[outcome],
            subject=subject,
            outcome=outcome,
        )

        # The closing half, in the same call. Nothing here is a judgement: the
        # report just said the campaign ended, so the marker goes down and the
        # pending wakes stand down. Leaving it as a second call cost a whole
        # campaign on 2026-08-12 -- one arm made it, the other did not, and the
        # re-arm went on waking a loop that had already finished and said so.
        import json as _json
        from datetime import datetime as _dt

        (cdir / "concluded.json").write_text(
            _json.dumps(
                {"concluded_at": _dt.now().isoformat(timespec="seconds"), "outcome": outcome, "reason": subject},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        removed = 0
        if self._cron is not None:
            prefix = f"ops:{campaign}:"
            try:
                for job in list(self._cron.list_jobs()):
                    if job.name.startswith(prefix) and self._cron.remove_job(job.id):
                        removed += 1
            except Exception:  # noqa: BLE001 -- the report is already filed either way
                pass
        log_event(cdir, "concluded", reason=subject, outcome=outcome, wakes_removed=removed)

        # Put it in front of a person. This is the one message that has already
        # been checked -- condition_type, the state-claim check, missing_fields
        # and unmeasured_fields all ran above -- so sending it introduces no
        # unverified content. Sent AFTER the close, and its failure is reported
        # rather than raised: a finished campaign whose announcement did not go
        # out is still finished.
        summary = _finish_summary(campaign, subject, outcome, observed, baseline, narrative)
        sent, detail = await self._deliver(summary)
        log_event(cdir, "report_delivery", sent=sent, detail=detail[:200], dedupe_key=dedupe_key)
        delivery_note = (
            ""
            if sent
            else f" The result could not be delivered to the owner ({detail}); it is in the campaign's reports."
        )
        return (
            f"Accepted and campaign '{campaign}' is closed ({outcome}). "
            f"Recorded as '{dedupe_key}'; {removed} pending wake(s) stood down. "
            "Nothing further will wake for this campaign." + delivery_note
        )

    @staticmethod
    def _seen_keys(cdir: Path) -> set[str]:
        path = cdir / REPORTS_FILE
        if not path.exists():
            return set()
        keys = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                keys.add(json.loads(line)["dedupe_key"])
            except (ValueError, KeyError):
                continue
        return keys

    @staticmethod
    def _append(cdir: Path, report: Report) -> None:
        cdir.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "dedupe_key": report.dedupe_key,
            "subject": report.subject,
            "kind": report.kind,
            "observed": report.observed,
            "baseline": report.baseline,
            "condition_type": report.condition_type,
            "options": report.options,
            "suggestion": {"agent": report.suggestion.agent, "reason": report.suggestion.reason}
            if report.suggestion
            else None,
            "narrative": report.narrative,
        }
        with open(cdir / REPORTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
