import { useState } from 'react';
import { Link } from 'react-router-dom';

import type { RavenSkillEntry } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog.tsx';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Button } from '@/components/ui/button';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from '@/components/ui/item';
import { isRemovableSkill } from '@/hooks/useRavenSkills';
import { useTranslation } from '@/i18n/useI18n.ts';
import FileX from '~icons/solar/file-corrupted-bold-duotone';
import SearchX from '~icons/solar/magnifer-bold-duotone';
import Search from '~icons/solar/magnifer-bold-duotone';
import Settings from '~icons/solar/settings-bold-duotone';
import Trash from '~icons/solar/trash-bin-trash-bold-duotone';

/**
 * One skill this conversation used, accumulated across turns from
 * ``skills_injected`` custom events. Read-only — this is observed usage,
 * not workspace membership.
 */
export type InjectedSkillEntry = {
	/** Addressing id, e.g. ``"local/weather"``. */
	id: string;
	/**
	 * Where the skill actually comes from (``builtin`` / ``workspace`` /
	 * ``hub`` / ``everos``). This is the registry source the backend
	 * reports, not the id's prefix: every on-disk skill is *addressed* as
	 * ``local/`` whatever its origin, so the prefix cannot say "builtin".
	 */
	source: string;
	/** Display name (the id's native part). */
	name: string;
	/** How many times this skill was injected / loaded. */
	count: number;
	/**
	 * How it entered the turn: injected by SkillForge's gate, or loaded by
	 * the model itself. Absent on entries persisted before this field
	 * existed.
	 */
	kind?: 'injected' | 'read_skill' | 'use_skill';
};

interface SkillPanelProps {
	/** Raven's own skill pool — what the chat agent can reach. */
	skills: RavenSkillEntry[];
	/** Whether the skill list is still loading. */
	loading?: boolean;
	/**
	 * Uninstall a skill. Only hub-cached skills are removable; the caller
	 * is expected to surface the gateway's refusal for anything else.
	 */
	onRemove: (skill: RavenSkillEntry) => Promise<void>;
	/**
	 * Skills used during the current conversation (accumulated,
	 * read-only). Rendered as a section above the pool. Defaults to empty.
	 */
	injected?: InjectedSkillEntry[];
}

/**
 * Pure content body for the Skill dock panel: what this conversation used,
 * then the skills the agent can reach. Holds only local UI state (search
 * text, delete confirmation target); all data arrives via props so it owns
 * no data fetching.
 *
 * Renders without its own header/border — the surrounding `Panel`
 * chrome (from `PanelDock`) provides those.
 *
 * Installing a skill is not offered here: it means browsing the hub, which
 * is a page of its own. The footer links there instead of duplicating it.
 *
 * @param skills - The skills to list.
 * @param loading - Whether the list is loading.
 * @param onRemove - Uninstall callback.
 * @returns The skill panel body.
 */
export function SkillPanel({ skills, loading = false, onRemove, injected = [] }: SkillPanelProps) {
	const { t } = useTranslation();
	const [search, setSearch] = useState('');
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [deleteTarget, setDeleteTarget] = useState<RavenSkillEntry | null>(null);

	const filtered = search
		? skills.filter((s) => s.name.toLowerCase().includes(search.toLowerCase()))
		: skills;

	return (
		<div className="flex flex-col flex-1 min-h-0 gap-y-2">
			{/* Section 1: skills SkillForge injected during this conversation
			    (accumulated, read-only). Rendered only when non-empty. */}
			{injected.length > 0 && (
				<div className="flex flex-col gap-y-1 rounded-md border border-border/60 bg-muted/30 p-2">
					<span className="text-xs font-semibold uppercase tracking-wide text-foreground/80">
						{t('panel.skill.usedTitle')}
					</span>
					<div className="flex flex-col gap-y-1">
						{injected.map((s) => (
							<div
								key={s.id}
								className="flex items-center gap-x-2 rounded border border-border/50 bg-background px-2 py-1"
							>
								<span className="rounded bg-muted px-1 py-0.5 text-[10px] uppercase text-muted-foreground">
									{s.source}
								</span>
								<span className="text-sm">{s.name}</span>
								{/* How it got here: the gate chose it, or the model
								    fetched it. Only shown for the model-loaded case,
								    which is the surprising one. */}
								{s.kind && s.kind !== 'injected' && (
									<span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">
										{t('panel.skill.usedByModel')}
									</span>
								)}
								{s.count > 1 && (
									<span className="ml-auto text-xs text-muted-foreground">×{s.count}</span>
								)}
							</div>
						))}
					</div>
				</div>
			)}

			{/* Section 2: the agent's own skill pool, read from raven's registry. */}
			<span className="text-xs font-semibold uppercase tracking-wide text-foreground/80">
				{t('panel.skill.poolTitle', { count: skills.length })}
			</span>
			<span className="text-muted-foreground text-sm">{t('panel.skill.description')}</span>
			<InputGroup>
				<InputGroupInput
					placeholder={t('panel.skill.searchPlaceholder')}
					value={search}
					onChange={(e) => setSearch(e.target.value)}
				/>
				<InputGroupAddon align="inline-end">
					<Search />
				</InputGroupAddon>
			</InputGroup>

			{loading ? (
				<div className="flex flex-1 items-center justify-center">
					<p className="text-muted-foreground text-sm">{t('panel.loading')}</p>
				</div>
			) : filtered.length === 0 ? (
				<PanelEmpty
					icon={search ? SearchX : FileX}
					title={search ? t('panel.search.emptyTitle') : t('panel.skill.emptyTitle')}
					description={
						search
							? t('panel.search.emptyDescription', { query: search })
							: t('panel.skill.emptyDescription')
					}
				/>
			) : (
				<div className="flex flex-col flex-1 min-h-0 overflow-y-auto gap-y-2">
					{filtered.map((skill) => (
						<Item key={`${skill.source}/${skill.name}`} variant="outline">
							<ItemContent>
								<ItemTitle className="flex items-center gap-x-2">
									<span className="rounded bg-muted px-1 py-0.5 text-[10px] uppercase text-muted-foreground">
										{skill.source}
									</span>
									{skill.name}
								</ItemTitle>
								<ItemDescription>{skill.description}</ItemDescription>
							</ItemContent>
							{/* Uninstall is offered only where it can succeed: builtin
							    and workspace-dir skills are refused by the gateway. */}
							{isRemovableSkill(skill) && (
								<ItemActions>
									<Button
										variant="outline"
										size="icon-sm"
										onClick={() => {
											setDeleteTarget(skill);
											setDeleteOpen(true);
										}}
									>
										<Trash />
									</Button>
								</ItemActions>
							)}
						</Item>
					))}
				</div>
			)}

			<Button variant="default" asChild>
				<Link to="/raven-skills">
					<Settings />
					{t('panel.skill.manage')}
				</Link>
			</Button>

			<DeleteDialog
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				title={t('common.deleteTitle', {
					entity: t('dialog-mcp-delete.skillEntity'),
					name: deleteTarget?.name ?? '',
				})}
				description={t('dialog-mcp-delete.skillDescription')}
				onConfirm={async () => {
					if (deleteTarget) await onRemove(deleteTarget);
				}}
			/>
		</div>
	);
}
