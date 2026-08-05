import { createContext, useContext } from 'react';

/** One node in a live DAG run's structure (from `dag_run_started`). */
export interface DagRunLiveNode {
	id: string;
	subagent?: string;
	/** Optional stateful instance handle naming the recurring conversation. */
	instance?: string | null;
	depends_on: string[];
}

/** Per-node wall-clock timing captured from live progress events. */
export interface DagNodeTiming {
	/** Epoch ms when the node transitioned to "running". */
	startedAt?: number;
	/** Epoch ms when the node reached a terminal status. */
	endedAt?: number;
}

/** Live state for one DAG run: its node structure + a node→status map. */
export interface DagRunLive {
	nodes: DagRunLiveNode[];
	byNode: Record<string, string>;
	/** Per-node timing annotations; present once ``dag_node_updated``
	 *  events have flowed, persists across terminal transitions. */
	timings?: Record<string, DagNodeTiming>;
	created_at?: string;
	manifest?: Record<string, unknown>;
}

/** The shared live-DAG state exposed to tool renderers. */
export interface DagRunsState {
	dagRuns: Record<string, DagRunLive>;
	latestRunId: string | null;
	runIdByToolCallId: Record<string, string>;
}

/**
 * Live per-run DAG node status, keyed by server-generated `run_id`. Populated
 * by `ChatViewport` from `dag_run_started` / `dag_node_updated` CustomEvents.
 * The default (no provider) is empty, so a renderer safely falls back to the
 * final result metadata / call args.
 */
export const DagRunsContext = createContext<DagRunsState>({
	dagRuns: {},
	latestRunId: null,
	runIdByToolCallId: {},
});

/** Read the current live-DAG state. */
export function useDagRuns(): DagRunsState {
	return useContext(DagRunsContext);
}
