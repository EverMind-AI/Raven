"""What the installed mechanisms did in a round, counted from the execution records.

Planning, participant, component and strategy rows say when a mechanism of the harness ran and what it decided
(passed, sent work back, ended a turn, refused a tool or a planning step). The Analyst attaches this activity to its
feedback so the Curator can check its own revision against facts; a record of the run can classify the same rows.

Each decision counts once: a loop rollback is the loop carrying out a resample already counted, and a planning tool
error repeats the strategy's own. An action strategy's decision is what the host applied (its `review` callback, in
the host's own verdicts), not the strategy's `.result`, whose words are the strategy's own (a generated reviewer may
say `revise` for what the host carries out as a resample); that result is kept as `assessed`. Other strategies'
`.callback` rows are the host applying a `.result` already counted.
"""

import json
import re
from collections import defaultdict

STRATEGY_ROWS = {
    "planning": "planning.strategy",
    "action": "action.strategy",
    "capability": "capability.strategy",
    "memory": "memory.strategy",
}
COMPONENT_TARGETS = {
    "tools": "capability.tools",
    "hooks": "action.hooks",
    "tool_gates": "action.tool_gates",
    "services": "action.services",
    "memory_backends": "memory.backends",
    "context_engines": "memory.context_engine",
    "session_observers": "memory.session_observers",
}
INTERVENTIONS = frozenset({"resample", "end", "refused"})
REVIEW_VERDICTS = frozenset({"accept", "resample", "end"})
REFUSAL = re.compile(r"requires user approval|refus|blocked|denied|not allowed|not permitted", re.IGNORECASE)
# Refusals that come from the host's own guards or argument checks, not from a mechanism of the harness.
NOT_MECHANISM = re.compile(
    r"requires user approval|outside allowed directories|outside (?:the )?working dir|extra inputs are not permitted"
    r"|validation error|not configured",
    re.IGNORECASE,
)
SUMMARY_LIMIT = 300
REASONS = 3
STILL = 3


def cut(value, limit: int = SUMMARY_LIMIT) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list | tuple) else []


def scope_of(value) -> str:
    if value in (None, "", "root"):
        return "root"
    if isinstance(value, dict):
        for key in ("name", "node", "harness", "id"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).removeprefix("child/")


def decision_of(result) -> str | None:
    if isinstance(result, dict):
        for key in ("verdict", "decision", "action", "status"):
            if isinstance(result.get(key), str):
                return result[key]
    return None


def reviewed(applied: dict) -> str:
    """Why the host sent a step back: the reviewer's reason, with the correction it injected when the reason is only a
    rule code."""
    reason = str(applied.get("reason") or "")
    injected = " ".join(
        str(message.get("content") or "") for message in dicts(applied.get("inject")) if message.get("content")
    )
    return reason if len(reason) >= 40 or not injected else injected


def participant(result) -> tuple[str, str]:
    if result is None:
        return "none", "no change"
    if isinstance(result, dict) and decision_of(result):
        return decision_of(result), cut(result.get("reason") or result.get("note") or result)
    if isinstance(result, list):
        names = [
            str((item.get("function") or {}).get("name") or item.get("name") or "")
            if isinstance(item, dict)
            else str(item)
            for item in result
        ]
        return "result", cut(f"{len(result)} entries: " + ", ".join(name for name in names if name))
    return "result", cut(result)


def strategy_row(kind: str, row: dict) -> tuple[str, str, str] | None:
    target = STRATEGY_ROWS[kind.split(".", 1)[0]]
    if kind.endswith(".call") or kind == "planning.observation":
        return None
    if kind == "planning.error" and row.get("operation") == "tool":
        return None
    if kind == "planning.error" and str(row.get("error") or "").startswith("ValueError:"):
        # A planning strategy rejects a step it does not allow by raising ValueError from `revise`.
        return target, "refused", cut(str(row["error"]).removeprefix("ValueError:").strip())
    if kind.endswith(".error"):
        return target, "error", cut(row.get("error"))
    if kind == "action.callback" and row.get("operation") == "review":
        applied = row.get("result") if isinstance(row.get("result"), dict) else {}
        if applied.get("verdict") in REVIEW_VERDICTS:
            return target, applied["verdict"], cut(reviewed(applied))
    if kind.endswith(".callback"):
        return target, "applied", cut(row.get("result") if row.get("result") is not None else row.get("operation"))
    if kind == "planning.context":
        return target, "context", cut(row.get("content"))
    if kind == "memory.context":
        return target, "compacted" if row.get("compacted") else "assembled", f"{row.get('estimated_tokens')} tokens"
    if kind == "planning.result":
        return target, str(row.get("operation") or "result"), cut(row.get("result"))
    if kind == "action.result":
        return target, "assessed", cut(row.get("result"))
    if kind.endswith(".result"):
        return target, decision_of(row.get("result")) or str(row.get("operation") or "result"), cut(row.get("result"))
    return (
        target,
        kind.split(".", 1)[1],
        cut({key: value for key, value in row.items() if key not in ("kind", "turn_id")}),
    )


def classify(row: dict, tools: dict) -> tuple[str, str, str, str] | None:
    """(kind, target, decision, summary) for a mechanism row, or None for rows that are not mechanism evidence."""
    kind = str(row.get("kind") or "")
    family = kind.split(".", 1)[0]
    if kind == "runner.event" and row.get("event_type") == "ToolEvent":
        event = row.get("event") or {}
        if event.get("phase") == "start":
            tools[event.get("tool_call_id")] = event.get("name")
        preview = str(event.get("result_preview") or "")
        if (
            event.get("phase") == "complete"
            and event.get("ok") is False
            and REFUSAL.search(preview)
            and not NOT_MECHANISM.search(preview)
        ):
            name = tools.get(event.get("tool_call_id")) or event.get("name") or "tool"
            return "tool.refused", f"tool:{name}", "refused", cut(preview)
        return None
    if family == "participant" and kind != "participant.call":
        target = str(row.get("target") or ",".join(row.get("targets") or []) or "participant")
        if kind == "participant.result":
            return (kind, target, *participant(row.get("result")))
        if kind == "participant.skipped":
            return kind, target, "skipped", f"phase {row.get('phase')}"
        return kind, target, "error" if kind.endswith("error") else kind.split(".", 1)[1], cut(row.get("error"))
    if family == "component" and kind != "component.call":
        target = COMPONENT_TARGETS.get(str(row.get("kind_name")), f"component.{row.get('kind_name')}")
        decision = "error" if kind.endswith("error") else kind.split(".", 1)[1]
        return kind, target, decision, cut(row.get("error") or f"{row.get('name')} from {row.get('factory')}")
    if family in STRATEGY_ROWS:
        found = strategy_row(kind, row)
        return (kind, *found) if found else None
    if kind.startswith("strategy.inference"):
        return kind, "strategy.inference", "error" if kind.endswith("error") else "inference", cut(row.get("error"))
    if family == "hosting":
        return kind, "hosting", kind.split(".", 1)[-1], cut(row.get("revision"))
    if kind == "loop.control" and (row.get("rollbacks") or row.get("rollbacks_refused")):
        rolled, refused = int(row.get("rollbacks") or 0), int(row.get("rollbacks_refused") or 0)
        return kind, "loop", "rollback" if rolled else "rollback_refused", f"{rolled} honoured, {refused} refused"
    return None


def scoped(rows, scope: str = "root"):
    """(harness scope, row) for each row, unpacking the child-process records a `child.execution` row carries."""
    for row in rows:
        if row.get("kind") == "child.execution":
            yield from scoped(dicts(row.get("records")), scope_of(row.get("harness")))
        else:
            yield scope, row


def _visible(row: dict) -> bool:
    """Whether a root row put something in front of the user: a reply or a delivered file."""
    event = row.get("event") or {}
    if row.get("kind") != "runner.event":
        return False
    if row.get("event_type") == "Text":
        return bool(str(event.get("content") or "").strip())
    return (
        row.get("event_type") == "ToolEvent"
        and event.get("phase") == "complete"
        and event.get("ok") is not False
        and bool((event.get("metadata") or {}).get("raven_delivery"))
    )


def _failed_tool(row: dict, tools: dict) -> tuple[str, str] | None:
    event = row.get("event") or {}
    if row.get("kind") != "runner.event" or row.get("event_type") != "ToolEvent" or event.get("phase") != "complete":
        return None
    if event.get("ok") is not False:
        return None
    return f"tool:{tools.get(event.get('tool_call_id')) or event.get('name') or 'tool'}", cut(
        event.get("result_preview")
    )


def activity(sessions) -> list[dict]:
    """Per session and mechanism: how often it ran, what it decided and a few of the reasons it gave.

    One row per (session, harness scope, target, decision); `acted` marks decisions that changed what happened
    (sent back, ended, refused). Beside the mechanisms' own rows it states three facts the records show:
    - tools that failed (`tool:<name>`, decision `failed`, with the error), such as a file that could not be opened;
    - a planning state that never changed over a session (`state_unchanged`), however far the conversation went;
    - interventions made after a reply or a file had already become visible to the user in that turn
      (`after_visible_output`), which a rollback cannot take back.
    Counted from records only, never judged.
    """
    groups: dict[tuple, dict] = {}
    reasons: dict[tuple, list[str]] = defaultdict(list)

    def add(session, scope, target, decision, summary, late=False):
        key = (session, scope, target, decision)
        group = groups.setdefault(
            key,
            {
                "session": session,
                "scope": scope,
                "target": target,
                "decision": decision,
                "count": 0,
                "acted": decision in INTERVENTIONS,
            },
        )
        group["count"] += 1
        if late:
            group["after_visible_output"] = group.get("after_visible_output", 0) + 1
        if summary and summary not in reasons[key] and len(reasons[key]) < REASONS:
            reasons[key].append(summary)

    for session, exchanges in sessions.items():
        states: dict[str, list[str]] = defaultdict(list)
        for exchange in exchanges:
            tools: dict = {}
            seen = False
            for scope, row in scoped(exchange.execution.records):
                found = classify(row, tools)
                if found is not None:
                    kind, target, decision, summary = found
                    add(session, scope, target, decision, summary, late=seen and decision in INTERVENTIONS)
                    if kind == "planning.result":
                        states[scope].append(json.dumps(row.get("result"), sort_keys=True, default=str))
                elif scope == "root" and (failure := _failed_tool(row, tools)):
                    add(session, scope, failure[0], "failed", failure[1])
                seen = seen or (scope == "root" and _visible(row))
        for scope, results in states.items():
            if len(results) >= STILL and len(set(results)) == 1:
                key = (session, scope, "planning.strategy", "state_unchanged")
                groups[key] = {
                    "session": session,
                    "scope": scope,
                    "target": "planning.strategy",
                    "decision": "state_unchanged",
                    "count": len(results),
                    "acted": False,
                }
                reasons[key] = [f"the plan state was the same in all {len(results)} results: {cut(results[0])}"]
    return [{**group, "reasons": reasons[key]} for key, group in groups.items()]
