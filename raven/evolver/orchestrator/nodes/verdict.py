"""Step ⑦ — draft a per-round verdict for the findings log (semantic).

After the gates decide bank/prune, the driver drafts a short narrative for the
findings log: what this round tried, what the result was, the next target, and
whether the curve looks like a capability ceiling. This is advisory — the loop's
stop decision is the deterministic :class:`TerminationTracker`, never this
verdict — but the ``ceiling_signal`` hint is what a human reads to decide an
early unseal (SOP §154 low-ceiling note).

Kept schema-light (three fields) so even a weak driver returns something usable;
parse failure is non-fatal — the caller falls back to a plain summary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from raven.evolver.orchestrator.nodes.semantic import CallFn, SemanticNode


@dataclass(frozen=True)
class Verdict:
    """Driver's narrative verdict on one round."""

    summary: str
    next_target: str
    ceiling_signal: bool


def _parse_verdict(raw: str) -> Verdict:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s[:4].lower() == "json":
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in verdict output")
    d = json.loads(s[start : end + 1])
    return Verdict(
        summary=str(d["summary"]),
        next_target=str(d.get("next_target", "")),
        ceiling_signal=bool(d.get("ceiling_signal", False)),
    )


_FIELD_LEGEND = (
    "Field legend for the results lines: screen/confirm = the candidate patch's "
    "pass-rate on the probe subset / the full train set; van = the vanilla (parent "
    "baseline) harness on the SAME tasks — not a validation split; full_lift = "
    "confirm - van; foc_c/foc_v = candidate/baseline pass-rate on the focused "
    "(diagnosed) subset; fisher_p / z = significance of that focused comparison; "
    "sent_c/sent_v = sentinel-guard pass-rates on known-healthy tasks; credited = "
    "whether the patch's mechanism demonstrably fired on the tasks it changed."
)


def draft_verdict(
    call_fn: CallFn,
    *,
    round_index: int,
    round_summary: str,
    history: str = "",
    why_keys: Optional[list[str]] = None,
    max_retries: int = 2,
) -> Verdict:
    """Draft a verdict for one round from a factual ``round_summary`` string.

    ``history`` (prior rounds' factual summaries) grounds ``ceiling_signal`` — a
    curve cannot be judged from one round. ``why_keys`` constrains
    ``next_target`` to the taxonomy so the field stays machine-readable.
    """
    target_rule = f" — MUST be one of the WHY keys: {', '.join(why_keys)}" if why_keys else ""
    messages = [
        {
            "role": "system",
            "content": (
                "You write a one-round verdict for a self-evolution log: an outer "
                "loop patches an agent harness, evaluates each candidate patch "
                "against a frozen baseline on a train set, and promotes or prunes "
                "it. " + _FIELD_LEGEND + " Output ONLY JSON with keys: summary "
                "(what happened this round, using the legend's terms), next_target "
                f"(what to attack next{target_rule}), ceiling_signal (true ONLY "
                "when the HISTORY shows >=2 consecutive rounds with no promotion "
                "and non-increasing lift — never judged from a single round). "
                "No prose."
            ),
        },
        {
            "role": "user",
            "content": (
                (f"HISTORY (previous rounds):\n{history}\n\n" if history else "")
                + f"Round {round_index} results:\n{round_summary}"
            ),
        },
    ]
    node: SemanticNode[Verdict] = SemanticNode(
        name=f"verdict:r{round_index}",
        call_fn=call_fn,
        parse_fn=_parse_verdict,
        parse_error_types=(ValueError, KeyError, json.JSONDecodeError),
        max_retries=max_retries,
    )
    return node.run(messages)


__all__ = ["Verdict", "draft_verdict"]
