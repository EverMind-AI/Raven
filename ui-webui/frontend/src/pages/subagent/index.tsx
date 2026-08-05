import { useState } from 'react';
import { toast } from 'sonner';

import type { RavenThirdPartySubagent } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Empty, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Sidebar,
	SidebarContent,
	SidebarGroup,
	SidebarGroupContent,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Skeleton } from '@/components/ui/skeleton';
import { Textarea } from '@/components/ui/textarea';
import { useRavenSubagents } from '@/hooks/useRavenSubagents';
import { useTranslation } from '@/i18n/useI18n';
import Plus from '~icons/solar/add-square-linear';
import ChevronDown from '~icons/solar/alt-arrow-down-linear';
import X from '~icons/solar/close-square-linear';
import Bot from '~icons/solar/cpu-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

type Kind = 'cli' | 'openai';

interface FormState {
	kind: Kind;
	name: string;
	description: string;
	command: string;
	resumeCommand: string;
	readsLocalFiles: boolean;
	idSource: 'provisioned' | 'derived';
	sessionIdPattern: string;
	outputPattern: string;
	transcriptFormat: 'text' | 'codex_jsonl' | 'claude_stream_json';
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

const EMPTY_FORM: FormState = {
	kind: 'cli',
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

function envToText(env: Record<string, string> | undefined): string {
	return Object.entries(env ?? {})
		.map(([k, v]) => `${k}=${v}`)
		.join('\n');
}

function textToEnv(text: string): Record<string, string> {
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

function toForm(a: RavenThirdPartySubagent): FormState {
	if (a.kind === 'openai') {
		return {
			...EMPTY_FORM,
			kind: 'openai',
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

/** `previous` is the stored entry being edited, used to keep an unchanged api key. */
function toEntry(f: FormState, previous?: RavenThirdPartySubagent): RavenThirdPartySubagent {
	const description = f.description.trim() || undefined;
	const timeout = f.timeout.trim() === '' ? null : Number(f.timeout);
	if (f.kind === 'openai') {
		const stored = previous?.kind === 'openai' ? previous.apiKey : undefined;
		return {
			name: f.name.trim(),
			kind: 'openai',
			description,
			baseUrl: f.baseUrl.trim(),
			model: f.model.trim(),
			apiKey: f.apiKey.trim() || stored || '',
			readsLocalFiles: f.readsLocalFiles,
			systemPrompt: f.systemPrompt.trim() || null,
			temperature: f.temperature.trim() === '' ? null : Number(f.temperature),
			maxTokens: f.maxTokens.trim() === '' ? null : Number(f.maxTokens),
			timeout,
			maxOutputChars: previous?.maxOutputChars,
		};
	}
	return {
		name: f.name.trim(),
		kind: 'cli',
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
	};
}

/**
 * Full-page manager for Raven's third-party sub-agents: a left list of
 * configured agents + a right create/edit form. Writes replace the whole
 * list on the real Raven config data plane (gateway proxy, whole-list PUT),
 * which validates and hot-applies to the running runtime. Reached from the
 * app rail (`/subagents`).
 */
export const SubAgentsPage = () => {
	const { t } = useTranslation();
	const { agents, presets, loading, save } = useRavenSubagents();
	// `null` = nothing selected; '' = creating new; otherwise editing that name.
	const [editingName, setEditingName] = useState<string | null>(null);
	const [form, setForm] = useState<FormState>(EMPTY_FORM);
	const [submitting, setSubmitting] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [deleteTarget, setDeleteTarget] = useState<RavenThirdPartySubagent | null>(null);

	const openCreate = () => {
		setForm(EMPTY_FORM);
		setError(null);
		setEditingName('');
	};
	const openEdit = (a: RavenThirdPartySubagent) => {
		setForm(toForm(a));
		setError(null);
		setEditingName(a.name);
	};
	const close = () => {
		setError(null);
		setEditingName(null);
	};

	const addFromPreset = (preset: RavenThirdPartySubagent) => {
		setForm(toForm(preset));
		setError(null);
		setEditingName('');
	};

	// Mirror the backend's rules (raven/config/schema.py). A stateful
	// *provisioned* command needs `{agent_id}`; a *derived* command must not
	// (the CLI mints the id itself).
	const stateful = form.resumeCommand.trim().length > 0;
	const derived = form.idSource === 'derived';
	const idInCommand = form.command.includes('{agent_id}');
	const cliOk = stateful
		? form.resumeCommand.includes('{agent_id}') && (derived ? !idInCommand : idInCommand)
		: !idInCommand;
	const openaiOk = !!form.baseUrl.trim() && !!form.model.trim();
	const nameCollides = agents.some((a) => a.name === form.name.trim() && a.name !== editingName);
	const canSubmit =
		!!form.name.trim() &&
		!nameCollides &&
		(form.kind === 'openai' ? openaiOk : !!form.command.trim() && cliOk);

	const submit = async () => {
		const previous = agents.find((a) => a.name === editingName);
		const entry = toEntry(form, previous);
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
			setEditingName(null);
			toast.success(t('subagent-sidebar.saved'));
		} catch (err) {
			setError(String((err as Error)?.message ?? err));
		} finally {
			setSubmitting(false);
		}
	};

	const remove = async (name: string) => {
		await save(agents.filter((a) => a.name !== name));
		if (editingName === name) setEditingName(null);
	};

	const set = (key: keyof FormState) => (e: { target: { value: string } }) =>
		setForm((f) => ({ ...f, [key]: e.target.value }));

	return (
		<div className="flex h-full w-full">
			{/* Left: sub-agent list */}
			<Sidebar collapsible="none" className="border-r">
				<SidebarHeader className="flex flex-col mt-5 gap-y-1">
					<div className="em-kicker">{t('subagent-sidebar.kicker')}</div>
					<div className="flex items-center justify-between">
						<div className="text-lg font-medium">
							<span className="em-accent">{t('subagent-sidebar.title')}</span>
						</div>
						<div className="flex items-center gap-x-1">
							{presets.length > 0 && (
								<DropdownMenu>
									<DropdownMenuTrigger asChild>
										<Button size="sm" variant="outline">
											{t('subagent-sidebar.addFromPreset')}
											<ChevronDown className="size-4" />
										</Button>
									</DropdownMenuTrigger>
									<DropdownMenuContent align="end">
										{presets.map((preset) => (
											<DropdownMenuItem
												key={preset.name}
												onClick={() => addFromPreset(preset)}
											>
												{preset.name}
											</DropdownMenuItem>
										))}
									</DropdownMenuContent>
								</DropdownMenu>
							)}
							<Button size="icon-sm" variant="outline" onClick={openCreate}>
								<Plus />
							</Button>
						</div>
					</div>
					<div className="text-muted-foreground text-xs">
						{t('subagent-sidebar.subtitle')}
					</div>
				</SidebarHeader>
				<SidebarContent>
					<SidebarGroup>
						<SidebarGroupContent>
							{loading ? (
								<div className="flex flex-col gap-y-2 p-2">
									{Array.from({ length: 3 }).map((_, i) => (
										<Skeleton key={i} className="h-8 rounded" />
									))}
								</div>
							) : agents.length === 0 ? (
								<Empty className="border-none py-8">
									<EmptyHeader>
										<EmptyTitle>{t('subagent-sidebar.empty')}</EmptyTitle>
									</EmptyHeader>
								</Empty>
							) : (
								<SidebarMenu>
									{agents.map((sa) => (
										<SidebarMenuItem key={sa.name}>
											<SidebarMenuButton
												isActive={editingName === sa.name}
												onClick={() => openEdit(sa)}
											>
												<Bot />
												<span className="min-w-0 flex-1 truncate">
													{sa.name}
												</span>
												<Badge
													variant="secondary"
													className="text-[10px] px-1 py-0"
												>
													{sa.kind === 'cli'
														? t('subagent-sidebar.kindBadgeCli')
														: t('subagent-sidebar.kindBadgeOpenai')}
												</Badge>
												{(sa.kind === 'cli' ? sa.resumeCommand : false) && (
													<Badge
														variant="outline"
														className="text-[10px] px-1 py-0"
													>
														{t('subagent-sidebar.statefulBadge')}
													</Badge>
												)}
											</SidebarMenuButton>
										</SidebarMenuItem>
									))}
								</SidebarMenu>
							)}
						</SidebarGroupContent>
					</SidebarGroup>
				</SidebarContent>
			</Sidebar>

			{/* Right: create / edit form */}
			<main className="flex-1 min-h-0 overflow-y-auto">
				{editingName !== null ? (
					<div className="flex flex-col gap-y-3 p-6 max-w-2xl">
						<div className="flex items-center justify-between">
							<h2 className="text-lg font-semibold">
								{editingName
									? t('subagent-sidebar.editTitle')
									: t('subagent-sidebar.newTitle')}
							</h2>
							<div className="flex items-center gap-x-2">
								{editingName && (
									<Button
										size="icon-sm"
										variant="destructive"
										onClick={() => {
											const target = agents.find(
												(a) => a.name === editingName,
											);
											if (target) setDeleteTarget(target);
										}}
									>
										<Trash2 />
									</Button>
								)}
								<Button size="icon-sm" variant="ghost" onClick={close}>
									<X />
								</Button>
							</div>
						</div>

						<Label className="text-xs">{t('subagent-sidebar.nameLabel')}</Label>
						<Input value={form.name} onChange={set('name')} placeholder="claude_code" />

						<Label className="text-xs">{t('subagent-sidebar.descLabel')}</Label>
						<Textarea value={form.description} onChange={set('description')} rows={2} />

						{editingName === '' && (
							<>
								<Label className="text-xs">{t('subagent-sidebar.typeLabel')}</Label>
								<select
									className="border rounded px-2 py-1 text-sm bg-background"
									value={form.kind}
									onChange={(e) =>
										setForm((f) => ({
											...f,
											kind: e.target.value as Kind,
										}))
									}
								>
									<option value="cli">{t('subagent-sidebar.typeCli')}</option>
									<option value="openai">
										{t('subagent-sidebar.typeOpenai')}
									</option>
								</select>
							</>
						)}

						{form.kind === 'cli' ? (
							<>
								<Label className="text-xs">
									{t('subagent-sidebar.commandLabel')}
								</Label>
								<Textarea
									value={form.command}
									onChange={set('command')}
									rows={2}
									placeholder="claude -p {prompt} --output-format stream-json --verbose --session-id {agent_id}"
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.resumeCommandLabel')}
								</Label>
								<Textarea
									value={form.resumeCommand}
									onChange={set('resumeCommand')}
									rows={2}
									placeholder="claude -p {prompt} --output-format stream-json --verbose --resume {agent_id}"
								/>
								<p
									className={
										stateful && !cliOk
											? 'text-[11px] text-destructive'
											: 'text-[11px] text-muted-foreground'
									}
								>
									{t('subagent-sidebar.resumeHint')}
								</p>

								<Label className="text-xs">
									{t('subagent-sidebar.idSourceLabel')}
								</Label>
								<select
									className="border rounded px-2 py-1 text-sm bg-background"
									value={form.idSource}
									onChange={(e) =>
										setForm((f) => ({
											...f,
											idSource: e.target.value as FormState['idSource'],
										}))
									}
								>
									<option value="provisioned">
										{t('subagent-sidebar.idSourceProvisioned')}
									</option>
									<option value="derived">
										{t('subagent-sidebar.idSourceDerived')}
									</option>
								</select>

								<Label className="text-xs">
									{t('subagent-sidebar.transcriptFormatLabel')}
								</Label>
								<select
									className="border rounded px-2 py-1 text-sm bg-background"
									value={form.transcriptFormat}
									onChange={(e) =>
										setForm((f) => ({
											...f,
											transcriptFormat: e.target
												.value as FormState['transcriptFormat'],
										}))
									}
								>
									<option value="text">
										{t('subagent-sidebar.transcriptText')}
									</option>
									<option value="codex_jsonl">
										{t('subagent-sidebar.transcriptCodex')}
									</option>
									<option value="claude_stream_json">
										{t('subagent-sidebar.transcriptClaude')}
									</option>
								</select>

								{derived && form.transcriptFormat === 'text' && (
									<>
										<Label className="text-xs">
											{t('subagent-sidebar.sessionIdPatternLabel')}
										</Label>
										<Input
											value={form.sessionIdPattern}
											onChange={set('sessionIdPattern')}
										/>
									</>
								)}

								<Label className="text-xs">
									{t('subagent-sidebar.outputPatternLabel')}
								</Label>
								<Input value={form.outputPattern} onChange={set('outputPattern')} />
								<p className="text-[11px] text-muted-foreground">
									{t('subagent-sidebar.patternHint')}
								</p>

								<Label className="text-xs">{t('subagent-sidebar.cwdLabel')}</Label>
								<Input value={form.cwd} onChange={set('cwd')} />

								<Label className="text-xs">{t('subagent-sidebar.envLabel')}</Label>
								<Textarea
									value={form.env}
									onChange={set('env')}
									rows={2}
									placeholder={'KEY=value'}
								/>
								<p className="text-[11px] text-muted-foreground">
									{t('subagent-sidebar.envHint')}
								</p>
							</>
						) : (
							<>
								<Label className="text-xs">
									{t('subagent-sidebar.baseUrlLabel')}
								</Label>
								<Input
									value={form.baseUrl}
									onChange={set('baseUrl')}
									placeholder="https://api.miromind.ai/v1"
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.modelLabel')}
								</Label>
								<Input
									value={form.model}
									onChange={set('model')}
									placeholder="mirothinker-1-7-deepresearch"
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.apiKeyLabel')}
								</Label>
								<Input
									type="password"
									value={form.apiKey}
									onChange={set('apiKey')}
									placeholder="sk_live_..."
								/>
								{editingName && (
									<p className="text-[11px] text-muted-foreground">
										{t('subagent-sidebar.apiKeyKeepHint')}
									</p>
								)}

								<Label className="text-xs">
									{t('subagent-sidebar.systemPromptLabel')}
								</Label>
								<Textarea
									value={form.systemPrompt}
									onChange={set('systemPrompt')}
									rows={2}
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.temperatureLabel')}
								</Label>
								<Input
									value={form.temperature}
									onChange={set('temperature')}
									inputMode="decimal"
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.maxTokensLabel')}
								</Label>
								<Input
									value={form.maxTokens}
									onChange={set('maxTokens')}
									inputMode="numeric"
								/>
							</>
						)}

						<Label className="text-xs">{t('subagent-sidebar.timeoutLabel')}</Label>
						<Input value={form.timeout} onChange={set('timeout')} inputMode="numeric" />
						<p className="text-[11px] text-muted-foreground">
							{t('subagent-sidebar.timeoutHint')}
						</p>

						<div className="mt-1 flex items-start gap-2">
							<Checkbox
								id="readsLocalFiles"
								checked={form.readsLocalFiles}
								onCheckedChange={(checked) =>
									setForm((f) => ({ ...f, readsLocalFiles: checked === true }))
								}
							/>
							<div className="grid gap-1">
								<Label htmlFor="readsLocalFiles" className="text-xs">
									{t('subagent-sidebar.readsLocalFilesLabel')}
								</Label>
								<p className="text-[11px] text-muted-foreground">
									{t('subagent-sidebar.readsLocalFilesHint')}
								</p>
							</div>
						</div>

						{nameCollides && (
							<p className="text-sm text-destructive">
								{t('subagent-sidebar.nameTaken', { name: form.name.trim() })}
							</p>
						)}
						{error && <p className="text-sm text-destructive">{error}</p>}
						<Button
							disabled={!canSubmit || submitting}
							onClick={submit}
							className="mt-1 self-start"
						>
							{submitting && <Loader2 className="size-3.5 animate-spin" />}
							{t('common.save')}
						</Button>
					</div>
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
