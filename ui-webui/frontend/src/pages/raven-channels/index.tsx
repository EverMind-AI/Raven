import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenConfigApi } from '@/api';
import type { RavenChannel } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';
import MessageSquare from '~icons/solar/chat-square-bold-duotone';
import Loader2 from '~icons/solar/refresh-linear';

function ChannelCard({ channel, onSaved }: { channel: RavenChannel; onSaved: () => void }) {
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [edits, setEdits] = useState<Record<string, string>>({});
	const [saving, setSaving] = useState(false);

	const fields = Object.entries(channel.specs).filter(([p]) => p !== 'enabled');

	const setField = async (name: string, fields_: Record<string, string>, note: string) => {
		setSaving(true);
		try {
			await ravenConfigApi.setChannel(name, fields_);
			toast.success(t('ravenChannels.restartHint', { note }));
			setEdits({});
			onSaved();
		} catch (e) {
			toast.error(
				t('ravenChannels.saveFailed', {
					error: e instanceof Error ? e.message : String(e),
				}),
			);
		} finally {
			setSaving(false);
		}
	};

	const toggle = () =>
		setField(
			channel.name,
			{ enabled: String(!channel.enabled) },
			channel.enabled
				? t('ravenChannels.toggledDisabled', { name: channel.name })
				: t('ravenChannels.toggledEnabled', { name: channel.name }),
		);

	const save = () => {
		// Send only changed fields; never resend a redacted secret sentinel.
		const changed: Record<string, string> = {};
		for (const [p, spec] of fields) {
			const v = edits[p];
			if (v === undefined) continue;
			if (spec.is_secret) {
				if (v !== '') changed[p] = v;
			} else if (v !== String(channel.config[p] ?? '')) {
				changed[p] = v;
			}
		}
		if (Object.keys(changed).length === 0) {
			toast.info(t('ravenChannels.noChanges'));
			return;
		}
		void setField(
			channel.name,
			changed,
			t('ravenChannels.savedChannel', { name: channel.name }),
		);
	};

	return (
		<div className="rounded-md border p-3">
			<div className="flex items-center justify-between">
				<div className="flex items-center gap-2">
					<span className="font-medium">{channel.name}</span>
					<Badge variant={channel.enabled ? 'default' : 'outline'}>
						{channel.enabled ? t('common.enabled') : t('common.disabled')}
					</Badge>
				</div>
				<div className="flex gap-1">
					<Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
						{open ? t('ravenChannels.hide') : t('ravenChannels.configure')}
					</Button>
					<Button size="sm" variant="outline" disabled={saving} onClick={toggle}>
						{channel.enabled ? t('ravenChannels.disable') : t('ravenChannels.enable')}
					</Button>
				</div>
			</div>
			{open && (
				<div className="mt-3 flex flex-col gap-3">
					{fields.map(([p, spec]) => (
						<div key={p} className="grid gap-1">
							<Label htmlFor={`${channel.name}-${p}`} className="text-xs">
								{p}
								{spec.required ? ' *' : ''}{' '}
								<span className="text-muted-foreground">({spec.type})</span>
							</Label>
							<Input
								id={`${channel.name}-${p}`}
								type={spec.is_secret ? 'password' : 'text'}
								value={
									edits[p] ??
									(spec.is_secret ? '' : String(channel.config[p] ?? ''))
								}
								placeholder={
									spec.is_secret
										? String(channel.config[p] ?? '')
										: spec.description || ''
								}
								onChange={(e) => setEdits({ ...edits, [p]: e.target.value })}
							/>
							{spec.description && (
								<span className="text-[11px] text-muted-foreground">
									{spec.description}
								</span>
							)}
						</div>
					))}
					<div>
						<Button size="sm" disabled={saving} onClick={save}>
							{saving && <Loader2 className="size-4 animate-spin" />}{' '}
							{t('common.save')}
						</Button>
					</div>
				</div>
			)}
		</div>
	);
}

/** Configure Raven's IM channels (P4). Config is applied on gateway restart. */
export function RavenChannelsPage() {
	const { t } = useTranslation();
	const [channels, setChannels] = useState<RavenChannel[]>([]);
	const [loading, setLoading] = useState(true);

	const load = useCallback(async () => {
		setLoading(true);
		try {
			const r = await ravenConfigApi.listChannels();
			setChannels(r.channels ?? []);
		} catch (e) {
			toast.error(
				t('ravenChannels.loadFailed', {
					error: e instanceof Error ? e.message : String(e),
				}),
			);
		} finally {
			setLoading(false);
		}
	}, [t]);

	useEffect(() => {
		void load();
	}, [load]);

	return (
		<div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
			<div className="flex items-center gap-3">
				<MessageSquare className="size-6" />
				<div>
					<div className="em-kicker">{t('ravenChannels.kicker')}</div>
					<h1 className="text-xl font-medium">
						<span className="em-accent">{t('ravenChannels.title')}</span>
					</h1>
					<p className="text-sm text-muted-foreground">{t('ravenChannels.subtitle')}</p>
				</div>
			</div>

			{loading ? (
				<div className="flex items-center gap-2 text-muted-foreground">
					<Loader2 className="size-4 animate-spin" /> {t('common.loading')}
				</div>
			) : (
				<div className="flex flex-col gap-2">
					{channels.map((c) => (
						<ChannelCard key={c.name} channel={c} onSaved={load} />
					))}
				</div>
			)}
		</div>
	);
}
