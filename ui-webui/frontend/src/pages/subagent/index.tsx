import { useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenSubagentTest, RavenThirdPartySubagent } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { SubagentIcon } from '@/components/SubagentIcon';
import { SubagentStatusDot } from '@/components/SubagentStatus';
import { Empty, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import {
	Sidebar,
	SidebarContent,
	SidebarGroup,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuAction,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { useRavenSubagents } from '@/hooks/useRavenSubagents';
import { useTranslation } from '@/i18n/useI18n';
import { installGroupOf, isPresetName, presetLabel } from '@/pages/subagent/catalog';
import { EMPTY_FORM, toEntry, toForm, validate } from '@/pages/subagent/form';
import type { FormState, Kind } from '@/pages/subagent/form';
import { SubagentForm } from '@/pages/subagent/SubagentForm';

/**
 * Full-page manager for Raven's third-party sub-agents: a left list of
 * configured agents + a right create/edit form. Writes replace the whole
 * list on the real Raven config data plane (gateway proxy, whole-list PUT),
 * which validates and hot-applies to the running runtime. Reached from the
 * app rail (`/subagents`).
 */
export const SubAgentsPage = () => {
	const { t } = useTranslation();
	const { agents, presets, probes, probesLoaded, loading, probing, reprobe, save } =
		useRavenSubagents();
	// `null` = nothing selected; '' = creating new; otherwise editing that name.
	const [editingName, setEditingName] = useState<string | null>(null);
	const [form, setForm] = useState<FormState>(EMPTY_FORM);
	// Preset mode is a deliberate selection, never inferred from the typed name:
	// the Name placeholder is itself a preset name, so matching on it would flip
	// the pane mid-typing and hide the field the user is editing.
	const [presetName, setPresetName] = useState<string | null>(null);
	const [submitting, setSubmitting] = useState(false);
	const [testing, setTesting] = useState(false);
	const [testResult, setTestResult] = useState<RavenSubagentTest | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [deleteTarget, setDeleteTarget] = useState<RavenThirdPartySubagent | null>(null);
	const [toggling, setToggling] = useState(false);

	const openCreate = (kind: Kind) => {
		setForm({ ...EMPTY_FORM, kind });
		setError(null);
		setTestResult(null);
		setEditingName('');
		setPresetName(null);
	};
	const openEdit = (a: RavenThirdPartySubagent) => {
		setForm(toForm(a));
		setError(null);
		setTestResult(null);
		setEditingName(a.name);
		setPresetName(a.preset ?? null);
	};
	const close = () => {
		setError(null);
		setTestResult(null);
		setEditingName(null);
		setPresetName(null);
	};

	const addFromPreset = (preset: RavenThirdPartySubagent) => {
		// Provenance comes from the row the user clicked, not from the payload: a
		// gateway that predates the `preset` field would otherwise leave this null
		// and the reserved-name guard would refuse the very preset being added.
		setForm({ ...toForm(preset), preset: preset.preset ?? preset.name });
		setError(null);
		setTestResult(null);
		setEditingName('');
		setPresetName(preset.name);
	};

	const { canSubmit, nameCollides, cliOk, stateful, derived, keyOk, hasStoredKey } = validate(
		form,
		agents,
		editingName,
	);
	const preset = presetName ? (presets.find((p) => p.name === presetName) ?? null) : null;

	const submit = async () => {
		const previous = agents.find((a) => a.name === editingName);
		const entry = toEntry(form, previous, preset?.description);
		// A preset's default name belongs to that preset. Refuse it for a hand-written
		// agent and for a different preset renamed onto it; allow a preset that simply
		// keeps its own name. Checked on rename too, not only on creation.
		if (isPresetName(entry.name, presets) && entry.preset !== entry.name) {
			setError(t('subagent-sidebar.presetNameReserved', { name: entry.name }));
			return;
		}
		// Name is the primary key: a different stored entry with the same name
		// would otherwise be silently dropped by the `entry.name` filter below,
		// so refuse before it can happen.
		if (agents.some((a) => a.name === entry.name && a.name !== editingName)) {
			setError(t('subagent-sidebar.nameTaken', { name: entry.name }));
			return;
		}
		// Name is the primary key, so a rename drops the old row.
		const next = [
			...agents.filter((a) => a.name !== editingName && a.name !== entry.name),
			entry,
		];
		setSubmitting(true);
		setError(null);
		try {
			await save(next);
			// Stay on the agent that was just written instead of dropping back to
			// the empty pane: creating flows straight into editing the new entry,
			// and a rename follows the new name. Re-syncing the form from `entry`
			// - the payload, not `agents`, which the same save only just replaced
			// - makes the pane show what is stored, so a typed api key returns to
			// blank-means-keep exactly as reopening the row would. The inline test
			// verdict describes the pre-save config, so it goes; the remembered
			// one survives in the status line when the save did not invalidate it.
			setForm(toForm(entry));
			setEditingName(entry.name);
			setTestResult(null);
			toast.success(t('subagent-sidebar.saved'));
		} catch (err) {
			setError(String((err as Error)?.message ?? err));
		} finally {
			setSubmitting(false);
		}
	};

	// Tested by name against what is saved (or against the Python-defined
	// preset), never by posting a command: see the gateway handler for why.
	const testTarget: { name: string; source: 'config' | 'preset' } | null = editingName
		? { name: editingName, source: 'config' }
		: presetName
			? { name: presetName, source: 'preset' }
			: null;
	// An unsaved openai preset ships no api key, so the probe short-circuits and the
	// verdict is always a red failure - actively misleading right after the user has
	// pasted a key into the form. An unsaved cli preset is worth testing: "is claude
	// installed and authenticated" is exactly what the Presets group should answer
	// before you commit to configuring it.
	const canTest = testTarget !== null && !(editingName === '' && form.kind === 'openai');

	const runTest = async () => {
		if (!testTarget || !canTest) return;
		setTesting(true);
		setTestResult(null);
		try {
			const res = await ravenConfigApi.testSubagent(testTarget);
			setTestResult(res.result);
		} catch {
			// client.ts toasts
		} finally {
			setTesting(false);
		}
	};

	const remove = async (name: string) => {
		await save(agents.filter((a) => a.name !== name));
		if (editingName === name) setEditingName(null);
	};

	// A renamed preset shows its name, not its preset label: the name is how Raven
	// addresses it, and hiding it behind "Claude Code" would leave the user unable to
	// see what to write in a DAG node.
	const rowLabel = (sa: RavenThirdPartySubagent) =>
		sa.preset && sa.name === sa.preset ? presetLabel(sa.preset, t) : sa.name;
	// A configured agent and a preset can share a name (a preset keeping its own
	// default name is both), so the source is part of the key.
	const probeOf = (source: 'config' | 'preset', name: string) =>
		probes[`${source}:${name}`] ?? null;
	// A preset row stands for the user's configured entry when there is one, so its
	// group and its switch reflect the key they actually saved rather than the
	// template's blank. `source` follows, because the probe is keyed by it.
	const presetRows = presets.map((p) => {
		const configured = agents.find((a) => a.preset === p.name) ?? null;
		const entry = configured ?? p;
		const source: 'config' | 'preset' = configured ? 'config' : 'preset';
		const probe = probeOf(source, entry.name);
		return { preset: p, configured, entry, source, probe, group: installGroupOf(entry, probe) };
	});
	// Anything that isn't the configured entry for some preset row. Identity, not
	// name: `presetRows` matches by `preset === p.name` with `Array.find`, which
	// returns at most one agent per preset name, so a second stored agent sharing
	// that preset -- a hand-edited config.json, which this design supports --
	// would otherwise be invisible (never matched by any preset row) and
	// undeletable (excluded here too, if we filtered on name). Comparing objects
	// also subsumes the unknown-preset case for free: an agent whose preset names
	// nothing we know about is never any row's `configured` either.
	const customAgents = agents.filter((a) => !presetRows.some((r) => r.configured === a));

	// Toggling writes the whole thirdParty list, so two overlapping writes would make
	// the later one carry a stale copy of the earlier. One at a time, with every
	// switch disabled while a write is in flight.
	const applyToggle = async (next: RavenThirdPartySubagent[]) => {
		setToggling(true);
		try {
			await save(next);
		} catch {
			// client.ts toasts; the switch snaps back because `agents` never changed
		} finally {
			setToggling(false);
		}
	};

	const toggleEnabled = (row: (typeof presetRows)[number], next: boolean) =>
		applyToggle(
			row.configured
				? agents.map((a) => (a.name === row.configured?.name ? { ...a, enabled: next } : a))
				: // Switching on an unconfigured preset IS configuring it: the shipped
					// payload is already schema-shaped, so it needs no form round-trip.
					// `next` is honored rather than hardcoded, so the write always
					// matches what was actually asked for.
					[
						...agents,
						{
							...row.preset,
							preset: row.preset.preset ?? row.preset.name,
							enabled: next,
						},
					],
		);

	const presetGroup = (titleKey: string, rows: typeof presetRows) =>
		rows.length === 0 ? null : (
			<SidebarGroup key={titleKey}>
				<SidebarGroupLabel className="justify-between">
					<span>{t(titleKey)}</span>
					<span className="text-gold font-semibold tabular-nums">{rows.length}</span>
				</SidebarGroupLabel>
				<SidebarGroupContent>
					<SidebarMenu>
						{rows.map((row) => {
							// The switch is on iff the row has a configured entry with
							// `enabled !== false`; an unconfigured preset is never on, no
							// matter what its template payload says.
							const enabledNow = row.configured
								? row.configured.enabled !== false
								: false;
							// Only the off -> on transition needs the agent to be usable:
							// an already-enabled row must stay switchable off no matter its
							// install group, or the only way to take a broken agent off the
							// roster is to delete it -- the exact problem this switch exists
							// to solve.
							const blockedForInstall = !enabledNow && row.group !== 'installed';
							return (
								<SidebarMenuItem key={row.preset.name}>
									<SidebarMenuButton
										isActive={editingName === row.entry.name}
										onClick={() =>
											row.configured
												? openEdit(row.configured)
												: addFromPreset(row.preset)
										}
									>
										<SubagentIcon type={row.preset.name} size={18} />
										<SubagentStatusDot probe={row.probe ?? undefined} />
										<span className="min-w-0 flex-1 truncate">
											{row.configured
												? rowLabel(row.configured)
												: presetLabel(row.preset.name, t)}
										</span>
									</SidebarMenuButton>
									{/* A sibling, not a child: SidebarMenuButton is itself a <button>,
									    so nesting an interactive control is invalid HTML and swallows
									    the click. SidebarMenuAction is also what gives the row its
									    right-hand clearance, via
									    group-has-data-[sidebar=menu-action]/menu-item:pr-8. Its own
									    aspect-square w-5 would squash a switch, hence the overrides.
									    The peer-variant top-1.5 (peer-data-[size=default]/menu-button)
									    outranks a plain top-1/2 on specificity, so both are overridden. */}
									<SidebarMenuAction
										asChild
										className="top-1/2 right-2 aspect-auto h-auto w-auto -translate-y-1/2 justify-start peer-data-[size=default]/menu-button:top-1/2"
									>
										<Switch
											size="sm"
											checked={enabledNow}
											disabled={blockedForInstall || toggling}
											onCheckedChange={(v) => void toggleEnabled(row, v)}
											aria-label={t('subagent-sidebar.enableLabel')}
											title={
												row.group === null
													? t('subagent-sidebar.statusChecking')
													: blockedForInstall
														? row.entry.kind === 'openai'
															? t('subagent-sidebar.enableBlockedNoKey')
															: t(
																	'subagent-sidebar.enableBlockedNotInstalled',
																)
														: t('subagent-sidebar.enableLabel')
											}
										/>
									</SidebarMenuAction>
								</SidebarMenuItem>
							);
						})}
					</SidebarMenu>
				</SidebarGroupContent>
			</SidebarGroup>
		);

	return (
		<div className="flex h-full w-full">
			{/* Left: sub-agent list */}
			<Sidebar collapsible="none" className="w-72 border-r">
				<SidebarHeader className="flex flex-col mt-5 gap-y-1">
					<div className="em-kicker">{t('subagent-sidebar.kicker')}</div>
					<div className="text-lg font-medium">
						<span className="em-accent">{t('subagent-sidebar.title')}</span>
					</div>
					<div className="text-muted-foreground text-xs">
						{t('subagent-sidebar.subtitle')}
					</div>
				</SidebarHeader>
				<SidebarContent>
					{loading ? (
						<div className="flex flex-col gap-y-2 p-2">
							{Array.from({ length: 3 }).map((_, i) => (
								<Skeleton key={i} className="h-8 rounded" />
							))}
						</div>
					) : (
						<>
							{/* Until probes land there is no cli install status, so every preset
							    stays in one group: splitting on partial information would make
							    rows jump between groups as results arrive. */}
							{!probesLoaded
								? presetGroup('subagent-sidebar.groupPresets', presetRows)
								: [
										presetGroup(
											'subagent-sidebar.groupInstalled',
											presetRows.filter((r) => r.group === 'installed'),
										),
										presetGroup(
											'subagent-sidebar.groupUninstalled',
											presetRows.filter((r) => r.group !== 'installed'),
										),
									]}
							<SidebarGroup>
								<SidebarGroupLabel>
									{t('subagent-sidebar.groupCustom')}
								</SidebarGroupLabel>
								<SidebarGroupContent>
									<SidebarMenu>
										{customAgents.map((sa) => (
											<SidebarMenuItem key={sa.name}>
												<SidebarMenuButton
													isActive={editingName === sa.name}
													onClick={() => openEdit(sa)}
												>
													<SubagentIcon type={sa.kind} size={18} />
													<SubagentStatusDot
														probe={
															probeOf('config', sa.name) ?? undefined
														}
													/>
													<span className="min-w-0 flex-1 truncate">
														{sa.name}
													</span>
												</SidebarMenuButton>
												<SidebarMenuAction
													asChild
													className="top-1/2 right-2 aspect-auto h-auto w-auto -translate-y-1/2 justify-start peer-data-[size=default]/menu-button:top-1/2"
												>
													<Switch
														size="sm"
														checked={sa.enabled ?? true}
														disabled={toggling}
														onCheckedChange={(v) =>
															void applyToggle(
																agents.map((a) =>
																	a.name === sa.name
																		? { ...a, enabled: v }
																		: a,
																),
															)
														}
														aria-label={t(
															'subagent-sidebar.enableLabel',
														)}
														title={t('subagent-sidebar.enableLabel')}
													/>
												</SidebarMenuAction>
											</SidebarMenuItem>
										))}
										<SidebarMenuItem>
											<SidebarMenuButton onClick={() => openCreate('cli')}>
												<SubagentIcon type="cli" size={18} />
												<span className="min-w-0 flex-1 truncate">
													{t('subagent-sidebar.newCli')}
												</span>
											</SidebarMenuButton>
										</SidebarMenuItem>
										<SidebarMenuItem>
											<SidebarMenuButton onClick={() => openCreate('openai')}>
												<SubagentIcon type="openai" size={18} />
												<span className="min-w-0 flex-1 truncate">
													{t('subagent-sidebar.newOpenai')}
												</span>
											</SidebarMenuButton>
										</SidebarMenuItem>
									</SidebarMenu>
								</SidebarGroupContent>
							</SidebarGroup>
						</>
					)}
				</SidebarContent>
			</Sidebar>

			{/* Right: create / edit form */}
			<main className="flex-1 min-h-0 overflow-y-auto">
				{editingName !== null ? (
					<SubagentForm
						form={form}
						setForm={setForm}
						preset={preset}
						editingName={editingName}
						submitting={submitting}
						error={error}
						canSubmit={canSubmit}
						nameCollides={nameCollides}
						cliOk={cliOk}
						stateful={stateful}
						derived={derived}
						keyOk={keyOk}
						hasStoredKey={hasStoredKey}
						probe={
							editingName
								? probeOf('config', editingName)
								: preset
									? probeOf('preset', preset.name)
									: null
						}
						probing={probing}
						probesLoaded={probesLoaded}
						onRefreshProbe={() => void reprobe()}
						testing={testing}
						testResult={testResult}
						canTest={canTest}
						onTest={() => void runTest()}
						onSubmit={submit}
						onClose={close}
						onDelete={() => {
							const target = agents.find((a) => a.name === editingName);
							if (target) setDeleteTarget(target);
						}}
					/>
				) : (
					<div className="flex h-full items-center justify-center">
						<Empty className="border-none">
							<EmptyHeader>
								<EmptyTitle>{t('subagent-sidebar.selectHint')}</EmptyTitle>
							</EmptyHeader>
						</Empty>
					</div>
				)}
			</main>

			<DeleteDialog
				open={deleteTarget !== null}
				onOpenChange={(open) => {
					if (!open) setDeleteTarget(null);
				}}
				title={t('common.deleteTitle', {
					entity: t('panel.subagent.entity'),
					name: deleteTarget?.name ?? '',
				})}
				description={t('common.deleteDescription')}
				onConfirm={async () => {
					if (deleteTarget) await remove(deleteTarget.name);
				}}
			/>
		</div>
	);
};
