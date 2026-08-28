#!/usr/bin/env python3
"""Post-batch EverOS flush — promote deferred-capture sessions.

The deferred write profile (``defer_extraction=true``) leaves every
session's messages in EverOS's unprocessed buffer so no extraction LLM
runs inside the measurement window. This script runs AFTER the batch:
it reads the ``.everos_sessions.jsonl`` sidecars the backend wrote into
each workspace and POSTs ``/memory/flush`` per recorded
(session_id, app_id, project_id).

Two timing facts to keep in mind (they shape the flow below):

1. ``/flush`` returning ``"extracted"`` covers boundary detection and
   the user-track episode extraction only. The agent track (cases /
   skills) is fire-and-forget to the OME engine — it runs in the
   background AFTER the response, with retry / dead-letter. Acceptance
   checks on ``.cases/`` must wait for the OME queue to drain.
2. Skill extraction needs the ``[embedding]`` capability (Tier 2). On a
   Tier-1 server flush still produces episodes and cases, but the skill
   track silently no-ops — the preflight below surfaces that before any
   flush is sent.

Usage:
  python scripts/everos_flush_batch.py --sessions-file ws/.everos_sessions.jsonl
  python scripts/everos_flush_batch.py --sessions-file 'batch_ws/*/.everos_sessions.jsonl'
  python scripts/everos_flush_batch.py --session raven_dr_abc123 \
      --app-id raven_dr --project-id w302s100_flowon

The server caps ``/flush``'s in-request work with
``memorize.session_lock_timeout_seconds`` (shipped default 360 s). Keep
the client budget ABOVE that cap: a slow flush then ends with the
server's own error envelope in hand instead of a client ReadTimeout
racing the server's cancel, and a flush that answers late still lands as
a result rather than an exception.

Equal budgets are the failure case, not the safe one — the server's timer
starts after routing, so it fires first and the caller sees a 500. The
default below clears both the shipped 360 and a server raised to 900.
Raising the server's cap without raising this leaves the client as the
binding constraint and the server-side change inert.

Note what a server-side cancel costs, because it bounds what a retry can
do: the memcells are committed and the buffer drained before the
user-track extraction finishes, so re-flushing answers ``no_extraction``
and the missing episodes do not come back. The agent track survives (it
is dispatched to the background queue), which makes the loss asymmetric
and easy to miss — compare the ``users/`` and ``agents/`` trees under the
memory root, not just the flush status.

Safe to run over sessions a process already promoted itself (``memory
.flush_on_task_end``): ``/flush`` is idempotent -- measured on everos
1.2.3, a second flush answers ``no_extraction`` in ~10 ms rather than
re-deriving or duplicating.

Exit code 0 iff every recorded session flushed successfully.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# Above the server's in-request cap by design — see the module docstring.
FLUSH_TIMEOUT_S = 960.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument(
        "--api-version",
        choices=["auto", "v1", "v2"],
        default="auto",
        help="URL prefix; auto probes v2 and falls back to v1 on 404",
    )
    p.add_argument(
        "--sessions-file",
        action="append",
        default=[],
        metavar="GLOB",
        help=".everos_sessions.jsonl sidecar(s) written by the backend; "
        "accepts globs, repeatable",
    )
    p.add_argument(
        "--session",
        action="append",
        default=[],
        help="explicit everos session id (needs --app-id/--project-id)",
    )
    p.add_argument("--app-id", default="default")
    p.add_argument("--project-id", default="default")
    p.add_argument(
        "--flush-timeout-s",
        type=float,
        default=FLUSH_TIMEOUT_S,
        help="client budget per /flush request (default %(default)ss); "
        "must stay above the server's "
        "memorize.session_lock_timeout_seconds (shipped default 360) so "
        "the server's error envelope arrives instead of the client "
        "disconnecting first",
    )
    p.add_argument(
        "--wait-secs",
        type=float,
        default=0.0,
        help="after flushing, poll /health until the cascade backlog "
        "drains or the budget runs out (0 = don't wait)",
    )
    return p.parse_args()


def load_sessions(args: argparse.Namespace) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern in args.sessions_file:
        paths = sorted(glob.glob(pattern))
        if not paths:
            print(f"warning: --sessions-file {pattern!r} matched nothing", file=sys.stderr)
        for path in paths:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
    for sid in args.session:
        rows.append(
            {
                "session_id": sid,
                "app_id": args.app_id,
                "project_id": args.project_id,
            },
        )
    unique: list[dict] = []
    for r in rows:
        key = (r["session_id"], r.get("app_id", "default"), r.get("project_id", "default"))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def preflight(client: httpx.Client, base_url: str) -> dict:
    """GET /health — fail fast if the service is down, warn on Tier gaps."""
    r = client.get(f"{base_url}/health")
    r.raise_for_status()
    health = r.json()
    caps = health.get("capabilities", {})
    print(f"everos {health.get('version', '?')} at {base_url}; capabilities={caps}")
    if not caps.get("llm", False):
        print(
            "warning: [llm] capability is down — boundary detection and "
            "every extraction will fail; fix the provider before flushing",
            file=sys.stderr,
        )
    if not caps.get("embed", False):
        print(
            "warning: no [embedding] capability (Tier 1) — episodes and "
            "cases will extract, the skill track will silently no-op",
            file=sys.stderr,
        )
    return health


class Flusher:
    """POST /flush with the same v2-probe/v1-fallback the adapter uses."""

    def __init__(self, client: httpx.Client, base_url: str, api_version: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._prefix = None if api_version == "auto" else api_version

    def flush(self, row: dict) -> dict:
        body = {
            "session_id": row["session_id"],
            "app_id": row.get("app_id", "default"),
            "project_id": row.get("project_id", "default"),
        }
        if self._prefix is not None:
            r = self._client.post(
                f"{self._base_url}/api/{self._prefix}/memory/flush",
                json=body,
            )
            r.raise_for_status()
            return r.json()
        r = self._client.post(f"{self._base_url}/api/v2/memory/flush", json=body)
        if r.status_code == 404:
            r = self._client.post(f"{self._base_url}/api/v1/memory/flush", json=body)
            if r.status_code != 404:
                self._prefix = "v1"
        else:
            self._prefix = "v2"
        r.raise_for_status()
        return r.json()


def wait_for_drain(client: httpx.Client, base_url: str, budget_s: float) -> None:
    """Poll /health until the cascade backlog reads 0 or the budget ends.

    A quiet cascade is a necessary-not-sufficient drain signal (OME job
    state is not exposed over HTTP), hence the closing reminder.
    """
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            health = client.get(f"{base_url}/health").json()
            pending = health.get("cascade", {}).get("pending")
            if pending == 0:
                print("cascade backlog drained (pending=0)")
                break
            if pending is None:
                print("health exposes no cascade backlog; skipping drain wait")
                break
            print(f"cascade pending={pending}; waiting …")
        except Exception as e:
            print(f"health poll failed ({e}); retrying", file=sys.stderr)
        time.sleep(5.0)
    print(
        "note: agent-track extraction (cases/skills) runs in the OME "
        "background queue — verify .cases/ on disk before acceptance, "
        "and stop the service with SIGTERM (graceful drain), not SIGKILL",
    )


def main() -> int:
    args = parse_args()
    rows = load_sessions(args)
    if not rows:
        print("nothing to flush (no sessions given)", file=sys.stderr)
        return 2

    failures = 0
    with httpx.Client(timeout=httpx.Timeout(args.flush_timeout_s)) as client:
        try:
            preflight(client, args.base_url.rstrip("/"))
        except Exception as e:
            print(f"preflight failed: {e}", file=sys.stderr)
            return 2
        flusher = Flusher(client, args.base_url, args.api_version)
        for i, row in enumerate(rows, 1):
            sid = row["session_id"]
            try:
                resp = flusher.flush(row)
                status = resp.get("data", {}).get("status", "?")
                print(f"[{i}/{len(rows)}] {sid}: {status}")
            except Exception as e:
                failures += 1
                print(f"[{i}/{len(rows)}] {sid}: FAILED ({e})", file=sys.stderr)
        if args.wait_secs > 0:
            wait_for_drain(client, args.base_url.rstrip("/"), args.wait_secs)

    if failures:
        print(f"{failures}/{len(rows)} flushes failed", file=sys.stderr)
        return 1
    print(f"flushed {len(rows)} sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
