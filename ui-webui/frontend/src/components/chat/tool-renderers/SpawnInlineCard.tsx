import type { CSSProperties } from 'react';

import { parseInput, ToolStateIcon } from './_shared';
import { subagentBadge, subagentRenderBody } from './DefaultRenderer';
import { spawnAgentName } from './SpawnRenderer';
import type { TFunction, ToolCallWithResult } from './types';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { cn } from '@/lib/utils';
import ChevronRight from '~icons/solar/alt-arrow-right-linear';

/**
 * Inline card for a `spawn` call in the conversation flow.
 *
 * A delegation is pulled out of the collapsible tool group so it stays
 * visible, and once out it is no longer inside the group's typographic
 * context: rendered through the generic row it inherited the message body's
 * size and colour, so it read as a paragraph of the assistant's own reply
 * rather than as something the agent did. This mirrors the tool group's
 * trigger exactly -- gold chevron, the same `text-xs uppercase tracking-wide`
 * scale, and the same `bg-gold-olive/10` body -- so a delegation lines up with
 * the "called N tools" rows above and below it.
 */
export function SpawnInlineCard({ pair, t }: { pair: ToolCallWithResult; t: TFunction }) {
	const input = parseInput(pair.call.input);
	const label = typeof input.label === 'string' ? input.label : '';
	const running = !pair.result || pair.result.state === 'running';
	return (
		<div className="flex flex-col text-muted-foreground">
			<Collapsible>
				<CollapsibleTrigger asChild>
					<Button
						variant="ghost"
						className="group flex h-auto w-full cursor-pointer items-center justify-start gap-1.5 px-0 py-1 hover:bg-transparent active:!translate-y-0 data-[state=open]:bg-transparent"
					>
						<ChevronRight className="size-3 shrink-0 text-gold/80 transition-transform group-hover:text-gold group-data-[state=open]:rotate-90" />
						<span
							className={cn('flex min-w-0 items-center gap-1.5', running && 'shimmer')}
							style={{ '--shimmer-color': 'var(--glow)' } as CSSProperties}
						>
							<span className="shrink-0 text-xs font-medium tracking-wide text-gold uppercase">
								{t('tool.callSubagent')}
							</span>
							<span className="shrink-0 text-xs font-medium text-gold">
								{spawnAgentName(pair.call)}
							</span>
							{label && <span className="truncate text-xs text-gold/70">{label}</span>}
						</span>
						{subagentBadge(pair, t)}
						<ToolStateIcon state={pair.result?.state} />
					</Button>
				</CollapsibleTrigger>
				<CollapsibleContent className="mt-1 flex flex-col gap-y-1 rounded bg-gold-olive/10 p-2.5 text-sm">
					{subagentRenderBody(pair, t)}
				</CollapsibleContent>
			</Collapsible>
		</div>
	);
}
