"""Decide from a cultivation record whether a run shows the case this simulation looks for.

The case: over several rounds of the owner's feedback, the Curator's revisions brought the employee to meet the
owner's core requirements. Two things count as value: the Curator shaping the Harness from what the owner handed over
(a mechanism built at onboarding that then enforced a rule, a held case below), and the Curator improving it from the
owner's supplements and feedback (a cultivated rule, and a hard case). A rule is **cultivated** when it failed in a
round while the employee already had the material covering it (a round where the owner said the miss only waited on
material does not count), a curation installed after that round answered it on any surface (prompt, skill, playbook,
planning, action, in the root or a child harness), and it held in every judged round after. A run **qualifies** when
it completed at least `MIN_ROUNDS` rounds, at least `CULTIVATED` rules were cultivated, planning or action enforced at
least one rule (a hard case or a held case, below: a prompt or skill revision alone never makes the case), its last
round met the core (no red-line rule missed, at most `TOLERATED` standard rules missed), every drill of the last round
still delivered a deck, and its isolation scan found no breach (`experimental.simulation.isolation`). It is **partial** when it shows cultivated, hard or held rules but misses
another condition; a run whose employee left its bounds is **invalid**, whatever else it shows.

A hard case is read the same deterministic way:

- A rule is a **case** when an installed curation after round s sedimented it as planning or action (the role the
  target catalogue gives the target), it failed or was mixed in some round up to s while the employee already had the
  material covering it (a round where the owner said the miss only waited on material does not count), every judged
  round after s passed, and in those later rounds, in the drills that judged the rule, that very target in that very
  harness (the root or the child whose process ran it) intervened for this rule: sent work back, ended it, or refused
  a planning step, or for a tool gate or hook refused a tool, with a reason that enforces this rule. Rows of other
  targets, rows that only ran, and interventions for other reasons (another rule, or a wrong block) do not count; a
  rule sedimented through several targets is still one case.
- Which rule an intervention enforces is read from `attribution` when the record has one (a model reads each reason
  against the rules, see `experimental.simulation.attribution`); otherwise from the reason naming the rule's id or
  sharing an SOP section marker with it.
- A rule is a **held case** when planning or action was installed for it at onboarding, it passed every judged round,
  and that target intervened for it. There is no "before" to compare with, so it is weaker evidence.
"""

import argparse
import json
from pathlib import Path

MIN_ROUNDS = 2
CULTIVATED = 2
TOLERATED = 1
HARD = frozenset({"planning", "action"})
GATES = frozenset({"action.tool_gates", "action.hooks"})
DECK = ".pptx"


def _completed(record: dict) -> list[dict]:
    return [
        item for item in record.get("rounds", []) if not item.get("partial") and item.get("evaluation", {}).get("items")
    ]


def _delivered(item: dict) -> bool:
    drills = item.get("drills", [])
    return bool(drills) and all(
        any(
            str(file.get("name", "")).lower().endswith(DECK)
            for turn in drill.get("exchanges", [])
            for file in turn.get("delivered", [])
        )
        for drill in drills
    )


def reason_key(scope: str, target: str, reason: str) -> str:
    return f"{scope}|{target}|{reason}"


def _rows_after(record: dict, criterion: str, landing: dict):
    """Mechanism rows of the landing's own target (a gate's refused tools included) in the rule's drills after it."""
    for item in _completed(record):
        if item["number"] <= landing["round"]:
            continue
        sessions = {
            entry.get("session")
            for entry in item.get("evaluation", {}).get("items", [])
            if entry.get("criterion") == criterion and entry.get("session")
        }
        for row in item.get("mechanism_evidence", []):
            if sessions and row.get("session") not in sessions:
                continue
            if row.get("scope", "root") != landing.get("scope", "root"):
                continue
            gate = landing["target"] in GATES and row.get("kind") == "tool.refused"
            if row.get("target") == landing["target"] or gate:
                yield row


def _reasons(row: dict) -> dict[str, int]:
    return row.get("reasons") or {row.get("summary", ""): row.get("count", 1)}


def _enforces(record: dict, criterion: str, row: dict, reason: str) -> bool:
    links = (record.get("attribution") or {}).get("links")
    if isinstance(links, dict):
        return criterion in links.get(reason_key(row.get("scope", "root"), row.get("target", ""), reason), ())
    from .record import _marker_match, _names, markers

    rule = next((entry.get("rule", "") for entry in record.get("ledger", []) if entry["criterion"] == criterion), "")
    cited = markers(reason)
    return _names(reason, criterion) or bool(cited and _marker_match(cited, markers(rule)))


def _acted(record: dict, criterion: str, landing: dict) -> tuple[int, int]:
    """Mechanism rows of the landing's own target after its round, and how many intervened for this rule."""
    rows = interventions = 0
    for row in _rows_after(record, criterion, landing):
        rows += row.get("count", 1)
        if row.get("intervention"):
            interventions += sum(
                count for reason, count in _reasons(row).items() if _enforces(record, criterion, row, reason)
            )
    return rows, interventions


def candidates(record: dict) -> dict[str, dict]:
    """Every intervention reason a hard landing's target gave, keyed by `reason_key`, with the rules it might enforce."""
    found: dict[str, dict] = {}
    for entry in record.get("ledger", []):
        for landing in entry.get("sedimented_in", []):
            if landing.get("facet") not in HARD:
                continue
            for row in _rows_after(record, entry["criterion"], landing):
                if not row.get("intervention"):
                    continue
                for reason, count in _reasons(row).items():
                    key = reason_key(row.get("scope", "root"), row.get("target", ""), reason)
                    slot = found.setdefault(
                        key,
                        {"scope": row.get("scope", "root"), "target": row.get("target", ""), "reason": reason},
                    )
                    slot.setdefault("rules", set()).add(entry["criterion"])
                    slot["count"] = max(slot.get("count", 0), count)
    return found


def _case(entry: dict, landings: list[dict]) -> dict:
    """One rule's case: the earliest landing's evidence, with rows and interventions summed over its targets."""
    first = min(landings, key=lambda item: item["sedimented"]["round"])
    return {
        "criterion": entry["criterion"],
        "severity": entry.get("severity"),
        "rule": entry.get("rule"),
        **first,
        "mechanism_rows": sum(item["mechanism_rows"] for item in landings),
        "interventions": sum(item["interventions"] for item in landings),
        "targets": sorted({item["sedimented"]["target"] for item in landings}),
    }


def _judged(record: dict, entry: dict) -> list[dict]:
    completed = {item["number"] for item in _completed(record)}
    return [
        moment for moment in entry.get("timeline", []) if moment["round"] in completed and moment["result"] != "unknown"
    ]


def _turned(record: dict, entry: dict, landing: dict) -> tuple[list[int], list[int], bool]:
    """The rounds up to the landing where the rule failed with its material in hand, the judged rounds after, and
    whether it held in every one of those."""
    waiting = {
        item["number"]: set((item.get("analysis") or {}).get("waiting_on_material") or [])
        for item in record.get("rounds", [])
    }
    judged, after = _judged(record, entry), landing["round"]
    before = [
        moment["round"]
        for moment in judged
        if moment["round"] <= after
        and moment["result"] in ("fail", "mixed")
        and entry["criterion"] not in waiting.get(moment["round"], ())
    ]
    later = [moment for moment in judged if moment["round"] > after]
    return before, [moment["round"] for moment in later], bool(later) and all(m["result"] == "pass" for m in later)


def cultivated(record: dict) -> list[dict]:
    """One entry per rule a curation turned from failing to holding, on whatever surface it landed."""
    found = []
    for entry in record.get("ledger", []):
        turned = [
            (landing, before, held)
            for landing in entry.get("sedimented_in", [])
            for before, held, holds in [_turned(record, entry, landing)]
            if before and holds
        ]
        if turned:
            landing, before, held = min(turned, key=lambda item: item[0]["round"])
            found.append(
                {
                    "criterion": entry["criterion"],
                    "severity": entry.get("severity"),
                    "rule": entry.get("rule"),
                    "failed_in": before,
                    "answered_after": landing["round"],
                    "held_in": held,
                    "surfaces": sorted({f"{item[0].get('scope', 'root')}:{item[0]['target']}" for item in turned}),
                }
            )
    return found


def core(record: dict) -> dict:
    """Whether the last completed round met the owner's core: every red-line rule judged there held, and at most
    `TOLERATED` standard rules missed."""
    completed = _completed(record)
    if not completed:
        return {"ok": False, "detail": "no completed round"}
    last = completed[-1]["number"]
    missed = {"red_line": [], "standard": []}
    for entry in record.get("ledger", []):
        moment = next((m for m in entry.get("timeline", []) if m["round"] == last), None)
        if moment and moment["result"] in ("fail", "mixed"):
            missed["red_line" if entry.get("severity") == "red_line" else "standard"].append(entry["criterion"])
    ok = not missed["red_line"] and len(missed["standard"]) <= TOLERATED
    detail = f"round {last} missed {len(missed['red_line'])} red-line and {len(missed['standard'])} standard rules"
    return {"ok": ok, "detail": detail + (f": {', '.join(missed['red_line'] + missed['standard'])}" if not ok else "")}


def cases(record: dict) -> tuple[list[dict], list[dict]]:
    """One entry per rule sedimented as planning or action that then intervened for it and held, and per rule that is
    only a held case (installed at onboarding), with where the evidence is."""
    found, held = [], []
    for entry in record.get("ledger", []):
        criterion = entry["criterion"]
        strong, weak = [], []
        for landing in entry.get("sedimented_in", []):
            if landing.get("facet") not in HARD:
                continue
            after = landing["round"]
            before, held_in, holds = _turned(record, entry, landing)
            rows, interventions = _acted(record, criterion, landing)
            evidence = {
                "sedimented": landing,
                "failed_in": before,
                "held_in": held_in,
                "mechanism_rows": rows,
                "interventions": interventions,
            }
            if before and holds and interventions:
                strong.append(evidence)
            elif after == 0 and holds and interventions:
                weak.append(evidence)
        if strong:
            found.append(_case(entry, strong))
        elif weak:
            held.append(_case(entry, weak))
    return found, held


def _isolated(scan) -> dict:
    if not isinstance(scan, dict):
        return {"ok": False, "detail": "the run was not scanned for isolation"}
    if scan.get("valid") is None:
        return {"ok": False, "detail": "the isolation scan found nothing to check, so the run is unverified"}
    if scan.get("valid") is True:
        return {"ok": True, "detail": f"no tool call in {scan.get('logs')} logs left the run's bounds"}
    kinds = sorted({finding.get("kind") for finding in scan.get("findings") or []})
    return {"ok": False, "detail": f"{scan.get('breaches')} tool calls left the run's bounds ({', '.join(kinds)})"}


def judge(record: dict) -> dict:
    """Whether the run qualifies, is partial or shows nothing, with each condition and a score to rank runs by."""
    completed = _completed(record)
    grown = cultivated(record)
    found, held = cases(record)
    delivered = bool(completed) and _delivered(completed[-1])
    met = core(record)
    checks = {
        "rounds": {"ok": len(completed) >= MIN_ROUNDS, "detail": f"{len(completed)} completed rounds after onboarding"},
        "cultivated": {
            "ok": len(grown) >= CULTIVATED,
            "detail": f"{len(grown)} rules went from failing to holding after a curation answered them",
        },
        "hard": {
            "ok": any(case["interventions"] for case in (*found, *held)),
            "detail": f"{len(found)} rules went from failing to holding under planning or action that intervened; "
            f"{len(held)} rules held from onboarding under planning or action that intervened",
        },
        "core": met,
        "delivered": {
            "ok": delivered,
            "detail": "every drill of the last round delivered a deck"
            if delivered
            else "the last round lacks a deck in some drill",
        },
        "isolated": _isolated(record.get("isolation")),
    }
    breached = isinstance(record.get("isolation"), dict) and record["isolation"].get("valid") is False
    if breached:
        verdict = "invalid"
    elif all(check["ok"] for check in checks.values()):
        verdict = "qualifies"
    elif grown or found or held:
        verdict = "partial"
    else:
        verdict = "none"
    red = sum(case["severity"] == "red_line" for case in found)
    intervened = sum(bool(case["interventions"]) for case in found)
    score = (
        5 * len(grown)
        + 3 * sum(item["severity"] == "red_line" for item in grown)
        + (5 if met["ok"] else 0)
        + 6 * red
        + 4 * (len(found) - red)
        + 4 * intervened
        + 2 * len(held)
        + len(completed)
        + (2 if delivered else 0)
    )
    return {
        "verdict": verdict,
        "score": score,
        "checks": checks,
        "cultivated": grown,
        "cases": found,
        "held_cases": held,
        "run": {key: record.get("run", {}).get(key) for key in ("name", "chain", "status")},
    }


def cli():
    parser = argparse.ArgumentParser(description="Judge whether a cultivation run shows the value case.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args()
    from .record import build_record

    print(json.dumps(judge(build_record(args.run_dir, state_root=args.state_root)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
