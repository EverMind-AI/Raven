import { client } from './client';

// Raven's own third-party sub-agent config (req5/P4). Keys are camelCase to
// match what the gateway returns (model_dump by_alias); the gateway accepts
// either alias on write.
export interface RavenCliSubagent {
	name: string;
	kind: 'cli';
	description?: string;
	/** Which built-in preset this entry came from; absent for a hand-written one.
	 *  Provenance only - `name` is user-editable, so the page groups on this. */
	preset?: string | null;
	/** Whether the dispatching model is offered this agent. Absent counts as
	 *  enabled, matching the backend default. */
	enabled?: boolean;
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
	transcriptFormat?: 'text' | 'codex_jsonl' | 'claude_stream_json' | 'openclaw_json' | 'opencode_json';
	cwd?: string | null;
	env?: Record<string, string>;
	timeout?: number | null;
	maxOutputChars?: number;
}

export interface RavenOpenAISubagent {
	name: string;
	kind: 'openai';
	description?: string;
	/** Which built-in preset this entry came from; absent for a hand-written one.
	 *  Provenance only - `name` is user-editable, so the page groups on this. */
	preset?: string | null;
	/** Whether the dispatching model is offered this agent. Absent counts as
	 *  enabled, matching the backend default. */
	enabled?: boolean;
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

/** One subagent's free availability check (`GET /raven/subagents/probe`).
 *  `target` is the resolved executable path for a cli agent, or the base URL
 *  for an openai one. */
export interface RavenSubagentProbe {
	name: string;
	source: 'config' | 'preset';
	kind: string;
	status: 'ready' | 'attention' | 'missing' | 'unknown';
	detail: string;
	target: string;
	elapsedMs: number;
	/** The remembered outcome of an explicit test, when one is still valid for this
	 *  exact configuration. Null when never tested, or when the configuration has
	 *  changed since - a stale verdict is dropped rather than shown as current. */
	lastTest?: { ok: boolean; detail: string; testedAtMs: number } | null;
}

/** One subagent's explicit test (`POST /raven/subagents/test`). `reply` carries
 *  the agent's own answer for a cli test and is always null for openai, which
 *  sends no completion. `kind` is null only when the name matched nothing. */
export interface RavenSubagentTest {
	name: string;
	source: 'config' | 'preset';
	kind: 'cli' | 'openai' | null;
	ok: boolean;
	detail: string;
	reply: string | null;
	elapsedMs: number;
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
	everos: {
		enabled: boolean;
		// Skill-extraction knobs (surface B). Optional so existing callers that
		// only toggle `enabled` stay valid.
		maxSkillsTopK?: number;
		retireConfidence?: number;
		minQualityForSkillExtract?: number;
		complexTaskToolCallThreshold?: number;
	};
	localDirs: RavenLocalDir[];
}

/** One EverOS memory role's model config (llm/embedding/rerank/multimodal),
 *  stored in ~/.everos/raven/everos.toml. `api_key` reads back redacted
 *  (`****set****` / `(empty)`); send it unchanged to keep the stored key. */
export interface EverOSModelConfig {
	model?: string;
	base_url?: string;
	api_key?: string;
	provider?: string;
	[k: string]: unknown;
}

export type EverOSSection = 'llm' | 'embedding' | 'rerank' | 'multimodal';

/** One curated memory-model provider: its base URL, which roles it serves, and
 *  (for rerank) the service protocol + endpoint override. Mirrors the onboard
 *  wizard's list so the page can offer a provider picker that pre-fills URLs. */
export interface EverOSProvider {
	name: string;
	label: string;
	label_zh: string;
	base_url: string;
	supports: EverOSSection[];
	rerank_provider?: string | null;
	rerank_base_url?: string | null;
	/** The registry's curated shortlist for this provider, already stripped of
	 *  the provider prefix. Chat-only, so it seeds the llm and multimodal
	 *  pickers; embedding and rerank have no catalogue and rely on a live fetch. */
	chat_models?: string[];
	/** True when the Credentials page already holds a key for this provider, so
	 *  the memory roles can borrow it instead of asking for the same secret. */
	has_credential?: boolean;
}

export interface EverOSMemoryConfig {
	llm: EverOSModelConfig;
	embedding: EverOSModelConfig;
	rerank: EverOSModelConfig;
	multimodal: EverOSModelConfig;
}

export interface RavenSkillEntry {
	id?: string;
	name: string;
	source: string;
	description: string;
	path?: string;
}

/**
 * One MCP server raven is configured with. ``connected`` / ``tools`` are live
 * runtime state, not config: MCP servers connect lazily on the first turn that
 * needs them, so a valid server reads as not connected until then.
 */
export interface RavenMcpServer {
	name: string;
	type?: 'stdio' | 'sse' | 'streamableHttp' | null;
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	url?: string;
	headers?: Record<string, string>;
	toolTimeout?: number;
	connected?: boolean;
	tools?: string[];
	/** Set when the config entry does not validate. The server is still listed
	 *  — a hand-broken entry has to stay visible to be fixable — but its fields
	 *  come back empty, and saving over it is refused until the config is fixed. */
	error?: string;
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
	// `statefulNames` is a sibling of `agents`, not a field on each entry: the gateway
	// resolves an acp agent's real resumability from its own capability snapshot,
	// which an older gateway does not send at all -- absent, not empty, is what
	// tells a caller to fall back to guessing from `agents` itself.
	listSubagents: () =>
		client.get<{ agents: RavenThirdPartySubagent[]; statefulNames?: string[] }>('/raven/subagents'),

	presets: () => client.get<{ presets: RavenThirdPartySubagent[] }>('/raven/subagents/presets'),

	probeSubagents: () => client.get<{ results: RavenSubagentProbe[] }>('/raven/subagents/probe'),

	testSubagent: (body: { name: string; source: 'config' | 'preset' }) =>
		client.post<{ result: RavenSubagentTest }>('/raven/subagents/test', body),

	listSubagentInstances: (sessionKey?: string) =>
		client.get<{ instances: RavenSubagentInstance[] }>(
			'/raven/subagents/instances',
			sessionKey ? { session_key: sessionKey } : undefined,
		),

	deleteSubagentInstances: (sessionKey: string) =>
		client.delete<{ removed: number }>('/raven/subagents/instances', {
			session_key: sessionKey,
		}),

	/** One instance's past direct turns; the only memory of them that survives a reload. */
	instanceHistory: (sessionId: string, agent: string, handle: string) =>
		client.get<{ turns: { role: 'user' | 'assistant'; content: string }[] }>(
			'/raven/subagents/instances/history',
			{ session_id: sessionId, agent, handle },
		),

	/**
	 * Send one prompt to an instance. Resolves when the turn is *accepted*, not
	 * when it is answered: the reply arrives as `subagent_direct_*` events on
	 * the session stream, which is what lets several instances answer at once.
	 */
	chatWithInstance: (body: {
		session_id: string;
		agent: string;
		handle: string;
		content: string;
	}) => client.post<{ accepted: boolean }>('/raven/subagents/instances/chat', body),

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

	// `sessionKey` names the session whose working directory holds the run dir;
	// without it the gateway can only guess, and between turns it guesses the
	// agent home, where a per-session run was never written.
	getDagNode: (
		runId: string,
		nodeId: string,
		maxOutputChars?: number,
		sessionKey?: string,
	) => {
		const query: Record<string, string> = {}
		if (maxOutputChars) query.max_output_chars = String(maxOutputChars)
		if (sessionKey) query.session_key = sessionKey

		return client.get<{ node: RavenDagNodeDetail }>(
			`/raven/subagents/dag/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}`,
			Object.keys(query).length > 0 ? query : undefined,
			{ silent: true },
		)
	},

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

	/** Tool credentials: the Serper key behind `web_search`, and each media
	 *  tool's key/model. Read per call rather than cached — `registered` is the
	 *  live loop's answer, and it only changes when the gateway restarts. */
	listTools: (opts?: { silent?: boolean }) =>
		client.get<{ tools: RavenToolCredential[] }>('/raven/tools', undefined, opts),

	// Fields are the config model's snake_case names (`api_key`, `api_base`,
	// `model`, `max_results`), matching what `raven.tools.set` validates against.
	setTool: (kind: string, fields: Record<string, string | number>) =>
		client.put<{ ok: boolean; restart_required?: boolean }>(`/raven/tools/${kind}`, {
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
			/** Present for a QR channel: how the rebind flow is going, on the poll
			 *  the dialog already makes. */
			rebind?: RavenRebindState | null;
		}>(`/raven/channels/${name}/qr`),

	/** Pair a QR channel to a different account. The adapter keeps the account it
	 *  has until a new scan is confirmed, and no gateway restart is needed — so
	 *  cancelling, or walking away, costs the user nothing. */
	rebindChannel: (name: string) =>
		client.post<RavenRebindState & { started: boolean; reason: string }>(
			`/raven/channels/${name}/rebind`,
			{},
		),

	cancelChannelRebind: (name: string) =>
		client.post<RavenRebindState>(`/raven/channels/${name}/rebind/cancel`, {}),

	// EverOS long-term memory models (llm/embedding/rerank/multimodal). Changes
	// take effect on the next gateway restart (restart_required is returned).
	everos: {
		// `backend` is the memory backend actually in force; these models are
		// inert unless it is 'everos'.
		get: () =>
			client.get<{ everos: EverOSMemoryConfig; backend?: string | null }>('/raven/everos'),
		// `reuseKeyFrom` names another role whose stored key this one should
		// borrow. The web only ever sees a redacted key, so the copy happens on
		// the gateway rather than by sending one back down.
		set: (
			section: EverOSSection,
			fields: EverOSModelConfig,
			borrow: { reuseKeyFrom?: EverOSSection; credentialProvider?: string } = {},
		) =>
			client.put<{ ok: boolean; restart_required: boolean }>(`/raven/everos/${section}`, {
				fields,
				reuse_key_from: borrow.reuseKeyFrom,
				credential_provider: borrow.credentialProvider,
			}),
		clear: (section: EverOSSection) =>
			client.delete<{ ok: boolean; restart_required: boolean }>(`/raven/everos/${section}`),
		test: (
			section: EverOSSection,
			fields: EverOSModelConfig,
			borrow: { reuseKeyFrom?: EverOSSection; credentialProvider?: string } = {},
		) =>
			client.post<{ ok: boolean; detail: string }>(`/raven/everos/${section}/test`, {
				fields,
				reuse_key_from: borrow.reuseKeyFrom,
				credential_provider: borrow.credentialProvider,
			}),
		providers: () => client.get<{ providers: EverOSProvider[] }>('/raven/everos/providers'),
		models: (body: {
			section: EverOSSection;
			base_url?: string;
			api_key?: string;
			provider_name?: string;
			reuse_key_from?: EverOSSection;
			credential_provider?: string;
		}) => client.post<{ models: string[] }>('/raven/everos/models', body),
	},

	/** Raven's own MCP servers — what the chat agent can reach. Distinct from
	 *  ``workspaceApi.mcp``, which lists the AgentScope workspace's servers and
	 *  has no effect on the gateway agent. */
	mcp: {
		list: () => client.get<{ servers: RavenMcpServer[]; connected: boolean }>('/raven/mcp'),
		/** Replaces the whole list. ``restart_required`` is always true: MCP
		 *  tools are registered once, at connect time. */
		set: (servers: RavenMcpServer[]) =>
			client.put<{ ok: boolean; restart_required: boolean }>('/raven/mcp', { servers }),
	},

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

/** Where a tool's key actually comes from, in the order the tool resolves them.
 *  `none` is the state the page turns into a prompt for one. */
export type RavenToolKeySource = 'own' | 'env' | 'openrouter' | 'none';

export interface RavenToolCredential {
	/** Config section: `web_search`, or a media tool (`image` / `speech` / `video`). */
	kind: string;
	/** The name the model calls it by, e.g. `image_generate`. */
	tool: string;
	/** Whether the *running* gateway offers it. A save cannot change this — the
	 *  tool is registered where the AgentLoop is built — which is why every save
	 *  reports `restart_required`. */
	registered: boolean;
	/** Media only: whether the config alone would register it at the next start. */
	configured?: boolean;
	settingPath: string;
	envKey: string;
	/** `****set****` or `(empty)`; the key itself never reaches the browser. */
	apiKey: string;
	keySource: RavenToolKeySource;
	/** web_search only. */
	maxResults?: number;
	/** Media only. */
	apiBase?: string;
	defaultApiBase?: string;
	model?: string;
	defaultModel?: string;
}

/** How a QR channel's rebind is going. `phase` is what the dialog draws:
 *  `waiting` (a code is up — an expiry reissues and stays here with a higher
 *  `refreshes`), `scanned` (seen, awaiting confirmation on the phone),
 *  `confirmed`, `failed`, `cancelled`, `idle`. */
export interface RavenRebindState {
	phase: 'idle' | 'waiting' | 'scanned' | 'confirmed' | 'failed' | 'cancelled';
	/** Codes reissued so far, against `max_refreshes` before the flow gives up. */
	refreshes: number;
	max_refreshes: number;
	/** Seconds since the current code was issued; null when none is up. Sent as an
	 *  age rather than a deadline because the two clocks are not shared. */
	code_age_s: number | null;
	/** Why it failed, when it did: `expired`, `no_token`, `error`. */
	detail: string;
}

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
