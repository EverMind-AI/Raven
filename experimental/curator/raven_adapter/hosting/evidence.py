"""Collect evidence from live child processes without launching additional instances."""

from collections import Counter

from raven.acp_client.pool import get_pool
from raven.agent.subagent.instances import get_registry

from .acp import INSPECT


async def inspect(client, *, offset=None):
    """A child's report; with `offset`, also every record from there on, fetched page by page."""
    if offset is None:
        return await client.request(INSPECT, {}, timeout=30)
    records, omitted = [], Counter()
    while True:
        report = await client.request(INSPECT, {"offset": offset}, timeout=30)
        records.extend(report["records"])
        omitted.update(report["omitted"])
        if report["next"] >= report["total"]:
            return {**report, "records": records, "omitted": dict(omitted)}
        offset = report["next"]


async def snapshots(names):
    reports = {}
    for name in names:
        for connection in get_pool().live(name):
            report = await inspect(connection.client)
            reports[(name, connection.launch_key, report["pid"])] = report
    return reports


async def record_children(names, before, recorder, conversation, execution):
    instances = get_registry().list_instances(conversation)
    runs = {
        row["payload"]["run_id"]
        for row in execution
        if row["kind"] == "dag.progress" and row.get("payload", {}).get("run_id")
    }
    for name in names:
        for connection in get_pool().live(name):
            current = await inspect(connection.client)
            identity = (name, connection.launch_key, current["pid"])
            offset = before[identity]["total"] if identity in before else 0
            if current["total"] <= offset:
                continue
            report = await inspect(connection.client, offset=offset)
            records = report["records"]
            if not records and not report["omitted"]:
                continue
            sessions = {row.get("conversation") for row in records if row.get("conversation")}
            recorder.add(
                "child.execution",
                harness=name,
                revision=report["revision"],
                launch=connection.launch_key,
                pid=report["pid"],
                log=report["log"],
                omitted=report["omitted"],
                instances=[
                    row
                    for row in instances
                    if row.get("agent") == name and (row.get("runId") in runs or row.get("agentId") in sessions)
                ],
                records=records,
            )
