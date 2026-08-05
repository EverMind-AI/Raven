import { createContext, useContext } from 'react';

/** Live status of one stateful sub-agent instance (keyed by handle). */
export interface InstanceStatus {
	status: string;
	transport: string;
	agentId?: string;
	prototype: string;
	action?: string;
}

/** Shared live instance-status map exposed to the instance monitor. */
export interface SubagentInstancesState {
	instances: Record<string, InstanceStatus>;
}

/**
 * Live per-instance status keyed by handle. Populated by `ChatViewport`
 * from `subagent_instance_updated` CustomEvents (live + durable replay).
 */
export const SubagentInstancesContext = createContext<SubagentInstancesState>({
	instances: {},
});

/** Read the current live instance-status map. */
export function useSubagentInstances(): SubagentInstancesState {
	return useContext(SubagentInstancesContext);
}
