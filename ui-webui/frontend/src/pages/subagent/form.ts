import type { RavenThirdPartySubagent } from '@/api';

export type Kind = 'cli' | 'openai';

export interface FormState {
	kind: Kind;
	preset: string | null;
	name: string;
	description: string;
	command: string;
	resumeCommand: string;
	readsLocalFiles: boolean;
	idSource: 'provisioned' | 'derived';
	sessionIdPattern: string;
	outputPattern: string;
	transcriptFormat: 'text' | 'codex_jsonl' | 'claude_stream_json' | 'openclaw_json' | 'opencode_json';
	cwd: string;
	env: string;
	baseUrl: string;
	model: string;
	apiKey: string;
	systemPrompt: string;
	temperature: string;
	maxTokens: string;
	timeout: string;
}

export const EMPTY_FORM: FormState = {
	kind: 'cli',
	preset: null,
	name: '',
	description: '',
	command: '',
	resumeCommand: '',
	readsLocalFiles: true,
	idSource: 'provisioned',
	sessionIdPattern: '',
	outputPattern: '',
	transcriptFormat: 'text',
	cwd: '',
	env: '',
	baseUrl: '',
	model: '',
	apiKey: '',
	systemPrompt: '',
	temperature: '',
	maxTokens: '',
	timeout: '',
};

export function envToText(env: Record<string, string> | undefined): string {
	return Object.entries(env ?? {})
		.map(([k, v]) => `${k}=${v}`)
		.join('\n');
}

export function textToEnv(text: string): Record<string, string> {
	const out: Record<string, string> = {};
	for (const line of text.split('\n')) {
		const trimmed = line.trim();
		const idx = trimmed.indexOf('=');
		if (!trimmed || idx <= 0) continue;
		out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
	}
	return out;
}

/** Every field falls back to a string, so a payload missing one yields a blank
 * input rather than an undefined that only fails later, inside `toEntry`'s
 * `.trim()`, as a "cannot read properties of undefined" with nothing naming the
 * field. A gateway older than the client is the case that actually hits this. */
const text = (v: string | null | undefined): string => v ?? '';
const num = (v: number | null | undefined): string => (v == null ? '' : String(v));

export function toForm(a: RavenThirdPartySubagent): FormState {
	if (a.kind === 'openai') {
		return {
			...EMPTY_FORM,
			kind: 'openai',
			preset: a.preset ?? null,
			name: text(a.name),
			description: text(a.description),
			baseUrl: text(a.baseUrl),
			model: text(a.model),
			// Never surface a stored key; blank means "keep it" on save.
			apiKey: '',
			readsLocalFiles: a.readsLocalFiles ?? false,
			systemPrompt: text(a.systemPrompt),
			temperature: num(a.temperature),
			maxTokens: num(a.maxTokens),
			timeout: num(a.timeout),
		};
	}
	return {
		...EMPTY_FORM,
		kind: 'cli',
		preset: a.preset ?? null,
		name: text(a.name),
		description: text(a.description),
		command: text(a.command),
		resumeCommand: text(a.resumeCommand),
		readsLocalFiles: a.readsLocalFiles ?? true,
		idSource: a.idSource ?? 'provisioned',
		sessionIdPattern: text(a.sessionIdPattern),
		outputPattern: text(a.outputPattern),
		transcriptFormat: a.transcriptFormat ?? 'text',
		cwd: text(a.cwd),
		env: envToText(a.env),
		timeout: num(a.timeout),
	};
}

/** `previous` is the stored entry being edited, used to keep an unchanged api key.
 *  `presetDescription` is the selected preset's shipped text: clearing the field
 *  reverts to that rather than to nothing, so the roster the model reads never
 *  goes blank for a preset the user merely did not describe.
 *
 *  Fields the form does not own are carried forward from `previous` rather than
 *  left out: the caller replaces the stored entry wholesale, and the schema
 *  defaults anything absent. `enabled` is the one that bites -- its default is
 *  true, so omitting it silently re-enables a switched-off agent on the next
 *  save of any unrelated field. `stateful` and `maxOutputChars` are here for the
 *  same reason. */
export function toEntry(
	f: FormState,
	previous?: RavenThirdPartySubagent,
	presetDescription?: string,
): RavenThirdPartySubagent {
	const description = f.description.trim() || presetDescription?.trim() || undefined;
	const timeout = f.timeout.trim() === '' ? null : Number(f.timeout);
	if (f.kind === 'openai') {
		const stored = previous?.kind === 'openai' ? previous.apiKey : undefined;
		return {
			name: f.name.trim(),
			kind: 'openai',
			preset: f.preset,
			description,
			baseUrl: f.baseUrl.trim(),
			model: f.model.trim(),
			apiKey: f.apiKey.trim() || stored || '',
			systemPrompt: f.systemPrompt.trim() || null,
			temperature: f.temperature.trim() === '' ? null : Number(f.temperature),
			maxTokens: f.maxTokens.trim() === '' ? null : Number(f.maxTokens),
			timeout,
			maxOutputChars: previous?.maxOutputChars,
			enabled: previous?.enabled,
		};
	}
	return {
		name: f.name.trim(),
		kind: 'cli',
		preset: f.preset,
		description,
		command: f.command.trim(),
		resumeCommand: f.resumeCommand.trim() || null,
		readsLocalFiles: f.readsLocalFiles,
		stateful: previous?.kind === 'cli' ? previous.stateful : undefined,
		idSource: f.idSource,
		sessionIdPattern: f.sessionIdPattern.trim() || null,
		outputPattern: f.outputPattern.trim() || null,
		transcriptFormat: f.transcriptFormat,
		cwd: f.cwd.trim() || null,
		env: textToEnv(f.env),
		timeout,
		maxOutputChars: previous?.maxOutputChars,
		enabled: previous?.enabled,
	};
}

/** Mirrors the backend's rules (raven/config/schema.py). A stateful
 *  *provisioned* command needs `{agent_id}`; a *derived* command must not
 *  (the CLI mints the id itself). */
export function validate(
	f: FormState,
	agents: RavenThirdPartySubagent[],
	editingName: string | null,
) {
	const stateful = f.resumeCommand.trim().length > 0;
	const derived = f.idSource === 'derived';
	const idInCommand = f.command.includes('{agent_id}');
	const cliOk = stateful
		? f.resumeCommand.includes('{agent_id}') && (derived ? !idInCommand : idInCommand)
		: !idInCommand;
	const openaiOk = !!f.baseUrl.trim() && !!f.model.trim();
	// An openai-kind agent cannot work without a key. Blank is still allowed when
	// one is already stored for this name - that is the "leave blank to keep the
	// stored key" path, which never surfaces the stored value.
	const hasStoredKey = agents.some(
		(a) => a.name === editingName && a.kind === 'openai' && !!a.apiKey,
	);
	const keyOk = f.kind !== 'openai' || !!f.apiKey.trim() || hasStoredKey;
	const nameCollides = agents.some((a) => a.name === f.name.trim() && a.name !== editingName);
	const canSubmit =
		!!f.name.trim() &&
		!nameCollides &&
		(f.kind === 'openai' ? openaiOk && keyOk : !!f.command.trim() && cliOk);
	return { canSubmit, nameCollides, cliOk, stateful, derived, keyOk, hasStoredKey };
}
