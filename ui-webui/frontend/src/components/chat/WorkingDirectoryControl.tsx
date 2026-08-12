import { useEffect, useState } from 'react';

import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import FolderOpen from '~icons/solar/folder-open-bold-duotone';

interface WorkingDirectoryControlProps {
	/** Current override, or null when the session uses its default dir. */
	value: string | null;
	/** Disable editing (e.g. no session selected yet). */
	disabled?: boolean;
	/**
	 * Persist a new override. Called with the trimmed absolute path, or
	 * `null` to clear the override. Not called when the value is unchanged.
	 */
	onChange: (path: string | null) => void | Promise<void>;
	/**
	 * Server-side rejection of the last commit (e.g. a path inside the
	 * agent's protected trees, or a session with work in flight). Shown
	 * the same way as the local "must be absolute" hint.
	 */
	error?: string | null;
}

/**
 * A compact row below the chat input for viewing / setting the session's
 * working directory. An empty field means "use the default session
 * workspace"; a non-empty value must be an absolute path. Commits on blur
 * or Enter; shows an inline hint when the path is not absolute, or when the
 * backend rejected the last commit.
 */
export function WorkingDirectoryControl({
	value,
	disabled,
	onChange,
	error,
}: WorkingDirectoryControlProps) {
	const { t } = useTranslation();
	const [draft, setDraft] = useState(value ?? '');
	const [invalid, setInvalid] = useState(false);

	// Re-sync the draft when the persisted value changes (session switch).
	useEffect(() => {
		setDraft(value ?? '');
		setInvalid(false);
	}, [value]);

	const commit = () => {
		const trimmed = draft.trim();
		if (trimmed === '') {
			setInvalid(false);
			if (value !== null) void onChange(null);
			return;
		}
		if (!trimmed.startsWith('/')) {
			setInvalid(true);
			return;
		}
		setInvalid(false);
		if (trimmed !== value) void onChange(trimmed);
	};

	return (
		<div className="flex w-full items-center gap-2 px-1 text-xs text-muted-foreground">
			<FolderOpen className="size-3.5 shrink-0" />
			<input
				type="text"
				value={draft}
				disabled={disabled}
				aria-label={t('workingDirectory.label')}
				placeholder={t('workingDirectory.placeholder')}
				onChange={(e) => setDraft(e.target.value)}
				onBlur={commit}
				onKeyDown={(e) => {
					if (e.key === 'Enter') {
						e.preventDefault();
						commit();
					}
				}}
				className={cn(
					'min-w-0 flex-1 border-b border-transparent bg-transparent py-0.5 outline-none focus:border-border',
					(invalid || error) && 'border-destructive focus:border-destructive',
				)}
			/>
			{invalid && (
				<span className="shrink-0 text-destructive">
					{t('workingDirectory.mustBeAbsolute')}
				</span>
			)}
			{!invalid && error && <span className="shrink-0 truncate text-destructive">{error}</span>}
		</div>
	);
}
