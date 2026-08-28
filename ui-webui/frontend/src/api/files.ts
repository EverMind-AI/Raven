import { apiUrl, client } from './client';

/** Trigger a browser "save as" for a URL the server marks as an attachment. */
function saveFromUrl(url: string, filename: string): void {
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	document.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
}

/**
 * A deliverable is streamed, not buffered: there is no size cap on delivery, so
 * fetching into a Blob would have to hold the whole file in the tab. A plain
 * anchor hands the stream to the browser instead. The cost is losing fetch's
 * error path, so a HEAD runs first and a failure becomes a caller-side error
 * rather than a blank tab.
 */
async function ensureAvailable(path: string): Promise<void> {
	await client.stream(path, { method: 'HEAD', silent: true });
}

export const filesApi = {
	/** Download one delivered file, addressed by its opaque token. */
	downloadDeliverable: async (token: string, filename: string): Promise<void> => {
		const path = `/raven/files/download?token=${encodeURIComponent(token)}`;
		await ensureAvailable(path);
		saveFromUrl(apiUrl(path).toString(), filename);
	},

	/** Download several delivered files as one zip ("Download all"). */
	downloadArchive: async (tokens: string[]): Promise<void> => {
		if (tokens.length === 0) return;
		const params = new URLSearchParams();
		for (const token of tokens) params.append('token', token);
		const path = `/raven/files/download-archive?${params.toString()}`;
		await ensureAvailable(path);
		saveFromUrl(apiUrl(path).toString(), 'deliverables.zip');
	},
};
