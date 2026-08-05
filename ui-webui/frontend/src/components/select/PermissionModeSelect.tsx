import type { PermissionMode } from '@/api/types';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils.ts';
import ChevronDown from '~icons/solar/alt-arrow-down-linear';
import UserRoundKey from '~icons/solar/user-id-bold-duotone';

const PERMISSION_MODES: { value: PermissionMode; label: string }[] = [
	{ value: 'default', label: 'Default' },
	{ value: 'accept_edits', label: 'Accept Edits' },
	{ value: 'explore', label: 'Explore' },
	{ value: 'bypass', label: 'Bypass' },
	{ value: 'dont_ask', label: "Don't Ask" },
];

interface Props {
	className?: string;
	value?: PermissionMode;
	disabled?: boolean;
	/**
	 * Why the picker is disabled. Replaces the normal trigger tooltip while
	 * `disabled` is set, so a greyed-out control still states its reason.
	 */
	disabledReason?: string;
	onChange?: (value: PermissionMode) => void;
}

export function PermissionModeSelect({
	className,
	value,
	disabled,
	disabledReason,
	onChange,
}: Props) {
	const { t } = useTranslation();

	const displayLabel = value
		? (PERMISSION_MODES.find((m) => m.value === value)?.label ?? value)
		: t('permission-mode.placeholder');

	const trigger = (
		<Button
			variant="outline"
			size="sm"
			className={cn('justify-between gap-1', className)}
			disabled={disabled}
			tooltip={disabled ? undefined : t('permission-mode.trigger-tooltip')}
		>
			<div className="flex flex-row items-center gap-x-2">
				<UserRoundKey />
				<span className="truncate">{displayLabel}</span>
			</div>
			<ChevronDown className="size-3.5 opacity-50" />
		</Button>
	);

	// A disabled Button carries `pointer-events-none`, so Radix never sees the
	// hover and Button's own tooltip stays hidden. Hang the reason off a wrapper
	// that still receives events, and skip the menu entirely -- it cannot open.
	if (disabled) {
		return (
			<Tooltip>
				<TooltipTrigger asChild>
					<span className="inline-flex">{trigger}</span>
				</TooltipTrigger>
				<TooltipContent>
					{disabledReason ?? t('permission-mode.trigger-tooltip')}
				</TooltipContent>
			</Tooltip>
		);
	}

	return (
		<DropdownMenu>
			<DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="min-w-48">
				<DropdownMenuGroup>
					<DropdownMenuLabel>{t('permission-mode.label')}</DropdownMenuLabel>
					{PERMISSION_MODES.map((mode) => (
						<Tooltip key={mode.value}>
							<TooltipTrigger asChild>
								<DropdownMenuItem onSelect={() => onChange?.(mode.value)}>
									{mode.label}
								</DropdownMenuItem>
							</TooltipTrigger>
							<TooltipContent side="right">
								{t(`permission-mode.${mode.value}-tooltip`)}
							</TooltipContent>
						</Tooltip>
					))}
				</DropdownMenuGroup>
			</DropdownMenuContent>
		</DropdownMenu>
	);
}
