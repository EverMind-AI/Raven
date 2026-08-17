import { useState, useEffect } from 'react';

import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogDescription,
} from '@/components/ui/dialog';
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { useTranslation } from '@/i18n/useI18n';
import CheckCircle from '~icons/solar/check-circle-bold-duotone';
import CircleAlert from '~icons/solar/danger-circle-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	currentName: string;
	onConfirm: (name: string) => Promise<void>;
	/** Copy for whatever is being renamed — a session, a chat group, … */
	title: string;
	description: string;
	label: string;
	placeholder: string;
}

/**
 * Single-field rename dialog, shared by the session and chat-group renames.
 * The copy is passed in rather than looked up here so one component can serve
 * both without branching on an entity type.
 *
 * @returns The dialog JSX.
 */
export function RenameDialog({
	open,
	onOpenChange,
	currentName,
	onConfirm,
	title,
	description,
	label,
	placeholder,
}: Props) {
	const { t } = useTranslation();
	const [name, setName] = useState(currentName);
	const [loading, setLoading] = useState(false);

	useEffect(() => {
		if (open) setName(currentName);
	}, [open, currentName]);

	const handleConfirm = async () => {
		if (!name.trim()) return;
		setLoading(true);
		try {
			await onConfirm(name.trim());
			onOpenChange(false);
		} finally {
			setLoading(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>{title}</DialogTitle>
					<DialogDescription>{description}</DialogDescription>
				</DialogHeader>
				<FieldGroup>
					<Field>
						<FieldLabel>{label}</FieldLabel>
						<Input
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder={placeholder}
							onKeyDown={(e) => {
								if (e.key === 'Enter') handleConfirm();
							}}
							autoFocus
						/>
					</Field>
				</FieldGroup>
				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={loading}>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button onClick={handleConfirm} disabled={loading || !name.trim()}>
						{loading ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<CheckCircle className="size-3.5" />
						)}
						{t('common.confirm')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
