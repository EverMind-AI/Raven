import type { Edge, Node } from '@xyflow/react';

import type { DagNodeStatus } from './deriveDag';

/** A node ready to render — structure, status, and click-detail fields. */
export interface DagVizNode {
	id: string;
	subagent?: string;
	/** Optional stateful instance handle bound to this DAG node. */
	instance?: string | null;
	depends_on: string[];
	status: DagNodeStatus;
	/** Final or current running duration in seconds. `null` when
	 *  unknown (e.g. completed before any timing data was captured
	 *  across a page reload). */
	durationSec?: number | null;
	/** Epoch ms when the node transitioned to "running"; powers the
	 *  live ticking duration badge until the node reaches terminal. */
	startedAt?: number | null;
	outputFile?: string | null;
	promptFile?: string | null;
	/** The node's `prompt_template` as submitted in the tool call. Available
	 *  from the call args alone, so it survives a reload even when the run
	 *  dir is gone. */
	promptTemplate?: string | null;
	error?: string | null;
	terminalOutput?: string | null;
	/** Set on a node carried over from an earlier run: which run and which node
	 *  its output was read from. Its own status is always `reused`. */
	reusedFrom?: { runId: string; nodeId: string } | null;
	/** Synthetic ids of the reused nodes this node reads. Kept apart from
	 *  `depends_on` so the detail pane's dependency list stays a statement about
	 *  this run's graph; the layout treats both alike. */
	reuseDeps?: string[];
}

/** The `data` payload carried by each React Flow node. Must be an object
 * type (not an interface) so it satisfies React Flow's `Record` bound. */
export type DagNodeData = {
	label: string;
	subagent?: string;
	status: DagNodeStatus;
	/** Final duration in seconds; ``undefined`` while unknown. */
	durationSec?: number | null;
	/** Epoch ms when the node transitioned to "running"; drives the
	 *  live ticking duration badge while the node is still running. */
	startedAt?: number | null;
	/** Origin of a reused node, for the box's hover title. */
	reusedFrom?: { runId: string; nodeId: string } | null;
};

export type DagFlowNode = Node<DagNodeData, 'dagNode'>;

const COL_WIDTH = 220;
const ROW_HEIGHT = 96;

/** Everything a node sits downstream of: its in-run dependencies plus the
 *  reused outputs it reads. Both place it a column to the right of the source,
 *  so the layout makes no distinction — only the edge styling does. */
function depsOf(node: DagVizNode): string[] {
	const reuse = node.reuseDeps ?? [];
	return reuse.length > 0 ? [...node.depends_on, ...reuse] : node.depends_on;
}

/**
 * Lay a DAG out left→right: a node's column is its longest-path depth from a
 * root (a node with no in-graph dependencies); rows within a column are
 * assigned in input order. Deterministic and dependency-free. A `visiting`
 * guard makes it safe even against an accidental cycle.
 */
export function layoutDag(nodes: DagVizNode[]): {
	rfNodes: DagFlowNode[];
	rfEdges: Edge[];
} {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const depthCache = new Map<string, number>();
	const visiting = new Set<string>();

	const depthOf = (id: string): number => {
		const cached = depthCache.get(id);
		if (cached !== undefined) return cached;
		const node = byId.get(id);
		const deps = node ? depsOf(node) : [];
		if (!node || deps.length === 0 || visiting.has(id)) {
			depthCache.set(id, 0);
			return 0;
		}
		visiting.add(id);
		let max = 0;
		for (const dep of deps) {
			if (byId.has(dep)) max = Math.max(max, depthOf(dep) + 1);
		}
		visiting.delete(id);
		depthCache.set(id, max);
		return max;
	};

	const rowCounters = new Map<number, number>();
	const rfNodes: DagFlowNode[] = nodes.map((n) => {
		const depth = depthOf(n.id);
		const row = rowCounters.get(depth) ?? 0;
		rowCounters.set(depth, row + 1);
		return {
			id: n.id,
			type: 'dagNode',
			position: { x: depth * COL_WIDTH, y: row * ROW_HEIGHT },
			data: {
				// A reused node's synthetic id is `reused:<run>:<node>`; label it
				// with the origin node's own name so the box reads like any other.
				label: n.reusedFrom?.nodeId ?? n.id,
				subagent: n.subagent,
				status: n.status,
				durationSec: n.durationSec ?? null,
				startedAt: n.startedAt ?? null,
				reusedFrom: n.reusedFrom ?? null,
			},
		};
	});

	const reuseDepsOf = (n: DagVizNode) => new Set(n.reuseDeps ?? []);

	const rfEdges: Edge[] = [];
	for (const n of nodes) {
		const reused = reuseDepsOf(n);
		for (const dep of depsOf(n)) {
			if (!byId.has(dep)) continue;
			const isReuse = reused.has(dep);
			rfEdges.push({
				id: `${dep}->${n.id}`,
				source: dep,
				target: n.id,
				// A reuse edge carries no in-run causality, so it never animates:
				// nothing is flowing along it while the consumer runs.
				animated: !isReuse && n.status === 'running',
				style: isReuse
					? { strokeDasharray: '4 3', opacity: 0.6 }
					: n.status === 'running'
						? { stroke: 'var(--gold)', strokeWidth: 2 }
						: undefined,
			});
		}
	}
	return { rfNodes, rfEdges };
}
