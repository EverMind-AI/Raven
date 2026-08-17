#!/usr/bin/env python3
"""Drive pbench through the installed hermes CLI (black-box one-shot chat).

Symmetric to RavenAgentBackend: one `hermes chat -q <prompt> -Q` per record,
fresh HERMES_HOME per record, real wall clock (no hermes_time mock).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))

from _common.drivers.pbench import PbenchDriver  # noqa: E402

HERMES_BIN = os.environ.get("HERMES_BIN", os.path.expanduser("~/.local/bin/hermes"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--prompts-dir", default=None)
    ap.add_argument("--home-template", required=True, help="dir with config.yaml/.env/auth.json to copy per record")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    driver = PbenchDriver(
        agent_name="hermes",
        context_mode="cold",
        prompts_dir=Path(args.prompts_dir) if args.prompts_dir else None,
    )
    samples = driver.load_samples(n=args.n)
    template = Path(args.home_template)

    env_key = ""
    for line in (template / ".env").read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            env_key = line.split("=", 1)[1].strip()

    sem = asyncio.Semaphore(args.concurrency)
    done = {"n": 0}

    async def run_one(i: int, sample) -> dict:
        prompt = driver.build_prompt(sample)
        async with sem:
            home = Path(tempfile.mkdtemp(prefix=f"hermes-cli-{i:03d}-"))
            for fn in ("config.yaml", ".env", "auth.json"):
                src = template / fn
                if src.exists():
                    shutil.copy2(src, home / fn)
            env = {
                **os.environ,
                "HERMES_HOME": str(home),
                "OPENROUTER_API_KEY": env_key,
            }
            cmd = [
                HERMES_BIN,
                "chat",
                "-q",
                prompt,
                "-Q",
                "--yolo",
                "-m",
                "qwen/qwen3.5-27b",
                "--provider",
                "openrouter",
            ]
            started = time.monotonic()
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                )
                status = "ok" if proc.returncode == 0 else "subprocess_error"
                text = proc.stdout
                error = None if proc.returncode == 0 else proc.stderr[-400:]
            except subprocess.TimeoutExpired:
                status, text, error = "timeout", "", f"timeout after {args.timeout}s"
            finally:
                shutil.rmtree(home, ignore_errors=True)
            elapsed = round(time.monotonic() - started, 2)

            dec = driver.parse_output(text, sample)
            predicted = dec["should_help"] if dec.get("parse_ok") else False
            rec = sample.raw
            row = {
                "category": rec.get("category", "?"),
                "context_mode": "cold",
                "truth_help_needed": rec.get("help_needed"),
                "agent": {
                    "system": "hermes-cli",
                    "status": status,
                    "error": error,
                    "elapsed_s": elapsed,
                    "parse_ok": bool(dec.get("parse_ok")),
                    "should_help": dec.get("should_help"),
                    "proposed_task": dec.get("proposed_task"),
                    "reason": dec.get("reason"),
                    "raw_final": (text or "")[:4000],
                },
                "predicted_help": predicted,
                "help_match": predicted == rec.get("help_needed"),
            }
            done["n"] += 1
            print(
                f"[{done['n']}/{len(samples)}] {'OK ' if row['help_match'] else 'MISS'} "
                f"pred={predicted} truth={rec.get('help_needed')} status={status} {elapsed}s",
                file=sys.stderr,
                flush=True,
            )
            return row

    async def _run() -> list[dict]:
        return list(await asyncio.gather(*[run_one(i, s) for i, s in enumerate(samples)]))

    rows = asyncio.run(_run())
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"meta": {"system_label": "hermes-cli"}, "results": rows}, indent=2, ensure_ascii=False))
    print("=" * 60, file=sys.stderr)
    print(driver.summarize(rows), file=sys.stderr)
    print(f"Results written to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
