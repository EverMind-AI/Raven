import type { TFunction } from 'i18next';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';

import { ravenProvidersApi } from '@/api';
import type { RavenProviderSummary, RavenProviderUpdate } from '@/api';
import { ProviderIcon } from '@/components/ProviderIcon';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Sidebar,
	SidebarContent,
	SidebarGroup,
	SidebarGroupContent,
	SidebarGroupLabel,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Skeleton } from '@/components/ui/skeleton';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import Plus from '~icons/solar/add-square-linear';
import IconCheck from '~icons/solar/check-circle-bold';

// `custom` is a generic OpenAI-compatible endpoint; label it as such.
function labelFor(p: RavenProviderSummary, t: TFunction): string {
	if (p.name === 'custom') return t('credential.openaiCompatible');
	return p.displayName;
}

function kindOf(p: RavenProviderSummary): 'gateway' | 'local' | 'oauth' | 'direct' {
	if (p.isOauth) return 'oauth';
	if (p.isLocal) return 'local';
	if (p.isGateway) return 'gateway';
	return 'direct';
}

const GROUP_ORDER: Array<{ kind: ReturnType<typeof kindOf>; titleKey: string }> = [
	{ kind: 'direct', titleKey: 'credential.groupDirect' },
	{ kind: 'gateway', titleKey: 'credential.groupGateway' },
	{ kind: 'local', titleKey: 'credential.groupLocal' },
	{ kind: 'oauth', titleKey: 'credential.groupOauth' },
];

export function CredentialPage() {
	const { t } = useTranslation();
	const [providers, setProviders] = useState<RavenProviderSummary[]>([]);
	const [loading, setLoading] = useState(true);
	const [selected, setSelected] = useState<string | null>(null);

	const refresh = useCallback(async () => {
		setLoading(true);
		try {
			const res = await ravenProvidersApi.list();
			setProviders(res.providers);
			setSelected((cur) => cur ?? res.providers[0]?.name ?? null);
		} catch {
			// client.ts already toasts on error
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refresh();
	}, [refresh]);

	const configured = providers.filter((p) => p.configured);
	const current = providers.find((p) => p.name === selected) ?? null;

	return (
		<div className="flex h-full">
			<Sidebar collapsible="none" className="w-72 border-r">
				<SidebarHeader className="gap-1 px-4 pt-4">
					<div className="em-kicker">{t('credential.kicker')}</div>
					<h1 className="text-lg font-medium">
						<span className="em-accent">{t('credential.title')}</span>
					</h1>
					<p className="text-sm text-muted-foreground">{t('credential.subtitle')}</p>
				</SidebarHeader>
				<SidebarContent>
					{loading ? (
						<div className="flex flex-col gap-2 p-4">
							{Array.from({ length: 6 }).map((_, i) => (
								<Skeleton key={i} className="h-8 rounded" />
							))}
						</div>
					) : (
						<>
							{configured.length > 0 && (
								<SidebarGroup>
									<SidebarGroupLabel className="justify-between">
										<span>{t('credential.configured')}</span>
										<span className="text-gold font-semibold tabular-nums">
											{configured.length}
										</span>
									</SidebarGroupLabel>
									<SidebarGroupContent>
										<SidebarMenu>
											{configured.map((p) => (
												<SidebarMenuItem key={p.name}>
													<SidebarMenuButton
														isActive={selected === p.name}
														onClick={() => setSelected(p.name)}
													>
														<ProviderIcon type={p.name} size={18} />
														<span className="min-w-0 flex-1 truncate">
															{labelFor(p, t)}
														</span>
													</SidebarMenuButton>
												</SidebarMenuItem>
											))}
										</SidebarMenu>
									</SidebarGroupContent>
								</SidebarGroup>
							)}
							{GROUP_ORDER.map(({ kind, titleKey }) => {
								const rows = providers.filter(
									(p) => kindOf(p) === kind && !p.configured,
								);
								if (rows.length === 0) return null;
								return (
									<SidebarGroup key={kind}>
										<SidebarGroupLabel>{t(titleKey)}</SidebarGroupLabel>
										<SidebarGroupContent>
											<SidebarMenu>
												{rows.map((p) => (
													<SidebarMenuItem key={p.name}>
														<SidebarMenuButton
															isActive={selected === p.name}
															onClick={() => setSelected(p.name)}
														>
															<ProviderIcon type={p.name} size={18} />
															<span className="min-w-0 flex-1 truncate">
																{labelFor(p, t)}
															</span>
														</SidebarMenuButton>
													</SidebarMenuItem>
												))}
											</SidebarMenu>
										</SidebarGroupContent>
									</SidebarGroup>
								);
							})}
						</>
					)}
				</SidebarContent>
			</Sidebar>
			<div className="min-w-0 flex-1 overflow-auto p-6">
				{current ? (
					<ProviderForm key={current.name} provider={current} onSaved={refresh} />
				) : (
					<div className="flex h-full items-center justify-center text-muted-foreground">
						{t('credential.selectProvider')}
					</div>
				)}
			</div>
		</div>
	);
}

interface ProviderFormProps {
	provider: RavenProviderSummary;
	onSaved: () => void | Promise<void>;
}

function ProviderForm({ provider, onSaved }: ProviderFormProps) {
	const { t } = useTranslation();
	// Pre-fill the base URL with the registry default when the user hasn't set
	// their own, so non-technical users see a working address instead of an empty
	// box (providers with no fixed endpoint — e.g. official OpenAI — stay blank).
	const initialBase = () => provider.apiBase ?? provider.defaultApiBase ?? '';
	const [apiKey, setApiKey] = useState('');
	const [apiBase, setApiBase] = useState(initialBase);
	// `models` = the ids the user has selected (what gets saved). `options` = the
	// chips shown to pick from — the curated shortlist plus any custom id added.
	const [models, setModels] = useState<string[]>(provider.models);
	const [options, setOptions] = useState<string[]>(provider.commonModels);
	const [newModel, setNewModel] = useState('');
	const [saving, setSaving] = useState(false);

	useEffect(() => {
		setApiKey('');
		setApiBase(provider.apiBase ?? provider.defaultApiBase ?? '');
		setModels(provider.models);
		setOptions(provider.commonModels);
		setNewModel('');
	}, [provider]);

	const save = async () => {
		setSaving(true);
		try {
			const body: RavenProviderUpdate = {
				apiBase: apiBase || null,
				models,
			};
			// Only send api_key when the user typed a new one (avoid clobbering).
			if (apiKey) body.apiKey = apiKey;
			await ravenProvidersApi.update(provider.name, body);
			toast.success(t('credential.savedToast', { provider: labelFor(provider, t) }));
			await onSaved();
		} catch {
			// client.ts toasts
		} finally {
			setSaving(false);
		}
	};

	const toggleModel = (m: string) =>
		setModels((ms) => (ms.includes(m) ? ms.filter((x) => x !== m) : [...ms, m]));

	// Power-user escape hatch: add an id the shortlist doesn't have, then select
	// it. It joins `options` so it renders as a chip like the curated ones.
	const addModel = () => {
		const m = newModel.trim();
		if (m) {
			setOptions((o) => (o.includes(m) ? o : [...o, m]));
			setModels((ms) => (ms.includes(m) ? ms : [...ms, m]));
		}
		setNewModel('');
	};

	// The base URL is mandatory when there is no working default the user could
	// fall back to: an explicit `requiresApiBase` provider (Azure, custom), or a
	// local deployment whose address only the user knows (vLLM with no default).
	// A local provider that ships a default (Ollama → localhost:11434) is exempt.
	const needsBase = provider.requiresApiBase || (provider.isLocal && !provider.defaultApiBase);

	return (
		<div className="mx-auto flex max-w-2xl flex-col gap-6">
			<div className="flex items-center gap-3">
				<ProviderIcon type={provider.name} size={32} />
				<h2 className="text-xl font-semibold">{labelFor(provider, t)}</h2>
			</div>

			{provider.isOauth ? (
				<div className="rounded-lg border bg-muted/40 p-4 text-sm text-muted-foreground">
					{t('credential.oauthRunPrefix')}{' '}
					<code>raven provider login {provider.name}</code>{' '}
					{t('credential.oauthRunSuffix', { status: provider.apiKeyRedacted })}
				</div>
			) : (
				<div className="flex flex-col gap-2">
					<Label htmlFor="apiKey">{t('credential.apiKeyLabel')}</Label>
					<Input
						id="apiKey"
						type="password"
						placeholder={
							provider.isLocal
								? t('credential.apiKeyLocalPlaceholder')
								: provider.apiKeyRedacted === '****set****'
									? t('credential.apiKeyKeepPlaceholder')
									: t('credential.apiKeyEnterPlaceholder', {
											name:
												provider.envKey ||
												t('credential.apiKeyFallbackName'),
										})
						}
						value={apiKey}
						onChange={(e) => setApiKey(e.target.value)}
					/>
				</div>
			)}

			<div className="flex flex-col gap-2">
				<Label htmlFor="apiBase">
					{t('credential.baseUrlLabel')}
					{needsBase && (
						<span className="ml-1 text-destructive">
							* {t('credential.baseUrlRequiredTag')}
						</span>
					)}
				</Label>
				<Input
					id="apiBase"
					placeholder={provider.defaultApiBase || t('credential.baseUrlPlaceholder')}
					value={apiBase}
					onChange={(e) => setApiBase(e.target.value)}
				/>
				{needsBase ? (
					<p className="text-xs text-muted-foreground">
						{provider.isLocal
							? t('credential.baseUrlLocalHint')
							: t('credential.baseUrlRequiredHint')}
					</p>
				) : (
					!provider.defaultApiBase &&
					!provider.isLocal && (
						<p className="text-xs text-muted-foreground">
							{t('credential.baseUrlOptionalHint')}
						</p>
					)
				)}
			</div>

			<div className="flex flex-col gap-2">
				<Label>{t('credential.modelsLabel')}</Label>
				<p className="text-xs text-muted-foreground">{t('credential.modelsPickHint')}</p>
				{options.length > 0 ? (
					<div className="flex flex-wrap gap-2">
						{options.map((m) => {
							const on = models.includes(m);
							const isDefault = m === provider.defaultModel;
							return (
								<button
									key={m}
									type="button"
									onClick={() => toggleModel(m)}
									aria-pressed={on}
									className={cn(
										'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm transition-colors',
										on
											? 'border-gold bg-gold/15 text-foreground'
											: 'border-border text-muted-foreground hover:border-gold/50 hover:text-foreground',
									)}
								>
									{on && <IconCheck className="size-3.5 text-gold" />}
									<span>{m}</span>
									{isDefault && (
										<Badge
											variant="secondary"
											className="px-1 py-0 text-[10px]"
										>
											{t('credential.recommended')}
										</Badge>
									)}
								</button>
							);
						})}
					</div>
				) : (
					<span className="text-sm text-muted-foreground">
						{t('credential.noModelOptions')}
					</span>
				)}

				<details className="mt-1 text-sm">
					<summary className="cursor-pointer text-muted-foreground hover:text-foreground">
						{t('credential.addCustomModel')}
					</summary>
					<div className="mt-2 flex gap-2">
						<Input
							placeholder={
								provider.defaultModel || t('credential.modelNamePlaceholder')
							}
							value={newModel}
							onChange={(e) => setNewModel(e.target.value)}
							onKeyDown={(e) => {
								if (e.key === 'Enter') {
									e.preventDefault();
									addModel();
								}
							}}
						/>
						<Button type="button" variant="outline" onClick={addModel}>
							<Plus className="size-4" /> {t('common.add')}
						</Button>
					</div>
				</details>
			</div>

			<div>
				<Button onClick={save} disabled={saving}>
					{saving ? t('common.saving') : t('common.save')}
				</Button>
			</div>
		</div>
	);
}
