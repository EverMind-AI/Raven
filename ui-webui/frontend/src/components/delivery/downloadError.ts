import { ApiError } from '@/api/client';
import type { TFunction } from '@/components/chat/tool-renderers/types';

/**
 * Turn a failed deliverable download into the message the user sees.
 *
 * The status matters, and a bare `catch` used to throw it away: every failure
 * reported as "this file is no longer available", so a gateway that was not
 * serving the download routes at all told the user their file had expired while
 * the file sat on disk untouched. Only 410 means the store no longer holds the
 * token (see the 404/410 split in `raven/web_rpc/files.py`); anything else --
 * 404 for a route the running gateway does not register, 502 for a gateway the
 * service cannot reach, 500 for a relay that broke -- is the download surface
 * being unavailable, which is a different problem with a different fix.
 *
 * The status is carried into the message because it is the one detail that makes
 * the difference actionable: `silent: true` on the pre-check suppresses the
 * client's own toast, so this string is all the user gets.
 */
export function downloadErrorMessage(err: unknown, t: TFunction): string {
	if (err instanceof ApiError && err.status === 410) {
		return t('tool.deliverFilesUnavailable');
	}
	const status = err instanceof ApiError ? err.status : undefined;
	return status === undefined
		? t('tool.deliverFilesDownloadFailed')
		: t('tool.deliverFilesDownloadFailedStatus', { status });
}
