import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenLocalDir, RavenSkillForge } from '@/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useTranslation } from '@/i18n/useI18n';
import Plus from '~icons/solar/add-square-linear';
import Loader2 from '~icons/solar/refresh-linear';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

interface FormState {
	enabled: boolean;
	weightLocal: string;
	weightEveros: string;
	weightHub: string;
	hubEndpoint: string;
	hubApiKey: string;
	hubMinSafety: string;
	hubTimeoutS: string;
	everosEnabled: boolean;
	localDirs: RavenLocalDir[];
}

function toForm(sf: RavenSkillForge): FormState {
	const w = sf.router?.weights ?? {};
	const h = sf.router?.hub ?? {};
	return {
		enabled: sf.enabled,
		weightLocal: w.local != null ? String(w.local) : '',
		weightEveros: w.everos != null ? String(w.everos) : '',
		weightHub: w.hub != null ? String(w.hub) : '',
		hubEndpoint: h.endpoint ?? '',
		hubApiKey: h.apiKey ?? '',
		hubMinSafety: h.minSafety != null ? String(h.minSafety) : '',
		hubTimeoutS: h.timeoutS != null ? String(h.timeoutS) : '',
		everosEnabled: sf.everos?.enabled ?? true,
		localDirs: sf.localDirs ?? [],
	};
}

function toFields(f: FormState): Partial<RavenSkillForge> {
	return {
		enabled: f.enabled,
		router: {
			weights: {
				local: f.weightLocal === '' ? undefined : Number(f.weightLocal),
				everos: f.weightEveros === '' ? undefined : Number(f.weightEveros),
				hub: f.weightHub === '' ? undefined : Number(f.weightHub),
			},
			hub: {
				endpoint: f.hubEndpoint || undefined,
				apiKey: f.hubApiKey || null,
				minSafety: f.hubMinSafety === '' ? undefined : Number(f.hubMinSafety),
				timeoutS: f.hubTimeoutS === '' ? undefined : Number(f.hubTimeoutS),
			},
		},
		everos: { enabled: f.everosEnabled },
		localDirs: f.localDirs,
	};
}

/**
 * SkillForge configuration (retrieval weights / EverOS source / local skill
 * dirs), now a tab in the Settings hub instead of a dialog buried in the skill
 * gallery. Self-contained: loads and saves its own config.
 */
export function SkillForgeSettingsPanel() {
	const { t } = useTranslation();
	const [form, setForm] = useState<FormState | null>(null);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [restartRequired, setRestartRequired] = useState(false);

	useEffect(() => {
		let cancelled = false;
		void (async () => {
			try {
				const { skillforge } = await ravenConfigApi.skills.get();
				if (!cancelled) setForm(toForm(skillforge));
			} catch (e) {
				toast.error(
					`${t('ravenSkills.loadFailed')}: ${e instanceof Error ? e.message : String(e)}`,
				);
			} finally {
				if (!cancelled) setLoading(false);
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [t]);

	const save = async () => {
		if (!form) return;
		setSaving(true);
		try {
			const r = await ravenConfigApi.skills.set(toFields(form));
			setRestartRequired(!!r.restart_required);
			toast.success(t('ravenSkills.saveSuccess'));
		} catch (e) {
			toast.error(
				`${t('ravenSkills.saveFailed')}: ${e instanceof Error ? e.message : String(e)}`,
			);
		} finally {
			setSaving(false);
		}
	};

	const updateDir = (idx: number, patch: Partial<RavenLocalDir>) => {
		if (!form) return;
		setForm({
			...form,
			localDirs: form.localDirs.map((d, i) => (i === idx ? { ...d, ...patch } : d)),
		});
	};
	const removeDir = (idx: number) => {
		if (!form) return;
		setForm({ ...form, localDirs: form.localDirs.filter((_, i) => i !== idx) });
	};
	const addDir = () => {
		if (!form) return;
		setForm({
			...form,
			localDirs: [
				...form.localDirs,
				{ path: '', enabled: true, name: null, alwaysEnabled: true },
			],
		});
	};

	if (loading || !form) {
		return (
			<div className="flex items-center gap-2 p-6 text-muted-foreground">
				<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
			</div>
		);
	}

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-3xl flex-col gap-5 p-6">
				<div>
					<div className="em-kicker">{t('settings.title')}</div>
					<h1 className="text-xl font-medium">{t('ravenSkills.configTitle')}</h1>
					<p className="mt-1 text-sm text-muted-foreground">
						{t('ravenSkills.configHint')}
					</p>
				</div>

				{restartRequired && (
					<div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-600 dark:text-amber-400">
						{t('ravenSkills.restartBanner')}
					</div>
				)}

				<Card>
					<CardContent className="flex items-center justify-between">
						<Label htmlFor="sf-enabled">{t('ravenSkills.enabled')}</Label>
						<Switch
							id="sf-enabled"
							checked={form.enabled}
							onCheckedChange={(v) => setForm({ ...form, enabled: !!v })}
						/>
					</CardContent>
				</Card>

				<Card>
					<CardHeader>
						<CardTitle>{t('ravenSkills.weightsTitle')}</CardTitle>
						<CardDescription>{t('ravenSkills.weightsDescription')}</CardDescription>
					</CardHeader>
					<CardContent className="grid grid-cols-3 gap-3">
						{(
							[
								['sf-w-local', 'weightLocal', 'weightLocal'],
								['sf-w-everos', 'weightEveros', 'weightEveros'],
								['sf-w-hub', 'weightHub', 'weightHub'],
							] as const
						).map(([id, key, labelKey]) => (
							<div key={id} className="grid gap-1.5">
								<Label htmlFor={id}>{t(`ravenSkills.${labelKey}`)}</Label>
								<Input
									id={id}
									type="number"
									min={0}
									max={1}
									step={0.1}
									value={form[key]}
									onChange={(e) => setForm({ ...form, [key]: e.target.value })}
								/>
							</div>
						))}
					</CardContent>
				</Card>

				<Card>
					<CardContent className="flex items-center justify-between">
						<Label htmlFor="sf-everos-enabled">{t('ravenSkills.everosEnabled')}</Label>
						<Switch
							id="sf-everos-enabled"
							checked={form.everosEnabled}
							onCheckedChange={(v) => setForm({ ...form, everosEnabled: !!v })}
						/>
					</CardContent>
				</Card>

				<Card>
					<CardHeader>
						<CardTitle>{t('ravenSkills.localDirsTitle')}</CardTitle>
						<CardDescription>{t('ravenSkills.localDirsDescription')}</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-col gap-3">
						{form.localDirs.length === 0 ? (
							<p className="text-sm text-muted-foreground">
								{t('ravenSkills.localDirsEmpty')}
							</p>
						) : (
							form.localDirs.map((d, idx) => (
								<div
									key={idx}
									className="flex items-center gap-2 rounded-md border p-2"
								>
									<div className="grid flex-1 gap-1.5">
										<Label htmlFor={`sf-dir-path-${idx}`} className="text-xs">
											{t('ravenSkills.localDirsPath')}
										</Label>
										<Input
											id={`sf-dir-path-${idx}`}
											value={d.path}
											placeholder="/path/to/skills"
											onChange={(e) =>
												updateDir(idx, { path: e.target.value })
											}
										/>
									</div>
									<div className="grid gap-1.5">
										<Label htmlFor={`sf-dir-name-${idx}`} className="text-xs">
											{t('ravenSkills.localDirsName')}
										</Label>
										<Input
											id={`sf-dir-name-${idx}`}
											value={d.name ?? ''}
											onChange={(e) =>
												updateDir(idx, { name: e.target.value || null })
											}
										/>
									</div>
									<div className="flex flex-col items-center gap-1.5">
										<Label
											htmlFor={`sf-dir-enabled-${idx}`}
											className="text-xs"
										>
											{t('ravenSkills.localDirsEnabled')}
										</Label>
										<Switch
											id={`sf-dir-enabled-${idx}`}
											checked={d.enabled}
											onCheckedChange={(v) =>
												updateDir(idx, { enabled: !!v })
											}
										/>
									</div>
									<Button
										size="icon"
										variant="ghost"
										onClick={() => removeDir(idx)}
										aria-label={t('ravenSkills.localDirsRemove')}
									>
										<Trash2 className="size-4" />
									</Button>
								</div>
							))
						)}
						<div>
							<Button size="sm" variant="outline" onClick={addDir}>
								<Plus className="size-4" /> {t('ravenSkills.localDirsAdd')}
							</Button>
						</div>
					</CardContent>
				</Card>

				<div className="flex justify-end border-t pt-4">
					<Button onClick={save} disabled={saving}>
						{saving && <Loader2 className="size-4 animate-spin" />} {t('common.save')}
					</Button>
				</div>
			</div>
		</div>
	);
}
