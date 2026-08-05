import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

/** The tool whose results carry a DAG manifest. */
export const RUN_SUBAGENT_DAG_TOOL = 'run_subagent_dag';

/** Per-node status vocabulary shared by the live overlay and the manifest.
 *
 * `interrupted` never comes off the wire: the runner only ever reports the
 * other four terminal states. It is what a *restored* run reports for a node
 * the instance registry still calls running even though the DAG tool is no
 * longer executing that run — a gateway restart, or a dropped terminal write.
 * Distinct from `failed` (the node itself errored) and from `skipped` (a
 * dependency did).
 *
 * `reused` is not a state of *this* run at all: it marks a node from an earlier
 * run whose output this run reads instead of recomputing (see
 * `deriveReusedNodes`). `toStatus` deliberately does not accept the name, so
 * nothing on the wire can claim a node was reused — only the client, which is
 * the only side that sees the cross-run reference. */
export type DagNodeStatus =
	| 'pending'
	| 'running'
	| 'completed'
	| 'failed'
	| 'skipped'
	| 'interrupted'
	| 'reused';

/** Translation key per status, for every surface that shows one as text.
 *
 * A `Record` over the union rather than a `dag.status.${s}` template, so adding
 * a status without translating it is a type error instead of a key that renders
 * raw. Deliberately separate from `subagent-monitor.nodeStatus.*`: that labels
 * the *registry's* vocabulary (which has `idle` / `cancelled` and no `reused`)
 * in shorthand tuned for a narrow side panel, so the two are not interchangeable
 * despite the overlap. */
export const DAG_STATUS_LABEL_KEY: Record<DagNodeStatus, string> = {
	pending: 'dag.status.pending',
	running: 'dag.status.running',
	completed: 'dag.status.completed',
	failed: 'dag.status.failed',
	skipped: 'dag.status.skipped',
	interrupted: 'dag.status.interrupted',
	reused: 'dag.status.reused',
};

/** One node entry in a run_subagent_dag result manifest. */
export interface DagFileEntry {
	node: string;
	subagent?: string;
	/** Optional stateful instance handle naming the recurring conversation. */
	instance?: string | null;
	depends_on?: string[];
	status: string;
	/** Epoch ms when the node transitioned to "running"; null if it never ran
	 *  (e.g. cascade-skipped before dispatch). */
	started_at?: number | null;
	/** Epoch ms when the node reached a terminal status; null while pending. */
	ended_at?: number | null;
	prompt_file?: string | null;
	output_file?: string | null;
	error?: string | null;
}

export interface DagSummary {
	total: number;
	completed: number;
	failed: number;
	skipped: number;
}

export interface DagTerminalOutput {
	node: string;
	text: string;
}

/** The structured metadata a run_subagent_dag tool result carries. */
export interface DagManifest {
	run_id: string;
	dir?: string;
	files: DagFileEntry[];
	summary?: DagSummary;
	terminal_outputs: DagTerminalOutput[];
}

/**
 * Read the DAG manifest from a tool_result block's metadata, returning `null`
 * when absent or malformed (mirrors the delivery `readManifest` guard).
 */
export function readDagManifest(metadata: Record<string, unknown> | undefined): DagManifest | null {
	if (!metadata) return null;
	const runId = metadata.run_id;
	const files = metadata.files;
	if (typeof runId !== 'string' || !Array.isArray(files)) return null;
	const terminal = Array.isArray(metadata.terminal_outputs)
		? (metadata.terminal_outputs as DagTerminalOutput[])
		: [];
	return {
		run_id: runId,
		dir: typeof metadata.dir === 'string' ? metadata.dir : undefined,
		files: files as DagFileEntry[],
		summary: (metadata.summary as DagSummary) ?? undefined,
		terminal_outputs: terminal,
	};
}

/** Coerce an arbitrary status string to a known `DagNodeStatus`. A cancelled
 *  node lands on `interrupted`: both mean "stopped before it could finish",
 *  and neither may fall through to `pending`, which would draw a node that is
 *  never going to run again as though it were still queued. */
export function toStatus(s: string | undefined): DagNodeStatus {
	if (s === 'cancelled') return 'interrupted';
	return s === 'running' ||
		s === 'completed' ||
		s === 'failed' ||
		s === 'skipped' ||
		s === 'interrupted'
		? s
		: 'pending';
}

/**
 * Resolve a node's status from the manifest and the live overlay.
 *
 * The manifest wins as soon as it reports a terminal status; the overlay only
 * fills the gap while a run is still in flight. Letting the overlay win
 * outright lets one lost `dag_node_updated` pin a node to `running` forever,
 * even after the authoritative manifest arrives saying it completed.
 */
export function resolveStatus(
	manifestStatus: string | undefined,
	liveStatus: string | undefined,
): DagNodeStatus {
	const fromManifest = toStatus(manifestStatus);
	if (fromManifest !== 'pending' && fromManifest !== 'running') return fromManifest;
	return toStatus(liveStatus ?? manifestStatus);
}

/**
 * Bind each `run_subagent_dag` tool-call to its own `run_id`.
 *
 * The k-th run-PRODUCING DAG call in transcript order (i.e. excluding
 * calls whose result errored — a validation error produced no run) maps
 * to the k-th run in chronological order. Correct under the
 * single-active-run invariant (the leader runs DAGs sequentially), and
 * reload-safe because both inputs rebuild from durable state.
 *
 * Runs are ordered by `run_id`, not `created_at`: raven mints a run id as
 * `<UTC timestamp>-<8 hex>` (`make_run_id`), so it sorts chronologically on
 * its own. `created_at` cannot be used — `dag_run_started` does not carry
 * one, so it is `''` for every live run, and runs restored from disk have no
 * timestamp to supply either.
 */
export function buildRunIdByToolCallId(
	msgs: Msg[],
	dagRuns: Record<string, unknown>,
): Record<string, string> {
	const callIds: string[] = [];
	const errored = new Set<string>();
	const resultState = new Map<string, string | undefined>();
	// First pass: collect tool_result states by id.
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const b of blocks) {
			if (b.type === 'tool_result') resultState.set(b.id, b.state);
		}
	}
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const b of blocks) {
			if (b.type === 'tool_call' && b.name === RUN_SUBAGENT_DAG_TOOL) {
				if (resultState.get(b.id) === 'error') errored.add(b.id);
				callIds.push(b.id);
			}
		}
	}
	const producing = callIds.filter((id) => !errored.has(id));
	const orderedRunIds = Object.keys(dagRuns).sort((a, b) => a.localeCompare(b));
	const map: Record<string, string> = {};
	producing.forEach((callId, i) => {
		if (i < orderedRunIds.length) map[callId] = orderedRunIds[i];
	});
	return map;
}
