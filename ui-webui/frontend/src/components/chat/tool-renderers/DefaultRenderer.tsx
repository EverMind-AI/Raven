import type { ToolCallBlock } from '@agentscope-ai/agentscope/message';
import * as mime from 'mime-types';
import type { ReactNode } from 'react';

import { getResultText, parseInput, toolArgClass, toolLabelClass, toolNameClass } from './_shared';
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
 * Argument names whose *values* must never reach the trigger line. The header
 * is always visible — it is on screen without expanding the row, stays in the
 * transcript, and lands in any screenshot — and a generic tool's argument
 * schema is written by whoever wrote the tool, so an MCP server is free to
 * take a token as a plain string argument. Matched on the name, since the
 * value of a credential is by definition unrecognisable.
 */
const SECRET_ARG = /(secret|passwd|password|token|api[-_]?key|access[-_]?key|auth|credential|cookie|session[-_]?id|private)/i;

/** Longest summary rendered before it is cut. CSS truncates the line anyway;
 *  this keeps a pathological argument out of the DOM in the first place. */
const SUMMARY_MAX = 120;

/**
 * A one-line summary of a call's arguments, for tools without a dedicated
 * renderer that knows which argument matters. Joins the primitive values in
 * declaration order and leaves the container to truncate; objects and arrays
 * are skipped (they never read well on one line), and anything whose argument
 * name looks like a credential is replaced by a bullet run rather than shown.
 *
 * Empty string when there is nothing worth showing, so the caller can drop
 * the slot entirely rather than render a blank span.
 */
export function summarizeInput(input: string): string {
	const parsed = parseInput(input);
	const out = Object.entries(parsed)
		.filter(([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
		.map(([k, v]) => (SECRET_ARG.test(k) ? '••••' : String(v)))
		.filter((v) => v.length > 0)
		.join(' ')
		.trim();
	return out.length > SUMMARY_MAX ? `${out.slice(0, SUMMARY_MAX)}…` : out;
}

/**
 * The tool-authored call label, when the backend supplied one. Raven's
 * `Tool.display_call` output rides on the tool *result*'s metadata (the
 * published ToolCallBlock has no field for it), so it is only available once
 * the call finishes — an in-flight row falls back to {@link summarizeInput}.
 */
function displayLabel(pair: ToolCallWithResult): string {
	const d = pair.result?.metadata?.display;
	return typeof d === 'string' ? d : '';
}

/**
 * Default trigger line for tools without a custom `renderHeader` (e.g. MCP
 * tools and CLI sub-agents): a "Call tool" / "Call Sub-Agent" label, the tool
 * name, and what it was called with — without the last slot two calls to the
 * same tool are indistinguishable.
 */
export function defaultRenderHeader(
	pair: ToolCallWithResult,
	t: TFunction,
	isSubagent = false,
): ReactNode {
	const summary = displayLabel(pair) || summarizeInput(pair.call.input);
	return (
		<>
			<span className={toolLabelClass}>
				{isSubagent ? t('tool.callSubagent') : t('tool.callGeneric')}
			</span>
			<span className={toolNameClass}>{defaultGetDisplayName(pair.call)}</span>
			{summary && <span className={toolArgClass}>{summary}</span>}
			{isSubagent && subagentBadge(pair, t)}
		</>
	);
}

/**
 * The result half of the default body: the output as text. The container caps
 * its height and scrolls, so no line counting / truncation is needed. Returns
 * `null` before the call has any result.
 *
 * Kept separate from {@link defaultRenderBody} because `subagentRenderBody`
 * shows its own prompt box and wants only the result part.
 */
export function defaultResultBody(pair: ToolCallWithResult, t: TFunction): ReactNode {
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
 * A copy of the parsed arguments with credential-named values masked, at any
 * depth. Which arguments a call was made with is the useful part; the secret
 * material itself is not, and expanding a row should not be what puts a token
 * on screen.
 */
export function redactInput(value: unknown): unknown {
	if (Array.isArray(value)) return value.map(redactInput);
	if (value && typeof value === 'object') {
		return Object.fromEntries(
			Object.entries(value as Record<string, unknown>).map(([k, v]) => [
				k,
				SECRET_ARG.test(k) ? '••••' : redactInput(v),
			]),
		);
	}
	return value;
}

/**
 * Default expandable body: what the tool was called with, then what it
 * returned. The input box is what makes a generic row (MCP tools, `exec`,
 * `read_file`, `ask_user`) inspectable at all, and it renders while the call
 * is still in flight — the result box only appears once there is a result.
 */
export function defaultRenderBody(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const parsed = redactInput(parseInput(pair.call.input)) as Record<string, unknown>;
	const hasInput = Object.keys(parsed).length > 0;
	const resultBody = defaultResultBody(pair, t);
	if (!hasInput) return resultBody;
	return (
		<div className="flex flex-col gap-2">
			<div className="flex flex-col border rounded-sm bg-background">
				<div className="px-2 py-1 border-b text-muted-foreground">
					{t('tool.callInput')}
				</div>
				<pre className="px-2 py-1 text-xs overflow-auto max-h-[200px] whitespace-pre-wrap">
					{JSON.stringify(parsed, null, 2)}
				</pre>
			</div>
			{resultBody}
		</div>
	);
}

/**
 * Body for a CLI sub-agent tool call: shows the delegated prompt (parsed from
 * the call input) in a framed box, followed by the default result body (the
 * sub-agent's response / background notification) when present. Renders the
 * prompt box even before a result arrives, so the delegated task is visible
 * while the sub-agent is still running.
 *
 * The two delegation shapes name the argument differently: a per-sub-agent
 * tool takes `prompt`, raven's single `spawn` tool takes `task`. Reading only
 * the first left the box empty for every `spawn` call.
 */
export function subagentRenderBody(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const parsed = parseInput(pair.call.input);
	const raw = parsed.prompt ?? parsed.task;
	const prompt = typeof raw === 'string' ? raw : undefined;
	// Result only: the prompt box below already shows this call's input.
	const resultBody = defaultResultBody(pair, t);
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
