import { client } from './client';

export interface RavenProviderSummary {
	name: string;
	displayName: string;
	isOauth: boolean;
	isLocal: boolean;
	isGateway: boolean;
	configured: boolean;
	apiKeyRedacted: string;
	apiBase: string | null;
	defaultApiBase: string;
	defaultModel: string;
	envKey: string;
	models: string[];
	/** Curated pick-list of recognizable model ids for this provider (union of a
	 *  hand-maintained shortlist and any already-configured custom models). */
	commonModels: string[];
	/** True when there is no universal default endpoint and the user must supply
	 *  their own base URL (e.g. Azure's per-tenant resource URL). */
	requiresApiBase: boolean;
}

export interface RavenProviderDetail extends RavenProviderSummary {
	fields: Record<
		string,
		{ type: string; default: unknown; isSecret: boolean; description: string }
	>;
}

export interface RavenProviderUpdate {
	apiKey?: string;
	apiBase?: string | null;
	models?: string[];
}

export const ravenProvidersApi = {
	list: () => client.get<{ providers: RavenProviderSummary[] }>('/raven/providers'),
	get: (name: string) => client.get<RavenProviderDetail>(`/raven/providers/${name}`),
	update: (name: string, body: RavenProviderUpdate) =>
		client.put<{ ok: boolean }>(`/raven/providers/${name}`, body),
	test: (name: string) => client.post<{ result: unknown }>(`/raven/providers/${name}/test`, {}),
	reset: (name: string) => client.post<{ ok: boolean }>(`/raven/providers/${name}/reset`, {}),
};
