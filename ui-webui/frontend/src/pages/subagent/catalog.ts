import type { TFunction } from 'i18next';

import type { RavenSubagentProbe, RavenThirdPartySubagent } from '@/api';

/** Which fields a preset needs outside the Advanced disclosure. */
export type BasicField = 'apiKey' | 'baseUrl' | 'model';

/** Fields a preset's provider accepts and then ignores. Only the members listed
 *  here are actually honoured by the form, so the union is deliberately narrow:
 *  a wider `string[]` would let `unsupported: ['temperature']` type-check and
 *  then silently do nothing. */
export type UnsupportedField = 'systemPrompt';

export interface PresetDisplay {
	labelKey: string;
	basicFields: BasicField[];
	/** Fields this preset's provider accepts and then ignores, so the form must
	 *  not offer them. Provider-specific, which is why it lives here rather than
	 *  in the config schema: a rule keyed to one preset name would put a vendor
	 *  quirk in the config layer. */
	unsupported?: UnsupportedField[];
}

/** Display metadata for the built-in presets, keyed by the `name` the backend
 *  ships in raven/agent/subagent/presets.py. A preset with no entry here still
 *  renders, with its name as the label and a neutral icon, so adding one in
 *  Python cannot break this page - the same contract ProviderIcon has with the
 *  provider registry. */
export const PRESET_DISPLAY: Record<string, PresetDisplay> = {
	claude_code: { labelKey: 'subagent-sidebar.presetClaudeCode', basicFields: [] },
	codex: { labelKey: 'subagent-sidebar.presetCodex', basicFields: [] },
	openclaw: { labelKey: 'subagent-sidebar.presetOpenclaw', basicFields: [] },
	hermes: { labelKey: 'subagent-sidebar.presetHermes', basicFields: [] },
	opencode: { labelKey: 'subagent-sidebar.presetOpencode', basicFields: [] },
	mirothinker: {
		labelKey: 'subagent-sidebar.presetMirothinker',
		basicFields: ['apiKey', 'baseUrl', 'model'],
		// The server drops a system-role message: the same instruction is obeyed
		// in the user role and ignored in the system role. Offering the field
		// would promise behaviour the endpoint does not deliver.
		unsupported: ['systemPrompt'],
	},
};

export function presetLabel(name: string, t: TFunction): string {
	const entry = PRESET_DISPLAY[name];
	return entry ? t(entry.labelKey) : name;
}

export function isPresetName(name: string, presets: RavenThirdPartySubagent[]): boolean {
	return presets.some((p) => p.name === name);
}

/** Which install group a preset row belongs to, or `null` while unknown.
 *
 *  The cli rule reads the probe: `ready` means `argv[0]` resolved on the
 *  login-shell PATH. The openai rule reads `apiKey` off the entry instead, and
 *  deliberately not the probe's detail text: the probe only short-circuits on a
 *  blank key when the source is a preset, so a *configured* openai entry with no
 *  key is actually sent and comes back "api key not set or rejected" - a string
 *  that cannot tell "no key" from "key rejected", which are different groups and
 *  different user actions. */
export function installGroupOf(
	entry: RavenThirdPartySubagent,
	probe: RavenSubagentProbe | null,
): 'installed' | 'uninstalled' | null {
	if (entry.kind === 'openai') {
		return (entry.apiKey ?? '').trim() === '' ? 'uninstalled' : 'installed';
	}
	if (probe === null) return null;
	return probe.status === 'ready' ? 'installed' : 'uninstalled';
}
