import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenHubItem, RavenSkillEntry } from '@/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import IconExternal from '~icons/solar/arrow-right-up-linear';
import IconBook from '~icons/solar/book-2-bold-duotone';
import IconBox from '~icons/solar/box-bold-duotone';
import IconChat from '~icons/solar/chat-round-dots-bold-duotone';
import IconClose from '~icons/solar/close-circle-bold';
import IconCpu from '~icons/solar/cpu-bold-duotone';
import IconDoc from '~icons/solar/documents-bold-duotone';
import Download from '~icons/solar/download-minimalistic-bold-duotone';
import IconFolder from '~icons/solar/folder-open-bold-duotone';
import Install from '~icons/solar/inbox-in-bold-duotone';
import MagicStick from '~icons/solar/magic-stick-3-bold-duotone';
import Magnifer from '~icons/solar/magnifer-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Settings from '~icons/solar/settings-bold-duotone';
import Star from '~icons/solar/star-bold';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';
import IconUsers from '~icons/solar/users-group-rounded-bold-duotone';

/** Public Skill Hub site the gallery catalog is sourced from (mirrors the
 *  backend ``_SKILLHUB_BASE``). Shown as an attribution link so users can browse
 *  the original site. */
const SKILLHUB_SITE = 'https://skillhub.evermind.ai';

const ALL = '__all__';
const INSTALLED = '__installed__';
const HUB = '__hub__';
const PAGE_SIZE = 24;

/** Fixed Skill Hub scenario categories (value = API `category`, label = display). */
const HUB_CATEGORIES: { value: string; label: string }[] = [
	{ value: 'DEV', label: 'Dev' },
	{ value: 'FRONTEND-UI', label: 'Frontend UI' },
	{ value: 'DEVOPS-INFRA', label: 'DevOps & Infra' },
	{ value: 'DATA', label: 'Data' },
	{ value: 'AI-ML', label: 'AI / ML' },
	{ value: 'TESTING', label: 'Testing' },
	{ value: 'SECURITY', label: 'Security' },
	{ value: 'AUTH', label: 'Auth' },
	{ value: 'MULTIMEDIA', label: 'Multimedia' },
	{ value: 'WRITING', label: 'Writing' },
	{ value: 'DOC-PROC', label: 'Doc & Process' },
	{ value: 'COMMS', label: 'Comms' },
	{ value: 'WORKFLOW', label: 'Workflow' },
	{ value: 'PRODUCTIVITY', label: 'Productivity' },
	{ value: 'META', label: 'Meta' },
	{ value: 'OTHER', label: 'Other' },
];

/** Deterministic hash so a skill always gets the same icon/color (no Math.random). */
function hashStr(s: string): number {
	let h = 0;
	for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
	return h;
}

/** Gradient icon tiles, picked by name hash — the vivid square in each card.
 *  White glyph on a saturated gradient reads well in both light and dark. */
const ICON_TILES = [
	{ cls: 'bg-gradient-to-br from-blue-400 to-indigo-600', Icon: IconChat },
	{ cls: 'bg-gradient-to-br from-rose-400 to-pink-600', Icon: IconBook },
	{ cls: 'bg-gradient-to-br from-violet-400 to-purple-600', Icon: IconCpu },
	{ cls: 'bg-gradient-to-br from-emerald-400 to-teal-600', Icon: IconDoc },
	{ cls: 'bg-gradient-to-br from-amber-400 to-orange-600', Icon: IconBox },
	{ cls: 'bg-gradient-to-br from-cyan-400 to-sky-600', Icon: IconFolder },
	{ cls: 'bg-gradient-to-br from-fuchsia-400 to-rose-600', Icon: IconUsers },
] as const;

function tileFor(name: string) {
	return ICON_TILES[hashStr(name) % ICON_TILES.length];
}

/** Soft category badge colors (light + dark), keyed by hub category value. */
const CAT_BADGE: Record<string, string> = {
	DEV: 'bg-blue-500/10 text-blue-600 dark:text-blue-300',
	'FRONTEND-UI': 'bg-pink-500/10 text-pink-600 dark:text-pink-300',
	'DEVOPS-INFRA': 'bg-orange-500/10 text-orange-600 dark:text-orange-300',
	DATA: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-300',
	'AI-ML': 'bg-violet-500/10 text-violet-600 dark:text-violet-300',
	TESTING: 'bg-lime-500/10 text-lime-600 dark:text-lime-300',
	SECURITY: 'bg-red-500/10 text-red-600 dark:text-red-300',
	AUTH: 'bg-amber-500/10 text-amber-600 dark:text-amber-300',
	MULTIMEDIA: 'bg-fuchsia-500/10 text-fuchsia-600 dark:text-fuchsia-300',
	WRITING: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
	'DOC-PROC': 'bg-teal-500/10 text-teal-600 dark:text-teal-300',
	COMMS: 'bg-sky-500/10 text-sky-600 dark:text-sky-300',
	WORKFLOW: 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-300',
	PRODUCTIVITY: 'bg-green-500/10 text-green-600 dark:text-green-300',
	META: 'bg-slate-500/10 text-slate-600 dark:text-slate-300',
	OTHER: 'bg-muted text-muted-foreground',
};

function catBadge(cat?: string): string {
	return (cat && CAT_BADGE[cat]) || 'bg-muted text-muted-foreground';
}

function catLabel(cat?: string): string {
	if (!cat) return '';
	return HUB_CATEGORIES.find((c) => c.value === cat)?.label ?? cat;
}

/** Badge color for a local skill's source (``builtin`` vs ``hub``). Distinct
 *  colors, identical shape/size, so the installed grid stays aligned. The label
 *  itself is the raw source string, so it always reads true to what the skill is. */
function sourceBadge(source: string): string {
	if (source === 'builtin') return 'bg-violet-500/10 text-violet-600 dark:text-violet-300';
	if (source === 'hub') return 'bg-blue-500/10 text-blue-600 dark:text-blue-300';
	return 'bg-muted text-muted-foreground';
}

/** Unified card model built from both local skills and hub search results. */
type CardItem = {
	key: string;
	kind: 'local' | 'hub';
	name: string;
	description: string;
	source: string;
	/** Sidebar bucket: hub ``category`` (DATA/DEV/…) or a local skill's source. */
	category?: string;
	tags: string[];
	rating?: number;
	/** GitHub stars of the source repo (live from skillhub). */
	stars?: number;
	/** Per-skill install count (live from skillhub). */
	installs?: number;
	local?: RavenSkillEntry;
	hub?: RavenHubItem;
};

/** One skill card in the gallery grid. */
function SkillCard({
	item,
	installing,
	removing,
	onView,
	onInstall,
	onRemove,
}: {
	item: CardItem;
	installing: boolean;
	removing: boolean;
	onView: (item: CardItem) => void;
	onInstall: (item: CardItem) => void;
	onRemove: (item: CardItem) => void;
}) {
	const { t } = useTranslation();
	const { cls, Icon } = tileFor(item.name);
	const clickable = true;
	// Installed (downloadable) local skills are removable; builtin ones are not.
	const removable = item.kind === 'local' && item.source !== 'builtin';
	return (
		<div
			role={clickable ? 'button' : undefined}
			tabIndex={clickable ? 0 : undefined}
			onClick={clickable ? () => onView(item) : undefined}
			onKeyDown={
				clickable
					? (e) => {
							if (e.key === 'Enter' || e.key === ' ') onView(item);
						}
					: undefined
			}
			className={cn(
				'group flex flex-col rounded-2xl border bg-card p-5 transition-all duration-200',
				clickable &&
					'cursor-pointer hover:-translate-y-1 hover:border-foreground/15 hover:shadow-lg hover:shadow-black/[0.06] dark:hover:shadow-black/40',
			)}
		>
			<div className="flex items-start justify-between">
				<div
					className={cn(
						'flex size-11 items-center justify-center rounded-xl text-white shadow-sm ring-1 ring-black/5 transition-transform duration-200 group-hover:scale-105',
						cls,
					)}
				>
					<Icon className="size-5" />
				</div>
				{item.kind === 'local' ? (
					// Installed grid: label the card by what it actually is — a
					// bundled ``builtin`` skill or one downloaded from the ``hub``.
					<span
						className={cn(
							'rounded-full px-2.5 py-0.5 text-[11px] font-medium',
							sourceBadge(item.source),
						)}
					>
						{item.source}
					</span>
				) : item.category ? (
					<span
						className={cn(
							'rounded-full px-2.5 py-0.5 text-[11px] font-medium',
							catBadge(item.category),
						)}
					>
						{catLabel(item.category)}
					</span>
				) : (
					<span className="rounded-md bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
						{item.source}
					</span>
				)}
			</div>

			<h3 className="mt-4 truncate font-semibold" title={item.name}>
				{item.name}
			</h3>
			<p className="mt-2 line-clamp-3 min-h-[3.75rem] text-sm text-muted-foreground">
				{item.description}
			</p>

			<div className="mt-3 flex flex-wrap gap-1.5">
				{item.tags.slice(0, 2).map((tg) => (
					<span
						key={tg}
						className="max-w-[10rem] truncate rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground"
					>
						{tg}
					</span>
				))}
			</div>

			<div className="mt-4 flex items-center justify-between">
				<div className="flex items-center gap-3 text-sm text-muted-foreground">
					{item.stars != null && (
						<span className="flex items-center gap-1" title="GitHub stars">
							<Star className="size-4 text-amber-400" />
							{item.stars.toLocaleString()}
						</span>
					)}
					{item.installs != null && (
						<span className="flex items-center gap-1" title="Installs">
							<Download className="size-4" />
							{item.installs.toLocaleString()}
						</span>
					)}
				</div>
				{item.kind === 'hub' && (
					<Button
						size="sm"
						disabled={installing}
						title={t('ravenSkills.installHint')}
						onClick={(e) => {
							e.stopPropagation();
							onInstall(item);
						}}
					>
						{installing ? (
							<Loader2 className="size-4 animate-spin" />
						) : (
							<Install className="size-4" />
						)}
						{t('ravenSkills.install')}
					</Button>
				)}
				{removable && (
					<Button
						size="sm"
						variant="ghost"
						disabled={removing}
						title={t('ravenSkills.uninstallHint')}
						className="text-muted-foreground hover:text-destructive"
						onClick={(e) => {
							e.stopPropagation();
							onRemove(item);
						}}
					>
						{removing ? (
							<Loader2 className="size-4 animate-spin" />
						) : (
							<Trash2 className="size-4" />
						)}
						{t('ravenSkills.uninstall')}
					</Button>
				)}
			</div>
		</div>
	);
}

/** Configure Raven's SkillForge (skill router weights, hub, EverOS, local dirs) (P4). */
export function RavenSkillsPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const [skills, setSkills] = useState<RavenSkillEntry[]>([]);
	const [loading, setLoading] = useState(true);
	// Two-level nav: left sidebar picks the source (builtin / hub / installed);
	// the hub category bar (top) further filters the hub catalog.
	const [activeSource, setActiveSource] = useState<string>(HUB);
	const [hubCategory, setHubCategory] = useState<string>(ALL);

	// The gallery only browses/installs; SkillForge config now lives in the
	// Settings hub (Settings > Skill retrieval), reachable from the button below.
	const load = async () => {
		setLoading(true);
		try {
			const { skills: available } = await ravenConfigApi.skills.listAvailable();
			setSkills(available ?? []);
		} catch (e) {
			toast.error(
				`${t('ravenSkills.loadFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setLoading(false);
		}
	};

	useEffect(() => {
		void load();
	}, []);

	// ── Skill Hub: paginated full-catalog browse + install ──
	const [query, setQuery] = useState('');
	const [submitted, setSubmitted] = useState('');
	const [items, setItems] = useState<RavenHubItem[]>([]);
	const [total, setTotal] = useState<number | null>(null);
	const [page, setPage] = useState(1);
	const [browsing, setBrowsing] = useState(true);
	const [loadingMore, setLoadingMore] = useState(false);
	const [installing, setInstalling] = useState<string | null>(null);
	const [removing, setRemoving] = useState<string | null>(null);

	// Fetch one catalog page (server-side category + free-text filtered).
	const fetchPage = async (pageNum: number, append: boolean) => {
		try {
			const res = await ravenConfigApi.skills.hub.browse(pageNum, {
				limit: PAGE_SIZE,
				category: hubCategory === ALL ? undefined : hubCategory,
				q: submitted || undefined,
			});
			setTotal(res.total ?? null);
			setItems((prev) => (append ? [...prev, ...(res.items ?? [])] : (res.items ?? [])));
			setPage(pageNum);
		} catch (e) {
			toast.error(
				`${t('ravenSkills.hubSearchFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		}
	};

	// Reload from page 1 whenever the category or the submitted query changes.
	// The "Installed" view is served from the local skills list, not the hub.
	useEffect(() => {
		if (activeSource !== HUB) {
			setBrowsing(false);
			return;
		}
		let cancelled = false;
		setBrowsing(true);
		void (async () => {
			await fetchPage(1, false);
			if (!cancelled) setBrowsing(false);
		})();
		return () => {
			cancelled = true;
		};
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [activeSource, hubCategory, submitted]);

	const loadMore = async () => {
		setLoadingMore(true);
		await fetchPage(page + 1, true);
		setLoadingMore(false);
	};
	const sentinelRef = useRef<HTMLDivElement | null>(null);

	const installHub = async (item: CardItem) => {
		const h = item.hub;
		if (!h) return;
		const id = h.id || h.skill_id || h.slug || h.name;
		setInstalling(id);
		try {
			await ravenConfigApi.skills.hub.install(id);
			toast.success(t('ravenSkills.installOk'));
			void load(); // refresh the local skills so it appears under "Installed"
		} catch (e) {
			toast.error(
				`${t('ravenSkills.installFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setInstalling(null);
		}
	};

	// Uninstall a downloaded skill (backend deletes its folder under the hub cache;
	// builtin / local-dir skills are refused server-side).
	const removeSkill = async (item: CardItem) => {
		const s = item.local;
		if (!s) return;
		if (!window.confirm(t('ravenSkills.uninstallConfirm', { name: s.name }))) return;
		const key = `${s.source}:${s.name}`;
		setRemoving(key);
		try {
			await ravenConfigApi.skills.remove({ name: s.name, id: s.id, source: s.source });
			toast.success(t('ravenSkills.uninstallOk'));
			void load(); // refresh so the removed skill disappears from "Installed"
		} catch (e) {
			toast.error(
				`${t('ravenSkills.uninstallFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setRemoving(null);
		}
	};

	// Navigate to the full-page skill detail (hub → by id; local → by name+source).
	const openDetail = (item: CardItem) => {
		const params = new URLSearchParams();
		if (item.hub) {
			params.set('id', item.hub.id || item.hub.skill_id || item.hub.slug || item.hub.name);
			params.set('kind', 'hub');
		} else if (item.local) {
			params.set('kind', 'local');
			if (item.local.id) params.set('id', item.local.id);
			params.set('name', item.local.name);
			params.set('source', item.local.source);
		}
		navigate(`/raven-skills/detail?${params.toString()}`);
	};

	// Map the hub catalog page into gallery cards.
	const cards = useMemo<CardItem[]>(
		() =>
			items.map((h) => {
				const tags = h.tags ?? h.scenario_tags ?? [];
				const quality = h.quality_score ?? h.score_safety;
				return {
					key: `hub:${h.id || h.skill_id || h.slug || h.name}`,
					kind: 'hub' as const,
					name: h.name,
					description: h.description || '',
					source: h.source || 'hub',
					category: h.category || undefined,
					tags,
					rating: quality != null ? quality * 5 : undefined,
					stars: h.github_star,
					installs: h.install_count,
					hub: h,
				};
			}),
		[items],
	);
	const hasMore = total != null && items.length < total;

	// Local / already-installed skills (builtin like weather + downloaded hub skills),
	// shown under the "Installed" sidebar entry. Clicking a card opens its SKILL.md.
	const localCards = useMemo<CardItem[]>(
		() =>
			skills.map((s) => ({
				key: `local:${s.source}:${s.name}`,
				kind: 'local' as const,
				name: s.name,
				description: s.description || '',
				source: s.source,
				category: s.source,
				tags: [],
				local: s,
			})),
		[skills],
	);
	// Installed view holds every local skill, with Raven's bundled ``builtin``
	// skills ordered before the ones downloaded from the hub (or added via a
	// local dir). Builtin no longer has its own hub chip — it lives here.
	const installedCards = useMemo(() => {
		const builtin = localCards.filter((c) => c.source === 'builtin');
		const downloaded = localCards.filter((c) => c.source !== 'builtin');
		return [...builtin, ...downloaded];
	}, [localCards]);
	const isLocalView = activeSource !== HUB;
	const displayCards = activeSource === INSTALLED ? installedCards : cards;

	// Infinite scroll: auto-load the next hub page when the bottom sentinel nears
	// the viewport (rootMargin prefetches before the user hits the very bottom).
	// Only the paginated hub catalog uses this; local lists load all at once.
	useEffect(() => {
		if (isLocalView || !hasMore) return;
		const el = sentinelRef.current;
		if (!el) return;
		const io = new IntersectionObserver(
			(entries) => {
				if (entries[0]?.isIntersecting && hasMore && !loadingMore && !browsing) {
					void loadMore();
				}
			},
			{ rootMargin: '600px 0px' },
		);
		io.observe(el);
		return () => io.disconnect();
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [isLocalView, hasMore, loadingMore, browsing, page, submitted, hubCategory]);

	if (loading) {
		return (
			<div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
				<div className="flex items-center gap-2 text-muted-foreground">
					<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
				</div>
			</div>
		);
	}

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-7xl flex-col gap-5 p-6">
				{/* Hero header: gradient band + icon badge + title/intro + search/settings */}
				<div className="relative overflow-hidden rounded-2xl border bg-gradient-to-br from-violet-500/10 via-blue-500/[0.06] to-transparent p-6 dark:from-violet-500/15 dark:via-blue-500/10">
					{/* soft decorative glow */}
					<div className="pointer-events-none absolute -top-16 -right-16 size-48 rounded-full bg-gradient-to-br from-violet-500/20 to-blue-500/10 blur-3xl" />
					<div className="relative flex flex-col gap-5">
						{/* Row 1: brand + intro, with count + settings on the right */}
						<div className="flex flex-wrap items-start justify-between gap-4">
							<div className="flex items-start gap-3.5">
								<div className="flex size-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 to-blue-600 text-white shadow-md ring-1 ring-black/5">
									<MagicStick className="size-6" />
								</div>
								<div>
									<div className="em-kicker">{t('ravenSkills.kicker')}</div>
									<h1 className="text-2xl font-semibold tracking-tight">
										<span className="em-accent">{t('ravenSkills.title')}</span>
									</h1>
									<p className="mt-1.5 max-w-2xl text-sm text-muted-foreground">
										{t('ravenSkills.pageIntro')}
									</p>
								</div>
							</div>
							<div className="flex items-center gap-4">
								{activeSource === HUB && total != null && (
									<div className="text-right leading-none">
										<span className="bg-gradient-to-br from-violet-500 to-blue-600 bg-clip-text text-2xl font-bold text-transparent">
											{total.toLocaleString()}
										</span>
										<span className="ml-1.5 text-sm text-muted-foreground">
											skills
										</span>
									</div>
								)}
								<Button
									variant="outline"
									size="sm"
									onClick={() => navigate('/settings?tab=skills')}
								>
									<Settings className="size-4" /> {t('ravenSkills.settings')}
								</Button>
							</div>
						</div>

						{/* Row 2: prominent catalog search (hub view only) */}
						{activeSource === HUB && (
							<div className="relative w-full max-w-2xl">
								<Magnifer className="pointer-events-none absolute top-1/2 left-4 size-5 -translate-y-1/2 text-muted-foreground" />
								<Input
									value={query}
									placeholder={t('ravenSkills.searchPlaceholder')}
									onChange={(e) => setQuery(e.target.value)}
									onKeyDown={(e) => {
										if (e.key === 'Enter') setSubmitted(query.trim());
										if (e.key === 'Escape') {
											setQuery('');
											setSubmitted('');
										}
									}}
									className="h-12 rounded-full border-transparent bg-background/80 pr-28 pl-12 text-base shadow-sm ring-1 ring-border backdrop-blur transition focus-visible:ring-2 focus-visible:ring-violet-500/40"
								/>
								{/* clear button — only when there's text */}
								{query && (
									<button
										type="button"
										aria-label={t('ravenSkills.searchClear')}
										onClick={() => {
											setQuery('');
											setSubmitted('');
										}}
										className="absolute top-1/2 right-[5.5rem] -translate-y-1/2 text-muted-foreground transition hover:text-foreground"
									>
										<IconClose className="size-5" />
									</button>
								)}
								{/* search button */}
								<Button
									size="sm"
									onClick={() => setSubmitted(query.trim())}
									className="absolute top-1/2 right-1.5 h-9 -translate-y-1/2 gap-1.5 rounded-full px-4"
								>
									{browsing ? (
										<Loader2 className="size-4 animate-spin" />
									) : (
										<Magnifer className="size-4" />
									)}
									{t('ravenSkills.searchButton')}
								</Button>
							</div>
						)}

						{/* Attribution: link out to the original Skill Hub site */}
						{activeSource === HUB && (
							<a
								href={SKILLHUB_SITE}
								target="_blank"
								rel="noreferrer noopener"
								className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground transition hover:text-foreground"
							>
								<span>{t('ravenSkills.hubSource')}</span>
								<span className="text-muted-foreground/40">·</span>
								<span className="inline-flex items-center gap-0.5 font-medium text-violet-600 dark:text-violet-300">
									{t('ravenSkills.hubVisit')}
									<IconExternal className="size-3.5" />
								</span>
							</a>
						)}
					</div>
				</div>

				{/* Body: category sidebar + card grid */}
				<div className="flex gap-6">
					<aside className="hidden w-48 shrink-0 sm:block">
						<div className="em-kicker mb-2 px-3">{t('ravenSkills.sourceGroup')}</div>
						<nav className="flex flex-col gap-1">
							{[
								{
									value: HUB,
									label: t('ravenSkills.hub'),
									Icon: MagicStick,
									count: null,
									activeWhen: activeSource === HUB,
								},
								{
									value: INSTALLED,
									label: t('ravenSkills.installed'),
									Icon: Download,
									count: installedCards.length,
									activeWhen: activeSource === INSTALLED,
								},
							].map((s) => {
								const active = s.activeWhen;
								return (
									<button
										key={s.value}
										onClick={() => setActiveSource(s.value)}
										className={cn(
											'flex items-center gap-2.5 rounded-xl px-3 py-2 text-left text-sm transition-all',
											active
												? 'bg-gradient-to-r from-violet-500/15 to-blue-500/10 font-semibold text-foreground shadow-sm ring-1 ring-inset ring-foreground/5'
												: 'text-muted-foreground hover:bg-muted/60',
										)}
									>
										<s.Icon
											className={cn(
												'size-4',
												active
													? 'text-violet-500 dark:text-violet-300'
													: '',
											)}
										/>
										<span className="flex-1">{s.label}</span>
										{s.count != null && (
											<span
												className={cn(
													'rounded-full px-1.5 py-0.5 text-[10px] tabular-nums',
													active
														? 'bg-background/70 text-foreground'
														: 'bg-muted text-muted-foreground',
												)}
											>
												{s.count}
											</span>
										)}
									</button>
								);
							})}
						</nav>
					</aside>

					<div className="min-w-0 flex-1">
						{/* Top category bar — hub catalog scenarios (All + categories).
						    Builtin is no longer a chip here; it lives in the Installed view. */}
						{activeSource === HUB && (
							<div className="mb-4 flex flex-wrap gap-2 border-b pb-3">
								{[
									{
										key: ALL,
										label: t('ravenSkills.categoryAll'),
										active: activeSource === HUB && hubCategory === ALL,
										on: () => {
											setActiveSource(HUB);
											setHubCategory(ALL);
										},
									},
									...HUB_CATEGORIES.map((c) => ({
										key: c.value,
										label: c.label,
										active: activeSource === HUB && hubCategory === c.value,
										on: () => {
											setActiveSource(HUB);
											setHubCategory(c.value);
										},
									})),
								].map((c) => (
									<button
										key={c.key}
										onClick={c.on}
										className={cn(
											'rounded-full px-3 py-1 text-xs font-medium transition-all',
											c.active
												? 'bg-gradient-to-r from-violet-500 to-blue-600 text-white shadow-sm'
												: 'bg-muted text-muted-foreground hover:bg-muted/70 hover:text-foreground',
										)}
									>
										{c.label}
									</button>
								))}
							</div>
						)}
						<div className="mb-4">
							<p className="text-sm text-muted-foreground">
								{activeSource === INSTALLED
									? t('ravenSkills.introInstalled')
									: t('ravenSkills.introHub')}
							</p>
							<div className="mt-1 flex items-center gap-2">
								<p className="text-xs text-muted-foreground/70">
									{t('ravenSkills.galleryCount', {
										count: isLocalView
											? displayCards.length
											: (total ?? cards.length),
									})}
								</p>
								{!isLocalView && submitted && (
									<button
										type="button"
										onClick={() => {
											setQuery('');
											setSubmitted('');
										}}
										className="inline-flex items-center gap-1 rounded-full bg-violet-500/10 py-0.5 pr-2 pl-2.5 text-xs font-medium text-violet-600 transition hover:bg-violet-500/20 dark:text-violet-300"
										title={t('ravenSkills.searchClear')}
									>
										{t('ravenSkills.searchActive', { q: submitted })}
										<IconClose className="size-3.5" />
									</button>
								)}
							</div>
						</div>
						{browsing && !isLocalView && cards.length === 0 ? (
							<div className="flex items-center justify-center gap-2 p-10 text-sm text-muted-foreground">
								<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
							</div>
						) : displayCards.length === 0 ? (
							<div className="rounded-2xl border border-dashed p-10 text-center text-sm text-muted-foreground">
								{t('ravenSkills.skillsTableEmpty')}
							</div>
						) : (
							<>
								<div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
									{displayCards.map((item) => (
										<SkillCard
											key={item.key}
											item={item}
											installing={
												item.kind === 'hub' &&
												installing ===
													(item.hub?.id ||
														item.hub?.skill_id ||
														item.hub?.slug ||
														item.hub?.name)
											}
											removing={
												item.kind === 'local' &&
												removing === `${item.source}:${item.name}`
											}
											onView={openDetail}
											onInstall={installHub}
											onRemove={removeSkill}
										/>
									))}
								</div>
								{/* Infinite-scroll sentinel: the observer above auto-loads the next
								    page as this nears the viewport — no click needed. */}
								{hasMore && !isLocalView && (
									<div
										ref={sentinelRef}
										className="mt-6 flex h-10 items-center justify-center gap-2 text-sm text-muted-foreground"
									>
										{loadingMore && (
											<>
												<Loader2 className="size-4 animate-spin" />{' '}
												{t('common.loading')}
											</>
										)}
									</div>
								)}
							</>
						)}
					</div>
				</div>
			</div>
		</div>
	);
}
