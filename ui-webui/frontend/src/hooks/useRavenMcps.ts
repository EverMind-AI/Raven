import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenMcpServer } from '@/api';

/**
 * Raven's MCP servers — the ones the chat agent can actually reach.
 *
 * Deliberately not the AgentScope workspace's MCP list: that store is not
 * wired to the gateway agent, so it describes servers this chat can never use
 * and swallows anything added through it.
 *
 * Writes replace the whole list and are handed to the running agent: each
 * server owns its own transport, so one can be attached or detached without a
 * restart. The write result carries `applied` — false when no live agent was
 * reachable, in which case the config is still saved and the next agent picks
 * it up. The callbacks return it rather than claiming either outcome.
 *
 * `applied: true` means the reconcile started, not that it finished; the reload
 * below picks up whatever has settled by then, and anything slower shows as
 * `connecting` until the panel is reopened.
 */
export function useRavenMcps() {
	const [mcps, setMcps] = useState<RavenMcpServer[]>([]);
	const [loading, setLoading] = useState(true);
	/** Why the last load failed, or null. Distinct from an empty list: the
	 *  panel must not present "we could not find out" as "nothing configured". */
	const [loadError, setLoadError] = useState<string | null>(null);
	/** True while at least one MCP server is connected right now. */
	const [connected, setConnected] = useState(false);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const res = await ravenConfigApi.mcp.list();
			setMcps(res.servers ?? []);
			setConnected(!!res.connected);
			setLoadError(null);
		} catch (e) {
			const detail = e instanceof Error ? e.message : String(e);
			toast.error(`Failed to load MCP servers: ${detail}`);
			// The last known list is kept rather than blanked: an empty list is
			// a claim about the config, and a failed read supports no claim.
			setLoadError(detail);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void reload();
	}, [reload]);

	/**
	 * Read-modify-write against the gateway, never against `mcps`.
	 *
	 * `raven.mcp.set` replaces the whole server map, so a list built from the
	 * local cache deletes every server that cache is missing — and the cache is
	 * empty before the first load resolves and stale after any concurrent edit.
	 * Re-reading makes a failed read abort the write instead of authorising a
	 * wipe, and makes the duplicate check below judge the real config.
	 */
	const mutate = useCallback(
		async (edit: (current: RavenMcpServer[]) => RavenMcpServer[]) => {
			const { servers } = await ravenConfigApi.mcp.list();
			const res = await ravenConfigApi.mcp.set(edit(servers ?? []));
			await reload();
			return res;
		},
		[reload],
	);

	const add = useCallback(
		async (servers: RavenMcpServer[]) =>
			mutate((current) => {
				const seen = new Set(current.map((m) => m.name));
				for (const s of servers) {
					if (seen.has(s.name)) {
						throw new Error(`MCP server "${s.name}" already exists.`);
					}
					seen.add(s.name);
				}
				return [...current, ...servers];
			}),
		[mutate],
	);

	const remove = useCallback(
		// Returns the write result like `add` does: the caller needs `applied` to
		// know whether the running agent actually dropped the server, or only the
		// config did.
		async (name: string) => mutate((current) => current.filter((m) => m.name !== name)),
		[mutate],
	);

	return { mcps, loading, loadError, connected, reload, add, remove };
}
