"""Gate 1 post-run audit: did the mechanisms under test actually run?

Merges activation_ledger.jsonl (hook fires + beacons + presence asserts)
with the pre-existing skill_injections.jsonl telemetry so all four
mechanism classes are counted from one call.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def _iter_jsonl(path: Path):
    """Iterate over JSON lines in a file, skipping malformed lines."""
    try:
        for line in path.open():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return


def audit_trials(roots: list[Path], expected_sources: list[str]) -> dict:
    """Audit all trials under given roots for mechanism activation."""
    counts: Counter = Counter()
    wired: Counter = Counter()
    n_ledgers = 0

    for root in roots:
        for ledger in Path(root).glob("**/activation_ledger.jsonl"):
            n_ledgers += 1
            for rec in _iter_jsonl(ledger):
                src = rec.get("source", "?")
                if rec.get("kind") == "hook_active":
                    wired[src] += 1
                else:
                    counts[src] += 1

        # Skills log only to skill_injections.jsonl and hooks only to the
        # ledger - no overlap, so merging the two never double-counts.
        for inj in Path(root).glob("**/skill_injections.jsonl"):
            for rec in _iter_jsonl(inj):
                for s in rec.get("skills", []):
                    name = s.get("name")
                    if name:
                        counts[name] += 1

    inert = [s for s in expected_sources if counts[s] == 0]
    inert_but_wired = [s for s in inert if wired[s] > 0]
    return {
        "verdict": "FAIL" if inert else "PASS",
        "inert_sources": inert,
        "inert_but_wired": inert_but_wired,
        "counts": {s: counts[s] for s in expected_sources},
        "wired": {s: wired[s] for s in expected_sources},
        "all_observed": dict(counts),
        "n_ledgers": n_ledgers,
    }
