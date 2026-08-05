import type { Msg } from '@agentscope-ai/agentscope/message';
import { useMemo } from 'react';
import { toast } from 'sonner';

import { DeliveredFileRow } from './DeliveredFileRow';
import { deriveDeliverables, downloadAllDeliverables } from './deriveDeliverables';
import { downloadErrorMessage } from './downloadError';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';

/**
 * Read-only right-dock panel aggregating every file delivered across the
 * session. Derived entirely from the transcript (no API call).
 */
export function DeliverablesPanel({ msgs }: { msgs: Msg[] }) {
	const { t } = useTranslation();
	const files = useMemo(() => deriveDeliverables(msgs), [msgs]);
	if (files.length === 0) {
		return <p className="p-3 text-xs text-muted-foreground">{t('tool.deliverFilesEmpty')}</p>;
	}
	return (
		<div className="flex flex-col gap-2 p-2">
			{files.map((file) => (
				<DeliveredFileRow key={file.path} file={file} />
			))}
			{files.length > 1 && (
				<Button
					variant="outline"
					size="sm"
					className="w-fit"
					// Awaited and caught: the pre-check runs with `silent: true`, so an
					// unhandled rejection here left a failed "Download all" with no
					// feedback at all -- not even the wrong toast the single-file row got.
					onClick={() => {
						downloadAllDeliverables(files).catch((err: unknown) => {
							toast.error(downloadErrorMessage(err, t));
						});
					}}
				>
					{t('tool.deliverFilesDownloadAll')}
				</Button>
			)}
		</div>
	);
}
