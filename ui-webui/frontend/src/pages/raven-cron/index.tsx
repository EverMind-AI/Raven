import type { TFunction } from 'i18next';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenCronAddRequest, RavenCronJob } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';
import Plus from '~icons/solar/add-square-linear';
import CalendarClock from '~icons/solar/alarm-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';
import Trash2 from '~icons/solar/trash-bin-minimalistic-bold-duotone';

type Kind = 'cron' | 'every' | 'at';

interface FormState {
	name: string;
	kind: Kind;
	expr: string;
	everySeconds: string;
	at: string; // datetime-local
	message: string;
}

const EMPTY_FORM: FormState = {
	name: '',
	kind: 'cron',
	expr: '0 9 * * *',
	everySeconds: '',
	at: '',
	message: '',
};

function scheduleSummary(j: RavenCronJob, t: TFunction): string {
	const s = j.schedule;
	if (s.kind === 'cron') return t('ravenCron.summaryCron', { expr: s.expr ?? '' });
	if (s.kind === 'every')
		return t('ravenCron.summaryEvery', { seconds: Math.round((s.every_ms ?? 0) / 1000) });
	if (s.kind === 'at')
		return t('ravenCron.summaryAt', {
			time: s.at_ms ? new Date(s.at_ms).toLocaleString() : '?',
		});
	return s.kind;
}

/** Configure Raven's own scheduled tasks (cron) on the live gateway (P4). */
export function RavenCronPage() {
	const { t } = useTranslation();
	const [jobs, setJobs] = useState<RavenCronJob[]>([]);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [form, setForm] = useState<FormState | null>(null);

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const r = await ravenConfigApi.listCron();
			setJobs(r.jobs ?? []);
		} catch (e) {
			toast.error(
				t('ravenCron.loadFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setLoading(false);
		}
	}, [t]);

	useEffect(() => {
		void load();
	}, [load]);

	const add = async () => {
		if (!form || !form.name.trim() || !form.message.trim()) {
			toast.error(t('ravenCron.required'));
			return;
		}
		const body: RavenCronAddRequest = {
			name: form.name.trim(),
			message: form.message.trim(),
			schedule: { kind: form.kind },
		};
		if (form.kind === 'cron') body.schedule.expr = form.expr.trim();
		else if (form.kind === 'every') body.schedule.every_ms = Number(form.everySeconds) * 1000;
		else if (form.kind === 'at')
			body.schedule.at_ms = form.at ? new Date(form.at).getTime() : undefined;
		setSaving(true);
		try {
			await ravenConfigApi.addCron(body);
			toast.success(t('ravenCron.created'));
			setForm(null);
			await load();
		} catch (e) {
			toast.error(
				t('ravenCron.addFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setSaving(false);
		}
	};

	const remove = async (id: string) => {
		setSaving(true);
		try {
			await ravenConfigApi.removeCron(id);
			await load();
		} catch (e) {
			toast.error(
				t('ravenCron.deleteFailed', { error: e instanceof Error ? e.message : String(e) }),
			);
		} finally {
			setSaving(false);
		}
	};

	return (
		<div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
			<div className="flex items-center gap-3">
				<CalendarClock className="size-6" />
				<div>
					<div className="em-kicker">{t('ravenCron.kicker')}</div>
					<h1 className="text-xl font-medium">
						{t('ravenCron.titleLead')}
						<span className="em-accent">{t('ravenCron.titleAccent')}</span>
					</h1>
					<p className="text-sm text-muted-foreground">{t('ravenCron.subtitle')}</p>
				</div>
			</div>

			<div>
				<Button size="sm" onClick={() => setForm({ ...EMPTY_FORM })} disabled={saving}>
					<Plus className="size-4" /> {t('ravenCron.addTask')}
				</Button>
			</div>

			{loading ? (
				<div className="flex items-center gap-2 text-muted-foreground">
					<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
				</div>
			) : jobs.length === 0 ? (
				<p className="text-sm text-muted-foreground">{t('ravenCron.empty')}</p>
			) : (
				<ul className="flex flex-col gap-2">
					{jobs.map((j) => (
						<li
							key={j.id}
							className="flex items-center justify-between rounded-md border p-3"
						>
							<div className="flex flex-col gap-1">
								<div className="flex items-center gap-2">
									<span className="font-medium">{j.name}</span>
									<Badge variant="secondary">{scheduleSummary(j, t)}</Badge>
									{!j.enabled && (
										<Badge variant="outline">{t('common.disabled')}</Badge>
									)}
								</div>
								<span className="text-sm text-muted-foreground">{j.message}</span>
								{j.next_run_at_ms && (
									<span className="text-xs text-muted-foreground">
										{t('ravenCron.nextRun', {
											time: new Date(j.next_run_at_ms).toLocaleString(),
										})}
									</span>
								)}
							</div>
							<Button
								size="icon"
								variant="ghost"
								disabled={saving}
								onClick={() => remove(j.id)}
								aria-label={t('ravenCron.deleteTask', { name: j.name })}
							>
								<Trash2 className="size-4" />
							</Button>
						</li>
					))}
				</ul>
			)}

			{form && (
				<div className="flex flex-col gap-3 rounded-md border p-4">
					<div className="grid gap-1.5">
						<Label htmlFor="rc-name">{t('common.name')}</Label>
						<Input
							id="rc-name"
							value={form.name}
							onChange={(e) => setForm({ ...form, name: e.target.value })}
						/>
					</div>
					<div className="grid gap-1.5">
						<Label htmlFor="rc-kind">{t('ravenCron.kindLabel')}</Label>
						<select
							id="rc-kind"
							className="h-9 rounded-md border bg-transparent px-3 text-sm"
							value={form.kind}
							onChange={(e) => setForm({ ...form, kind: e.target.value as Kind })}
						>
							<option value="cron">{t('ravenCron.kindCron')}</option>
							<option value="every">{t('ravenCron.kindEvery')}</option>
							<option value="at">{t('ravenCron.kindAt')}</option>
						</select>
					</div>
					{form.kind === 'cron' && (
						<div className="grid gap-1.5">
							<Label htmlFor="rc-expr">{t('ravenCron.exprLabel')}</Label>
							<Input
								id="rc-expr"
								value={form.expr}
								onChange={(e) => setForm({ ...form, expr: e.target.value })}
								placeholder="0 9 * * *"
							/>
						</div>
					)}
					{form.kind === 'every' && (
						<div className="grid gap-1.5">
							<Label htmlFor="rc-every">{t('ravenCron.everyLabel')}</Label>
							<Input
								id="rc-every"
								type="number"
								value={form.everySeconds}
								onChange={(e) => setForm({ ...form, everySeconds: e.target.value })}
							/>
						</div>
					)}
					{form.kind === 'at' && (
						<div className="grid gap-1.5">
							<Label htmlFor="rc-at">{t('ravenCron.atLabel')}</Label>
							<Input
								id="rc-at"
								type="datetime-local"
								value={form.at}
								onChange={(e) => setForm({ ...form, at: e.target.value })}
							/>
						</div>
					)}
					<div className="grid gap-1.5">
						<Label htmlFor="rc-msg">{t('ravenCron.messageLabel')}</Label>
						<Input
							id="rc-msg"
							value={form.message}
							onChange={(e) => setForm({ ...form, message: e.target.value })}
							placeholder={t('ravenCron.messagePlaceholder')}
						/>
					</div>
					<div className="flex items-center gap-2">
						<Button size="sm" onClick={add} disabled={saving}>
							{saving && <Loader2 className="size-4 animate-spin" />}{' '}
							{t('ravenCron.submit')}
						</Button>
						<Button
							size="sm"
							variant="ghost"
							onClick={() => setForm(null)}
							disabled={saving}
						>
							{t('common.cancel')}
						</Button>
					</div>
				</div>
			)}
		</div>
	);
}
