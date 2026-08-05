import { useCallback, useEffect, useState } from 'react';

import { ravenProvidersApi } from '@/api';

export interface RavenModelGroup {
	provider: string;
	displayName: string;
	/** Ordered options: the user's explicitly selected models first, then the rest
	 *  of the provider's curated shortlist. `selectedCount` marks the boundary. */
	models: string[];
	/** How many leading entries of `models` are user-selected (pinned on top). */
	selectedCount: number;
}

/**
 * Picker options sourced from raven's own provider config (`~/.raven/config.json`),
 * which is what actually routes a turn. Only configured providers appear, and the
 * option list mirrors what the settings page offers so the two stay in sync. The
 * user's selected models are never a filter — the full curated `commonModels`
 * shortlist is always offered, with the selected ones pinned to the top so the
 * picker defaults to them while other choices stay one click away. Falls back to
 * the registry `defaultModel` only when a provider has neither.
 */
export function useRavenModels() {
	const [groups, setGroups] = useState<RavenModelGroup[]>([]);
	const [loading, setLoading] = useState(false);

	const refetch = useCallback(async () => {
		setLoading(true);
		try {
			const { providers } = await ravenProvidersApi.list();
			setGroups(
				providers
					.filter((p) => p.configured)
					.map((p) => {
						// Selected first, then the rest of the curated shortlist
						// (which already includes the selected ids — dedupe them
						// out of the tail). Neither present → the registry default.
						const rest = p.commonModels.filter((m) => !p.models.includes(m));
						let models = [...p.models, ...rest];
						if (models.length === 0 && p.defaultModel) models = [p.defaultModel];
						return {
							provider: p.name,
							displayName: p.displayName,
							models,
							selectedCount: p.models.length,
						};
					})
					.filter((g) => g.models.length > 0),
			);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refetch();
	}, [refetch]);

	return { groups, loading, refetch };
}
