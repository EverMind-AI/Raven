import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenToolCredential } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import IconExternal from '~icons/solar/arrow-right-up-linear';
import IconImage from '~icons/solar/gallery-bold-duotone';
import IconSearch from '~icons/solar/magnifer-bold-duotone';
import IconSpeech from '~icons/solar/play-circle-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import IconTools from '~icons/solar/tuning-2-bold-duotone';
import IconVideo from '~icons/solar/videocamera-record-bold-duotone';

/** The redaction markers the config layer reads secrets back as. */
const KEY_SET = '****set****';

const META: Record<string, { Icon: typeof IconSearch; tile: string; docs?: string }> = {
	web_search: {
		Icon: IconSearch,
		tile: 'from-emerald-500 to-teal-600',
		docs: 'https://serper.dev',
	},
	image: { Icon: IconImage, tile: 'from-violet-500 to-purple-600' },
	speech: { Icon: IconSpeech, tile: 'from-amber-500 to-orange-600' },
	video: { Icon: IconVideo, tile: 'from-rose-500 to-pink-600' },
};

/** Whether a stored secret exists, from the marker alone — the key itself never
 *  reaches the browser, so this is all there is to read. */
function keyIsSet(row: RavenToolCredential): boolean {
	return row.apiKey === KEY_SET;
}

/** A media tool counts as configured once its model or its own key is set; that
 *  is the same rule AgentLoop applies when it decides to register the tool. */
function isConfigured(row: RavenToolCredential): boolean {
	return row.kind === 'web_search' ? keyIsSet(row) : (row.configured ?? false);
}

function StatusPill({ row }: { row: RavenToolCredential }) {
	const { t } = useTranslation();
	if (row.registered) {
		return (
			<Badge variant="outline" className="border-emerald-500/40 text-emerald-600 dark:text-emerald-400">
				{t('ravenTools.statusLive')}
			</Badge>
		);
	}
	if (isConfigured(row)) {
		// Saved, but this process was built without it. Distinct from "off": the
		// user has done their part and the only thing left is the restart.
		return (
			<Badge variant="outline" className="border-amber-500/40 text-amber-600 dark:text-amber-400">
				{t('ravenTools.statusPending')}
			</Badge>
		);
	}
	return <Badge variant="outline" className="text-muted-foreground">{t('ravenTools.statusOff')}</Badge>;
}

/** One line saying which key the tool will actually use, and where it came from.
 *  The resolution order is the tool's own (`own` → openrouter → env), so a user
 *  who left the field blank still learns whether the call can succeed. */
function KeySourceNote({ row }: { row: RavenToolCredential }) {
	const { t } = useTranslation();
	if (row.keySource === 'own') return null;
	const tone =
		row.keySource === 'none' ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground';
	const text =
		row.keySource === 'env'
			? t('ravenTools.keyFromEnv', { env: row.envKey })
			: row.keySource === 'openrouter'
				? t('ravenTools.keyFromOpenrouter')
				: row.kind === 'web_search'
					? t('ravenTools.keyMissingSearch', { env: row.envKey })
					: t('ravenTools.keyMissingMedia', { env: row.envKey });
	return <p className={cn('text-sm', tone)}>{text}</p>;
}

/** Blanking the box cannot mean "remove it" — that is also what "I did not
 *  retype it" looks like — so dropping a key needs its own action. */
function ClearKeyButton({ onClear }: { onClear: () => void }) {
	const { t } = useTranslation();
	return (
		<Button size="sm" variant="ghost" className="h-7 px-2 text-sm" onClick={onClear}>
			{t('ravenTools.clearKey')}
		</Button>
	);
}

function ToolCard({
	row,
	onSaved,
	onRestartNeeded,
}: {
	row: RavenToolCredential;
	onSaved: () => void;
	onRestartNeeded: () => void;
}) {
	const { t } = useTranslation();
	const [apiKey, setApiKey] = useState('');
	const [model, setModel] = useState(row.model ?? '');
	const [apiBase, setApiBase] = useState(row.apiBase ?? '');
	const [maxResults, setMaxResults] = useState(String(row.maxResults ?? ''));
	const [showAdvanced, setShowAdvanced] = useState(false);
	const [saving, setSaving] = useState(false);

	const meta = META[row.kind] ?? { Icon: IconTools, tile: 'from-slate-500 to-slate-600' };
	const { Icon } = meta;
	const isSearch = row.kind === 'web_search';
	const label = t(`ravenTools.names.${row.kind}`);

	const commit = async (fields: Record<string, string | number>, note: string) => {
		setSaving(true);
		try {
			const r = await ravenConfigApi.setTool(row.kind, fields);
			if (r?.restart_required) onRestartNeeded();
			toast.success(note);
			setApiKey('');
			onSaved();
		} catch (e) {
			toast.error(
				t('ravenTools.saveFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setSaving(false);
		}
	};

	const save = () => {
		const fields: Record<string, string | number> = {};
		// Never resend the redaction marker: an untouched box means "leave the
		// stored key alone", which is not the same as clearing it.
		if (apiKey !== '') fields.api_key = apiKey;
		if (isSearch) {
			if (maxResults !== String(row.maxResults ?? '')) {
				// Sent as a number: the schema types it `int`, and a string would be
				// rejected by the writer rather than coerced.
				const n = Number(maxResults);
				if (!Number.isInteger(n) || n < 1) {
					toast.error(t('ravenTools.maxResultsInvalid'));
					return;
				}
				fields.max_results = n;
			}
		} else {
			if (model !== (row.model ?? '')) fields.model = model;
			if (apiBase !== (row.apiBase ?? '')) fields.api_base = apiBase;
		}
		if (Object.keys(fields).length === 0) {
			toast.info(t('ravenTools.nothingToSave'));
			return;
		}
		void commit(fields, t('ravenTools.saved', { name: label }));
	};

	const enableWithDefault = () =>
		void commit({ model: row.defaultModel ?? '' }, t('ravenTools.enabled', { name: label }));

	const disable = () =>
		void commit(
			isSearch ? { api_key: '' } : { model: '', api_key: '' },
			t('ravenTools.disabled', { name: label }),
		);

	// `h-full` + the `mt-auto` footer below: grid cells stretch to the tallest
	// card in the row, and without it the save button floats mid-card on the
	// shorter one (web_search has no model field, so the pair never matches).
	return (
		<div className="flex h-full flex-col rounded-2xl border bg-card p-6">
			<div className="flex items-start gap-3">
				<div
					className={cn(
						'flex size-12 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br text-white shadow-sm ring-1 ring-black/5',
						meta.tile,
					)}
				>
					<Icon className="size-6" />
				</div>
				<div className="min-w-0 flex-1">
					<div className="flex flex-wrap items-center gap-2">
						<h2 className="text-lg font-medium">{label}</h2>
						<StatusPill row={row} />
						<code className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
							{row.tool}
						</code>
					</div>
					<p className="mt-1 text-[15px] text-muted-foreground">
						{t(`ravenTools.blurbs.${row.kind}`)}
					</p>
				</div>
				{isConfigured(row) && (
					<Button
						size="sm"
						variant="ghost"
						className="shrink-0"
						disabled={saving}
						onClick={disable}
					>
						{t('ravenTools.disable')}
					</Button>
				)}
			</div>

			<div className="mt-5 grid gap-4">
				{!isSearch && (
					<div className="grid gap-2">
						<div className="flex items-center justify-between">
							<Label htmlFor={`${row.kind}-model`} className="text-[15px]">
								{t('ravenTools.model')}
							</Label>
							{!isConfigured(row) && (
								<Button
									size="sm"
									variant="ghost"
									className="h-7 px-2 text-sm"
									disabled={saving}
									onClick={enableWithDefault}
								>
									{t('ravenTools.useDefaultModel')}
								</Button>
							)}
						</div>
						<Input
							id={`${row.kind}-model`}
							className="h-10 text-[15px]"
							value={model}
							placeholder={row.defaultModel}
							onChange={(e) => setModel(e.target.value)}
						/>
						{/* Setting a model is what switches a media tool on, so say so
						    rather than leaving an empty box looking optional -- but only
						    while it is off. On a working tool the sentence is three lines
						    of advice about a decision already taken. */}
						{!isConfigured(row) && (
							<p className="text-sm text-muted-foreground">{t('ravenTools.modelHint')}</p>
						)}
					</div>
				)}

				<div className="grid gap-2">
					<div className="flex items-center justify-between">
						<Label htmlFor={`${row.kind}-key`} className="text-[15px]">
							{t('ravenTools.apiKey')}
						</Label>
						{keyIsSet(row) && apiKey === '' && (
							<ClearKeyButton onClear={() => void commit({ api_key: '' }, t('ravenTools.keyCleared'))} />
						)}
					</div>
					<Input
						id={`${row.kind}-key`}
						className="h-10 text-[15px]"
						type="password"
						value={apiKey}
						placeholder={
							keyIsSet(row) ? t('ravenTools.keySetPlaceholder') : t('ravenTools.keyPlaceholder')
						}
						onChange={(e) => setApiKey(e.target.value)}
					/>
					<KeySourceNote row={row} />
					<p className="text-[13px] text-muted-foreground/70">
						<code>{row.settingPath}</code> · <code>{row.envKey}</code>
						{meta.docs && (
							<>
								{' · '}
								<a
									className="inline-flex items-center gap-0.5 underline underline-offset-2 hover:text-foreground"
									href={meta.docs}
									target="_blank"
									rel="noreferrer"
								>
									{new URL(meta.docs).host}
									<IconExternal className="size-3" />
								</a>
							</>
						)}
					</p>
				</div>

				{isSearch && (
					<div className="grid gap-2">
						<Label htmlFor={`${row.kind}-max`} className="text-[15px]">
							{t('ravenTools.maxResults')}
						</Label>
						<Input
							id={`${row.kind}-max`}
							className="h-10 w-28 text-[15px]"
							type="number"
							min={1}
							value={maxResults}
							onChange={(e) => setMaxResults(e.target.value)}
						/>
					</div>
				)}

				{showAdvanced && !isSearch && (
					<div className="grid gap-2">
						<Label htmlFor={`${row.kind}-base`} className="text-[15px]">
							{t('ravenTools.apiBase')}
						</Label>
						<Input
							id={`${row.kind}-base`}
							className="h-10 text-[15px]"
							value={apiBase}
							placeholder={row.defaultApiBase}
							onChange={(e) => setApiBase(e.target.value)}
						/>
					</div>
				)}
			</div>

			<div className="mt-auto flex items-center justify-end gap-2 pt-3">
				{!isSearch && (
					<button
						type="button"
						className="mr-auto text-sm text-muted-foreground hover:text-foreground"
						onClick={() => setShowAdvanced((v) => !v)}
					>
						{showAdvanced ? t('ravenTools.hideAdvanced') : t('ravenTools.showAdvanced')}
					</button>
				)}
				<Button disabled={saving} onClick={save}>
					{saving && <Loader2 className="size-4 animate-spin" />} {t('common.save')}
				</Button>
			</div>
		</div>
	);
}

/** Keys and models for the tools that only exist once they have one. */
export function ToolsPage() {
	const { t } = useTranslation();
	const [tools, setTools] = useState<RavenToolCredential[]>([]);
	const [loading, setLoading] = useState(true);
	const [restartNeeded, setRestartNeeded] = useState(false);
	const [restarting, setRestarting] = useState(false);

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const r = await ravenConfigApi.listTools();
			setTools(r.tools ?? []);
		} catch (e) {
			toast.error(
				t('ravenTools.loadFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setLoading(false);
		}
	}, [t]);

	useEffect(() => {
		void load();
	}, [load]);

	const restartGateway = async () => {
		setRestarting(true);
		try {
			await ravenConfigApi.restartGateway();
		} catch {
			// The gateway drops the connection as it re-execs, so a transport error
			// here is expected — treat it as "restart in progress", not a failure.
		}
		toast.success(t('ravenTools.restarting'));
		// Poll until it answers again rather than reloading on a fixed timer, or a
		// slow boot drops the page onto a backend that is still down.
		const deadline = Date.now() + 60000;
		const waitForGateway = async () => {
			while (Date.now() < deadline) {
				await new Promise((r) => setTimeout(r, 1500));
				try {
					await ravenConfigApi.listTools({ silent: true });
					break;
				} catch {
					// still restarting
				}
			}
			window.location.reload();
		};
		void waitForGateway();
	};

	const liveCount = tools.filter((row) => row.registered).length;

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-7xl flex-col gap-5 p-6">
				<div className="relative overflow-hidden rounded-2xl border bg-gradient-to-br from-emerald-500/10 via-teal-500/[0.06] to-transparent p-6 dark:from-emerald-500/15 dark:via-teal-500/10">
					<div className="pointer-events-none absolute -top-16 -right-16 size-48 rounded-full bg-gradient-to-br from-emerald-500/20 to-teal-500/10 blur-3xl" />
					<div className="relative flex items-start gap-3.5">
						<div className="flex size-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-emerald-500 to-teal-600 text-white shadow-md ring-1 ring-black/5">
							<IconTools className="size-6" />
						</div>
						<div>
							<div className="em-kicker">{t('ravenTools.kicker')}</div>
							<h1 className="text-2xl font-semibold tracking-tight">
								<span className="em-accent">{t('ravenTools.title')}</span>
							</h1>
							<p className="mt-1 max-w-xl text-sm text-muted-foreground">
								{t('ravenTools.subtitle')}
							</p>
							{!loading && (
								<p className="mt-1.5 text-xs text-muted-foreground/70">
									{t('ravenTools.summary', { live: liveCount, total: tools.length })}
								</p>
							)}
						</div>
					</div>
				</div>

				{restartNeeded && (
					<div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-700 dark:text-amber-300">
						<span>{t('ravenTools.restartBanner')}</span>
						<Button
							size="sm"
							variant="outline"
							disabled={restarting}
							onClick={restartGateway}
							className="border-amber-500/50 text-amber-700 hover:bg-amber-500/10 dark:text-amber-300"
						>
							{restarting && <Loader2 className="size-4 animate-spin" />}{' '}
							{t('ravenTools.restartNow')}
						</Button>
					</div>
				)}

				{loading ? (
					<div className="flex items-center gap-2 text-muted-foreground">
						<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
					</div>
				) : (
					// One column below `lg`, not `md`: measured at 800px wide, two
					// columns leave each card 342px and every model id and config
					// path wraps. The pair needs ~1024px before it reads as a grid.
					<div className="grid gap-5 lg:grid-cols-2">
						{tools.map((row) => (
							<ToolCard
								key={row.kind}
								row={row}
								onSaved={load}
								onRestartNeeded={() => setRestartNeeded(true)}
							/>
						))}
					</div>
				)}
			</div>
		</div>
	);
}
