# -*- coding: utf-8 -*-
"""Rebuild durable run-state projections as replayable CustomEvents.

Pure helpers used by the SSE generator to re-inject stored DAG-run and
sub-agent-instance state on (re)connect, in a deterministic order so a
client that reloaded after the run ended sees the same picture the live
overlay held.
"""
from typing import TYPE_CHECKING

from ...event import CustomEvent

if TYPE_CHECKING:
    from ._session_projection import SessionProjection

DAG_RUN_KIND = "dag_run"
SUBAGENT_INSTANCE_KIND = "subagent_instance"


def dag_run_replay_events(entries: list[dict]) -> list[CustomEvent]:
    """Rebuild DAG-run projection entries as ordered CustomEvents.

    Args:
        entries (`list[dict]`):
            Stored ``dag_run`` projection payloads.

    Returns:
        `list[CustomEvent]`:
            For each entry: one ``dag_run_started`` (carrying
            ``created_at``), a ``dag_node_updated`` per non-``pending``
            node, then ``dag_run_completed`` when a manifest is present.
    """
    events: list[CustomEvent] = []
    for entry in entries:
        run_id = entry.get("run_id")
        events.append(
            CustomEvent(
                name="dag_run_started",
                value={
                    "run_id": run_id,
                    "created_at": entry.get("created_at"),
                    "nodes": entry.get("nodes", []),
                },
            ),
        )
        for node_id, st in entry.get("byNode", {}).items():
            if st != "pending":
                events.append(
                    CustomEvent(
                        name="dag_node_updated",
                        value={
                            "run_id": run_id,
                            "node": node_id,
                            "status": st,
                        },
                    ),
                )
        if entry.get("manifest") is not None:
            events.append(
                CustomEvent(
                    name="dag_run_completed",
                    value={"run_id": run_id, "manifest": entry["manifest"]},
                ),
            )
    return events


def subagent_instance_replay_events(
    entries: list[dict],
) -> list[CustomEvent]:
    """Rebuild sub-agent-instance projection entries as CustomEvents.

    Args:
        entries (`list[dict]`):
            Stored ``subagent_instance`` projection payloads.

    Returns:
        `list[CustomEvent]`:
            One ``subagent_instance_updated`` event per entry.
    """
    return [
        CustomEvent(name="subagent_instance_updated", value=entry)
        for entry in entries
    ]


async def purge_run_state(
    projection: "SessionProjection",
    session_id: str,
) -> None:
    """Drop this session's DAG-run and instance projection feeds.

    Args:
        projection (`SessionProjection`):
            The shared projection store.
        session_id (`str`):
            The session being deleted.
    """
    await projection.purge(session_id, DAG_RUN_KIND)
    await projection.purge(session_id, SUBAGENT_INSTANCE_KIND)
