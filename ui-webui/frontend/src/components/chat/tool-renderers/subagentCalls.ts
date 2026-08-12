/** Raven's single delegation tool. Every sub-agent is reached through it, with
 *  the prototype named in the `agent` argument. */
export const SPAWN_TOOL = 'spawn';

/**
 * Whether a tool call delegates to a sub-agent.
 *
 * Two shapes exist and both must be recognised. A raven gateway registers one
 * `spawn` tool for every sub-agent and names the prototype in its arguments,
 * so the *call* name is always `spawn`. Other deployments register one tool
 * per sub-agent, so the call name is the prototype name and `subagentNames`
 * (built from the configured prototypes) is what identifies it.
 *
 * Matching only the second shape is what made every raven `spawn` call render
 * as an anonymous "Call tool": the set holds prototype names, which a call
 * named `spawn` can never match.
 *
 * Its own module rather than `_shared.tsx` because that file exports
 * components, and a shared constant there trips `react-refresh`.
 */
export function isSubagentCall(name: string, subagentNames?: Set<string>): boolean {
	return name === SPAWN_TOOL || (subagentNames?.has(name) ?? false);
}
