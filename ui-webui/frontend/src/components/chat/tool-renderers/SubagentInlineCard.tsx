
import { getResultText, parseInput, ToolStateIcon, toolArgClass, toolLabelClass } from './_shared';
import { subagentBadge } from './DefaultRenderer';
import type { TFunction, ToolCallWithResult } from './types';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import ChevronRight from '~icons/solar/alt-arrow-right-linear';
import Bot from '~icons/solar/cpu-bold-duotone';

/**
 * Strip harness-injected ``<system-reminder>…</system-reminder>`` blocks
 * from a sub-agent's output. These carry framework/context noise the user
 * should never see in the chat; removing them (and collapsing the blank
 * runs they leave behind) keeps both the preview and the expanded body
 * readable. Case-insensitive and non-greedy so multiple blocks are each
 * removed independently.
 */
function stripSystemReminders(text: string): string {
	return text
		.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/gi, '')
		.replace(/\n{3,}/g, '\n\n')
		.trim();
}

/** Collapse whitespace and cap to a single short preview line. */
function previewLine(text: string, max = 200): string {
	const oneLine = text.replace(/\s+/g, ' ').trim();
	return oneLine.length > max ? `${oneLine.slice(0, max)}…` : oneLine;
}

/**
 * Inline card for a single sub-agent tool call.
 *
 * Rendered directly in the conversation flow (extracted out of the
 * tool-chain group, like the DAG card) so a delegated sub-agent task is
 * always visible. Collapsed by default: the trigger row shows only the
 * sub-agent name, its stateful ``handle · new|resumed`` badge and a
 * short, system-reminder-stripped preview of the response — never the
 * full prompt or raw ``<system-reminder>`` noise. Clicking expands the
 * card to reveal the full delegated prompt (input) and the full response
 * (output), both with ``<system-reminder>`` blocks stripped.
 */
export function SubagentInlineCard({ pair, t }: { pair: ToolCallWithResult; t: TFunction }) {
	const parsed = parseInput(pair.call.input);
	const prompt = typeof parsed.prompt === 'string' ? stripSystemReminders(parsed.prompt) : '';
	const output = stripSystemReminders(getResultText(pair.result));
	const running = !pair.result || pair.result.state === 'running';
	const preview = running ? t('common.running') : previewLine(output);

	return (
		<div className="flex flex-col rounded-md border bg-background p-3">
			<Collapsible className="group/sa flex flex-col">
				<CollapsibleTrigger asChild>
					<button
						type="button"
						className="group flex w-full cursor-pointer items-center gap-2 text-left text-sm"
					>
						<Bot className="size-3.5 shrink-0 text-foreground/80" />
						<span className={cn(toolLabelClass, 'font-medium', running && 'shimmer')}>
							{t('tool.callSubagent')}
						</span>
						<span className={toolArgClass}>{pair.call.name}</span>
						{subagentBadge(pair, t)}
						<ToolStateIcon state={pair.result?.state} />
						<ChevronRight className="ml-auto size-3 shrink-0 transition-transform group-data-[state=open]/sa:rotate-90" />
					</button>
				</CollapsibleTrigger>
				{preview && (
					<p className="mt-1 line-clamp-2 text-xs text-muted-foreground group-data-[state=open]/sa:hidden">
						{preview}
					</p>
				)}
				<CollapsibleContent className="mt-2 flex flex-col gap-2">
					{prompt && (
						<div className="flex flex-col rounded-sm border bg-background">
							<div className="border-b px-2 py-1 text-xs text-muted-foreground">
								{t('tool.subagentPrompt')}
							</div>
							<div className="max-h-[240px] overflow-auto px-2 py-1 text-xs break-words whitespace-pre-wrap">
								{prompt}
							</div>
						</div>
					)}
					{output && (
						<div className="flex flex-col rounded-sm border bg-background">
							<div className="border-b px-2 py-1 text-xs text-muted-foreground">
								{t('tool.subagentResponse')}
							</div>
							<div className="max-h-[240px] overflow-auto px-2 py-1 text-xs break-words whitespace-pre-wrap">
								{output}
							</div>
						</div>
					)}
				</CollapsibleContent>
			</Collapsible>
		</div>
	);
}
