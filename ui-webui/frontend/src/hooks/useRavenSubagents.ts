import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenThirdPartySubagent } from '@/api';

/**
 * Raven's third-party sub-agent config. Writes replace the whole list: the
 * gateway validates it, persists it to ~/.raven, and hot-applies it to the
 * running AgentLoop.
 */
export function useRavenSubagents() {
	const [agents, setAgents] = useState<RavenThirdPartySubagent[]>([]);
	const [presets, setPresets] = useState<RavenThirdPartySubagent[]>([]);
	const [loading, setLoading] = useState(true);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const [a, p] = await Promise.all([
				ravenConfigApi.listSubagents(),
				ravenConfigApi.presets(),
			]);
			setAgents(a.agents ?? []);
			setPresets(p.presets ?? []);
		} catch (e) {
			toast.error(`Failed to load sub-agents: ${e instanceof Error ? e.message : String(e)}`);
			setAgents([]);
			setPresets([]);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void reload();
	}, [reload]);

	const save = useCallback(async (next: RavenThirdPartySubagent[]) => {
		await ravenConfigApi.setSubagents(next);
		setAgents(next);
	}, []);

	return { agents, presets, loading, reload, save };
}
