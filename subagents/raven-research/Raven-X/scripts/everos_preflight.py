#!/usr/bin/env python3
"""Pre-batch EverOS preflight — fail fast before any arm launches.

Run this from the batch launcher (or by hand) before starting a
measurement batch that writes to EverOS. It never starts or stops the
service — service lifecycle is owned by the operator, outside the
measurement window — it only answers "is the thing that is running the
thing the batch needs":

1. **Reachable** — ``GET /health`` answers at ``--base-url``.
2. **Capabilities** — ``[llm]`` must be up (boundary detection and every
   extraction die without it); ``[embedding]`` missing is a Tier-1
   warning (episodes/cases extract, the skill track silently no-ops),
   fatal only with ``--require-embed``.
3. **API mount** — probes which of ``/api/v2`` / ``/api/v1`` is mounted,
   using a flush of an empty, never-written session (LLM-free, writes
   nothing).
4. **Deferred capture honored** — POSTs one tiny ``/add`` with
   ``defer_extraction: true`` and times it. A build that ignores the
   field (any released tag <= 1.2.3) runs boundary detection — an LLM
   call — inside the request, so the add takes many seconds instead of
   milliseconds. That build would run extraction inside the measurement
   window, which breaks the deferred write profile; version strings
   cannot catch this (the pinned d07cddc also reports "1.2.3").

The defer probe writes one buffered row under the dedicated scope
``app_id=everos_preflight / project_id=probe``, with a fixed session id
and timestamp, so every later run dedups into that same single row —
the scope never grows and is never flushed.

Usage:
  python scripts/everos_preflight.py
  python scripts/everos_preflight.py --base-url http://127.0.0.1:8000 --require-embed

Exit code: 0 all checks passed; 1 a check failed; 2 service unreachable.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

PROBE_APP_ID = "everos_preflight"
PROBE_PROJECT_ID = "probe"

# A deferred add is buffer-only (sub-second); an eager add runs the
# boundary-detection LLM inside the request (observed >10s live). The
# cutoff sits far from both.
DEFER_PROBE_MAX_S = 5.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument(
        "--require-embed",
        action="store_true",
        help="fail (not just warn) when the [embedding] capability is down",
    )
    p.add_argument(
        "--skip-defer-probe",
        action="store_true",
        help="skip the defer_extraction behavioral probe (writes nothing)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="per-request budget; the defer probe fails at %(default)ss "
        "anyway if the server runs an in-request LLM",
    )
    return p.parse_args()


def check_health(client: httpx.Client, base_url: str, *, require_embed: bool) -> bool:
    r = client.get(f"{base_url}/health")
    r.raise_for_status()
    health = r.json()
    caps = health.get("capabilities", {})
    print(f"health: everos {health.get('version', '?')} at {base_url}; capabilities={caps}")
    ok = True
    if not caps.get("llm", False):
        print(
            "FAIL: [llm] capability is down — boundary detection and every "
            "extraction will fail; fix the provider before launching",
            file=sys.stderr,
        )
        ok = False
    if not caps.get("embed", False):
        msg = (
            "no [embedding] capability (Tier 1) — episodes and cases will "
            "extract, the skill track will silently no-op"
        )
        if require_embed:
            print(f"FAIL: {msg}", file=sys.stderr)
            ok = False
        else:
            print(f"warning: {msg}", file=sys.stderr)
    return ok


def check_api_mount(client: httpx.Client, base_url: str) -> str | None:
    """Which API prefix is mounted, probed with an LLM-free no-op.

    Flushing a session that was never written short-circuits on the
    empty buffer ("no_extraction") before any LLM call, so the probe is
    cheap and leaves no state — unlike a /search, which needs a valid
    query and runs a real retrieval.
    """
    body = {
        "session_id": "everos_preflight_mount_probe",
        "app_id": PROBE_APP_ID,
        "project_id": PROBE_PROJECT_ID,
    }
    for prefix in ("v2", "v1"):
        r = client.post(f"{base_url}/api/{prefix}/memory/flush", json=body)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        print(f"api mount: /api/{prefix} answers")
        return prefix
    print("FAIL: neither /api/v2 nor /api/v1 is mounted", file=sys.stderr)
    return None


def check_defer_honored(client: httpx.Client, base_url: str, prefix: str) -> bool:
    """Behavioral probe: a deferred /add must return without an LLM call.

    Fixed session id + timestamp + single message: the server's dedup
    key is (session_id, timestamp_ms, index-within-request), so every
    run after the first merges into the same single buffered row.
    """
    body = {
        "session_id": "everos_preflight_defer_probe",
        "app_id": PROBE_APP_ID,
        "project_id": PROBE_PROJECT_ID,
        "defer_extraction": True,
        "messages": [
            {
                "sender_id": "preflight",
                "role": "user",
                "timestamp": 1,
                "content": "deferred-capture preflight probe",
            }
        ],
    }
    t0 = time.monotonic()
    try:
        r = client.post(f"{base_url}/api/{prefix}/memory/add", json=body)
        r.raise_for_status()
    except httpx.TimeoutException:
        print(
            f"FAIL: deferred /add did not answer within {time.monotonic() - t0:.1f}s "
            "— this build runs boundary detection inside the request, i.e. it "
            "ignores defer_extraction (released tags <= 1.2.3 do). Deploy the "
            "pinned sha with deferred capture before launching a batch.",
            file=sys.stderr,
        )
        return False
    elapsed = time.monotonic() - t0
    if elapsed > DEFER_PROBE_MAX_S:
        print(
            f"FAIL: deferred /add took {elapsed:.1f}s (> {DEFER_PROBE_MAX_S}s) — "
            "the server most likely ignores defer_extraction and ran an "
            "in-request LLM call. Deploy the pinned sha before launching.",
            file=sys.stderr,
        )
        return False
    print(f"defer_extraction: honored (add answered in {elapsed * 1000:.0f} ms)")
    return True


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    failed = False
    with httpx.Client(timeout=httpx.Timeout(args.timeout)) as client:
        try:
            if not check_health(client, base_url, require_embed=args.require_embed):
                failed = True
        except Exception as e:
            print(f"unreachable: {e}", file=sys.stderr)
            return 2
        prefix = check_api_mount(client, base_url)
        if prefix is None:
            failed = True
        elif args.skip_defer_probe:
            print("defer_extraction: probe skipped")
        elif not check_defer_honored(client, base_url, prefix):
            failed = True
    if failed:
        print("preflight FAILED", file=sys.stderr)
        return 1
    print("preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
