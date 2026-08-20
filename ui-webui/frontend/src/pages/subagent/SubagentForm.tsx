import type { Dispatch, SetStateAction } from 'react';

import type { RavenSubagentProbe, RavenSubagentTest, RavenThirdPartySubagent } from '@/api';
import { SubagentIcon } from '@/components/SubagentIcon';
import { SubagentStatusLine } from '@/components/SubagentStatus';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';
import { PRESET_DISPLAY, presetLabel } from '@/pages/subagent/catalog';
import type { BasicField } from '@/pages/subagent/catalog';
import type { FormState, Kind } from '@/pages/subagent/form';
import X from '~icons/solar/close-square-linear';
import Loader2 from '~icons/solar/refresh-linear';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

interface SubagentFormProps {
	form: FormState;
	setForm: Dispatch<SetStateAction<FormState>>;
	preset: RavenThirdPartySubagent | null;
	editingName: string | null;
	submitting: boolean;
	error: string | null;
	canSubmit: boolean;
	nameCollides: boolean;
	cliOk: boolean;
	stateful: boolean;
	derived: boolean;
	keyOk: boolean;
	hasStoredKey: boolean;
	probe: RavenSubagentProbe | null;
	probing: boolean;
	probesLoaded: boolean;
	onRefreshProbe: () => void;
	testing: boolean;
	testResult: RavenSubagentTest | null;
	canTest: boolean;
	onTest: () => void;
	onSubmit: () => void;
	onClose: () => void;
	onDelete: () => void;
}

const CUSTOM_OPENAI_BASIC_FIELDS: BasicField[] = ['baseUrl', 'model', 'apiKey'];

/**
 * Create/edit pane for a single subagent. Shows only what the selected agent
 * actually needs outside the `Advanced` disclosure: an openai `preset` shows
 * its own `basicFields` (see `catalog.ts`), falling back to the full openai
 * trio when it has no catalog entry or an empty `basicFields`, so a preset
 * added in Python alone still renders something actionable. A custom agent
 * (no `preset`) keeps its kind's full basic set - `command` for cli,
 * `baseUrl`/`model`/`apiKey` for openai - plus Name, and Type when it is new.
 */
export function SubagentForm({
	form,
	setForm,
	preset,
	editingName,
	submitting,
	error,
	canSubmit,
	nameCollides,
	cliOk,
	stateful,
	derived,
	keyOk,
	hasStoredKey,
	probe,
	probing,
	probesLoaded,
	onRefreshProbe,
	testing,
	testResult,
	canTest,
	onTest,
	onSubmit,
	onClose,
	onDelete,
}: SubagentFormProps) {
	const { t } = useTranslation();
	const set = (key: keyof FormState) => (e: { target: { value: string } }) =>
		setForm((f) => ({ ...f, [key]: e.target.value }));
	const presetStateful = preset?.kind === 'cli' && !!preset.resumeCommand;
	// A preset with no catalog entry (or one whose basicFields is empty) falls
	// back to the full trio rather than an empty list: an empty list would hide
	// baseUrl/model/apiKey - and the Required badge - inside the collapsed
	// Advanced disclosure, leaving a disabled Save with no visible cause.
	const catalogBasicFields = preset ? PRESET_DISPLAY[preset.name]?.basicFields : undefined;
	const openaiBasicFields: BasicField[] =
		catalogBasicFields && catalogBasicFields.length > 0
			? catalogBasicFields
			: CUSTOM_OPENAI_BASIC_FIELDS;
	const unsupported = preset ? (PRESET_DISPLAY[preset.name]?.unsupported ?? []) : [];

	const baseUrlField = (
		<>
			<Label className="text-xs">{t('subagent-sidebar.baseUrlLabel')}</Label>
			<Input
				value={form.baseUrl}
				onChange={set('baseUrl')}
				placeholder="https://api.miromind.ai/v1"
			/>
		</>
	);
	const modelField = (
		<>
			<Label className="text-xs">{t('subagent-sidebar.modelLabel')}</Label>
			<Input
				value={form.model}
				onChange={set('model')}
				placeholder="mirothinker-1-7-deepresearch"
			/>
		</>
	);
	const timeoutField = (
		<>
			<Label className="text-xs">{t('subagent-sidebar.timeoutLabel')}</Label>
			<Input value={form.timeout} onChange={set('timeout')} inputMode="numeric" />
			<p className="text-[11px] text-muted-foreground">{t('subagent-sidebar.timeoutHint')}</p>
		</>
	);
	const apiKeyField = (
		<>
			<div className="flex items-center gap-2">
				<Label className="text-xs">{t('subagent-sidebar.apiKeyLabel')}</Label>
				{!keyOk && (
					<Badge variant="outline" className="text-[10px] px-1 py-0">
						{t('subagent-sidebar.apiKeyRequiredTag')}
					</Badge>
				)}
			</div>
			<Input
				type="password"
				value={form.apiKey}
				onChange={set('apiKey')}
				placeholder="sk_live_..."
			/>
			{hasStoredKey && (
				<p className="text-[11px] text-muted-foreground">
					{t('subagent-sidebar.apiKeyKeepHint')}
				</p>
			)}
		</>
	);

	return (
		<div className="flex flex-col gap-y-3 p-6 max-w-2xl">
			<div className="flex items-center justify-between">
				<div className="flex min-w-0 items-center gap-x-2">
					{preset ? (
						<>
							<SubagentIcon type={preset.name} size={24} />
							<h2 className="truncate text-lg font-semibold">
								{presetLabel(preset.name, t)}
							</h2>
							<Badge variant="secondary" className="text-[10px] px-1 py-0">
								{preset.kind === 'cli'
									? t('subagent-sidebar.kindBadgeCli')
									: t('subagent-sidebar.kindBadgeOpenai')}
							</Badge>
							{presetStateful && (
								<Badge variant="outline" className="text-[10px] px-1 py-0">
									{t('subagent-sidebar.statefulBadge')}
								</Badge>
							)}
						</>
					) : (
						<h2 className="text-lg font-semibold">
							{editingName
								? t('subagent-sidebar.editTitle')
								: t('subagent-sidebar.newTitle')}
						</h2>
					)}
				</div>
				<div className="flex items-center gap-x-2">
					{editingName && (
						<Button size="icon-sm" variant="destructive" onClick={onDelete}>
							<Trash2 />
						</Button>
					)}
					<Button size="icon-sm" variant="ghost" onClick={onClose}>
						<X />
					</Button>
				</div>
			</div>
			{preset?.description && (
				<p className="text-sm text-muted-foreground">{preset.description}</p>
			)}

			{(editingName || preset) && (
				<SubagentStatusLine
					probe={probe}
					probing={probing}
					probesLoaded={probesLoaded}
					onRefresh={onRefreshProbe}
					showCliHint={form.kind === 'cli'}
				/>
			)}

			<Label className="text-xs">{t('subagent-sidebar.nameLabel')}</Label>
			<Input value={form.name} onChange={set('name')} placeholder="claude_code" />
			<p className="text-[11px] text-muted-foreground">{t('subagent-sidebar.nameHint')}</p>

			<Label className="text-xs">{t('subagent-sidebar.descLabel')}</Label>
			<Textarea
				value={form.description}
				onChange={set('description')}
				rows={3}
				placeholder={preset?.description}
			/>
			<p className="text-[11px] text-muted-foreground">{t('subagent-sidebar.descHint')}</p>

			{preset === null && editingName === '' && (
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
						<option value="openai">{t('subagent-sidebar.typeOpenai')}</option>
					</select>
				</>
			)}

			{form.kind === 'cli' ? (
				<>
					{preset === null && (
						<>
							<Label className="text-xs">{t('subagent-sidebar.commandLabel')}</Label>
							<Textarea
								value={form.command}
								onChange={set('command')}
								rows={2}
								placeholder="claude -p {prompt} --output-format stream-json --verbose --session-id {agent_id}"
							/>
						</>
					)}

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
				</>
			) : (
				<>
					{openaiBasicFields.includes('baseUrl') && baseUrlField}
					{openaiBasicFields.includes('model') && modelField}
					{openaiBasicFields.includes('apiKey') && apiKeyField}
				</>
			)}

			{nameCollides && (
				<p className="text-sm text-destructive">
					{t('subagent-sidebar.nameTaken', { name: form.name.trim() })}
				</p>
			)}
			{error && <p className="text-sm text-destructive">{error}</p>}
			<div className="mt-1 flex items-center gap-x-2">
				<Button disabled={!canSubmit || submitting} onClick={onSubmit}>
					{submitting && <Loader2 className="size-3.5 animate-spin" />}
					{t('common.save')}
				</Button>
				<Button variant="outline" disabled={!canTest || testing} onClick={onTest}>
					{testing && <Loader2 className="size-3.5 animate-spin" />}
					{testing ? t('subagent-sidebar.testRunning') : t('subagent-sidebar.testButton')}
				</Button>
			</div>
			<p className="text-muted-foreground text-[11px]">
				{!canTest
					? t('subagent-sidebar.testNewHint')
					: form.kind === 'cli'
						? t('subagent-sidebar.testCliWarning')
						: t('subagent-sidebar.testSavedHint')}
			</p>
			{testResult && (
				<div className="rounded-lg border px-3 py-2">
					<p
						className={
							testResult.ok
								? 'text-sm font-medium text-emerald-600 dark:text-emerald-400'
								: 'text-destructive text-sm font-medium'
						}
					>
						{testResult.ok
							? t('subagent-sidebar.testPassed')
							: t('subagent-sidebar.testFailed')}
						<span className="text-muted-foreground ml-2 font-normal tabular-nums">
							{(testResult.elapsedMs / 1000).toFixed(1)}s
						</span>
					</p>
					<p className="text-muted-foreground mt-1 text-[11px] break-words whitespace-pre-wrap">
						{testResult.detail}
					</p>
					{testResult.reply && (
						<p className="mt-2 text-xs break-words whitespace-pre-wrap">
							<span className="text-muted-foreground">
								{t('subagent-sidebar.testReplyLabel')}:{' '}
							</span>
							{testResult.reply}
						</p>
					)}
					{testResult.kind === 'openai' && (
						<p className="text-muted-foreground mt-1 text-[11px]">
							{t('subagent-sidebar.testOpenaiNote')}
						</p>
					)}
				</div>
			)}

			<details className="mt-2 rounded-lg border px-3 py-2">
				<summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
					{t('subagent-sidebar.advanced')}
				</summary>
				<div className="mt-3 flex flex-col gap-y-3">
					{form.kind === 'cli' && (
						<>
							{preset !== null && (
								<>
									<Label className="text-xs">
										{t('subagent-sidebar.commandLabel')}
									</Label>
									<Textarea
										value={form.command}
										onChange={set('command')}
										rows={2}
									/>
								</>
							)}

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

							<Label className="text-xs">{t('subagent-sidebar.idSourceLabel')}</Label>
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
								<option value="text">{t('subagent-sidebar.transcriptText')}</option>
								<option value="codex_jsonl">
									{t('subagent-sidebar.transcriptCodex')}
								</option>
								<option value="claude_stream_json">
									{t('subagent-sidebar.transcriptClaude')}
								</option>
								<option value="openclaw_json">
									{t('subagent-sidebar.transcriptOpenclaw')}
								</option>
								<option value="opencode_json">
									{t('subagent-sidebar.transcriptOpencode')}
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
						</>
					)}

					{form.kind === 'openai' && (
						<>
							{!openaiBasicFields.includes('baseUrl') && baseUrlField}
							{!openaiBasicFields.includes('model') && modelField}
							{!openaiBasicFields.includes('apiKey') && apiKeyField}

							{!unsupported.includes('systemPrompt') && (
								<>
									<Label className="text-xs">
										{t('subagent-sidebar.systemPromptLabel')}
									</Label>
									<Textarea
										value={form.systemPrompt}
										onChange={set('systemPrompt')}
										rows={2}
									/>
								</>
							)}

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

					{timeoutField}
				</div>
			</details>
		</div>
	);
}
