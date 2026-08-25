"""Detect a test run mutating the developer's real ~/.raven state.

Isolation fixtures only cover the paths someone remembered to redirect. Two
mechanisms defeat them: a test that spawns a real raven subprocess (the child
resolves its own config path and never sees the parent's monkeypatched
constants), and a process-level singleton that binds a directory once and
silently swallows later writes -- the second failure mode is indistinguishable
from isolation working, because both leave the shared file untouched *and* the
isolated one empty.

So this guard does not isolate. It watches, and fails the session loudly if the
real state moved, which catches paths nobody thought to redirect. It is the
positive control for every isolation fixture in this suite.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CRON_STORE_RELPATH = "cron/jobs.json"
OPS_RELPATH = "ops"


def snapshot(home: Path) -> dict[str, object]:
    """Digest the shared state a test run must never touch.

    The cron store is compared by content rather than mtime so an atomic
    rewrite with identical bytes is not reported -- the round-2 incident showed
    a rewrite that preserved every field value, which is a mutation of the file
    but not of the state, and only the latter can change an experiment reading.
    """
    store = home / CRON_STORE_RELPATH
    ops = home / OPS_RELPATH
    return {
        "cron_store": hashlib.sha256(store.read_bytes()).hexdigest() if store.is_file() else None,
        "ops_campaigns": sorted(p.name for p in ops.iterdir()) if ops.is_dir() else [],
    }


def describe_drift(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """Human-readable violations, empty when the shared state is unchanged."""
    problems: list[str] = []
    if before["cron_store"] != after["cron_store"]:
        was, now = before["cron_store"], after["cron_store"]
        detail = "created" if was is None else ("deleted" if now is None else "rewritten with different content")
        problems.append(f"~/.raven/{CRON_STORE_RELPATH} was {detail} during this test session")
    old_campaigns, new_campaigns = set(before["ops_campaigns"]), set(after["ops_campaigns"])  # type: ignore[arg-type]
    if added := sorted(new_campaigns - old_campaigns):
        problems.append(f"~/.raven/{OPS_RELPATH}/ gained campaign dir(s): {', '.join(added)}")
    if removed := sorted(old_campaigns - new_campaigns):
        problems.append(f"~/.raven/{OPS_RELPATH}/ lost campaign dir(s): {', '.join(removed)}")
    return problems


def failure_report(problems: list[str]) -> str:
    lines = [
        "",
        "=" * 78,
        "SHARED STATE VIOLATION -- this test session mutated the real ~/.raven",
        "=" * 78,
    ]
    lines += [f"  - {p}" for p in problems]
    lines += [
        "",
        "  A running on-call campaign keeps its wake schedule and ledger there.",
        "  A test that writes it can delete a wake another process is executing,",
        "  which looks identical to the agent never scheduling one.",
        "",
        "  Most likely cause: a test spawned a real raven subprocess. The child",
        "  resolves its own config path, so a monkeypatched constant in the",
        "  parent does not reach it -- pass an explicit isolated --config.",
        "=" * 78,
        "",
    ]
    return "\n".join(lines)
