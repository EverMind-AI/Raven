import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { EverOSModelConfig, EverOSProvider, EverOSSection } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useTranslation } from '@/i18n/useI18n';
import CheckCircle from '~icons/solar/check-circle-bold-duotone';
import XCircle from '~icons/solar/close-circle-bold-duotone';
import Database from '~icons/solar/database-bold-duotone';
import Download from '~icons/solar/download-minimalistic-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Restart from '~icons/solar/restart-linear';
import TestTube from '~icons/solar/test-tube-bold-duotone';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';
import Tuning from '~icons/solar/tuning-bold-duotone';

// The redacted placeholder the gateway sends back for a secret that is set.
// Echoing it on save/test/fetch means "keep / use the stored key".
const SECRET_SET = '****set****';
// A stable, per-role recommended model id (mirrors the onboard wizard's example)
// so the model dropdown always has a sensible default even before a live fetch.
const RECOMMENDED: Record<EverOSSection, string> = {
	llm: 'gpt-4.1-mini',
	embedding: 'Qwen/Qwen3-Embedding-4B',
	rerank: 'Qwen/Qwen3-Reranker-4B',
	multimodal: 'google/gemini-3-flash-preview',
};
const CUSTOM_MODEL = '__custom__';
const RERANK_PROTOCOLS = ['vllm', 'deepinfra', 'dashscope'];

/** How long a key field has to stop changing before the automatic model fetch
 *  goes out. Long enough that typing a key is one request, not one per key. */
const AUTO_FETCH_DEBOUNCE_MS = 700;

const SECTIONS: { key: EverOSSection; required: boolean }[] = [
	{ key: 'llm', required: true },
	{ key: 'embedding', required: true },
	{ key: 'rerank', required: false },
	{ key: 'multimodal', required: false },
];

interface SecForm {
	providerName: string; // '' = custom OpenAI-compatible endpoint
	baseUrl: string; // effective base URL (auto from provider, or typed for custom)
	model: string;
	apiKey: string;
	rerankProvider: string; // stored `provider` field, rerank only
	keySet: boolean;
	customModel: boolean;
	reuseKey: boolean; // borrow the llm role's stored key on save/test
	useCredential: boolean; // borrow the key the Credentials page holds for this provider
}

type SecForms = Record<EverOSSection, SecForm>;
type TestState = { ok: boolean; detail: string } | 'pending' | undefined;

const EMPTY_SEC: SecForm = {
	providerName: '',
	baseUrl: '',
	model: '',
	apiKey: '',
	rerankProvider: '',
	keySet: false,
	customModel: false,
	reuseKey: false,
	useCredential: false,
};

/** Extraction tuning as named levels. Most users want a stance, not four
 *  numbers; the raw inputs stay available behind the custom level. */
const KNOB_PRESETS = {
	selective: {
		maxSkillsTopK: 3,
		retireConfidence: 0.2,
		minQualityForSkillExtract: 0.4,
		complexTaskToolCallThreshold: 30,
	},
	balanced: {
		maxSkillsTopK: 5,
		retireConfidence: 0.1,
		minQualityForSkillExtract: 0.2,
		complexTaskToolCallThreshold: 20,
	},
	eager: {
		maxSkillsTopK: 8,
		retireConfidence: 0.05,
		minQualityForSkillExtract: 0.1,
		complexTaskToolCallThreshold: 10,
	},
} as const;

type KnobLevel = keyof typeof KNOB_PRESETS | 'custom';
const KNOB_LEVELS: KnobLevel[] = ['selective', 'balanced', 'eager', 'custom'];

const norm = (u: string) => u.replace(/\/+$/, '');

/** The base URL a provider serves for a role (rerank may override it). */
function effectiveBase(p: EverOSProvider, section: EverOSSection): string {
	return section === 'rerank' && p.rerank_base_url ? p.rerank_base_url : p.base_url;
}

function toSec(
	section: EverOSSection,
	c: EverOSModelConfig | undefined,
	providers: EverOSProvider[],
): SecForm {
	const storedBase = typeof c?.base_url === 'string' ? c.base_url : '';
	const match = providers.find(
		(p) => p.supports.includes(section) && norm(effectiveBase(p, section)) === norm(storedBase),
	);
	return {
		providerName: match?.name ?? '',
		baseUrl: match ? effectiveBase(match, section) : storedBase,
		model: typeof c?.model === 'string' ? c.model : '',
		apiKey: '',
		rerankProvider: typeof c?.provider === 'string' ? c.provider : '',
		keySet: c?.api_key === SECRET_SET,
		customModel: false,
		reuseKey: false,
		useCredential: false,
	};
}

/** A role counts as usable only with an endpoint, a model and a key -- the
 *  memory backend needs all three, so two of them is still "not set up". */
function isReady(f: SecForm): boolean {
	return Boolean(
		f.baseUrl.trim() &&
		f.model.trim() &&
		(f.keySet || f.apiKey || f.reuseKey || f.useCredential),
	);
}

function matchLevel(k: {
	maxSkillsTopK: string;
	retireConfidence: string;
	minQualityForSkillExtract: string;
	complexTaskToolCallThreshold: string;
}): KnobLevel {
	for (const [name, p] of Object.entries(KNOB_PRESETS)) {
		if (
			Number(k.maxSkillsTopK) === p.maxSkillsTopK &&
			Number(k.retireConfidence) === p.retireConfidence &&
			Number(k.minQualityForSkillExtract) === p.minQualityForSkillExtract &&
			Number(k.complexTaskToolCallThreshold) === p.complexTaskToolCallThreshold
		) {
			return name as KnobLevel;
		}
	}
	return 'custom';
}

/** What an automatic model fetch depends on. Two fetches with the same signature
 *  would ask the same question of the same endpoint with the same credential, so
 *  the answer is already known; anything else deserves a fresh attempt. */
function autoSignature(f: SecForm): string {
	// Serialised rather than joined on a separator: a base URL or a key may
	// contain whatever character the separator is, and two states that collapse
	// to the same string would skip a fetch that was actually due.
	return JSON.stringify([
		f.baseUrl.trim(),
		f.providerName,
		f.apiKey,
		f.keySet,
		f.reuseKey,
		f.useCredential,
	]);
}

/** Build the write/test payload. `api_key` echoes the redacted sentinel when the
 *  user left the field blank but a key is already stored. */
function toFields(f: SecForm, section: EverOSSection): EverOSModelConfig {
	const fields: EverOSModelConfig = {
		model: f.model.trim() || undefined,
		base_url: f.baseUrl.trim() || undefined,
	};
	if (f.apiKey) fields.api_key = f.apiKey;
	else if (f.keySet) fields.api_key = SECRET_SET;
	if (section === 'rerank') {
		// A custom rerank endpoint has to name its protocol; a catalog one takes
		// it from the catalog, and an empty value there means "whatever EverOS
		// defaults to", which is the right answer. The fallback matters because
		// the select cannot show a blank: with no matching option the browser
		// displays the first one, so the user read "vllm" while the state was ''
		// and nothing was ever sent. Same expression as the select's value, so
		// what is displayed is what gets written.
		const custom = f.providerName === '';
		const proto = custom ? f.rerankProvider || RERANK_PROTOCOLS[0] : f.rerankProvider;
		if (proto) fields.provider = proto;
	}
	return fields;
}

interface KnobForm {
	enabled: boolean;
	maxSkillsTopK: string;
	retireConfidence: string;
	minQualityForSkillExtract: string;
	complexTaskToolCallThreshold: string;
}

/**
 * EverOS long-term memory configuration (P4). Two surfaces on one page:
 *  A) the four memory model roles (llm / embedding / rerank / multimodal),
 *     configured by picking a provider (which pre-fills the base URL) and a
 *     model from a live-fetched dropdown -- only the API key is typed;
 *  B) the skill-extraction knobs under skillForge.everos.
 * Both apply on the next gateway restart, hence the restart banner.
 */
export function EverOSPage() {
	const { t } = useTranslation();
	const [providers, setProviders] = useState<EverOSProvider[]>([]);
	const [secs, setSecs] = useState<SecForms | null>(null);
	const [knobs, setKnobs] = useState<KnobForm | null>(null);
	const [loading, setLoading] = useState(true);
	const [busy, setBusy] = useState<EverOSSection | 'knobs' | null>(null);
	const [fetching, setFetching] = useState<EverOSSection | null>(null);
	const [modelOpts, setModelOpts] = useState<Partial<Record<EverOSSection, string[]>>>({});
	const [fetchFailed, setFetchFailed] = useState<Partial<Record<EverOSSection, boolean>>>({});
	// The (endpoint, credential) pair each role was last auto-fetched for, so a
	// corrected key or a new endpoint gets another attempt while an unchanged one
	// does not loop.
	const [autoTried, setAutoTried] = useState<Partial<Record<EverOSSection, string>>>({});
	const [tests, setTests] = useState<Partial<Record<EverOSSection, TestState>>>({});
	const [restartRequired, setRestartRequired] = useState(false);
	const [restarting, setRestarting] = useState(false);
	const [customKnobs, setCustomKnobs] = useState(false);
	const [backend, setBackend] = useState<string | null | undefined>(undefined);
	// Which roles are on disk as currently shown. Editing any field drops the
	// mark, so the badge never claims a form still holds what was written.
	const [saved, setSaved] = useState<Partial<Record<EverOSSection, boolean>>>({});

	useEffect(() => {
		let cancelled = false;
		void (async () => {
			try {
				const [ev, sk, pv] = await Promise.all([
					ravenConfigApi.everos.get(),
					ravenConfigApi.skills.get(),
					ravenConfigApi.everos.providers(),
				]);
				if (cancelled) return;
				const provs = pv.providers ?? [];
				setProviders(provs);
				setBackend(ev.backend);
				setSecs({
					llm: toSec('llm', ev.everos.llm, provs),
					embedding: toSec('embedding', ev.everos.embedding, provs),
					rerank: toSec('rerank', ev.everos.rerank, provs),
					multimodal: toSec('multimodal', ev.everos.multimodal, provs),
				});
				const e = sk.skillforge.everos;
				setKnobs({
					enabled: e.enabled,
					maxSkillsTopK: String(e.maxSkillsTopK ?? 5),
					retireConfidence: String(e.retireConfidence ?? 0.1),
					minQualityForSkillExtract: String(e.minQualityForSkillExtract ?? 0.2),
					complexTaskToolCallThreshold: String(e.complexTaskToolCallThreshold ?? 20),
				});
			} catch (e) {
				toast.error(
					`${t('everOS.loadFailed')}: ${e instanceof Error ? e.message : String(e)}`,
				);
			} finally {
				if (!cancelled) setLoading(false);
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [t]);

	/** Model ids to offer before anything is fetched. Only the chat roles have a
	 *  catalogue to draw on, and only for the provider actually selected. */
	const seedModels = (key: EverOSSection, providerName: string): string[] => {
		if (key === 'embedding' || key === 'rerank') return [];
		const p = providers.find((x) => x.name === providerName);
		return p?.chat_models ?? [];
	};

	const patchSec = (key: EverOSSection, patch: Partial<SecForm>) => {
		setSecs((s) => (s ? { ...s, [key]: { ...s[key], ...patch } } : s));
		setSaved((m) => (m[key] ? { ...m, [key]: false } : m));
	};

	/** Point a role at a new endpoint. A model id only means anything to the
	 *  endpoint that serves it, so carrying the old one over just produces a
	 *  "model does not exist" on the first test -- drop it unless the new
	 *  provider is known to offer it, or the endpoint is not actually moving. */
	const retarget = (
		key: EverOSSection,
		p: EverOSProvider | undefined,
		rest: Partial<SecForm>,
	) => {
		setModelOpts((m) => ({ ...m, [key]: undefined }));
		const was = secs?.[key];
		const nextBase = String(rest.baseUrl ?? (p ? effectiveBase(p, key) : '')).trim();
		// An unchanged endpoint still serves the model it served a moment ago, so
		// clearing it here is pure loss. It used to happen on every "Reuse LLM
		// key" for embedding and rerank: neither has a catalogue to seed from, so
		// the seedModels test below can only ever answer false for them, while
		// reuseFromLlm deliberately keeps baseUrl as it was.
		const stays = Boolean(nextBase) && nextBase === String(was?.baseUrl ?? '').trim();
		const keeps = stays || (p ? seedModels(key, p.name).includes(was?.model ?? '') : false);
		patchSec(key, {
			providerName: p?.name ?? '',
			baseUrl: p ? effectiveBase(p, key) : '',
			rerankProvider: key === 'rerank' ? (p?.rerank_provider ?? '') : '',
			model: keeps ? (was?.model ?? '') : '',
			customModel: keeps ? Boolean(was?.customModel) : false,
			useCredential: Boolean(p?.has_credential),
			...rest,
		});
	};

	const pickProvider = (key: EverOSSection, name: string) =>
		retarget(key, name ? providers.find((x) => x.name === name) : undefined, {});

	/** Adopt the llm role's provider and borrow its key, the shortcut the CLI
	 *  wizard offers: one endpoint and one key usually serve every role. */
	const reuseFromLlm = (key: EverOSSection) => {
		if (!secs) return;
		const src = secs.llm;
		const p = providers.find((x) => x.name === src.providerName);
		const usable = p && p.supports.includes(key) ? p : undefined;
		retarget(key, usable, {
			// A provider that does not serve this role still lends its key, but
			// the endpoint has to stay whatever the user set here.
			baseUrl: usable ? effectiveBase(usable, key) : secs[key].baseUrl,
			apiKey: src.apiKey, // a key typed but not yet saved carries over directly
			reuseKey: !src.apiKey,
		});
	};

	const fetchModels = async (key: EverOSSection, opts: { silent?: boolean } = {}) => {
		if (!secs) return;
		const f = secs[key];
		setFetching(key);
		try {
			const r = await ravenConfigApi.everos.models({
				section: key,
				base_url: f.baseUrl.trim() || undefined,
				api_key: f.apiKey || (f.keySet ? SECRET_SET : undefined),
				provider_name: f.providerName || undefined,
				reuse_key_from: f.reuseKey && !f.apiKey ? 'llm' : undefined,
				credential_provider: f.useCredential && !f.apiKey ? f.providerName : undefined,
			});
			setModelOpts((m) => ({ ...m, [key]: r.models ?? [] }));
			setFetchFailed((m) => ({ ...m, [key]: false }));
			if (!r.models?.length && !opts.silent) toast.info(t('everOS.noModels'));
		} catch (e) {
			// An auto-fetch runs without the user asking, so a dead endpoint or a
			// wrong key must not throw a toast at them mid-typing; the empty
			// dropdown and its hint already say the list could not be loaded.
			//
			// The failure is recorded in its own flag, not by writing [] into
			// modelOpts. That write reads as "loaded, and the endpoint offers
			// nothing", which is a different fact, and since [] is not undefined
			// it also made the value terminal: one failed attempt used up the
			// automatic path for that role until the page was reloaded.
			setFetchFailed((m) => ({ ...m, [key]: true }));
			if (!opts.silent) toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			setFetching(null);
		}
	};

	// Embedding and rerank have no catalogue to seed from, so without this the
	// dropdown sits empty until the user works out that the button is the way
	// to populate it. Runs once per role per (endpoint, credential) pair; the
	// button re-runs it on demand.
	useEffect(() => {
		if (!secs || fetching !== null) return;
		const due = SECTIONS.find(({ key }) => {
			const f = secs[key];
			return (
				seedModels(key, f.providerName).length === 0 &&
				modelOpts[key] === undefined &&
				f.baseUrl.trim() &&
				// useCredential belongs here as much as the other two: it is the
				// cheapest path for the user, the key is already on disk, and
				// fetchModels does send credential_provider -- yet leaving it out
				// meant that path never got an automatic fetch at all.
				(f.keySet || f.apiKey || f.reuseKey || f.useCredential) &&
				autoTried[key] !== autoSignature(f)
			);
		});
		if (!due) return;
		const sig = autoSignature(secs[due.key]);
		// A typed key arrives one character at a time. Firing per keystroke sends
		// a request that cannot succeed, and the attempt was being recorded, so
		// the first character spent the whole automatic path. Wait for the field
		// to settle instead; the signature makes a corrected key try again.
		const timer = setTimeout(() => {
			setAutoTried((m) => ({ ...m, [due.key]: sig }));
			void fetchModels(due.key, { silent: true });
		}, AUTO_FETCH_DEBOUNCE_MS);
		return () => clearTimeout(timer);
		// fetchModels/seedModels close over the same state this effect reads.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [secs, modelOpts, fetching, autoTried]);

	/** What this role may borrow instead of a typed key: a sibling role's, or
	 *  the one the Credentials page already holds for the same provider. */
	const borrowFor = (f: SecForm) =>
		f.apiKey
			? {}
			: {
					reuseKeyFrom: f.reuseKey ? ('llm' as EverOSSection) : undefined,
					credentialProvider: f.useCredential ? f.providerName : undefined,
				};

	/** Drop the stored key and nothing else. An empty string is the wire's way
	 *  of saying "remove"; omitting the field means "leave it alone", and
	 *  deleting the section would take the model and base URL with it. */
	const clearKey = async (key: EverOSSection) => {
		if (!secs) return;
		setBusy(key);
		try {
			const r = await ravenConfigApi.everos.set(key, { api_key: '' });
			toast.success(t('everOS.apiKeyCleared'));
			if (r.restart_required) setRestartRequired(true);
			patchSec(key, { apiKey: '', keySet: false, reuseKey: false, useCredential: false });
			setTests((m) => ({ ...m, [key]: undefined }));
		} catch (e) {
			toast.error(`${t('everOS.saveFailed')}: ${e instanceof Error ? e.message : String(e)}`);
		} finally {
			setBusy(null);
		}
	};

	const saveSec = async (key: EverOSSection) => {
		if (!secs) return;
		setBusy(key);
		try {
			const r = await ravenConfigApi.everos.set(
				key,
				toFields(secs[key], key),
				borrowFor(secs[key]),
			);
			toast.success(t('everOS.saved', { role: t(`everOS.sections.${key}.label`) }));
			if (r.restart_required) setRestartRequired(true);
			// All three borrow paths land a key on disk, so all three have to mark
			// it. Leaving useCredential out let a save succeed while the form still
			// believed there was no key: the placeholder kept asking for one and
			// "Clear key" was never offered.
			const stored = secs[key];
			patchSec(key, {
				apiKey: '',
				keySet:
					stored.apiKey || stored.reuseKey || stored.useCredential ? true : stored.keySet,
				reuseKey: false,
			});
			// After patchSec, which clears the mark for any edit.
			setSaved((m) => ({ ...m, [key]: true }));
			setBusy(null);
			// "Saved" only means it is on disk. Probe straight away so the card
			// answers the question the user actually has -- does this work --
			// against the key that was just stored rather than the cleared field.
			await probe(key, { ...toFields(secs[key], key), api_key: SECRET_SET });
		} catch (e) {
			toast.error(`${t('everOS.saveFailed')}: ${e instanceof Error ? e.message : String(e)}`);
		} finally {
			setBusy(null);
		}
	};

	const probe = async (
		key: EverOSSection,
		fields: EverOSModelConfig,
		borrow: { reuseKeyFrom?: EverOSSection; credentialProvider?: string } = {},
	) => {
		setTests((m) => ({ ...m, [key]: 'pending' }));
		try {
			const r = await ravenConfigApi.everos.test(key, fields, borrow);
			setTests((m) => ({ ...m, [key]: { ok: r.ok, detail: r.detail } }));
		} catch (e) {
			setTests((m) => ({
				...m,
				[key]: { ok: false, detail: e instanceof Error ? e.message : String(e) },
			}));
		}
	};

	const testSec = (key: EverOSSection) => {
		if (!secs) return;
		void probe(key, toFields(secs[key], key), borrowFor(secs[key]));
	};

	const clearSec = async (key: EverOSSection) => {
		setBusy(key);
		try {
			const r = await ravenConfigApi.everos.clear(key);
			toast.success(t('everOS.cleared', { role: t(`everOS.sections.${key}.label`) }));
			if (r.restart_required) setRestartRequired(true);
			patchSec(key, { ...EMPTY_SEC });
			setTests((m) => ({ ...m, [key]: undefined }));
			setSaved((m) => ({ ...m, [key]: false }));
			setModelOpts((m) => ({ ...m, [key]: undefined }));
		} catch (e) {
			toast.error(`${t('everOS.saveFailed')}: ${e instanceof Error ? e.message : String(e)}`);
		} finally {
			setBusy(null);
		}
	};

	/** Snap the four knobs to a named level. Picking `custom` only reveals the
	 *  raw inputs; it must not rewrite the values the user already has. */
	const applyLevel = (lv: KnobLevel) => {
		if (!knobs) return;
		if (lv === 'custom') {
			setCustomKnobs(true);
			return;
		}
		setCustomKnobs(false);
		const p = KNOB_PRESETS[lv];
		setKnobs({
			...knobs,
			maxSkillsTopK: String(p.maxSkillsTopK),
			retireConfidence: String(p.retireConfidence),
			minQualityForSkillExtract: String(p.minQualityForSkillExtract),
			complexTaskToolCallThreshold: String(p.complexTaskToolCallThreshold),
		});
	};

	const saveKnobs = async () => {
		if (!knobs) return;
		setBusy('knobs');
		try {
			const r = await ravenConfigApi.skills.set({
				everos: {
					enabled: knobs.enabled,
					maxSkillsTopK: Number(knobs.maxSkillsTopK),
					retireConfidence: Number(knobs.retireConfidence),
					minQualityForSkillExtract: Number(knobs.minQualityForSkillExtract),
					complexTaskToolCallThreshold: Number(knobs.complexTaskToolCallThreshold),
				},
			});
			toast.success(t('everOS.knobsSaved'));
			if (r.restart_required) setRestartRequired(true);
		} catch (e) {
			toast.error(`${t('everOS.saveFailed')}: ${e instanceof Error ? e.message : String(e)}`);
		} finally {
			setBusy(null);
		}
	};

	const restart = async () => {
		setRestarting(true);
		try {
			await ravenConfigApi.restartGateway();
			toast.success(t('everOS.restarting'));
			setRestartRequired(false);
			setTimeout(() => window.location.reload(), 6000);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
			setRestarting(false);
		}
	};

	const missingRequired = secs
		? SECTIONS.filter((s) => s.required && !isReady(secs[s.key])).map((s) => s.key)
		: [];
	const backendOff = backend !== undefined && backend !== 'everos';
	const memoryReady = Boolean(secs) && missingRequired.length === 0 && !backendOff;
	// Values that match no preset are custom on their own; the flag additionally
	// lets the user open the raw inputs while sitting on a preset.
	const knobLevel: KnobLevel = !knobs ? 'balanced' : customKnobs ? 'custom' : matchLevel(knobs);

	if (loading || !secs || !knobs) {
		return (
			<div className="mx-auto flex max-w-5xl items-center gap-2 p-6 text-muted-foreground">
				<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
			</div>
		);
	}

	// Both hosts of this page clip their content (the layout's SidebarInset and
	// the settings hub's right pane are overflow-hidden), so the page owns its
	// scroll container -- same shape the skills page uses.
	return (
		<div className="h-full min-h-0 flex-1 overflow-y-auto">
			<div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
				<div className="flex items-center gap-3">
					<Database className="size-6" />
					<div>
						<div className="em-kicker">{t('everOS.kicker')}</div>
						<h1 className="text-xl font-medium">
							{t('everOS.titleLead')}
							<span className="em-accent">{t('everOS.titleAccent')}</span>
						</h1>
						<p className="text-sm text-muted-foreground">{t('everOS.subtitle')}</p>
					</div>
				</div>

				{restartRequired && (
					<div className="flex items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
						<span className="text-sm">{t('everOS.restartBanner')}</span>
						<Button size="sm" onClick={restart} disabled={restarting}>
							{restarting ? (
								<Loader2 className="size-4 animate-spin" />
							) : (
								<Restart className="size-4" />
							)}{' '}
							{t('everOS.restartNow')}
						</Button>
					</div>
				)}

				{/* Whether memory actually works, up front: the two required roles
				    decide it, and a half-configured page otherwise looks fine. */}
				<Card>
					<CardHeader>
						<CardTitle className="flex items-center gap-2 text-base">
							{memoryReady ? (
								<CheckCircle className="size-5 text-emerald-600" />
							) : (
								<XCircle className="size-5 text-amber-600" />
							)}
							{memoryReady ? t('everOS.statusReady') : t('everOS.statusIncomplete')}
						</CardTitle>
						<CardDescription>
							{memoryReady
								? t('everOS.statusReadyHint')
								: backendOff
									? t('everOS.statusBackendOff', {
											backend: backend || 'markdown',
										})
									: t('everOS.statusIncompleteHint', {
											roles: missingRequired
												.map((k) => t(`everOS.sections.${k}.label`))
												.join(', '),
										})}
						</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-wrap gap-2">
						{SECTIONS.map(({ key, required }) => (
							<Badge
								key={key}
								variant={isReady(secs[key]) ? 'secondary' : 'outline'}
								className={isReady(secs[key]) ? '' : 'text-muted-foreground'}
							>
								{isReady(secs[key]) ? '✓' : '—'} {t(`everOS.sections.${key}.label`)}
								{!required && ` (${t('everOS.optional')})`}
							</Badge>
						))}
					</CardContent>
				</Card>

				{/* Surface A: memory model roles */}
				<div className="flex flex-col gap-4">
					<h2 className="text-sm font-medium text-muted-foreground">
						{t('everOS.modelsTitle')}
					</h2>
					{/* Two-up once there is room: the cards are narrow forms, and one
					    per row left most of a wide window empty. */}
					<div className="grid gap-4 lg:grid-cols-2">
						{SECTIONS.map(({ key, required }) => {
							const f = secs[key];
							const tstate = tests[key];
							const idp = `everos-${key}`;
							const roleProviders = providers.filter((p) => p.supports.includes(key));
							const seeded = seedModels(key, f.providerName);
							// The global per-role suggestion is only honest when it
							// actually belongs to the chosen endpoint; on any other
							// provider it is a model that would fail on first use.
							const rec = seeded.includes(RECOMMENDED[key]) ? RECOMMENDED[key] : '';
							const opts = Array.from(
								new Set(
									[...seeded, f.model, ...(modelOpts[key] ?? [])].filter(Boolean),
								),
							);
							const isCustomEndpoint = f.providerName === '';
							return (
								<Card key={key}>
									<CardHeader>
										<CardTitle className="flex items-center gap-2 text-base">
											{t(`everOS.sections.${key}.label`)}
											<Badge variant={required ? 'secondary' : 'outline'}>
												{required
													? t('everOS.required')
													: t('everOS.optional')}
											</Badge>
										</CardTitle>
										<CardDescription>
											{t(`everOS.sections.${key}.purpose`)}
											{key === 'embedding' && ` ${t('everOS.embeddingHint')}`}
										</CardDescription>
									</CardHeader>
									<CardContent className="flex flex-col gap-3">
										{/* One endpoint and one key usually serve every role, so
									    offer the llm role's as a starting point. */}
										{key !== 'llm' && (secs.llm.keySet || secs.llm.apiKey) && (
											<div className="flex items-center justify-between gap-2 rounded-md border border-dashed p-2">
												<span className="text-xs text-muted-foreground">
													{f.reuseKey
														? t('everOS.reusingLlm')
														: t('everOS.reuseLlmHint')}
												</span>
												<Button
													size="sm"
													variant="outline"
													onClick={() => reuseFromLlm(key)}
													disabled={f.reuseKey}
												>
													{t('everOS.reuseLlm')}
												</Button>
											</div>
										)}

										{/* Provider (pre-fills the base URL) */}
										<div className="grid gap-1.5">
											<Label htmlFor={`${idp}-prov`}>
												{t('everOS.provider')}
											</Label>
											<select
												id={`${idp}-prov`}
												className="h-9 rounded-md border bg-transparent px-3 text-sm"
												value={f.providerName}
												onChange={(e) => pickProvider(key, e.target.value)}
											>
												<option value="">
													{t('everOS.providerCustom')}
												</option>
												{roleProviders.map((p) => (
													<option key={p.name} value={p.name}>
														{p.label}
													</option>
												))}
											</select>
											{!isCustomEndpoint && (
												<p className="text-xs text-muted-foreground">
													{t('everOS.baseUrlAuto')}: {f.baseUrl}
												</p>
											)}
											{f.useCredential && (
												<p className="flex items-center gap-1 text-xs text-emerald-600">
													<CheckCircle className="size-3" />
													{t('everOS.credentialReused')}
												</p>
											)}
										</div>

										{/* Base URL: typed only for a custom endpoint */}
										{isCustomEndpoint && (
											<div className="grid gap-1.5">
												<Label htmlFor={`${idp}-url`}>
													{t('everOS.baseUrlCustom')}
												</Label>
												<Input
													id={`${idp}-url`}
													value={f.baseUrl}
													placeholder="https://api.example.com/v1"
													onChange={(e) =>
														patchSec(key, { baseUrl: e.target.value })
													}
												/>
											</div>
										)}

										{/* API key: the one required typed field */}
										<div className="grid gap-1.5">
											<div className="flex items-center justify-between">
												<Label htmlFor={`${idp}-key`}>
													{t('everOS.apiKey')}
												</Label>
												{/* Blanking the box cannot mean "remove it" -- that
												    is also what "I did not retype it" looks like -- so
												    dropping a key needs its own action. */}
												{f.keySet && !f.apiKey && (
													<Button
														size="sm"
														variant="ghost"
														className="h-6 px-2 text-xs"
														onClick={() => clearKey(key)}
													>
														{t('everOS.apiKeyClear')}
													</Button>
												)}
											</div>
											<Input
												id={`${idp}-key`}
												type="password"
												value={f.apiKey}
												placeholder={
													f.reuseKey
														? t('everOS.apiKeyReusePlaceholder')
														: f.useCredential
															? t(
																	'everOS.apiKeyCredentialPlaceholder',
																)
															: f.keySet
																? t('everOS.apiKeySetPlaceholder')
																: t('everOS.apiKeyPlaceholder')
												}
												onChange={(e) =>
													patchSec(key, { apiKey: e.target.value })
												}
											/>
										</div>

										{/* Rerank protocol: only a manual choice for a custom endpoint */}
										{key === 'rerank' && isCustomEndpoint && (
											<div className="grid gap-1.5">
												<Label htmlFor={`${idp}-proto`}>
													{t('everOS.rerankProtocol')}
												</Label>
												<select
													id={`${idp}-proto`}
													className="h-9 rounded-md border bg-transparent px-3 text-sm"
													value={f.rerankProvider || RERANK_PROTOCOLS[0]}
													onChange={(e) =>
														patchSec(key, {
															rerankProvider: e.target.value,
														})
													}
												>
													{RERANK_PROTOCOLS.map((p) => (
														<option key={p} value={p}>
															{p}
														</option>
													))}
												</select>
											</div>
										)}

										{/* Model: a dropdown; live-fetch fills it, custom is the escape hatch */}
										<div className="grid gap-1.5">
											<div className="flex items-center justify-between">
												<Label htmlFor={`${idp}-model`}>
													{t('everOS.model')}
												</Label>
												<Button
													size="sm"
													variant="ghost"
													className="h-6 px-2 text-xs"
													onClick={() => fetchModels(key)}
													disabled={fetching === key}
												>
													{fetching === key ? (
														<Loader2 className="size-3 animate-spin" />
													) : (
														<Download className="size-3" />
													)}{' '}
													{fetching === key
														? t('everOS.fetching')
														: t('everOS.fetchModels')}
												</Button>
											</div>
											{f.customModel ? (
												<Input
													id={`${idp}-model`}
													value={f.model}
													placeholder={
														rec || seeded[0] || RECOMMENDED[key]
													}
													onChange={(e) =>
														patchSec(key, { model: e.target.value })
													}
												/>
											) : (
												<select
													id={`${idp}-model`}
													className="h-9 rounded-md border bg-transparent px-3 text-sm"
													value={opts.includes(f.model) ? f.model : ''}
													onChange={(e) => {
														if (e.target.value === CUSTOM_MODEL) {
															patchSec(key, { customModel: true });
														} else {
															patchSec(key, {
																model: e.target.value,
															});
														}
													}}
												>
													<option value="" disabled>
														{t('everOS.selectModel')}
													</option>
													{opts.map((m) => (
														<option key={m} value={m}>
															{m === rec && rec
																? `${m}  (${t('everOS.recommended')})`
																: m}
														</option>
													))}
													<option value={CUSTOM_MODEL}>
														{t('everOS.modelCustom')}
													</option>
												</select>
											)}
											{opts.length === 0 && (
												<p className="text-xs text-muted-foreground">
													{fetchFailed[key]
														? t('everOS.modelFetchFailedHint')
														: t('everOS.modelEmptyHint')}
												</p>
											)}
										</div>

										{(saved[key] || tstate) && (
											<div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
												{saved[key] && (
													<span className="flex items-center gap-1 text-emerald-600">
														<CheckCircle className="size-4" />
														{t('everOS.savedBadge')}
													</span>
												)}
												{tstate === 'pending' && (
													<span className="flex items-center gap-1 text-muted-foreground">
														<Loader2 className="size-4 animate-spin" />
														{t('everOS.testing')}
													</span>
												)}
												{tstate && tstate !== 'pending' && (
													<span
														className={`flex items-center gap-1 ${
															tstate.ok
																? 'text-emerald-600'
																: 'text-red-600'
														}`}
													>
														{tstate.ok ? (
															<CheckCircle className="size-4" />
														) : (
															<XCircle className="size-4" />
														)}
														<span className="break-all">
															{tstate.ok
																? t('everOS.testOk')
																: t('everOS.testFail')}
															{tstate.detail
																? `: ${tstate.detail}`
																: ''}
														</span>
													</span>
												)}
											</div>
										)}

										<div className="flex flex-wrap items-center gap-2">
											<Button
												size="sm"
												onClick={() => saveSec(key)}
												disabled={busy === key}
											>
												{busy === key && (
													<Loader2 className="size-4 animate-spin" />
												)}{' '}
												{t('everOS.save')}
											</Button>
											<Button
												size="sm"
												variant="outline"
												onClick={() => testSec(key)}
												disabled={tstate === 'pending'}
											>
												{tstate === 'pending' ? (
													<Loader2 className="size-4 animate-spin" />
												) : (
													<TestTube className="size-4" />
												)}{' '}
												{tstate === 'pending'
													? t('everOS.testing')
													: t('everOS.test')}
											</Button>
											{!required && (
												<Button
													size="sm"
													variant="ghost"
													onClick={() => clearSec(key)}
													disabled={busy === key}
												>
													<Trash2 className="size-4" />{' '}
													{t('everOS.clear')}
												</Button>
											)}
										</div>
									</CardContent>
								</Card>
							);
						})}
					</div>
				</div>

				{/* Surface B: skill-extraction knobs */}
				<Card>
					<CardHeader>
						<CardTitle className="flex items-center gap-2 text-base">
							<Tuning className="size-5" /> {t('everOS.knobsTitle')}
						</CardTitle>
						<CardDescription>{t('everOS.knobsDesc')}</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-col gap-4">
						<div className="flex items-center justify-between gap-3">
							<div>
								<Label>{t('everOS.extractEnabled')}</Label>
								<p className="text-sm text-muted-foreground">
									{t('everOS.extractEnabledDesc')}
								</p>
							</div>
							<Switch
								checked={knobs.enabled}
								onCheckedChange={(v) => setKnobs({ ...knobs, enabled: v })}
							/>
						</div>
						{/* A stance, not four numbers. The raw inputs stay reachable
						    through the custom level for anyone who wants them. */}
						<div className="grid gap-2">
							<Label>{t('everOS.levelLabel')}</Label>
							<div className="grid gap-2 sm:grid-cols-2">
								{KNOB_LEVELS.map((lv) => (
									<button
										key={lv}
										type="button"
										disabled={!knobs.enabled}
										onClick={() => applyLevel(lv)}
										className={`rounded-lg border p-3 text-left transition disabled:opacity-50 ${
											knobLevel === lv
												? 'border-primary bg-primary/5 ring-1 ring-primary'
												: 'hover:bg-muted/60'
										}`}
									>
										<div className="text-sm font-medium">
											{t(`everOS.levels.${lv}.label`)}
										</div>
										<div className="text-xs text-muted-foreground">
											{t(`everOS.levels.${lv}.desc`)}
										</div>
									</button>
								))}
							</div>
						</div>

						{knobLevel === 'custom' && (
							<div className="flex flex-col gap-4">
								<div className="grid gap-1.5">
									<Label htmlFor="knob-topk">{t('everOS.maxSkillsTopK')}</Label>
									<Input
										id="knob-topk"
										type="number"
										value={knobs.maxSkillsTopK}
										onChange={(e) =>
											setKnobs({ ...knobs, maxSkillsTopK: e.target.value })
										}
									/>
									<p className="text-xs text-muted-foreground">
										{t('everOS.maxSkillsTopKDesc')}
									</p>
								</div>
								<div className="grid gap-1.5">
									<Label htmlFor="knob-retire">
										{t('everOS.retireConfidence')}
									</Label>
									<Input
										id="knob-retire"
										type="number"
										step="0.05"
										value={knobs.retireConfidence}
										onChange={(e) =>
											setKnobs({ ...knobs, retireConfidence: e.target.value })
										}
									/>
									<p className="text-xs text-muted-foreground">
										{t('everOS.retireConfidenceDesc')}
									</p>
								</div>
								<div className="grid gap-1.5">
									<Label htmlFor="knob-quality">{t('everOS.minQuality')}</Label>
									<Input
										id="knob-quality"
										type="number"
										step="0.05"
										value={knobs.minQualityForSkillExtract}
										onChange={(e) =>
											setKnobs({
												...knobs,
												minQualityForSkillExtract: e.target.value,
											})
										}
									/>
									<p className="text-xs text-muted-foreground">
										{t('everOS.minQualityDesc')}
									</p>
								</div>
								<div className="grid gap-1.5">
									<Label htmlFor="knob-complex">
										{t('everOS.complexThreshold')}
									</Label>
									<Input
										id="knob-complex"
										type="number"
										value={knobs.complexTaskToolCallThreshold}
										onChange={(e) =>
											setKnobs({
												...knobs,
												complexTaskToolCallThreshold: e.target.value,
											})
										}
									/>
									<p className="text-xs text-muted-foreground">
										{t('everOS.complexThresholdDesc')}
									</p>
								</div>
							</div>
						)}
						<div>
							<Button size="sm" onClick={saveKnobs} disabled={busy === 'knobs'}>
								{busy === 'knobs' && <Loader2 className="size-4 animate-spin" />}{' '}
								{t('everOS.saveKnobs')}
							</Button>
						</div>
					</CardContent>
				</Card>
			</div>
		</div>
	);
}
