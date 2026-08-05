import type { ToolCallBlock } from '@agentscope-ai/agentscope/message';
import * as mime from 'mime-types';
import type { ReactNode } from 'react';

import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { TFunction, ToolCallWithResult } from './types';

export function defaultGetDisplayName(call: ToolCallBlock): string {
	return call.name;
}

/**
 * A compact `handle · new|resumed` badge for a stateful sub-agent call.
 * Handle comes from the call input; the create/resume action from the
 * result metadata. Returns null when the call carries no instance handle
 * (stateless sub-agent) so stateless calls look unchanged.
 */
export function subagentBadge(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const input = parseInput(pair.call.input);
	const handle = typeof input.instance === 'string' ? input.instance : undefined;
	if (!handle) return null;
	const action = pair.result?.metadata?.action;
	const label =
		action === 'resume'
			? t('tool.subagentResumed')
			: action === 'create'
				? t('tool.subagentNew')
				: null;
	return (
		<span className="text-xs text-muted-foreground">
			[{handle}
			{label ? ` · ${label}` : ''}]
		</span>
	);
}

export function defaultRenderConfirmBody(call: ToolCallBlock): ReactNode {
	return (
		<div className="w-full max-w-full overflow-hidden text-ellipsis truncate">
			<div className="text-secondary-foreground">{call.input}</div>
		</div>
	);
}

/**
 * Default trigger line for tools without a custom `renderHeader` (e.g. MCP
 * tools and CLI sub-agents): a "Call tool" / "Call Sub-Agent" label followed
 * by the tool name in the shared argument style.
 */
export function defaultRenderHeader(
	pair: ToolCallWithResult,
	t: TFunction,
	isSubagent = false,
): ReactNode {
	return (
		<>
			<span className={toolLabelClass}>
				{isSubagent ? t('tool.callSubagent') : t('tool.callGeneric')}
			</span>
			<span className={toolArgClass}>{defaultGetDisplayName(pair.call)}</span>
			{isSubagent && subagentBadge(pair, t)}
		</>
	);
}

/**
 * Default expandable body: the result output as text. The container caps its
 * height and scrolls, so no line counting / truncation is needed. Returns
 * `null` before the call has any result so the row stays non-expandable.
 */
export function defaultRenderBody(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const { call, result } = pair;
	if (!result) return null;
	if (call.state === 'asking' || result.state === 'running') {
		return (
			<div className="flex flex-col border rounded-sm bg-background">
				<div className="px-2 py-1 whitespace-nowrap overflow-x-auto">
					{t('common.running')}
				</div>
			</div>
		);
	}
	if (result.state === 'interrupted') {
		const result = getResultText(pair.result);
		return (
			<div className="flex flex-col border rounded-sm bg-background">
				<div className="px-2 py-1 whitespace-nowrap overflow-x-auto">{result}</div>
			</div>
		);
	}

	// TODO: render multimodal outputs
	let text: string;
	if (typeof result.output === 'string') {
		text = result.output;
	} else {
		text = result.output
			.map((b) => {
				if (b.type === 'text') return b.text;
				const mainType = b.source.media_type.split('/')[0].toUpperCase();
				const ext = (mime.extension(b.source.media_type) || 'bin').toLowerCase();
				return `[${mainType}.${ext}]`;
			})
			.join('\n');
	}

	return (
		<pre className="border rounded-sm bg-background p-2 text-xs overflow-auto max-h-[200px] whitespace-pre-wrap">
			{text}
		</pre>
	);
}

/**
 * Body for a CLI sub-agent tool call: shows the delegated `prompt` (parsed
 * from the call input) in a framed box, followed by the default result body
 * (the sub-agent's response / background notification) when present. Renders
 * the prompt box even before a result arrives, so the delegated task is
 * visible while the sub-agent is still running.
 */
export function subagentRenderBody(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const parsed = parseInput(pair.call.input);
	const prompt = typeof parsed.prompt === 'string' ? parsed.prompt : undefined;
	const resultBody = defaultRenderBody(pair, t);
	if (!prompt && !resultBody) return null;
	return (
		<div className="flex flex-col gap-2 mt-1">
			{prompt && (
				<div className="flex flex-col border rounded-sm bg-background">
					<div className="px-2 py-1 border-b text-muted-foreground">
						{t('tool.subagentPrompt')}
					</div>
					<div className="px-2 py-1 text-xs whitespace-pre-wrap break-words overflow-auto max-h-[200px]">
						{prompt}
					</div>
				</div>
			)}
			{resultBody}
		</div>
	);
}
