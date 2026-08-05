import { useState } from 'react';

import type { Skill } from '@/api';
import { AddSkillDialog } from '@/components/dialog/AddSkillDialog.tsx';
import { DeleteDialog } from '@/components/dialog/DeleteDialog.tsx';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Button } from '@/components/ui/button';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from '@/components/ui/item';
import { useTranslation } from '@/i18n/useI18n.ts';
import PlusCircle from '~icons/solar/add-circle-bold-duotone';
import FileX from '~icons/solar/file-corrupted-bold-duotone';
import SearchX from '~icons/solar/magnifer-bold-duotone';
import Search from '~icons/solar/magnifer-bold-duotone';
import Trash from '~icons/solar/trash-bin-trash-bold-duotone';

/**
 * One skill SkillForge injected into this conversation, accumulated
 * across turns from ``skills_injected`` custom events. Read-only — this
 * is observed usage, not workspace membership.
 */
export type InjectedSkillEntry = {
	/** Source-qualified id, e.g. ``"local/weather"``. */
	id: string;
	/** Source prefix parsed from the id (``local`` / ``hub`` / ``everos``). */
	source: string;
	/** Display name (the id's native part). */
	name: string;
	/** How many turns this skill was injected in. */
	count: number;
};

interface SkillPanelProps {
	/** The skills equipped in the workspace. */
	skills: Skill[];
	/** Whether the skill list is still loading. */
	loading?: boolean;
	/**
	 * Add a skill to the workspace.
	 *
	 * @param skillPath - Path of the skill to add.
	 */
	onAdd: (skillPath: string) => Promise<void>;
	/**
	 * Remove a skill by name.
	 *
	 * @param name - The skill name to remove.
	 */
	onRemove: (name: string) => Promise<void>;
	/**
	 * Skills SkillForge injected during the current conversation
	 * (accumulated, read-only). Rendered as a section above the
	 * workspace skills. Defaults to empty.
	 */
	injected?: InjectedSkillEntry[];
}

/**
 * Pure content body for the Skill dock panel: a search box, the list
 * of equipped skills, and an "Add Skill" action. Holds only local UI
 * state (search text, delete confirmation target); all data arrives
 * via props so it owns no data fetching.
 *
 * Renders without its own header/border — the surrounding `Panel`
 * chrome (from `PanelDock`) provides those.
 *
 * @param skills - The skills to list.
 * @param loading - Whether the list is loading.
 * @param onAdd - Add-skill callback.
 * @param onRemove - Remove-skill callback.
 * @returns The skill panel body.
 */
export function SkillPanel({
	skills,
	loading = false,
	onAdd,
	onRemove,
	injected = [],
}: SkillPanelProps) {
	const { t } = useTranslation();
	const [search, setSearch] = useState('');
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

	const filtered = search
		? skills.filter((s) => s.name.toLowerCase().includes(search.toLowerCase()))
		: skills;

	return (
		<div className="flex flex-col flex-1 min-h-0 gap-y-2">
			{/* Section 1: skills SkillForge injected during this conversation
			    (accumulated, read-only). Rendered only when non-empty. */}
			{injected.length > 0 && (
				<div className="flex flex-col gap-y-1 rounded-md border border-border/60 bg-muted/30 p-2">
					<div className="flex items-baseline justify-between">
						<span className="text-xs font-semibold uppercase tracking-wide text-foreground/80">
							{t('panel.skill.usedTitle')}
						</span>
						<span className="text-[10px] text-muted-foreground">
							{t('panel.skill.usedHint')}
						</span>
					</div>
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
								{s.count > 1 && (
									<span className="ml-auto text-xs text-muted-foreground">×{s.count}</span>
								)}
							</div>
						))}
					</div>
				</div>
			)}

			{/* Section 2: skills equipped in the workspace (existing). */}
			<span className="text-xs font-semibold uppercase tracking-wide text-foreground/80">
				{t('panel.skill.workspaceTitle')}
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
						<Item key={skill.name} variant="outline">
							<ItemContent>
								<ItemTitle>{skill.name}</ItemTitle>
								<ItemDescription>{skill.description}</ItemDescription>
							</ItemContent>
							<ItemActions>
								<Button
									variant="outline"
									size="icon-sm"
									onClick={() => {
										setDeleteTarget(skill.name);
										setDeleteOpen(true);
									}}
								>
									<Trash />
								</Button>
							</ItemActions>
						</Item>
					))}
				</div>
			)}

			<AddSkillDialog onAdd={onAdd}>
				<Button variant="default">
					<PlusCircle />
					{t('panel.skill.add')}
				</Button>
			</AddSkillDialog>

			<DeleteDialog
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				title={t('common.deleteTitle', {
					entity: t('dialog-mcp-delete.skillEntity'),
					name: deleteTarget ?? '',
				})}
				description={t('dialog-mcp-delete.skillDescription')}
				onConfirm={async () => {
					if (deleteTarget) await onRemove(deleteTarget);
				}}
			/>
		</div>
	);
}
