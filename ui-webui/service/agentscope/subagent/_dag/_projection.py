# -*- coding: utf-8 -*-
"""Pure folding of DAG progress events into a durable projection entry.

Kept free of any ``agentscope.app`` import so it stays in the
``subagent`` layer; the app-facing write (upsert/publish) lives in
``_agent_tools.build_dag_progress_publisher``.
"""


def fold_dag_run_entry(
    prev: dict | None,
    name: str,
    value: dict,
    created_at: str,
) -> dict | None:
    """Fold one progress event into a self-contained per-run entry.

    Args:
        prev (`dict | None`):
            The accumulated entry so far, or ``None`` before the run
            started.
        name (`str`):
            The event name (``dag_run_started`` / ``dag_node_updated`` /
            ``dag_run_completed``).
        value (`dict`):
            The event payload.
        created_at (`str`):
            Timestamp stamped onto a new entry (used only for
            ``dag_run_started``).

    Returns:
        `dict | None`:
            The updated entry, or ``None`` when an update arrives before
            the run started (dropped).
    """
    if name == "dag_run_started":
        nodes = value.get("nodes", [])
        return {
            "run_id": value.get("run_id"),
            "created_at": created_at,
            "nodes": nodes,
            "byNode": {n["id"]: "pending" for n in nodes},
        }
    if prev is None:
        return None
    entry = {**prev, "byNode": dict(prev["byNode"])}
    if name == "dag_node_updated":
        entry["byNode"][value["node"]] = value["status"]
    elif name == "dag_run_completed":
        entry["manifest"] = value["manifest"]
    return entry
