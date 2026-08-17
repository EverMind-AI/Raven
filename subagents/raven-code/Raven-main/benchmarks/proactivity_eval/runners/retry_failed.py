#!/usr/bin/env python3
"""Retry parse_ok=False records from a prior pbench output and patch in-place.

Usage:
    uv run python proactivity-eval/runners/retry_failed.py <output.json> \\
        [--agent openclaw] [--mode None] [--concurrency 4]

Relies on sample_stratified(n=len(records)) being deterministic, so the
sample order matches the original --all run. Only failed indices are
re-executed; successful rows are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from _common import get_backend, get_driver, load_dotenvs  # noqa: E402

load_dotenvs()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("output", help="JSON file to patch in-place")
    ap.add_argument("--agent", default="openclaw")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--benchmark", default="pbench", choices=["pbench"], help="cases re-run not implemented yet")
    ap.add_argument(
        "--with-memory", action="store_true", help="Pass through to backend overrides (must match original run)."
    )
    return ap.parse_args()


async def main() -> None:
    args = parse_args()
    out = Path(args.output)
    if not out.exists():
        sys.exit(f"output file not found: {out}")

    rows = json.loads(out.read_text(encoding="utf-8"))
    failed_idx = [i for i, r in enumerate(rows) if (r.get("agent") or {}).get("parse_ok") is False]
    if not failed_idx:
        print("no failed rows to retry; nothing to do.")
        return
    print(f"[retry] {len(failed_idx)} / {len(rows)} rows to redo")

    driver = get_driver(args.benchmark)
    samples = driver.load_samples()  # deterministic --all order
    if len(samples) != len(rows):
        sys.exit(f"sample count mismatch: driver loaded {len(samples)} vs file has {len(rows)}")

    overrides: dict = {}
    if args.with_memory:
        overrides["with_memory"] = True
    backend = get_backend(args.agent, mode=args.mode, overrides=overrides)

    runtime_meta_base = {
        "agent": args.agent,
        "mode": args.mode,
        "runtime": args.mode if args.agent == "raven" else None,
        "benchmark": args.benchmark,
        "system_label": args.agent,
    }

    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = {"n": 0}
    total = len(failed_idx)

    async def run_one(i: int):
        sample = samples[i]
        cat = sample.raw.get("category") or sample.raw.get("id") or f"#{i}"
        async with sem:
            print(f"[{done['n'] + 1}/{total}] START idx={i} {cat}", flush=True)
            outcome = await backend.run_one(
                sample,
                driver,
                session_id=sample.session_hint,
            )
            runtime_meta_i = dict(runtime_meta_base)
            if outcome.meta:
                runtime_meta_i.update(
                    {
                        k: v
                        for k, v in outcome.meta.items()
                        if k
                        in ("model", "route", "delivered", "fake_now", "full_doc", "cron_prompt", "plausibility_note")
                    }
                )
            new_row = driver.make_row(sample, outcome, runtime_meta_i)
            done["n"] += 1
            ok = (new_row.get("agent") or {}).get("parse_ok")
            print(
                f"[{done['n']}/{total}] DONE  idx={i} parse_ok={ok} "
                f"pred={new_row.get('predicted_help')} "
                f"truth={new_row.get('truth_help_needed')} "
                f"elapsed={outcome.elapsed_s}s",
                flush=True,
            )
            return i, new_row

    tasks = [asyncio.create_task(run_one(i)) for i in failed_idx]
    results = await asyncio.gather(*tasks)

    try:
        backend.close()
    except Exception:
        pass

    # Backup original then patch
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = out.with_name(out.stem + f".pre-retry-{stamp}" + out.suffix)
    shutil.copy2(out, backup)

    still_failed: list[int] = []
    for i, new_row in results:
        # Preserve any fields the new row may have lost
        for preserve in ("context_mode",):
            if preserve in rows[i] and preserve not in new_row:
                new_row[preserve] = rows[i][preserve]
        rows[i] = new_row
        if (new_row.get("agent") or {}).get("parse_ok") is False:
            still_failed.append(i)

    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[retry] backup -> {backup}")
    print(f"[retry] patched {len(failed_idx)} rows; still failing: {len(still_failed)}")
    if still_failed:
        print(f"        indices: {still_failed}")


if __name__ == "__main__":
    asyncio.run(main())
