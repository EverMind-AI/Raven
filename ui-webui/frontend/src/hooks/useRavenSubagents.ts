import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenSubagentProbe, RavenThirdPartySubagent } from '@/api';

/**
 * Raven's third-party subagent config. Writes replace the whole list: the
 * gateway validates it, persists it to ~/.raven, and hot-applies it to the
 * running AgentLoop.
 *
 * Availability probes are a second, deliberately decoupled fetch: they are keyed
 * `<source>:<name>` because a configured agent and a preset can share a name,
 * and they are never awaited by `reload`, so a slow endpoint cannot hold up the
 * list render.
 */
export function useRavenSubagents() {
	const [agents, setAgents] = useState<RavenThirdPartySubagent[]>([]);
	const [presets, setPresets] = useState<RavenThirdPartySubagent[]>([]);
	const [probes, setProbes] = useState<Record<string, RavenSubagentProbe>>({});
	const [loading, setLoading] = useState(true);
	const [probing, setProbing] = useState(false);
	// Distinguishes "no probe yet because none has ever landed" from "no probe for
	// this row because it hasn't been re-probed since" - the first fetch failing
	// must not read as "checking", which claims work in progress that isn't.
	const [probesLoaded, setProbesLoaded] = useState(false);

	const reprobe = useCallback(async () => {
		setProbing(true);
		try {
			const res = await ravenConfigApi.probeSubagents();
			const next: Record<string, RavenSubagentProbe> = {};
			for (const r of res.results ?? []) next[`${r.source}:${r.name}`] = r;
			setProbes(next);
			setProbesLoaded(true);
		} catch {
			// client.ts already toasts. Keep the previous statuses rather than
			// blanking them: a stale dot beats no dot at all.
		} finally {
			setProbing(false);
		}
	}, []);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const [a, p] = await Promise.all([
				ravenConfigApi.listSubagents(),
				ravenConfigApi.presets(),
			]);
			setAgents(a.agents ?? []);
			setPresets(p.presets ?? []);
			void reprobe();
		} catch (e) {
			toast.error(`Failed to load subagents: ${e instanceof Error ? e.message : String(e)}`);
			setAgents([]);
			setPresets([]);
		} finally {
			setLoading(false);
		}
	}, [reprobe]);

	useEffect(() => {
		void reload();
	}, [reload]);

	const save = useCallback(
		async (next: RavenThirdPartySubagent[]) => {
			await ravenConfigApi.setSubagents(next);
			setAgents(next);
			// Re-probe after a save so fixing a key or a model name updates the
			// status without a page reload.
			void reprobe();
		},
		[reprobe],
	);

	return { agents, presets, probes, probesLoaded, loading, probing, reload, reprobe, save };
}
