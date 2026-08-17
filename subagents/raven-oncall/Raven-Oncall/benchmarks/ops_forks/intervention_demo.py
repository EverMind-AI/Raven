"""Demonstrate that a watch unit's intervention paths actually work, before it is scored.

    python intervention_demo.py --meta ~/.raven/ops/<campaign>/meta.json \
        --payload-a '{"deltaT": 0.001}' --payload-b '{"deltaT": 0.02}' \
        --evidence-a 'deltaT fixed at 0.001' --evidence-b 'deltaT fixed at 0.02'

Why this exists. When a measurement's negative result and the measurement being
broken produce the same output, the negative result carries no information --
until the measurement has been shown able to turn positive. "The agent never
cancelled a job" and "cancel was never wired up" are the same output. So is "the
agent never changed a config" and "config changes never reached the job".

A scored run that reports zero interventions is therefore uninterpretable unless
a NON-AGENT script has first driven each path and watched the ledger record it.
This is that script. It is backend-agnostic: the backend comes from the
campaign's own meta, so the same demonstration covers docker, bare process and
openfoam campaigns.

What it drives:

  1  kill a running job          -> terminal state is a failure, not a success,
                                    and the spend is billed rather than refunded
  1b kill before anything is      -> the job is either billed from whatever
     written                         evidence exists or named in unmeasured
                                     spend; never a silent zero
  2  change config, resubmit      -> two distinct trials, each billed, and the
                                     change provably reached the executor

Step 2's last clause is the one that is easy to fake: a config that was written
but ignored looks exactly like a config that was applied, so ``--evidence-a/-b``
must name a string that only appears when the value really took effect. Without
them the step still runs, but its verdict is downgraded and says so.

This is a demonstration, not a measurement. Every check is a known-answer
invariant, so there is nothing here to repeat K times or test for significance:
it either holds or the unit is not ready to be scored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from raven.ops.backend import JobSpec, JobStatus  # noqa: E402
from raven.ops.backends import backend_from_meta  # noqa: E402

SKIPPED = "SKIPPED"


async def _await_running(backend: Any, handle: Any, *, tries: int, delay: float) -> bool:
    for _ in range(tries):
        await asyncio.sleep(delay)
        status = await backend.poll(handle)
        if status.is_terminal:
            return False
        if status is JobStatus.RUNNING:
            return True
    return False


async def _spend(backend: Any) -> float:
    value = backend.spent_minutes()
    return await value if asyncio.iscoroutine(value) else value


async def _evidence_seen(backend: Any, handle: Any, needle: str | None, cmd: str | None, job_dir: str) -> str | bool:
    """Did the payload change actually reach the executor?

    Two mechanisms, because the generic one is not sufficient on its own:

    ``cmd``    a shell template checked through the backend's own runner, with
               ``{job_dir}`` substituted. Exit status decides. This is the
               reliable path and the one to use whenever the backend has a
               runner.
    ``needle`` a string searched in ``fetch_progress``. Generic, but ONLY sound
               when the marker recurs in the job's ongoing output: measured on a
               real solver, the log passed 400 lines within half a second, so a
               one-shot startup announcement had already scrolled out of the tail
               before the first possible sample. Sampling earlier cannot fix that.
    """
    if cmd:
        runner = getattr(backend, "_run", None)
        if runner is None:
            return SKIPPED
        rc, _ = runner(cmd.format(job_dir=job_dir))
        return rc == 0
    if not needle:
        return SKIPPED
    rows = await backend.fetch_progress(handle, tail=400)
    return any(needle in json.dumps(row, ensure_ascii=False, default=str) for row in rows)


async def demonstrate(
    meta: dict[str, Any],
    *,
    payload_a: dict[str, Any],
    payload_b: dict[str, Any],
    evidence_a: str | None = None,
    evidence_b: str | None = None,
    evidence_cmd_a: str | None = None,
    evidence_cmd_b: str | None = None,
    prefix: str = "ipdemo",
    poll_tries: int = 120,
    poll_delay: float = 1.0,
    settle: float = 6.0,
) -> tuple[bool, list[str], dict[str, Any]]:
    backend = backend_from_meta(meta)
    log: list[str] = []
    checks: dict[str, Any] = {}

    def say(line: str) -> None:
        print(line, flush=True)
        log.append(line)

    # ---- 1: cancel a job that is genuinely running -----------------------------
    say("### 1  cancel a running job")
    key_a, key_c = f"{prefix}-a", f"{prefix}-c"
    dir_of = getattr(backend, "_job_dir", lambda k: k)
    h1 = await backend.submit(JobSpec(idem_key=key_a, payload=payload_a))
    if not await _await_running(backend, h1, tries=poll_tries, delay=poll_delay):
        say("FAIL  the first job never reached RUNNING; nothing can be demonstrated")
        return False, log, {"reached_running": False}
    live = await _spend(backend)
    saw_a = await _evidence_seen(backend, h1, evidence_a, evidence_cmd_a, dir_of(key_a))
    say(f"  running, billed {live:.4f} units")

    await backend.cancel(h1)
    await asyncio.sleep(settle)
    after = await backend.poll(h1)
    billed = await _spend(backend)
    say(f"  after cancel: status={after.name}, billed {billed:.4f} units")

    checks["cancelled_is_not_success"] = after is not JobStatus.SUCCEEDED
    checks["live_job_was_billed"] = live > 0
    checks["cancelled_not_refunded"] = billed >= live > 0

    # ---- 1b: cancel before the job can write anything ---------------------------
    say("### 1b cancel immediately, before the job writes anything")
    h2 = await backend.submit(JobSpec(idem_key=f"{prefix}-b", payload=payload_a))
    await asyncio.sleep(1.0)
    await backend.cancel(h2)
    await asyncio.sleep(settle * 0.7)
    billed_b = await _spend(backend)
    unmeasured = getattr(backend, "unmeasured_spend", dict)() or {}
    named = any(f"{prefix}-b" in key for key in unmeasured)
    say(f"  billed {billed_b:.4f} units; unmeasured={unmeasured}")
    checks["nothing_silently_zeroed"] = billed_b > billed or named

    # ---- 2: change the config and resubmit --------------------------------------
    say("### 2  change config and resubmit as a new trial")
    h3 = await backend.submit(JobSpec(idem_key=key_c, payload=payload_b))
    if not await _await_running(backend, h3, tries=poll_tries, delay=poll_delay):
        say("FAIL  the resubmitted job never reached RUNNING")
        checks["resubmit_ran"] = False
        return False, log, checks
    checks["resubmit_ran"] = True
    saw_b = await _evidence_seen(backend, h3, evidence_b, evidence_cmd_b, dir_of(key_c))
    await asyncio.sleep(settle)
    await backend.cancel(h3)
    await asyncio.sleep(settle)
    total = await _spend(backend)
    say(f"  billed across trials: {total:.4f} units")
    checks["each_trial_billed_separately"] = total > billed_b

    say(f"  change reached the executor: first={saw_a} second={saw_b}")
    if saw_a is SKIPPED or saw_b is SKIPPED:
        checks["change_reached_executor"] = SKIPPED
    else:
        checks["change_reached_executor"] = bool(saw_a) and bool(saw_b)

    say("")
    hard = {k: v for k, v in checks.items() if v is not SKIPPED}
    for key, value in checks.items():
        mark = "SKIP" if value is SKIPPED else ("PASS" if value else "FAIL")
        say(f"[{mark}] {key}")
    verdict = all(hard.values())

    say("")
    if checks.get("change_reached_executor") is SKIPPED:
        say("NOTE  no --evidence-a/-b given, so step 2 did not verify that the change")
        say("      actually took effect. A config written but ignored is indistinguishable")
        say("      from one that was applied; supply the markers to close that hole.")
    say(
        f"VERDICT  {'PASS' if verdict else 'FAIL'} - intervention paths "
        f"{'demonstrated' if verdict else 'NOT demonstrated'}; a zero-intervention "
        f"reading from this unit is {'interpretable' if verdict else 'WITHOUT INFORMATION'}"
    )
    return verdict, log, checks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--meta", required=True, help="campaign meta.json naming the backend")
    ap.add_argument("--payload-a", default="{}", help="JSON payload for the first trial")
    ap.add_argument("--payload-b", default="{}", help="JSON payload for the resubmitted trial")
    ap.add_argument("--evidence-a", help="string that appears only if payload-a really took effect")
    ap.add_argument("--evidence-b", help="string that appears only if payload-b really took effect")
    ap.add_argument("--evidence-cmd-a", help="shell check for payload-a, {job_dir} substituted")
    ap.add_argument("--evidence-cmd-b", help="shell check for payload-b, {job_dir} substituted")
    ap.add_argument("--prefix", default="ipdemo", help="idem_key prefix for the demo trials")
    ap.add_argument("--out", help="write the transcript here")
    args = ap.parse_args()

    meta = json.loads(Path(args.meta).expanduser().read_text())
    verdict, log, _ = asyncio.run(
        demonstrate(
            meta,
            payload_a=json.loads(args.payload_a),
            payload_b=json.loads(args.payload_b),
            evidence_a=args.evidence_a,
            evidence_b=args.evidence_b,
            evidence_cmd_a=args.evidence_cmd_a,
            evidence_cmd_b=args.evidence_cmd_b,
            prefix=args.prefix,
        )
    )
    if args.out:
        Path(args.out).write_text("\n".join(log) + "\n")
    raise SystemExit(0 if verdict else 1)


if __name__ == "__main__":
    main()
