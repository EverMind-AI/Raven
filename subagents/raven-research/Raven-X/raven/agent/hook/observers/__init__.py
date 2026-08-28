"""Loop observers — AgentHook implementations that watch the ReAct
iteration phases and intervene through the hook decision channels
(rollback / short-circuit) instead of editing the loop body.

Housing them here keeps the agent loop a thin dispatcher: new guards
are new observers, not new inline branches in ``_run_agent_loop``.
"""

from typing import Any

from raven.agent.hook.observers.budget import BudgetObserver
from raven.agent.hook.observers.loopscan import LoopscanObserver
from raven.agent.hook.observers.refusal import RefusalObserver
from raven.agent.hook.observers.search import DuplicateQueryObserver, EmptySearchObserver

_STAMP_STR_CAP = 200


def _scalar_snapshot(namespace: dict[str, Any]) -> dict[str, Any]:
    """Export a namespace by value TYPE, never by an enumerated key list.

    A key-name whitelist silently drops the next counter someone adds, and the
    loss is invisible from the writing side: the observer sees its own metadata
    and only the trajectory is short. That has already cost one batch — three
    salvage counters were written by ForcedFinalizeGate, dropped here, and a
    downstream scorer keyed on them never fired, so the arm measured the
    previous version's behaviour under the new version's label.

    Scalars only: an unbounded structure (a hit log, a seen-set) still needs its
    own explicit reducer, which is why the namespaces below that summarise lists
    are left hand-written.
    """
    out: dict[str, Any] = {}
    for key, value in namespace.items():
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) and value:
            out[key] = value[:_STAMP_STR_CAP]
    return out


def terminal_state(metadata: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe end-of-turn snapshot of the observer namespaces.

    AgentLoop stamps this onto the turn's last persisted message so
    offline attribution (which observer fired, how often, did the
    intervention budget run out) reads from the trajectory instead of
    scraping a bounded stderr tail. Internal bookkeeping that is
    unbounded or not JSON-serializable (dup-query's seen-set,
    empty-search's watermark) is reduced to counts; namespaces with no
    signal are omitted, so quiet turns get no stamp at all.
    """
    out: dict[str, Any] = {}
    loopscan = metadata.get("loopscan")
    if loopscan and loopscan.get("hits"):
        hits = loopscan["hits"]
        out["loopscan"] = {
            "hits": len(hits),
            "rollbacks": loopscan.get("rollbacks", 0),
            "exhausted": bool(loopscan.get("exhausted")),
            "last_share": hits[-1].get("share"),
            "last_windows": hits[-1].get("windows"),
            "hit_log": [dict(h) for h in hits],
        }
    dup_query = metadata.get("dup_query")
    if dup_query and dup_query.get("rollbacks"):
        out["dup_query"] = {"rollbacks": dup_query["rollbacks"]}
    refusal = metadata.get("refusal")
    if refusal and refusal.get("rollbacks"):
        out["refusal"] = {"rollbacks": refusal["rollbacks"]}
    empty_search = metadata.get("empty_search")
    if empty_search and empty_search.get("tagged"):
        out["empty_search"] = {"streak": empty_search.get("streak", 0), "tagged": True}
    verify_gate = metadata.get("verify_gate")
    if verify_gate and verify_gate.get("reviews"):
        out["verify_gate"] = _scalar_snapshot(verify_gate)
    force_finalize = metadata.get("force_finalize")
    if force_finalize and (force_finalize.get("empty_hits") or force_finalize.get("terminal_hits")):
        out["force_finalize"] = _scalar_snapshot(force_finalize)
    spin_breaker = metadata.get("spin_breaker")
    if spin_breaker and spin_breaker.get("hits"):
        out["spin_breaker"] = {
            "hits": len(spin_breaker["hits"]),
            "triggers": spin_breaker.get("triggers", 0),
            "hit_log": [dict(h) for h in spin_breaker["hits"]],
        }
    fetch_floor = metadata.get("fetch_floor")
    if fetch_floor:
        # Exported whenever the observer ran, not only when it wrote a note. Gating on
        # notes>0 left 40 of 120 items with no record at all, which made
        # fetch_floor.searches a selection-biased subsample of the search-heavy tail:
        # mean 83.7 over the 80 items present against a true mean of 58.1 over all 120.
        out["fetch_floor"] = _scalar_snapshot(fetch_floor)
    # ★ 20260825 Framework: dr@3.3's fetchGate wrote its counters every iteration -
    # with a comment saying a counter that appears only on firing cannot tell "did
    # not fire" from "was not installed" - and then this function never exported the
    # namespace, so none of them reached ``traj_raw.jsonl``. ``fetch_gate.py`` states
    # acceptance counts firings off the trajectory rather than off a hook, precisely
    # because "a key that only exists inside the hook has been dropped by a
    # serialisation allowlist here before". It was dropped again, one level up: the
    # hardening went into ``_scalar_snapshot`` (no per-KEY whitelist) while THIS list
    # is a per-NAMESPACE whitelist with the same failure mode. Unconditional, like
    # ``fetch_floor`` above and for its reason.
    fetch_gate = metadata.get("fetch_gate")
    if fetch_gate:
        # ★ 20260825: hand-written reducer -- this must NOT route through
        # `_scalar_snapshot`. `gate_streak_at_fire` is `list[int]` and
        # `_scalar_snapshot` passes only bool/int/float/str, so it was silently
        # dropped -- and it is the only pre-registered first-class field that
        # carries position (`gate_fired` is a bare count that cannot say WHICH
        # search the gate fired on), so the section-17.6 score-independent
        # mechanism check ("after a removed web_search, is the next action a
        # fetch") could not locate the firing point in `traj_raw` at all. This
        # is the third time the family was eaten by a serialisation filter
        # (per-key whitelist, per-namespace whitelist, now per-value-type), and
        # `_scalar_snapshot`'s own docstring already says the summary-list
        # namespaces are hand-written -- `loopscan`/`spin_breaker` both do;
        # this one was routed wrong at birth.
        out["fetch_gate"] = {
            **_scalar_snapshot(fetch_gate),
            "gate_streak_at_fire": [int(x) for x in (fetch_gate.get("gate_streak_at_fire") or ())],
        }
    budget = metadata.get("budget")
    if budget:
        out["budget"] = {k: v for k, v in budget.items() if isinstance(v, int)}
    # dr@3.4. Exported whenever the gate ran, not only when it bounced: "the bar
    # was installed and every draft cleared it" and "no bar was installed" are
    # different states, and the second one is what every arm without the knob
    # reads as. The delivered shape is stamped separately as ``report_shape``.
    report_shape_gate = metadata.get("report_shape_gate")
    if report_shape_gate:
        out["report_shape_gate"] = _scalar_snapshot(report_shape_gate)
    # dr@3.4-askuser. Unconditional whenever the gate ran, following
    # ``fetch_floor`` above rather than the gated namespaces: gating on
    # ``asked`` would export only the turns that asked, and "how often does it
    # ask" is the single number the default-on decision rests on - a numerator
    # with no denominator. All values are already scalars because the gate
    # reduces the payload to counts before writing; the question text never
    # enters this namespace, since ``_STAMP_STR_CAP`` would cut it mid-sentence
    # and its length is unbounded.
    ask_user = metadata.get("ask_user")
    if ask_user:
        out["ask_user"] = _scalar_snapshot(ask_user)
    # Rollbacks the loop REFUSED past its per-turn cap. The requesting gate has
    # already booked its side (the verify gate writes ``reject``+``revisions``
    # before returning), so without this count the trajectory says a bounce
    # happened that never did.
    if metadata.get("rollbacks_refused"):
        out["rollbacks_refused"] = int(metadata["rollbacks_refused"])
    # How the turn ended, stamped unconditionally: batch attribution needs to
    # tell a model that gave no answer from a harness that lost one (a context
    # overflow surfaces as a dead call whose response is never persisted, so the
    # trajectory alone looks like a mid-research stop). Cheap and always
    # present, unlike the observer namespaces above.
    turn_end = metadata.get("turn_end")
    if turn_end:
        out["turn_end"] = {k: v for k, v in turn_end.items() if isinstance(v, (int, str, bool, type(None)))}
    return out


__all__ = [
    "BudgetObserver",
    "DuplicateQueryObserver",
    "EmptySearchObserver",
    "LoopscanObserver",
    "RefusalObserver",
    "terminal_state",
]
