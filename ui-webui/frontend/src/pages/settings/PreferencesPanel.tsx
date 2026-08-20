import { useTheme } from 'next-themes';
import { useState } from 'react';
import { toast } from 'sonner';

import { defaultBaseUrl } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import i18n from '@/i18n';
import { useTranslation } from '@/i18n/useI18n';

/** Preferences tab of the Settings hub: appearance (theme / language) and the
 *  server address. The address is an optional override — left blank, the app
 *  falls back to the auto-detected service URL. */
export function PreferencesPanel() {
	const { t } = useTranslation();
	const { resolvedTheme, setTheme } = useTheme();
	const isZh = i18n.language.startsWith('zh');
	const [server, setServer] = useState(() => localStorage.getItem('server_url') ?? '');

	const saveConnection = () => {
		// A changed server shifts what the app connects to, and loaded pages hold
		// data fetched from the old one — reload rather than leaving a half-applied
		// state. Blank clears the override back to auto-detection.
		const changed = server !== (localStorage.getItem('server_url') ?? '');
		if (server.trim()) localStorage.setItem('server_url', server.trim());
		else localStorage.removeItem('server_url');
		toast.success(t('settings.prefSaved'));
		if (changed) setTimeout(() => window.location.reload(), 400);
	};

	return (
		<div className="h-full overflow-y-auto">
			<div className="mx-auto flex max-w-2xl flex-col gap-5 p-6">
				<div>
					<div className="em-kicker">{t('settings.title')}</div>
					<h1 className="text-xl font-medium">{t('settings.tabPreferences')}</h1>
				</div>

				<Card>
					<CardHeader>
						<CardTitle>{t('settings.prefAppearance')}</CardTitle>
					</CardHeader>
					<CardContent className="flex flex-col gap-4">
						<div className="flex items-center justify-between">
							<Label htmlFor="pref-dark">{t('settings.prefDarkMode')}</Label>
							<Switch
								id="pref-dark"
								checked={resolvedTheme === 'dark'}
								onCheckedChange={(v) => setTheme(v ? 'dark' : 'light')}
							/>
						</div>
						<div className="flex items-center justify-between">
							<Label>{t('settings.prefLanguage')}</Label>
							<Button
								variant="outline"
								size="sm"
								onClick={() => i18n.changeLanguage(isZh ? 'en' : 'zh')}
							>
								{isZh ? '中文' : 'English'}
							</Button>
						</div>
					</CardContent>
				</Card>

				<Card>
					<CardHeader>
						<CardTitle>{t('settings.prefConnection')}</CardTitle>
						<CardDescription>{t('settings.prefConnectionHint')}</CardDescription>
					</CardHeader>
					<CardContent className="flex flex-col gap-4">
						<div className="grid gap-1.5">
							<Label htmlFor="pref-server">{t('settings.prefServerUrl')}</Label>
							<Input
								id="pref-server"
								value={server}
								onChange={(e) => setServer(e.target.value)}
								placeholder={defaultBaseUrl()}
							/>
						</div>
						<div className="flex justify-end">
							<Button onClick={saveConnection}>{t('common.save')}</Button>
						</div>
					</CardContent>
				</Card>
			</div>
		</div>
	);
}
