import type { RavenDagRun, RavenSubagentInstance } from '@/api';
import type { DagNodeTiming, DagRunLive } from '@/components/chat/DagRunsContext';

/**
 * Rebuilding a session's DAG graphs after a page reload.
 *
 * `dagRuns` is fed exclusively by the `dag_run_started` / `dag_node_updated` /
 * `dag_run_completed` CustomEvents, which live for exactly one page session:
 * nothing replays them, and in gateway mode the tool result carries no manifest
 * metadata either. So a reload used to redraw every finished graph as
 * all-pending, and a reload mid-run left it frozen there.
 *
 * The durable record is the run dir raven writes under
 * `<workspace>/.ravenx_dag/<run_id>/`, reachable via `raven.subagents.dag.get`.
 * Discovering *which* runs belong to this session goes through the instance
 * registry, which is already session-scoped and already survives a reload.
 */

/** Run ids this session touched, newest first, from its `dag-node` rows. */
export function dagRunIdsOf(rows: RavenSubagentInstance[]): string[] {
	const seen = new Set<string>();
	for (const row of rows) {
		if (row.kind === 'dag-node' && row.runId) seen.add(row.runId);
	}
	// Ids are `<UTC timestamp>-<8 hex>`, so a plain descending sort is newest
	// first without needing the rows' own timestamps.
	return [...seen].sort((a, b) => b.localeCompare(a));
}

/** Convert one restored run into the live-overlay shape the renderers read. */
export function toDagRunLive(run: RavenDagRun): DagRunLive {
	const byNode: Record<string, string> = {};
	const timings: Record<string, DagNodeTiming> = {};
	for (const f of run.files) {
		byNode[f.node] = f.status;
		if (f.started_at != null || f.ended_at != null) {
			timings[f.node] = {
				startedAt: f.started_at ?? undefined,
				endedAt: f.ended_at ?? undefined,
			};
		}
	}
	return {
		nodes: run.files.map((f) => ({
			id: f.node,
			subagent: f.subagent,
			instance: f.instance ?? null,
			depends_on: f.depends_on ?? [],
		})),
		byNode,
		timings,
		// Only a finalized run gets a manifest. An unfinalized one is a partial
		// picture stitched from the instance registry, and handing it over as a
		// manifest would let the graph draw a summary strip for a run still in
		// flight and let a not-yet-written node status read as authoritative.
		manifest: run.finalized
			? {
					run_id: run.run_id,
					dir: run.dir,
					files: run.files,
					summary: run.summary,
					terminal_outputs: run.terminal_outputs ?? [],
				}
			: undefined,
	};
}

/** How far along a status is. Merges take the higher rank, never the newer
 *  writer, so neither source can walk a node backwards. */
function rank(status: string | undefined): number {
	if (status === 'running') return 1;
	if (status === undefined || status === 'pending') return 0;
	return 2;
}

/**
 * Fold one restored run into whatever the live map already holds for it.
 *
 * Both inputs can be the stale one, so neither wins outright:
 *
 *  - restored is older when a reload lands mid-run and the event stream then
 *    carries the run forward (an unfinalized restore reads its statuses off the
 *    instance registry, which lags the events);
 *  - the live copy is older when the events stopped arriving — the exact case
 *    a reload creates — and a later poll of the same run is what advances it.
 *
 * So a node's status merges by rank, which is monotonic and therefore
 * order-independent: a node that reached `completed` in either source stays
 * completed, and a `pending` on one side never overwrites a `running` on the
 * other. Same reasoning as `resolveStatus`, one layer down.
 *
 * Returns `prev` by identity when the restore adds nothing, so a poll that
 * finds no change does not re-render every consumer of the map.
 */
export function mergeRestoredRun(prev: DagRunLive | undefined, restored: DagRunLive): DagRunLive {
	if (!prev) return restored;

	let changed = false;
	const byNode: Record<string, string> = { ...prev.byNode };
	for (const [node, status] of Object.entries(restored.byNode)) {
		if (rank(status) > rank(byNode[node])) {
			byNode[node] = status;
			changed = true;
		}
	}

	const timings: Record<string, DagNodeTiming> = { ...prev.timings };
	for (const [node, t] of Object.entries(restored.timings ?? {})) {
		const cur = timings[node];
		const startedAt = cur?.startedAt ?? t.startedAt;
		const endedAt = cur?.endedAt ?? t.endedAt;
		if (startedAt !== cur?.startedAt || endedAt !== cur?.endedAt) {
			timings[node] = { startedAt, endedAt };
			changed = true;
		}
	}

	const nodes = prev.nodes.length > 0 ? prev.nodes : restored.nodes;
	if (nodes !== prev.nodes) changed = true;
	const manifest = prev.manifest ?? restored.manifest;
	if (manifest !== prev.manifest) changed = true;

	if (!changed) return prev;
	return { nodes, byNode, timings, created_at: prev.created_at, manifest };
}

/** Apply a batch of restored runs to the map, preserving identity on a no-op. */
export function mergeRestoredRuns(
	prev: Record<string, DagRunLive>,
	restored: RavenDagRun[],
): Record<string, DagRunLive> {
	let changed = false;
	const next = { ...prev };
	for (const run of restored) {
		const merged = mergeRestoredRun(prev[run.run_id], toDagRunLive(run));
		if (merged !== prev[run.run_id]) {
			next[run.run_id] = merged;
			changed = true;
		}
	}
	return changed ? next : prev;
}
