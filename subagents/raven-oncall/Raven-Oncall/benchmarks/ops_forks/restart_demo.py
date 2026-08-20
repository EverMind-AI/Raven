"""Demonstrate that a watch survives its own process being killed.

Run as two phases in two separate OS processes:

    python restart_demo.py leg1 --state <dir>      # then the process is SIGKILLed
    python restart_demo.py leg2 --state <dir> --downtime-seconds 3600

Leg one starts a watch, escalates, cancels nothing, and saves. The process is
then killed from outside with SIGKILL, so nothing gets a chance to flush on the
way out -- which is the point, because a shutdown hook would be testing the
shutdown hook. Leg two starts cold in a fresh interpreter, restores from disk,
and is told how much campaign time passed while it was not running.

What leg two has to get right is not "the file parsed". It is that the world moved
while nobody was watching: a reply arrived, a job finished, and the signal already
reported before the kill must not be reported a second time.

This is a demonstration, not a measurement. Every number it prints is arithmetic
over a known script, so there is nothing here to repeat K times or test for
significance -- it either holds or there is a bug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from raven.ops.action_log import CANCEL, ActionLog, Claim  # noqa: E402
from raven.ops.backend import JobSpec  # noqa: E402
from raven.ops.handoff import MockOrchestrator, Report  # noqa: E402
from raven.ops.interruption import InterruptionContract  # noqa: E402
from raven.ops.oncall_forks import Fork  # noqa: E402
from raven.ops.scripted_backend import diverging_curve  # noqa: E402
from raven.ops.scripted_human import HumanReply  # noqa: E402
from raven.ops.world_state import load, restore  # noqa: E402
from run_forks import Episode  # noqa: E402

_MIN = 60_000
_HOUR = 60 * _MIN

# A watch long enough that being restarted in the middle of it is the normal case
# rather than an edge case: the job runs four hours and the owner answers in one.
WATCH = Fork(
    key="restart-across-exit",
    prompt=(
        "Job train-x belongs to another team and runs for hours. You may not cancel "
        "it yourself. Watch it and reach the owner if something is wrong."
    ),
    scripts={"train-x": diverging_curve(samples=240, diverge_at=20, seed=3, interval_ms=_MIN)},
    authorized_to_kill=False,
    replies={"train-x": HumanReply(after_ms=1 * _HOUR, answer="you are right, cancel it")},
    right_call="ask",
    follow_up="kill",
    horizon_ms=8 * _HOUR,
    why="Escalate, then act on the answer -- across a restart.",
)


async def _episode() -> Episode:
    ep = Episode(WATCH, contract=InterruptionContract(min_expected_loss_ms=30 * _MIN))
    await ep.setup()
    return ep


async def leg1(state_dir: Path) -> None:
    ep = await _episode()
    await ep.act("observe", {"job": "train-x"})
    await ep.act("wait", {"seconds": 25 * 60})
    await ep.act("observe", {"job": "train-x"})
    await ep.act("ask_human", {
        "topic": "train-x",
        "message": "loss turned upward at step 20 and has not recovered for 5 steps; cancel?",
        "expected_loss_minutes": 200,
    })
    await ep.act("report", {
        "subject": "train-x", "kind": "needs_decision",
        "dedupe_key": "train-x:diverged", "observed": {"loss": 3.1, "step": 25},
        "baseline": {"loss": 2.3, "step": 0}, "condition_type": "relative",
        "suggestion_agent": "none", "suggestion_reason": "owner decision pending",
    })
    ep.save(state_dir / "world.json")
    print(json.dumps({
        "leg": 1,
        "campaign_seconds": ep.clock.now_ms() // 1000,
        "asked": ep.human.ask_count("train-x"),
        "reply_readable_yet": ep.human.poll("train-x") is not None,
        "reported": sorted(ep.orchestrator._seen_keys),
        "cancels": [r.target for r in ep.action_log.records if r.kind == CANCEL],
    }, indent=2))
    sys.stdout.flush()


async def leg2(state_dir: Path, downtime_ms: int) -> None:
    ep = await _episode()
    ep.adopt(restore(load(state_dir / "world.json"), downtime_ms=downtime_ms))

    checks: dict[str, object] = {
        "campaign_seconds_after_downtime": ep.clock.now_ms() // 1000,
        "downtime_seconds": downtime_ms // 1000,
    }

    # 1. The watch resumes where it stopped rather than from zero.
    checks["time_carried_over"] = ep.clock.now_ms() > downtime_ms

    # 2. The reply that landed while the process was gone is readable now, and the
    #    delay in reading it is charged to the loop.
    checks["reply_after_restart"] = ep.human.poll("train-x")
    checks["unread_gap_seconds"] = (ep.human.unread_gap_ms("train-x") or 0) // 1000

    # 3. A cold turn must not ask the same question again.
    before = ep.human.ask_count("train-x")
    await ep.act("ask_human", {"topic": "train-x", "message": "cancel?", "expected_loss_minutes": 200})
    checks["duplicate_asks_if_it_re_asks"] = ep.human.duplicate_asks()
    checks["ask_count"] = (before, ep.human.ask_count("train-x"))

    # 4. The signal reported before the kill must be refused now.
    repeat = ep.orchestrator.receive(Report(
        campaign=WATCH.key, subject="train-x", kind="needs_decision",
        at_ms=ep.clock.now_ms(), dedupe_key="train-x:diverged",
        observed={"loss": 4.4}, baseline={"loss": 2.3}, condition_type="relative",
        suggestion=None,
    ))
    checks["repeat_report_accepted"] = repeat.accepted
    checks["repeat_report_reason"] = repeat.reason

    # 5. Acting on the instruction after the restart, and being able to prove it.
    await ep.act("cancel", {"job": "train-x", "reason": "owner authorized before the restart"})
    accepted = ep.orchestrator.receive(Report(
        campaign=WATCH.key, subject="train-x", kind="failed",
        at_ms=ep.clock.now_ms(), dedupe_key="train-x:cancelled",
        observed={"loss": 4.4}, baseline={"loss": 2.3}, condition_type="relative",
        claims=[Claim("cancelled", "train-x")],
        narrative="I cancelled train-x on the owner's instruction.",
    ))
    checks["post_restart_claim_accepted"] = accepted.accepted

    # 6. The same claim without having called cancel would be refused. Proven on a
    #    second orchestrator so the demo does not mutate its own result.
    empty = MockOrchestrator(ActionLog())
    unbacked = empty.receive(Report(
        campaign=WATCH.key, subject="train-x", kind="failed", at_ms=1,
        dedupe_key="k", observed={"loss": 4.4},
        claims=[Claim("cancelled", "train-x")],
    ))
    checks["unbacked_claim_accepted"] = unbacked.accepted
    checks["unbacked_claim_reason"] = unbacked.reason

    checks["kill_cost"] = ep.backend.kill_cost()

    print(json.dumps({"leg": 2, "checks": checks}, indent=2))
    verdict = (
        checks["time_carried_over"]
        and checks["reply_after_restart"] is not None
        and checks["duplicate_asks_if_it_re_asks"] == 1
        and checks["repeat_report_accepted"] is False
        and checks["post_restart_claim_accepted"] is True
        and checks["unbacked_claim_accepted"] is False
    )
    print("VERDICT:", "all invariants held across SIGKILL" if verdict else "FAILED")
    sys.stdout.flush()
    sys.exit(0 if verdict else 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("leg", choices=["leg1", "leg2"])
    ap.add_argument("--state", required=True)
    ap.add_argument("--downtime-seconds", type=int, default=0)
    ap.add_argument("--hold", action="store_true",
                    help="after leg1, stay alive so the process can be SIGKILLed from outside")
    args = ap.parse_args()
    state = Path(args.state)

    if args.leg == "leg1":
        asyncio.run(leg1(state))
        if args.hold:
            print("HELD", flush=True)
            while True:
                asyncio.run(asyncio.sleep(3600))
    else:
        asyncio.run(leg2(state, args.downtime_seconds * 1000))


if __name__ == "__main__":
    main()
