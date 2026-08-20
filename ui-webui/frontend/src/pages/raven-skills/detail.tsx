import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useNavigate, useSearchParams } from 'react-router-dom';
import remarkGfm from 'remark-gfm';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenHubItem } from '@/api';
import { getBaseUrl } from '@/api/client';
import { Button } from '@/components/ui/button';
import { useTranslation } from '@/i18n/useI18n';
import AddToWorkspace from '~icons/solar/add-square-bold-duotone';
import ArrowLeft from '~icons/solar/alt-arrow-left-linear';
import Download from '~icons/solar/download-minimalistic-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Star from '~icons/solar/star-bold';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

type Detail = RavenHubItem & { skill_md?: string };

/** A folder maps name→subtree; a file maps name→null. */
type Tree = { [name: string]: Tree | null };

/** Build a nested tree from flat relative paths like "scripts/foo.mjs". */
function buildTree(paths: string[]): Tree {
	const root: Tree = {};
	for (const p of paths) {
		const parts = p.split('/').filter(Boolean);
		let node = root;
		parts.forEach((part, i) => {
			if (i === parts.length - 1) {
				if (!(part in node)) node[part] = null; // file leaf
			} else {
				if (node[part] == null || typeof node[part] !== 'object') node[part] = {};
				node = node[part] as Tree;
			}
		});
	}
	return root;
}

/** Folders before files, then alphabetical. */
function sortedEntries(tree: Tree): [string, Tree | null][] {
	return Object.entries(tree).sort(([a, av], [b, bv]) => {
		const aFolder = av !== null;
		const bFolder = bv !== null;
		if (aFolder !== bFolder) return aFolder ? -1 : 1;
		return a.localeCompare(b);
	});
}

/** One tree row — a collapsible folder (▾/▸) or a leaf file. */
function TreeNode({ name, node }: { name: string; node: Tree | null }) {
	const [open, setOpen] = useState(true);
	if (node === null) {
		return (
			<div className="flex items-center gap-1.5 py-0.5 pl-4 font-mono text-xs text-muted-foreground">
				{name}
			</div>
		);
	}
	return (
		<div>
			<button
				type="button"
				onClick={() => setOpen((o) => !o)}
				className="flex items-center gap-1 py-0.5 text-xs font-medium hover:text-foreground"
			>
				<span className="inline-block w-3 text-muted-foreground">{open ? '▾' : '▸'}</span>
				<span className="font-mono">{name}</span>
			</button>
			{open && (
				<div className="ml-2 border-l pl-2">
					{sortedEntries(node).map(([n, v]) => (
						<TreeNode key={n} name={n} node={v} />
					))}
				</div>
			)}
		</div>
	);
}

/**
 * Full-page skill detail. Reached by clicking a card in the gallery
 * (``/raven-skills/detail?id=…&kind=hub|local``). Hub skills fetch the
 * live catalog entry (metadata + skill_md); local/builtin skills read
 * their on-disk body.
 */
export function SkillDetailPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const [sp] = useSearchParams();
	const id = sp.get('id') || '';
	const kind = sp.get('kind') || 'hub';
	const name = sp.get('name') || '';
	const source = sp.get('source') || '';
	const isHub = kind === 'hub';

	const [loading, setLoading] = useState(true);
	const [detail, setDetail] = useState<Detail | null>(null);
	const [body, setBody] = useState('');
	const [installing, setInstalling] = useState(false);
	const [removing, setRemoving] = useState(false);

	useEffect(() => {
		let cancelled = false;
		setLoading(true);
		void (async () => {
			try {
				if (isHub) {
					const d = await ravenConfigApi.skills.hub.skill(id);
					if (!cancelled) {
						setDetail(d);
						setBody(d.skill_md || d.description || '');
					}
				} else {
					const b = await ravenConfigApi.skills.body({
						id: id || undefined,
						name,
						source,
					});
					if (!cancelled) {
						// Map the enriched local body into the same shape the hub view
						// uses, so the installed detail page renders identically.
						setDetail({
							name: name || b.name,
							source: b.source || source,
							description: b.description,
							category: b.category ?? undefined,
							license: b.license ?? undefined,
							tags: b.tags ?? [],
							source_url: b.source_url ?? undefined,
							files: b.files ?? [],
						} as Detail);
						setBody(b.skillMd || '');
					}
				}
			} catch (e) {
				toast.error(
					`${t('ravenSkills.bodyFailed')}: ${e instanceof Error ? e.message : String(e)}`,
				);
			} finally {
				if (!cancelled) setLoading(false);
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [id, kind, name, source, isHub, t]);

	const install = async () => {
		setInstalling(true);
		try {
			await ravenConfigApi.skills.hub.install(id);
			toast.success(t('ravenSkills.installOk'));
		} catch (e) {
			toast.error(
				`${t('ravenSkills.installFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setInstalling(false);
		}
	};

	const uninstall = async () => {
		if (!window.confirm(t('ravenSkills.uninstallConfirm', { name: detail?.name || name })))
			return;
		setRemoving(true);
		try {
			await ravenConfigApi.skills.remove({
				name: detail?.name || name,
				id: id || undefined,
				source,
			});
			toast.success(t('ravenSkills.uninstallOk'));
			navigate(-1);
		} catch (e) {
			toast.error(
				`${t('ravenSkills.uninstallFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setRemoving(false);
		}
	};

	const stars = detail?.github_star;
	const installs = detail?.install_count;
	const quality = detail?.quality_score;
	const tags = detail?.tags ?? [];
	const files = detail?.files ?? [];
	// A local skill is removable unless it's one of Raven's bundled builtins.
	const removable = !isHub && (detail?.source ?? source) !== 'builtin';
	// Download URL differs by kind: hub pulls from the catalog by id; local zips
	// the on-disk folder. Both return an attachment .zip.
	const downloadUrl = isHub
		? `${getBaseUrl()}/raven/skills/hub/download?id=${encodeURIComponent(id)}&name=${encodeURIComponent(
				detail?.name || '',
			)}`
		: `${getBaseUrl()}/raven/skills/download-local?name=${encodeURIComponent(
				detail?.name || name,
			)}&source=${encodeURIComponent(detail?.source ?? source)}`;

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-3xl flex-col gap-5 p-6">
				<Button variant="ghost" size="sm" className="w-fit" onClick={() => navigate(-1)}>
					<ArrowLeft className="size-4" /> {t('ravenSkills.back')}
				</Button>

				{loading ? (
					<div className="flex items-center gap-2 p-6 text-muted-foreground">
						<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
					</div>
				) : !detail ? (
					<div className="rounded-2xl border border-dashed p-10 text-center text-sm text-muted-foreground">
						{t('ravenSkills.skillsTableEmpty')}
					</div>
				) : (
					<>
						<div className="flex flex-col gap-2">
							<h1 className="text-2xl font-semibold">{detail.name}</h1>
							<div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
								{detail.category && (
									<span className="rounded bg-muted px-1.5 py-0.5 text-xs uppercase">
										{detail.category}
									</span>
								)}
								{/* builtin skills get a distinct badge; everything else shows its source */}
								{!isHub && (detail.source ?? source) === 'builtin' && (
									<span className="rounded bg-muted px-1.5 py-0.5 text-xs">
										{t('ravenSkills.builtin')}
									</span>
								)}
								{detail.source && <span>{detail.source}</span>}
								{detail.license && <span>· {detail.license}</span>}
							</div>
							{detail.description && (
								<p className="text-sm text-muted-foreground">
									{detail.description}
								</p>
							)}
						</div>

						<div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
							{stars != null && (
								<span className="flex items-center gap-1">
									<Star className="size-4 text-amber-400" />
									{stars.toLocaleString()}
								</span>
							)}
							{installs != null && (
								<span className="flex items-center gap-1">
									<Download className="size-4" />
									{installs.toLocaleString()}
								</span>
							)}
							{quality != null && <span>★ {(quality * 5).toFixed(1)}</span>}
							<div className="ml-auto flex items-center gap-3">
								{detail.source_url && (
									<a
										href={detail.source_url}
										target="_blank"
										rel="noreferrer"
										className="text-xs underline hover:text-foreground"
									>
										GitHub ↗
									</a>
								)}
								<a
									href={downloadUrl}
									title={t('ravenSkills.downloadZipHint')}
									className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
								>
									<Download className="size-3.5" /> {t('ravenSkills.downloadZip')}
								</a>
								{isHub ? (
									<Button
										size="sm"
										onClick={install}
										disabled={installing}
										title={t('ravenSkills.installHint')}
									>
										{installing ? (
											<Loader2 className="size-4 animate-spin" />
										) : (
											<AddToWorkspace className="size-4" />
										)}{' '}
										{t('ravenSkills.installToWorkspace')}
									</Button>
								) : (
									removable && (
										<Button
											size="sm"
											variant="outline"
											onClick={uninstall}
											disabled={removing}
											title={t('ravenSkills.uninstallHint')}
											className="text-muted-foreground hover:text-destructive"
										>
											{removing ? (
												<Loader2 className="size-4 animate-spin" />
											) : (
												<Trash2 className="size-4" />
											)}{' '}
											{t('ravenSkills.uninstall')}
										</Button>
									)
								)}
							</div>
						</div>

						{tags.length > 0 && (
							<div className="flex flex-wrap gap-1.5">
								{tags.map((tg) => (
									<span
										key={tg}
										className="rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground"
									>
										{tg}
									</span>
								))}
							</div>
						)}

						{files.length > 0 && (
							<div className="flex flex-col gap-1.5">
								<div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
									{t('ravenSkills.fileStructure')}
								</div>
								<div className="rounded-md border p-3">
									{sortedEntries(buildTree(files)).map(([n, v]) => (
										<TreeNode key={n} name={n} node={v} />
									))}
								</div>
							</div>
						)}

						<div className="prose prose-sm dark:prose-invert max-w-none rounded-md border bg-muted/30 p-5">
							<ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
						</div>
					</>
				)}
			</div>
		</div>
	);
}
