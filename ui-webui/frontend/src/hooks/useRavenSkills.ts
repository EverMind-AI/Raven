import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenSkillEntry } from '@/api';

/** Only hub-cached skills can be uninstalled; builtin and workspace-dir
 *  skills live outside the cache and the gateway refuses to delete them. */
export const isRemovableSkill = (s: RavenSkillEntry) => s.source === 'hub';

/**
 * Raven's own skill pool — what the chat agent can actually reach, read
 * straight from the live runtime's registry.
 *
 * Deliberately not the AgentScope workspace's skill list: that store is not
 * wired to the gateway agent, so it reports an empty set while raven has a
 * full registry.
 */
export function useRavenSkills() {
	const [skills, setSkills] = useState<RavenSkillEntry[]>([]);
	const [loading, setLoading] = useState(true);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const { skills: available } = await ravenConfigApi.skills.listAvailable();
			setSkills(available ?? []);
		} catch (e) {
			toast.error(`Failed to load skills: ${e instanceof Error ? e.message : String(e)}`);
			setSkills([]);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void reload();
	}, [reload]);

	const remove = useCallback(
		async (skill: RavenSkillEntry) => {
			await ravenConfigApi.skills.remove({
				name: skill.name,
				id: skill.id,
				source: skill.source,
			});
			await reload();
		},
		[reload],
	);

	return { skills, loading, reload, remove };
}
