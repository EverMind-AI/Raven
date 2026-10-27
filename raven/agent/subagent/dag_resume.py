# -*- coding: utf-8 -*-
"""Read a run back with live state overlaid, for a client that missed the events.

The ``dag_*`` progress events are not replayed anywhere, so a client that was not
listening for all of them -- a reloaded browser tab, a TUI whose gateway
restarted mid-run -- can only rebuild the graph from disk. An unfinalized run has
no ``manifest.json``, so every node reads back ``pending``; the instance registry
rows are the only durable record of how far it actually got.

Shared by both RPC surfaces so a resumed graph cannot mean two different things
depending on which client asked.
"""

from collections.abc import Callable
from typing import Any

from raven.agent.subagent.dag_reader import attempt_fields, registry_outcome
from raven.agent.subagent.dag_store import tally_node_statuses
from raven.agent.subagent.instances import get_registry
from raven.config.agent_names import canonical_agent_name


async def read_run_reconciled(
    tool: Any, run_id: str, session_key: str | None, *, live_runs: "Callable[[], set[str]] | None" = None
) -> dict:
    """One run's state, with the instance registry overlaid when it is unfinalized.

    Args:
        tool (`SubAgentDagTool`):
            The live tool, read for the run dir. Its own ``active_run_ids`` is
            the fallback for liveness, and a narrow one: ``_cancels`` is
            per-instance, so the registered tool answers ``False`` for a run the
            playbook engine's private tool owns, and every unfinished node of it
            gets overlaid ``interrupted``.
        live_runs (`Callable[[], set[str]] | None`):
            Liveness across *every* graph tool, which is what a caller holding
            the agent loop can supply (``subagent.dag_live.live_run_ids``). The
            fourth consumer of this question and the one that was missed: the two
            ``subagents.*`` readers and the web cancel were routed through the
            loop, this one kept reading the tool, so a backgrounded playbook run
            reopened from history came back with every running node marked
            interrupted.
        run_id (`str`):
            The run to read.
        session_key (`str | None`):
            Names the session whose working directory holds the run dir, and
            scopes the registry rows consulted for the overlay.

    Returns:
        `dict`:
            The manifest-shaped payload from :func:`dag_reader.read_run`, with
            per-node status/timestamps and the summary recomputed from the
            overlay when the run had not finalized, and each node's branch
            outcome (``reason``, ``selected_routes``, ``route_evidence``) filled
            from its row. A node whose row records a reassignment is reported
            under the agent, instance and attempt history that row names, so a
            reload does not fall back to the agent ``graph.json`` submitted.
    """
    run = await tool.read_run(run_id, session_key)
    if run.get("finalized"):
        return run

    rows = {
        row.get("nodeId"): row
        for row in get_registry().list_instances(session_key)
        if row.get("kind") == "dag-node" and row.get("runId") == run_id
    }
    # A run this tool is no longer executing cannot have a live node, whatever
    # the rows say -- the gateway may have restarted, or a terminal write may
    # have been dropped. Without this the resumed graph pins those nodes to
    # "running" forever.
    live = run_id in (live_runs() if live_runs is not None else set(tool.active_run_ids()))
    statuses: list[str] = []
    for entry in run["files"]:
        row = rows.get(entry["node"])
        if row is not None:
            status = row.get("status") or entry["status"]
            if status in ("pending", "running", "exception") and not live:
                status = "interrupted"
            entry["status"] = status
            # A node's first registry write is its "running" transition, so
            # createdAtMs doubles as the start time a client needs to resume its
            # live duration counter.
            entry["started_at"] = entry["started_at"] or row.get("createdAtMs")
            if status not in ("pending", "running", "exception"):
                entry["ended_at"] = entry["ended_at"] or row.get("updatedAtMs")
            # Only the keys the entry lacks: a value it already has is the manifest's.
            for key, value in registry_outcome(row).items():
                entry.setdefault(key, value)
            entry.update(attempt_fields(entry, row))
            if "attemptAgents" in row:
                # A node the row says was reassigned: it is the durable record of
                # who runs the node now, and graph.json names the agent that gave
                # up. Explicit nulls are taken as written -- a stateless attempt
                # clears the handle the submitted node carried.
                entry["subagent"] = canonical_agent_name(str(row.get("agent") or ""))
                entry["instance"] = row.get("nodeInstance")
                entry["instance_auto"] = bool(row.get("nodeInstanceAuto"))
        statuses.append(entry["status"])
    run["summary"] = tally_node_statuses(statuses)
    return run
