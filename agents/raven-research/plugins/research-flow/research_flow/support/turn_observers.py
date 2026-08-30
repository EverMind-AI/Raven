"""The turn's gate counters, reduced to a JSON-safe stamp.

The fork's loop ran ``terminal_state(hook_ctx.metadata)`` at turn end and
stamped the result on the turn's last assistant message
(``agent/hook/observers/__init__.py``, ``agent/loop/main.py:3658``). A plugin
has no message to stamp, so the same reduction lands in the session record
instead -- but the reduction itself has to exist, or a gate's counters end at
the turn that produced them and "the gate never fired" and "the gate was never
installed" become the same reading.

Two rules the fork paid for and this port keeps:

* **Export by value TYPE, never by an enumerated key list.** A per-key whitelist
  silently drops the next counter someone adds, and the loss is invisible from
  the writing side -- three ``ForcedFinalizeGate`` salvage counters were dropped
  that way and a downstream scorer keyed on them never fired.
* **Namespaces are not enumerated either.** The fork hardened the per-key list
  and left a per-NAMESPACE list one level up with the same failure mode, which
  ate ``fetch_gate`` whole. Here every namespace the turn wrote is exported;
  only the two keys the loop itself owns are skipped.

The two list-valued fields still need hand-written reducers, because "scalars
only" drops them without a word and both carry position rather than a count:
``spin_breaker.hits`` is the restart log, ``fetch_gate.gate_streak_at_fire``
says WHICH search the gate closed on.
"""

from __future__ import annotations

from typing import Any

_STAMP_STR_CAP = 200

# Written by the loop, not by a gate: the session's operating profile, which the
# record already carries as ``mode``.
_LOOP_KEYS = frozenset({"mode", "mode_overlay"})


def _scalar_snapshot(namespace: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in namespace.items():
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and value:
            out[key] = value[:_STAMP_STR_CAP]
    return out


def turn_observers(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Every namespace the turn's gates wrote, reduced to scalars and counts."""
    out: dict[str, Any] = {}
    for name, value in (metadata or {}).items():
        if name in _LOOP_KEYS:
            continue
        if isinstance(value, (bool, int, float)):
            out[name] = value
        elif isinstance(value, str) and value:
            out[name] = value[:_STAMP_STR_CAP]
        elif isinstance(value, dict):
            snapshot = _scalar_snapshot(value)
            hits = value.get("hits")
            if isinstance(hits, list):
                snapshot["hits"] = len(hits)
                # Each entry through the same scalar filter as the namespace
                # itself: this dict is written to the session file with
                # ``json.dumps``, and one unserialisable field in a hit would
                # take the turn's whole record with it.
                snapshot["hit_log"] = [_scalar_snapshot(h) for h in hits if isinstance(h, dict)]
            streaks = value.get("gate_streak_at_fire")
            if isinstance(streaks, list):
                snapshot["gate_streak_at_fire"] = [int(x) for x in streaks]
            if snapshot:
                out[name] = snapshot
    return out


__all__ = ["turn_observers"]
