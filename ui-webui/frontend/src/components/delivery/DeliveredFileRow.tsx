import { useState } from 'react';
import { toast } from 'sonner';

import type { DeliveredFile } from './deriveDeliverables';
import { downloadErrorMessage } from './downloadError';
import { filesApi } from '@/api';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import Download from '~icons/solar/download-minimalistic-bold-duotone';
import File from '~icons/solar/file-bold-duotone';
import FileAudio from '~icons/solar/file-bold-duotone';
import FileText from '~icons/solar/file-text-bold-duotone';
import FileImage from '~icons/solar/gallery-bold-duotone';
import FileVideo from '~icons/solar/videocamera-record-bold-duotone';

function FileKindIcon({ mediaType, className }: { mediaType: string; className?: string }) {
	switch (mediaType.split('/')[0]) {
		case 'image':
			return <FileImage className={className} />;
		case 'audio':
			return <FileAudio className={className} />;
		case 'video':
			return <FileVideo className={className} />;
		case 'text':
			return <FileText className={className} />;
		default:
			return mediaType === 'application/pdf' ? (
				<FileText className={className} />
			) : (
				<File className={className} />
			);
	}
}

/** Humanize a byte count, e.g. `1536` → `"1.5 KB"`. */
function humanSize(bytes: number): string {
	if (bytes < 1024) return `${bytes} B`;
	const units = ['KB', 'MB', 'GB', 'TB'];
	let value = bytes / 1024;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit += 1;
	}
	return `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * One delivered-file row: icon, title/name, size, optional description and
 * an authenticated download button. Shared by the inline delivery card and
 * the Deliverables panel.
 */
export function DeliveredFileRow({ file }: { file: DeliveredFile }) {
	const { t } = useTranslation();
	const [busy, setBusy] = useState(false);
	const onDownload = async () => {
		setBusy(true);
		try {
			await filesApi.downloadDeliverable(file.token, file.name);
		} catch (err) {
			toast.error(downloadErrorMessage(err, t));
		} finally {
			setBusy(false);
		}
	};
	return (
		<div className="flex items-center gap-3 rounded-lg border bg-muted/40 px-3 py-2">
			<span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-background text-muted-foreground">
				<FileKindIcon mediaType={file.media_type} className="size-4" />
			</span>
			<span className="flex min-w-0 flex-1 flex-col">
				<span className="truncate text-sm font-medium text-foreground">
					{file.title || file.name}
				</span>
				<span className="truncate text-xs text-muted-foreground">
					{file.name} · {humanSize(file.size)}
				</span>
				{file.description && (
					<span className="truncate text-xs text-muted-foreground">
						{file.description}
					</span>
				)}
			</span>
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={busy}
				onClick={onDownload}
				aria-label={`Download ${file.name}`}
			>
				<Download className="size-4" />
			</Button>
		</div>
	);
}
