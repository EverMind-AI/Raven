import { Fragment } from 'react';

import type { ChatModelConfig } from '@/api';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuSeparator,
	DropdownMenuSub,
	DropdownMenuSubContent,
	DropdownMenuSubTrigger,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import type { RavenModelGroup } from '@/hooks/useRavenModels';
import { useTranslation } from '@/i18n/useI18n.ts';
import ChevronDown from '~icons/solar/alt-arrow-down-linear';

interface Props {
	value?: ChatModelConfig | null;
	/**
	 * Called when the user selects a model, or — when `allowClear` is true —
	 * clears the selection (in which case `null` is emitted).
	 */
	onChange?: (value: ChatModelConfig | null) => void;
	/** Raven-sourced options. When omitted the picker renders its empty state. */
	ravenGroups?: RavenModelGroup[];
	ravenLoading?: boolean;
	/** Override the trigger label shown when no model is selected. */
	placeholder?: string;
	/**
	 * When true, append a "clear selection" item to the dropdown that emits
	 * `null` via `onChange`. Used by the fallback selector.
	 */
	allowClear?: boolean;
	/** Override the label of the "clear selection" item. */
	clearLabel?: string;
}

/**
 * Raven reads a model string as `<provider config section>/<name the endpoint knows>`
 * and routes the turn on that first segment. A group's option strings come from that
 * provider's own `models` list, where the head is implied by the section they sit in,
 * so put it back rather than leaving raven to infer the provider from a bare name --
 * two providers can offer the same name, and the inference cannot see which group the
 * user opened.
 */
function qualifyModel(provider: string, model: string): string {
	return model.startsWith(`${provider}/`) ? model : `${provider}/${model}`;
}

export function LlmSelect({
	value,
	onChange,
	ravenGroups,
	ravenLoading,
	placeholder,
	allowClear = false,
	clearLabel,
}: Props) {
	const { t } = useTranslation();
	const hasOptions = (ravenGroups?.length ?? 0) > 0;

	const handleSelect = (provider: string, model: string) => {
		onChange?.({
			type: provider,
			credential_id: 'raven',
			model: qualifyModel(provider, model),
			parameters: {},
		});
	};

	const displayLabel = value?.model
		? value.model
		: ravenLoading
			? t('llm-select.loading')
			: (placeholder ?? t('llm-select.placeholder'));

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>
				<Button variant="outline" size="sm" className="justify-between gap-1">
					<span className="truncate">{displayLabel}</span>
					<ChevronDown className="size-3.5 opacity-50" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="min-w-48 max-h-72 overflow-y-auto">
				{!ravenLoading && !hasOptions ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						{t('llm-select.empty.title')}
					</div>
				) : (
					(ravenGroups ?? []).map((group) => (
						<DropdownMenuSub key={group.provider}>
							<DropdownMenuSubTrigger>{group.displayName}</DropdownMenuSubTrigger>
							<DropdownMenuSubContent className="max-h-72 overflow-y-auto">
								{group.models.map((model, i) => (
									<Fragment key={model}>
										{/* Divide the user's pinned picks from the rest of the shortlist. */}
										{group.selectedCount > 0 && i === group.selectedCount && (
											<DropdownMenuSeparator />
										)}
										<DropdownMenuItem
											onSelect={() => handleSelect(group.provider, model)}
										>
											{model}
										</DropdownMenuItem>
									</Fragment>
								))}
							</DropdownMenuSubContent>
						</DropdownMenuSub>
					))
				)}
				{allowClear ? (
					<>
						<DropdownMenuSeparator />
						<DropdownMenuItem onSelect={() => onChange?.(null)}>
							{clearLabel ?? t('llm-select.clear')}
						</DropdownMenuItem>
					</>
				) : null}
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
