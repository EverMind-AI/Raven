import type { DagVizNode } from './layoutDag';
import type { DagRunLive } from '@/components/chat/DagRunsContext';

/**
 * Reused upstream nodes: drawing the work a retry DAG carried over.
 *
 * When a run fails partway, the agent's retry submits a smaller graph that
 * reads the surviving outputs of the earlier run instead of recomputing them,
 * as a cross-run file reference:
 *
 *   "inputs": {"geo": {"file": ".ravenx_dag/<run_id>/geo_plan.out.md"}}
 *
 * Those referenced nodes are not in the retry's own graph, so nothing drew
 * them and the retry looked like it invented its inputs. This module recovers
 * them from the tool call's arguments — the only source that carries `inputs`
 * (neither the manifest nor the `dag_*` events do) and the one that survives a
 * reload, because the call is part of the transcript.
 */

/** Prefix that keeps a ghost node's id from colliding with a real one: the
 *  origin run may well hold a node named the same as one in this run. */
const REUSED_ID_PREFIX = 'reused:';

/** `<workdir>/.ravenx_dag/<run_id>/<node_id>.out.md`, the only path shape that
 *  names a DAG node's output. Both ids have fixed charsets (`make_run_id` and
 *  the graph schema's name pattern), so the split is unambiguous. A path that
 *  points anywhere else is an ordinary file reference, not a reuse. */
const OUTPUT_REF_RE =
	/(?:^|\/)\.ravenx_dag\/(\d{8}T\d{6}Z-[0-9a-f]{8})\/([A-Za-z0-9_-]+)\.out\.md$/;

/** `{{ ref: <path> }}` / `{{ ref_path: <path> }}` — the placeholder form that
 *  reads a file by path rather than through a declared input. */
const REF_PLACEHOLDER_RE = /\{\{\s*ref(?:_path)?:\s*([^}]+?)\s*\}\}/g;

/** Where a reused output came from. */
export interface ReusedRef {
	runId: string;
	nodeId: string;
}

/** The subset of a submitted DAG node this module reads. */
export interface ReuseCallNode {
	id?: string;
	inputs?: unknown;
	prompt_template?: string;
}

/** The synthetic graph id for a reused node. */
export function reusedNodeId(ref: ReusedRef): string {
	return `${REUSED_ID_PREFIX}${ref.runId}:${ref.nodeId}`;
}

/** Parse a file reference into the DAG node whose output it names, or `null`
 *  when the path is not a node output file. */
export function parseReusedOutputRef(path: string): ReusedRef | null {
	const match = OUTPUT_REF_RE.exec(path.trim());
	return match ? { runId: match[1], nodeId: match[2] } : null;
}

/** Every node-output path one submitted node reads, from both reference
 *  channels the render grammar supports: file inputs and `ref` placeholders. */
function refsOfCallNode(node: ReuseCallNode): ReusedRef[] {
	const out: ReusedRef[] = [];
	const inputs = node.inputs;
	if (inputs && typeof inputs === 'object') {
		for (const value of Object.values(inputs as Record<string, unknown>)) {
			if (!value || typeof value !== 'object') continue;
			const file = (value as { file?: unknown }).file;
			if (typeof file !== 'string') continue;
			const ref = parseReusedOutputRef(file);
			if (ref) out.push(ref);
		}
	}
	if (typeof node.prompt_template === 'string') {
		for (const match of node.prompt_template.matchAll(REF_PLACEHOLDER_RE)) {
			const ref = parseReusedOutputRef(match[1]);
			if (ref) out.push(ref);
		}
	}
	return out;
}

/**
 * Map each submitted node to the reused outputs it reads.
 *
 * `currentRunId` is excluded: a reference into the run's own directory is an
 * in-run dependency the graph already draws, not a carried-over result. It is
 * optional because a call whose run has not started yet has no id — and in that
 * case any run id in a path is by construction an earlier run's.
 */
export function collectReuseRefs(
	callNodes: ReuseCallNode[],
	currentRunId?: string,
): Map<string, ReusedRef[]> {
	const byConsumer = new Map<string, ReusedRef[]>();
	for (const node of callNodes) {
		if (typeof node.id !== 'string') continue;
		const refs = refsOfCallNode(node).filter((r) => r.runId !== currentRunId);
		if (refs.length > 0) byConsumer.set(node.id, refs);
	}
	return byConsumer;
}

/**
 * Build one ghost node, filling subagent / status / timing from the origin run
 * when this session has it restored.
 *
 * The status shown is always `reused`, never the origin's own: what matters
 * here is that this run did not execute the node. The origin's real status is a
 * click away, in its own graph.
 */
function buildReusedNode(ref: ReusedRef, origin: DagRunLive | undefined): DagVizNode {
	const originNode = origin?.nodes.find((n) => n.id === ref.nodeId);
	const timing = origin?.timings?.[ref.nodeId];
	const durationSec =
		timing?.startedAt != null && timing?.endedAt != null
			? Math.max(0, (timing.endedAt - timing.startedAt) / 1000)
			: null;
	const manifest = origin?.manifest as
		| { files?: Array<{ node: string; output_file?: string | null }> }
		| undefined;
	const outputFile = manifest?.files?.find((f) => f.node === ref.nodeId)?.output_file ?? null;
	return {
		id: reusedNodeId(ref),
		subagent: originNode?.subagent,
		instance: originNode?.instance ?? null,
		depends_on: [],
		status: 'reused',
		durationSec,
		startedAt: null,
		outputFile,
		reusedFrom: ref,
	};
}

/**
 * Append the reused nodes a call reads and wire them to their consumers.
 *
 * Deduplicated by synthetic id, so two nodes reading the same origin output
 * yield one ghost with two edges. A ref whose consumer is not among
 * `vizNodes` is dropped — it would draw an edge to nothing.
 *
 * Returns `vizNodes` by identity when there is no reuse, so the common case
 * adds no new array for `DagGraph`'s layout signature to churn on.
 */
export function withReusedNodes(
	vizNodes: DagVizNode[],
	callNodes: ReuseCallNode[],
	currentRunId: string | undefined,
	dagRuns: Record<string, DagRunLive>,
): DagVizNode[] {
	const byConsumer = collectReuseRefs(callNodes, currentRunId);
	if (byConsumer.size === 0) return vizNodes;

	const ghosts = new Map<string, DagVizNode>();
	const withDeps = vizNodes.map((node) => {
		const refs = byConsumer.get(node.id);
		if (!refs || refs.length === 0) return node;
		const deps: string[] = [];
		for (const ref of refs) {
			const id = reusedNodeId(ref);
			if (!ghosts.has(id)) ghosts.set(id, buildReusedNode(ref, dagRuns[ref.runId]));
			if (!deps.includes(id)) deps.push(id);
		}
		return { ...node, reuseDeps: deps };
	});
	if (ghosts.size === 0) return vizNodes;
	// Ghosts first so `layoutDag`'s per-column row order puts a reused node
	// above the nodes that consume it rather than trailing them.
	return [...ghosts.values(), ...withDeps];
}

/** How many reused nodes a derived graph carries; drives the summary chip. */
export function reusedCountOf(vizNodes: DagVizNode[]): number {
	return vizNodes.reduce((n, node) => (node.reusedFrom ? n + 1 : n), 0);
}
