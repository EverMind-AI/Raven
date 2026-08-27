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

# How long after an unanswered blocking question to come back and look. The
# broker's own wait runs first and is not ours to set (measured 2026-08-21: five
# minutes), so the owner has roughly a quarter of an hour in total.
#
# What happens at that wake is the point, not the number. Three ways out, and the
# wake message names all three: do work that does not depend on the answer, wait
# once more, or hand back what there is. What it must not do is nothing, which is
# what happened on 2026-08-21 -- two campaigns sat untouched for three hours with
# 86% and 81% of their budgets unspent.
_ASK_WAKE_MINUTES = 10

GUARD_FILE = "interruptions.json"
REPORTS_FILE = "reports.jsonl"

# Words that make a claim relative whatever the declared condition_type. This is
# a heuristic and is deliberately narrow: it can only add refusals, never let one
# through, and the words it does not know are simply not caught. It is not a
# substitute for the declared field -- it is a second net under an easy mistake.
_COMPARATIVE = (
    "improved", "improvement", "better", "worse", "degraded", "declined", "dropped",
    "increased", "decreased", "gained", "lost", "rose", "fell", "higher", "lower",
    "outperform", "beat", "regressed", "over where it started", "than before",
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

    def __init__(self, registry: Any = None, broker: Any = None,
                 cron_service: Any = None) -> None:
        self._registry = registry
        self._broker = broker
        # Needed only on the branch below where the wait ends with no answer: the
        # turn is about to end and nothing else would bring the campaign back.
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        # Where the SAFETY WAKE is armed, which is not where the question is
        # delivered, and the two were one field until this split them.
        #
        # Delivery deliberately has no context: passing the turn's channel would
        # send a wake turn's question to "cron", which no front end subscribes to,
        # so it is left empty and the messaging tool's own default is used -- that
        # is how this tool has been reaching the operator all along.
        #
        # The wake is a different question -- which store to write into, and which
        # window owns it -- and answering it from the same empty field meant
        # ``_schedule_ops_wake`` returned "No session context to schedule a wake"
        # and armed nothing, on every ask, since the wake was written. Measured
        # 2026-08-26: a campaign asked a blocking question with all four trials
        # terminal, no wake anywhere, and sat until someone went looking. The
        # 2026-08-21 incident this wake exists for describes the same three hours;
        # the wake shipped and never once armed.
        self._wake_channel = ""
        self._wake_chat_id = ""
        self._session_key = ""

    def set_broker(self, broker: Any) -> None:
        """Late-bound, the same way ask_user gets one: the broker exists before
        the tool registry does."""
        self._broker = broker

    def set_registry(self, registry: Any) -> None:
        self._registry = registry

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel, self._chat_id = channel, chat_id

    def set_wake_context(self, channel: str, chat_id: str, session_key: str = "") -> None:
        """Where to arm the safety wake, without touching where the question goes.

        Deliberately not folded into ``set_context``: the loop calls this one and
        not that one, so delivery keeps falling to the messaging tool's default
        while the wake gets the turn's own routing and the window that owns it.
        """
        self._wake_channel, self._wake_chat_id = channel, chat_id
        self._session_key = session_key or ""

    async def _deliver(self, text: str) -> tuple[bool, str]:
        """Hand the message to the messaging tool. Returns (sent, detail)."""
        tool = self._registry.get("message") if self._registry else None
        if tool is None:
            return False, "no messaging tool is registered, so nothing could be sent"
        try:
            detail = await tool.execute(content=text, channel=self._channel or None,
                                       chat_id=self._chat_id or None)
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
            "campaign: do not use general messaging tools for it.\n"
            "blocks_progress says whether anything worth doing is left while you wait. It is "
            "false when some other branch is worth exploring under either answer -- rule out a "
            "different parameter, check a second case -- and true when the next useful step is "
            "one the answer could make wrong, which is usually anything that spends compute you "
            "may not be authorised to spend. Blocking does NOT mean sitting still: reading logs, "
            "reading the case and writing down what you found cost nothing and stay open to you. "
            "What it stops is submitting more trials."
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
                "blocks_progress": {
                    "type": "boolean",
                    "description": "True when nothing useful is left to do until this is answered; "
                                   "false when another branch is worth doing under either answer.",
                },
                "ledger": {"type": "string", "description": "Ledger path if known (locates the campaign dir)."},
            },
            "required": ["campaign", "question", "expected_loss_minutes", "blocks_progress"],
        }

    async def execute(
        self,
        campaign: str,
        question: str,
        expected_loss_minutes: int,
        blocks_progress: bool = False,
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
            blocks_progress=bool(blocks_progress),
        )
        # The returns say what happened to the message and what now follows from
        # the caller's own reading of it.
        #
        # An earlier version deliberately said nothing about silence, on the
        # grounds that handling it was the judgement being scored. That held while
        # the judgement was "what do I do if nobody answers". It is now
        # blocks_progress, declared here by the caller, and the consequence is
        # mechanical: a blocking question closes the submit path until it is
        # answered. Withholding a rule that is already enforced only means finding
        # it out by being refused.
        if not decision.allowed:
            return (
                f"NOT DELIVERED. The campaign's interruption contract refused it: {decision.reason}\n"
                "Nobody has seen this."
            )

        # Waiting for the answer, but only where waiting costs nothing. Nothing is
        # burning and there is nothing else worth doing, so holding here gets the
        # answer in seconds instead of at the end of a twenty-minute timer -- and
        # the owner, who is looking at this window, does not have to wonder
        # whether their reply landed. With a trial still running, or with another
        # branch worth exploring, holding would buy nothing and cost the work that
        # could have happened meanwhile.
        if blocks_progress and self._broker is not None and not _anything_running(cdir):
            answer = await self._broker.await_question(
                f"ops:{campaign}",
                prompt=f"[ops campaign '{campaign}'] {question}",
                default="",
            )
            if str(answer or "").strip():
                _append_owner_answer(cdir, str(answer).strip())
                log_event(cdir, "ask_owner_delivery", sent=True, detail="answered inline")
                return f"The owner answered: {str(answer).strip()[:500]}"
            log_event(cdir, "ask_owner_delivery", sent=True, detail="asked inline, no answer yet")
            # The wait is over and nothing is running, so this turn is about to end
            # with no pending wake -- and then nothing brings the campaign back.
            # Measured 2026-08-21: two campaigns asked a blocking question, waited
            # out the broker, and sat untouched for three hours with their budgets
            # 86% and 81% unspent. The old text promised "the owner's reply will
            # reach the next round"; there was no next round.
            woke = ""
            if self._cron is not None:
                from raven.agent.tools.ops import _schedule_ops_wake

                woke = _schedule_ops_wake(
                    # No set_context on this tool by design (see the registration
                    # comment in the loop), so there is no session key to own the wake.
                    self._cron, self._wake_channel, self._wake_chat_id,
                    owner=getattr(self, "_session_key", ""),
                    name=f"ops:{campaign}:after-ask",
                    message=(
                        f"[Ops campaign '{campaign}'] {_ASK_WAKE_MINUTES} minutes ago you "
                        f"asked the owner and nobody answered. A reply would have reached you "
                        f"the moment it was typed, so this wake means there was none: decide "
                        f"without them now, one of three.\n"
                        f"  ops_submit -- there is work worth doing that does not depend on "
                        f"the answer. Do that work.\n"
                        f"  ops_check_later -- you truly cannot choose the next step without "
                        f"the owner. Wait once more, briefly. If that wake also finds no "
                        f"answer, finish then: waiting a third time buys nothing.\n"
                        f"  ops_finish -- the work is done, or the answer can no longer "
                        f"change the result. Say in the report which decision was never made. "
                        f"If ops_finish refuses over the unanswered question, record with "
                        f"ops_note why the answer no longer matters, then finish.\n"
                        f"(An answer left by some other route -- another window, ops_note from "
                        f"the CLI -- would show in ops_tune_status. Worth one look, not a wait.)"
                    ),
                    eta_seconds=_ASK_WAKE_MINUTES * 60,
                )
            return (
                (woke + "\n" if woke else "") +
                "Asked, and nobody answered while you waited. Nothing is running, so nothing "
                "was lost by waiting. The question stays on the record and the campaign stays "
                "open; the owner's reply will reach the next round.\n"
                "You called this blocking, so no more trials go out until it is answered. "
                "Looking is untouched: read the logs, read the case, and write down what you "
                "find. Do not close the campaign over silence -- that is the one step that "
                "cannot be undone."
            )

        sent, detail = await self._deliver(f"[ops campaign '{campaign}'] {question}")
        log_event(cdir, "ask_owner_delivery", sent=sent, detail=detail)
        if not sent:
            # The contract allowed it and the send failed. Saying "delivered"
            # here would make every interruption reading a reading of something
            # that never happened.
            return (
                f"The contract allowed this, but it was NOT delivered: {detail}\n"
                "Nobody has seen it."
            )
        # A wake here too, on the same reasoning as the blocking branch above: the
        # turn is about to end and nothing else is guaranteed to bring the campaign
        # back. Idempotent per campaign, so a submit or check_later later in this
        # same turn simply replaces it. Measured 2026-08-21: two of the four asks
        # that day were non-blocking by default (the parameter is required and was
        # not passed), and both campaigns then sat untouched for three hours.
        # Only when nothing is running. A trial in flight already has a wake set for
        # when it should be done, and scheduling here would REPLACE it (wakes are
        # idempotent per campaign) with a sooner one -- waking to find the job still
        # going, and throwing away the eta the loop had reasoned about.
        woke = ""
        if self._cron is not None and not _anything_running(cdir):
            from raven.agent.tools.ops import _schedule_ops_wake

            woke = _schedule_ops_wake(
                self._cron, self._wake_channel, self._wake_chat_id,
                owner=getattr(self, "_session_key", ""),
                name=f"ops:{campaign}:after-ask",
                message=(
                    f"[Ops campaign '{campaign}'] {_ASK_WAKE_MINUTES} minutes ago you asked "
                    f"the owner and nobody answered. A reply would have reached you the moment "
                    f"it was typed, so this wake means there was none: decide without them "
                    f"now, one of three.\n"
                    f"  ops_submit -- there is work worth doing that does not depend on the "
                    f"answer. Do that work.\n"
                    f"  ops_check_later -- you truly cannot choose the next step without the "
                    f"owner. Wait once more, briefly. If that wake also finds no answer, "
                    f"finish then: waiting a third time buys nothing.\n"
                    f"  ops_finish -- the work is done, or the answer can no longer change "
                    f"the result. Say in the report which decision was never made."
                ),
                eta_seconds=_ASK_WAKE_MINUTES * 60,
            )
        note = "" if decision.estimated else " (recorded as an interruption you could not price)"
        follows = (
            "You called this blocking, so no more trials go out until it is answered. Looking "
            "is untouched: read the logs, read the case, and write down what you find."
            if blocks_progress else
            "You called this non-blocking, so carry on with whatever does not depend on the "
            "answer -- that was the reading that made it non-blocking."
        )
        return (
            (woke + "\n" if woke else "") +
            f"Delivered to the owner{note}. "
            "They may take a long time to reply, or never reply.\n"
            f"{follows} Do not close the campaign over silence -- that is the one step that "
            "cannot be undone."
        )


def _finish_summary(campaign: str, subject: str, outcome: str, observed: dict,
                    baseline: dict | None, narrative: str, watch: str = "") -> str:
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
    if watch:
        lines.append(watch)
    if narrative.strip():
        lines.append("")
        lines.append(narrative.strip())
    return "\n".join(lines)


def _watch_kept(cdir) -> str:
    """How many times this campaign looked, and over how long. "" if unknown.

    Read from the trail and attached rather than asked of the loop, for the same
    reason the trail exists: it is the one part of the report that cannot be
    written from memory after a cold start. It also separates two endings the
    report gate cannot -- a watch whose correct outcome was to stay silent, and
    one that never looked, file identical reports otherwise (20 of SentinelBench's
    100 tasks are of the first kind).

    A count, no verdict. Whether twelve looks over four hours was attentive
    depends on what was being watched, and that is the reader's call.
    """
    from raven.ops.attendance import attendance

    kept = attendance(cdir)
    if not kept.looks and not kept.wakes:
        return ""
    parts = [f"{kept.looks} look{'' if kept.looks == 1 else 's'}"]
    if kept.minutes_open is not None:
        hours, minutes = divmod(int(kept.minutes_open), 60)
        parts.append(f"over {hours}h {minutes}m" if hours else f"over {minutes}m")
    if kept.wakes:
        parts.append(f"{kept.wakes} wake{'' if kept.wakes == 1 else 's'} arranged")
    return "watch kept: " + ", ".join(parts) + " (from the campaign's own record)"


def blocking_question_open(cdir) -> str:
    """An unanswered question the loop itself marked as blocking, or "".

    Public because ops_submit needs it: the refusal there rests on the loop's own
    reading, not on ours -- it said nothing worth doing was left, so spending
    compute contradicts it.
    """
    import json as _j

    path = cdir / "events.jsonl"
    if not path.exists():
        return ""
    asked_at, question = "", ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = _j.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "ask_owner" and e.get("allowed") is not False:
            if e.get("blocks_progress"):
                asked_at, question = str(e.get("ts") or ""), str(e.get("question") or "a question")
            else:
                asked_at, question = "", ""
    if not question:
        return ""
    notes = cdir / "notes.jsonl"
    if notes.exists():
        for line in notes.read_text(encoding="utf-8").splitlines():
            try:
                n = _j.loads(line)
            except ValueError:
                continue
            if str(n.get("ts") or "") >= asked_at:
                return ""
    return question


def _anything_running(cdir) -> bool:
    """Whether a trial of this campaign is still burning machine time."""
    import json as _j

    try:
        led = _j.loads((cdir / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    terminal = {"succeeded", "failed", "cancelled"}
    return any(str(r.get("status")) not in terminal
               for r in (led.get("records") or {}).values())


def _append_owner_answer(cdir, answer: str) -> None:
    """An answer given inline is the same fact as one typed into the chat, and
    has to land in the same place -- the next wake reads notes, not this turn."""
    from raven.agent.tools.ops import _append_note

    _append_note(cdir, answer[:2000], source="owner")


def _unanswered_question(cdir) -> str:
    """The question the owner has not come back on, or "".

    Read from the events, which is what a grader and a later turn read anyway; a
    second place to keep this in sync is a second place for it to be wrong. An
    owner's note closes it, and so does one the loop writes to say the question
    no longer matters -- both are a statement on the record that the answer is no
    longer being waited for.
    """
    import json as _j

    path = cdir / "events.jsonl"
    if not path.exists():
        return ""
    asked_at, question = "", ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = _j.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "ask_owner" and e.get("allowed") is not False:
            asked_at, question = str(e.get("ts") or ""), str(e.get("question") or "a question")
    if not question:
        return ""
    # Any note written after the question closes it: the owner's answer arrives
    # as one (captured from the chat), and so does the loop saying the answer no
    # longer matters. Timestamps rather than a flag, because notes.jsonl is where
    # both already land.
    notes = cdir / "notes.jsonl"
    if notes.exists():
        for line in notes.read_text(encoding="utf-8").splitlines():
            try:
                n = _j.loads(line)
            except ValueError:
                continue
            if str(n.get("ts") or "") >= asked_at:
                return ""
    return question


def _write_report_md(cdir, campaign: str, subject: str, outcome: str,
                     observed: dict, baseline: dict | None, narrative: str,
                     watch: str = "") -> "Path | None":
    """The report as a file, next to the campaign. Returns its path, or None.

    A campaign's result lived in reports.jsonl and in one delivered message.
    Both are fine for reading once; neither survives what happens next -- the
    owner comes back a day later and wants to ask about it, and a wake turn is a
    cold start with no memory of having written it. A file is what both of them
    can open.

    Nothing about the shape is decided here. The four things the loop already
    filed are laid out in order and the prose is passed through as it was
    written, so a single number, a table of twenty rows and a link to a plot are
    all just what the narrative happens to contain (2026-08-18: a sweep of blade
    angles wants a table, a tuning run wants one number, and picking a layout for
    them here would only get in the way of the third thing nobody has asked for
    yet).
    """
    from datetime import datetime as _dt

    try:
        stamp = _dt.now().strftime("%Y%m%dT%H%M%S")
        path = cdir / f"report-{stamp}.md"
        lines = [f"# {subject}", "",
                 f"- campaign: `{campaign}`",
                 f"- outcome: **{outcome}**",
                 f"- filed: {_dt.now().isoformat(timespec='seconds')}"]
        if watch:
            lines.append(f"- {watch}")
        lines.append("")
        if observed:
            lines += ["## Observed", ""]
            lines += [f"- {k}: {v}" for k, v in observed.items()]
            lines.append("")
        if baseline:
            lines += ["## Started at", ""]
            lines += [f"- {k}: {v}" for k, v in baseline.items()]
            lines.append("")
        if narrative.strip():
            lines += ["## Report", "", narrative.strip(), ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except OSError:
        # A report that cannot be written to disk is still filed in the ledger and
        # still delivered; losing the copy must not lose the campaign's ending.
        return None


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
            detail = await tool.execute(content=text, channel=self._channel or None,
                                        chat_id=self._chat_id or None)
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
            log_event(cdir, "report_refused", reason="relative_prose_declared_absolute",
                      dedupe_key=dedupe_key, words=comparative)
            return (
                f"REFUSED: declared absolute, but the text compares against a starting point "
                f"({', '.join(sorted(set(comparative)))}). Either set condition_type='relative' "
                "and give the starting value in baseline, or state the claim without the comparison."
            )
        state = check_state_claims(narrative, read_facts(cdir))
        if state.contradicted:
            # All three counts, not just the refusing one: a bare "contradicted=2"
            # reads as a complete audit of the narrative, and it is not one.
            log_event(cdir, "report_refused", reason="state_claim_contradicted",
                      dedupe_key=dedupe_key, contradicted=state.contradicted,
                      unresolved=state.unresolved, unverifiable=state.unverifiable,
                      claim_counts=state.counts())
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

        # Closing is the one irreversible step here: the ledger shuts, the wakes
        # are cleared, the budget stops. A question the owner has not answered yet
        # is the strongest reason not to take it. Measured 2026-08-14: an arm
        # asked twice, heard nothing, and concluded 'done' with 84% of the budget
        # unspent and its objective never measured -- neither waited nor
        # continued, just quit. Waiting costs nothing; the campaign sits open and
        # one sentence restarts it.
        unanswered = _unanswered_question(cdir)
        if unanswered:
            log_event(cdir, "report_refused", reason="unanswered_question", dedupe_key=dedupe_key)
            return (
                "REFUSED: you asked the owner something and have not heard back:\n"
                f"  {unanswered[:300]}\n"
                "Closing now ends the campaign for good -- the ledger shuts, the wakes are "
                "cleared, and what is left of the budget goes with them. Waiting costs nothing "
                "by comparison: the campaign stays open and one sentence from the owner starts "
                "it again.\n"
                "If the answer no longer changes anything, say so with ops_note and finish "
                "again. Otherwise keep going on whatever does not depend on it, or wait."
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
            return (
                f"REFUSED: {', '.join(placeholders)} is present but is not a measured value. "
                f"Fix and send again."
            )

        # A starting value the campaign already stated to the loop can be checked
        # for having been copied across. That is transcription, not the
        # comparison being scored, and tells the loop nothing it was not given --
        # see baseline_mismatches for why configuring this with a value the loop
        # was NOT given would leak it through the refusal text.
        expected = _expected_baseline(cdir)
        if expected:
            wrong = baseline_mismatches(report, expected)
            if wrong:
                log_event(cdir, "report_refused", reason="baseline_mismatch",
                          missing=["baseline"], dedupe_key=dedupe_key)
                return "REFUSED: " + "; ".join(wrong) + ".\nFix and send again."

        self._append(cdir, report)
        log_event(cdir, "report_accepted", dedupe_key=dedupe_key, report_kind=_REPORT_KIND[outcome],
                  subject=subject, outcome=outcome)

        # The closing half, in the same call. Nothing here is a judgement: the
        # report just said the campaign ended, so the marker goes down and the
        # pending wakes stand down. Leaving it as a second call cost a whole
        # campaign on 2026-08-12 -- one arm made it, the other did not, and the
        # re-arm went on waking a loop that had already finished and said so.
        import json as _json
        from datetime import datetime as _dt

        (cdir / "concluded.json").write_text(
            _json.dumps({"concluded_at": _dt.now().isoformat(timespec="seconds"),
                         "outcome": outcome, "reason": subject}, ensure_ascii=False),
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
        # Counted before the report is written, so the figure in the report and
        # the figure in the trail are the same reading.
        watch = _watch_kept(cdir)
        log_event(cdir, "concluded", reason=subject, outcome=outcome, wakes_removed=removed,
                  watch_kept=watch)

        # Put it in front of a person. This is the one message that has already
        # been checked -- condition_type, the state-claim check, missing_fields
        # and unmeasured_fields all ran above -- so sending it introduces no
        # unverified content. Sent AFTER the close, and its failure is reported
        # rather than raised: a finished campaign whose announcement did not go
        # out is still finished.
        md = _write_report_md(cdir, campaign, subject, outcome, observed, baseline, narrative, watch)
        summary = _finish_summary(campaign, subject, outcome, observed, baseline, narrative, watch)
        if md is not None:
            summary += f"\n\nWritten to {md}"
        sent, detail = await self._deliver(summary)
        log_event(cdir, "report_delivery", sent=sent, detail=detail[:200], dedupe_key=dedupe_key)
        delivery_note = (
            "" if sent
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
