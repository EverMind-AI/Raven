import { useEffect, useState } from 'react';

import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogDescription,
	DialogTrigger,
} from '@/components/ui/dialog';
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { useAgents } from '@/hooks/useAgents';
import { useTranslation } from '@/i18n/useI18n';
import { formatApiErrorForAlert } from '@/lib/api-error';
import PlusCircle from '~icons/solar/add-circle-bold-duotone';
import CircleAlert from '~icons/solar/danger-circle-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';

interface Props {
	/** Called with the new group's id once creation succeeds. */
	onCreated?: (groupId: string) => void;
	triggerId?: string;
}

/**
 * Trigger button plus dialog for creating a chat group — the container the
 * session list hangs off.
 *
 * A group is stored as an AgentScope agent record (hence `useAgents`), but a
 * name is the only thing collected: the record's `system_prompt` / context /
 * ReAct / invite fields never reach a raven turn, so offering them here would
 * be configuration that silently does nothing.
 *
 * @returns The trigger + dialog JSX.
 */
export function ChatGroupDialog({ onCreated, triggerId }: Props) {
	const { create } = useAgents();
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [name, setName] = useState('');
	const [submitting, setSubmitting] = useState(false);
	const [errorMsg, setErrorMsg] = useState('');

	useEffect(() => {
		if (!open) {
			setName('');
			setErrorMsg('');
		}
	}, [open]);

	const handleSubmit = async () => {
		const trimmed = name.trim();
		if (!trimmed) return;
		setErrorMsg('');
		setSubmitting(true);
		try {
			const { agent_id } = await create({ name: trimmed }, { silent: true });
			setOpen(false);
			onCreated?.(agent_id);
		} catch (e) {
			setErrorMsg(formatApiErrorForAlert(e));
		} finally {
			setSubmitting(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={setOpen}>
			<DialogTrigger asChild>
				<Button id={triggerId}>
					<PlusCircle />
					<span>{t('dialog-group-create.trigger')}</span>
				</Button>
			</DialogTrigger>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>{t('dialog-group-create.title')}</DialogTitle>
					<DialogDescription>{t('dialog-group-create.description')}</DialogDescription>
				</DialogHeader>
				<FieldGroup>
					<Field>
						<FieldLabel>{t('dialog-group-create.nameLabel')}</FieldLabel>
						<Input
							value={name}
							onChange={(e) => {
								setErrorMsg('');
								setName(e.target.value);
							}}
							placeholder={t('dialog-group-create.namePlaceholder')}
							onKeyDown={(e) => {
								if (e.key === 'Enter') void handleSubmit();
							}}
							autoFocus
						/>
					</Field>
				</FieldGroup>
				{errorMsg && (
					<Alert variant="destructive">
						<CircleAlert />
						<AlertDescription className="whitespace-pre-wrap">
							{errorMsg}
						</AlertDescription>
					</Alert>
				)}
				<DialogFooter>
					<Button variant="ghost" onClick={() => setOpen(false)} disabled={submitting}>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button onClick={handleSubmit} disabled={!name.trim() || submitting}>
						{submitting ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<PlusCircle className="size-3.5" />
						)}
						{submitting ? t('common.creating') : t('common.create')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
