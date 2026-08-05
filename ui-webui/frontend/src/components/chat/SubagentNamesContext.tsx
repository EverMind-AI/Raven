import { createContext, useContext } from 'react';

/**
 * The set of registered sub-agent tool names for the current user. Used by
 * the tool-call renderers to label a call as "Call Sub-Agent" rather than
 * the generic "Call tool". Empty by default (no sub-agents / no provider).
 */
export const SubagentNamesContext = createContext<Set<string>>(new Set());

/** Read the current sub-agent name set. */
export function useSubagentNames(): Set<string> {
	return useContext(SubagentNamesContext);
}
