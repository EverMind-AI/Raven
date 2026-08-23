import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

import { filesApi } from '@/api';

/** One file delivered to the user, as recorded in a `deliver_files` manifest. */
export interface DeliveredFile {
	path: string;
	name: string;
	title?: string | null;
	description?: string | null;
	size: number;
	media_type: string;
	token: string;
	download_path: string;
}

/** An entry that could not be delivered. */
export interface InvalidDelivery {
	path: string;
	reason: string;
}

/** The `raven_delivery` manifest attached to a `deliver_files` tool result. */
export interface DeliveryManifest {
	message?: string | null;
	files: DeliveredFile[];
	invalid: InvalidDelivery[];
}

/** The tool name whose results carry a delivery manifest. */
export const DELIVER_FILES_TOOL = 'deliver_files';

/**
 * Read the `raven_delivery` manifest from a tool_result block's metadata,
 * returning `null` when it is absent or malformed.
 */
export function readManifest(
	metadata: Record<string, unknown> | undefined,
): DeliveryManifest | null {
	const raw = metadata?.raven_delivery;
	if (!raw || typeof raw !== 'object') return null;
	const manifest = raw as Partial<DeliveryManifest>;
	if (!Array.isArray(manifest.files)) return null;
	return {
		message: manifest.message ?? null,
		files: manifest.files as DeliveredFile[],
		invalid: (manifest.invalid as InvalidDelivery[]) ?? [],
	};
}

/**
 * Derive the union of all files delivered across the session transcript.
 *
 * Scans every `tool_result` block that carries a manifest, and unions the
 * files, de-duplicating by `path` with latest-wins (a re-delivered file updates
 * rather than duplicates). Keyed on the manifest rather than the tool name
 * because a delivery reached through the `tool_call` forwarder (tool-search
 * compaction) arrives under that name instead.
 */
export function deriveDeliverables(msgs: Msg[]): DeliveredFile[] {
	const byPath = new Map<string, DeliveredFile>();
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const block of blocks) {
			if (block.type !== 'tool_result') continue;
			const manifest = readManifest(block.metadata);
			if (!manifest) continue;
			for (const file of manifest.files) byPath.set(file.path, file);
		}
	}
	return [...byPath.values()];
}

/** Download every given file as a single zip. */
export async function downloadAllDeliverables(files: DeliveredFile[]): Promise<void> {
	if (files.length === 0) return;
	await filesApi.downloadArchive(files.map((f) => f.token));
}
