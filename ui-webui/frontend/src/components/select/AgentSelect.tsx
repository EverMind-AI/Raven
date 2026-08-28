import type { AgentView } from '@/api';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useTranslation } from '@/i18n/useI18n.ts';
import ChevronDown from '~icons/solar/alt-arrow-down-linear';
import Check from '~icons/solar/check-read-linear';

interface Props {
	agents: AgentView[];
	/** Currently selected agent id, or `null` when none is selected. */
	value?: string | null;
	onChange?: (agentId: string) => void;
	/** Override the trigger label shown when no agent is selected. */
	placeholder?: string;
}

/**
 * Chat-group picker used in the chat sidebar. A group is stored as an
 * AgentScope agent record, hence the `AgentView` shape. Rendered on top of the
 * shadcn dropdown-menu primitives (same base as `LlmSelect`) so
 * per-row content (icons, badges) is fully composable.
 */
export function AgentSelect({ agents, value, onChange, placeholder }: Props) {
	const { t } = useTranslation();
	const selected = agents.find((a) => a.id === value) ?? null;
	const displayLabel = selected
		? selected.data.name
		: (placeholder ?? t('chat.group.selectPlaceholder'));

	const renderItem = (agent: AgentView) => {
		const isSelected = agent.id === value;
		return (
			<DropdownMenuItem key={agent.id} onSelect={() => onChange?.(agent.id)}>
				<Check
					className={`size-3.5 shrink-0 ${isSelected ? 'opacity-100' : 'opacity-0'}`}
				/>
				<span className="min-w-0 flex-1 truncate">{agent.data.name}</span>
			</DropdownMenuItem>
		);
	};

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<Button
					variant="outline"
					size="sm"
					className="flex-1 min-w-0 justify-between gap-1"
				>
					<span className="truncate">{displayLabel}</span>
					<ChevronDown className="size-3.5 opacity-50" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="min-w-56 max-h-72 overflow-y-auto">
				{agents.length === 0 ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						<p className="font-medium">{t('chat.group.emptyTitle')}</p>
						<p className="text-xs mt-1">{t('chat.group.emptyDescription')}</p>
					</div>
				) : (
					agents.map(renderItem)
				)}
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
