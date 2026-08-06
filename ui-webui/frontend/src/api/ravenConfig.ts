import { client } from './client';

// Raven's own third-party sub-agent config (req5/P4). Keys are camelCase to
// match what the gateway returns (model_dump by_alias); the gateway accepts
// either alias on write.
export interface RavenCliSubagent {
	name: string;
	kind: 'cli';
	description?: string;
	command: string;
	resumeCommand?: string | null;
	// Declared capabilities the DAG tool advertises and enforces before it runs a
	// node. `stateful` must agree with `resumeCommand` (the gateway rejects a
	// mismatch), so it is carried through unchanged rather than edited here.
	stateful?: boolean | null;
	readsLocalFiles?: boolean;
	idSource?: 'provisioned' | 'derived';
	sessionIdPattern?: string | null;
	outputPattern?: string | null;
	transcriptFormat?: 'text' | 'codex_jsonl' | 'claude_stream_json';
	cwd?: string | null;
	env?: Record<string, string>;
	timeout?: number | null;
	maxOutputChars?: number;
}

export interface RavenOpenAISubagent {
	name: string;
	kind: 'openai';
	description?: string;
	baseUrl: string;
	model: string;
	apiKey?: string;
	stateful?: boolean | null;
	readsLocalFiles?: boolean;
	systemPrompt?: string | null;
	temperature?: number | null;
	maxTokens?: number | null;
	timeout?: number | null;
	maxOutputChars?: number;
}

export interface RavenSubagentInstance {
	kind: 'cli' | 'dag-node';
	sessionKey: string;
	agent: string;
	handle: string;
	agentId?: string;
	runId?: string;
	nodeId?: string;
	status?:
		| 'pending'
		| 'running'
		| 'completed'
		| 'failed'
		| 'skipped'
		| 'cancelled'
		| 'interrupted';
	createdAtMs: number;
	updatedAtMs: number;
}

/** One node of a DAG run read back from its durable run dir. Same shape as a
 *  `dag_run_completed` manifest entry, plus the node's `prompt_template`
 *  (which lives only in the run dir's `graph.json`). */
export interface RavenDagRunFile {
	node: string;
	subagent?: string;
	instance?: string | null;
	depends_on?: string[];
	status: string;
	started_at?: number | null;
	ended_at?: number | null;
	prompt_file?: string | null;
	output_file?: string | null;
	error?: string | null;
	prompt_template?: string | null;
}

/** A DAG run rebuilt from disk. `finalized` is false while the run is still
 *  executing (no `manifest.json` yet), in which case the gateway has already
 *  overlaid the instance registry's per-node statuses onto `files`. */
export interface RavenDagRun {
	run_id: string;
	dir?: string;
	finalized: boolean;
	files: RavenDagRunFile[];
	terminal_outputs: Array<{ node: string; text: string }>;
	summary?: { total: number; completed: number; failed: number; skipped: number };
}

/** One DAG node's rendered prompt and the head of its output file. */
export interface RavenDagNodeDetail {
	run_id: string;
	node: string;
	prompt: string | null;
	prompt_file?: string | null;
	output: string | null;
	output_file?: string | null;
	output_chars: number;
	output_truncated: boolean;
}

export type RavenThirdPartySubagent = RavenCliSubagent | RavenOpenAISubagent;

export interface RavenCronSchedule {
	kind: 'cron' | 'every' | 'at';
	expr?: string | null;
	every_ms?: number | null;
	at_ms?: number | null;
	tz?: string | null;
}

export interface RavenCronJob {
	id: string;
	name: string;
	enabled: boolean;
	schedule: RavenCronSchedule;
	message: string;
	channel?: string | null;
	to?: string | null;
	next_run_at_ms?: number | null;
}

export interface RavenCronAddRequest {
	name: string;
	schedule: RavenCronSchedule;
	message: string;
	channel?: string | null;
	to?: string | null;
	deliver?: boolean;
}

export interface RavenLocalDir {
	path: string;
	enabled: boolean;
	name?: string | null;
	alwaysEnabled: boolean;
}

export interface RavenSkillForge {
	enabled: boolean;
	router: {
		weights: { local?: number; everos?: number; hub?: number };
		hub: { endpoint?: string; apiKey?: string | null; minSafety?: number; timeoutS?: number };
	};
	everos: { enabled: boolean };
	localDirs: RavenLocalDir[];
}

export interface RavenSkillEntry {
	id?: string;
	name: string;
	source: string;
	description: string;
	path?: string;
}

export interface RavenSkillBody {
	skillMd: string;
	name: string;
	source?: string;
	version?: string;
	path?: string;
	// Enrichment so an installed skill's detail page matches the hub one.
	description?: string;
	category?: string | null;
	license?: string | null;
	tags?: string[];
	source_url?: string | null;
	files?: string[];
}

/** A skill-hub catalog item (metadata only; body fetched separately).
 *  Field names mirror the live skillhub.evermind.ai OpenAPI response. */
export interface RavenHubItem {
	id?: string;
	slug?: string;
	skill_id?: string;
	name: string;
	description?: string;
	source?: string;
	category?: string;
	tags?: string[];
	quality_score?: number;
	github_star?: number;
	install_count?: number;
	source_url?: string;
	download_url?: string;
	license?: string;
	version?: string;
	/** Relative paths of the skill's bundled files (SKILL.md, scripts/…). */
	files?: string[];
	/** Legacy aliases kept for older hub deployments. */
	scenario_tags?: string[];
	score_safety?: number;
}

export const ravenConfigApi = {
	listSubagents: () => client.get<{ agents: RavenThirdPartySubagent[] }>('/raven/subagents'),

	presets: () => client.get<{ presets: RavenThirdPartySubagent[] }>('/raven/subagents/presets'),

	listSubagentInstances: (sessionKey?: string) =>
		client.get<{ instances: RavenSubagentInstance[] }>(
			'/raven/subagents/instances',
			sessionKey ? { session_key: sessionKey } : undefined,
		),

	deleteSubagentInstances: (sessionKey: string) =>
		client.delete<{ removed: number }>('/raven/subagents/instances', {
			session_key: sessionKey,
		}),

	cancelDagRun: (runId: string) =>
		client.post<{ cancelled: boolean }>(
			`/raven/subagents/dag/${encodeURIComponent(runId)}/cancel`,
		),

	// Both DAG reads are `silent`: they are best-effort background restores of a
	// run whose dir may have been pruned, and a toast per stale run on every
	// session load would be noise the user can do nothing about.
	getDagRun: (runId: string, sessionKey?: string) =>
		client.get<{ run: RavenDagRun }>(
			`/raven/subagents/dag/${encodeURIComponent(runId)}`,
			sessionKey ? { session_key: sessionKey } : undefined,
			{ silent: true },
		),

	getDagNode: (runId: string, nodeId: string, maxOutputChars?: number) =>
		client.get<{ node: RavenDagNodeDetail }>(
			`/raven/subagents/dag/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}`,
			maxOutputChars ? { max_output_chars: String(maxOutputChars) } : undefined,
			{ silent: true },
		),

	cancelSubagentInstance: (body: { session_key: string; agent: string; handle: string }) =>
		client.post<{ cancelled: boolean }>('/raven/subagents/instances/cancel', body),

	setSubagents: (agents: RavenThirdPartySubagent[]) =>
		client.put<{ ok: boolean; count: number }>('/raven/subagents', { agents }),

	listCron: () => client.get<{ jobs: RavenCronJob[] }>('/raven/cron'),

	addCron: (body: RavenCronAddRequest) => client.post<{ job: RavenCronJob }>('/raven/cron', body),

	removeCron: (jobId: string) => client.delete<{ removed: boolean }>(`/raven/cron/${jobId}`),

	// `silent` suppresses the error toast, for callers that poll this as a
	// liveness probe (e.g. waiting out a gateway restart).
	listChannels: (opts?: { silent?: boolean }) =>
		client.get<{ channels: RavenChannel[] }>('/raven/channels', undefined, opts),

	setChannel: (name: string, fields: Record<string, string>) =>
		client.put<{ ok: boolean; restart_required?: boolean }>(`/raven/channels/${name}`, {
			fields,
		}),

	// Re-exec the gateway to apply restart-required config (channels). The gateway
	// drops every connection when it restarts, so callers should reconnect after.
	restartGateway: () => client.post<{ ok: boolean }>('/raven/gateway/restart', {}),

	// The pending login QR (PNG data URI) for a QR-login channel (weixin/whatsapp),
	// or null when nothing is pending / already connected.
	// `qr` is a PNG data URI; `qr_text` carries the raw payload instead when the
	// gateway has no `qrcode` package to rasterise with.
	channelQr: (name: string) =>
		client.get<{
			qr: string | null;
			qr_text?: string | null;
			connected: boolean;
			running: boolean;
		}>(`/raven/channels/${name}/qr`),

	skills: {
		get: () => client.get<{ skillforge: RavenSkillForge }>('/raven/skills'),
		set: (fields: Partial<RavenSkillForge>) =>
			client.put<{ ok: boolean; restart_required: boolean }>('/raven/skills', { fields }),
		listAvailable: () => client.get<{ skills: RavenSkillEntry[] }>('/raven/skills/available'),
		body: (params: { name?: string; id?: string; source?: string }) =>
			client.post<RavenSkillBody>('/raven/skills/body', params),
		/** Uninstall a downloaded skill (deletes its folder under the hub cache).
		 *  Backend refuses builtin / local-dir skills. */
		remove: (params: { name?: string; id?: string; source?: string }) =>
			client.post<{ ok: boolean; removed?: string }>('/raven/skills/remove', params),
		hub: {
			test: (endpoint?: string, apiKey?: string) =>
				client.post<{ ok: boolean; detail: string }>('/raven/skills/hub/test', {
					endpoint,
					apiKey,
				}),
			search: (q: string, limit = 20) =>
				client.get<{ items: RavenHubItem[] }>('/raven/skills/hub/search', {
					q,
					limit: String(limit),
				}),
			/** Paginated full-catalog browse (all ~93k skills) via the hub's
			 *  ``/skills/search``. Supports category + free-text (q) filtering. */
			browse: (page = 1, opts: { limit?: number; category?: string; q?: string } = {}) =>
				client.get<{
					items: RavenHubItem[];
					total?: number;
					page?: number;
					limit?: number;
				}>('/raven/skills/hub/browse', {
					page: String(page),
					limit: String(opts.limit ?? 24),
					...(opts.category ? { category: opts.category } : {}),
					...(opts.q ? { q: opts.q } : {}),
				}),
			/** Full metadata + skill_md for one hub catalog skill (detail view). */
			skill: (id: string) =>
				client.get<RavenHubItem & { skill_md?: string }>('/raven/skills/hub/skill', { id }),
			install: (id: string) =>
				client.post<{ ok: boolean; slug?: string; version?: string; dir?: string }>(
					'/raven/skills/hub/install',
					{ id },
				),
		},
	},
};

export interface RavenChannelFieldSpec {
	type: string;
	default: unknown;
	is_secret: boolean;
	required: boolean;
	description: string;
}

export interface RavenChannel {
	name: string;
	enabled: boolean;
	specs: Record<string, RavenChannelFieldSpec>;
	config: Record<string, unknown>;
}
