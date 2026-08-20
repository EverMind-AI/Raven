import type { ToolCallBlock } from '@agentscope-ai/agentscope/message';
import type { ReactNode } from 'react';

import { parseInput, toolArgClass, toolLabelClass, toolNameClass } from './_shared';
import { subagentBadge, subagentRenderBody } from './DefaultRenderer';
import type { TFunction, ToolCallWithResult, ToolRenderer } from './types';

/**
 * The sub-agent this call delegates to. Raven registers one `spawn` tool for
 * every prototype and names the target in an argument, so the tool name says
 * nothing — without this, two calls to different sub-agents render identically.
 *
 * Both spellings: the argument was renamed `agent` → `subagent` when the field
 * was unified across the agent table, and these are the model's own arguments
 * recorded with the call, so a conversation opened from history hands us calls
 * written before the rename for as long as those transcripts exist. Reading only
 * one name made the fallback fire on *every* call rather than on the rare
 * unconfigured one, which is precisely the failure the paragraph above says this
 * function exists to prevent.
 */
export function spawnAgentName(call: ToolCallBlock): string {
	const input = parseInput(call.input);
	const agent = input.subagent ?? input.agent;
	return typeof agent === 'string' && agent ? agent : call.name;
}

/**
 * Trigger line for a `spawn` call: the sub-agent's name, then the model's own
 * short label for the task.
 *
 * `label` is the tool's display argument ("Optional short label for the task
 * (for display)"). It is written for exactly this slot and was previously
 * discarded, leaving repeated calls to one sub-agent indistinguishable.
 */
function renderHeader(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const input = parseInput(pair.call.input);
	const label = typeof input.label === 'string' ? input.label : '';
	return (
		<>
			<span className={toolLabelClass}>{t('tool.callSubagent')}</span>
			<span className={toolNameClass}>{spawnAgentName(pair.call)}</span>
			{label && <span className={toolArgClass}>{label}</span>}
			{subagentBadge(pair, t)}
		</>
	);
}

/**
 * Renderer for raven's `spawn` tool.
 *
 * Deliberately a collapsible row rather than the `SubagentInlineCard` the
 * per-sub-agent-tool shape uses: `spawn` is fire-and-forget, and its result is
 * an acknowledgement ("Subagent [x] started ...") rather than the sub-agent's
 * reply, which arrives later as its own announce turn. The card presents the
 * result under a "Response" heading, which for `spawn` would show the ack as
 * if it were the answer.
 */
export const SpawnRenderer: ToolRenderer = {
	getDisplayName: (call) => spawnAgentName(call),
	renderHeader,
	renderBody: subagentRenderBody,
};
