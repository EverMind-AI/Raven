import { useSearchParams } from 'react-router-dom';

import { PreferencesPanel } from './PreferencesPanel';
import { SkillForgeSettingsPanel } from './SkillForgeSettingsPanel';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import { CredentialPage } from '@/pages/credential';
import { KnowledgePage } from '@/pages/knowledge';
import { RavenChannelsPage } from '@/pages/raven-channels';
import { RavenCronPage } from '@/pages/raven-cron';
import { SubAgentsPage } from '@/pages/subagent';
import IconAlarm from '~icons/solar/alarm-bold-duotone';
import IconBook from '~icons/solar/book-2-bold-duotone';
import IconChannels from '~icons/solar/chat-square-bold-duotone';
import IconCpu from '~icons/solar/cpu-bold-duotone';
import IconKey from '~icons/solar/key-bold-duotone';
import IconSkills from '~icons/solar/magic-stick-3-bold-duotone';
import IconSettings from '~icons/solar/settings-bold-duotone';

/**
 * Settings hub — one place for every config surface, replacing the scattered
 * rail entries. A left tab rail picks a section; the right pane embeds the
 * existing page component for that section (they are self-contained). Tab
 * selection lives in the ``?tab=`` query so links can deep-target a section.
 */
const TABS = [
	{ key: 'models', Icon: IconKey, Node: CredentialPage, labelKey: 'settings.tabModels' },
	{
		key: 'skills',
		Icon: IconSkills,
		Node: SkillForgeSettingsPanel,
		labelKey: 'settings.tabSkills',
	},
	{ key: 'knowledge', Icon: IconBook, Node: KnowledgePage, labelKey: 'settings.tabKnowledge' },
	{ key: 'subagents', Icon: IconCpu, Node: SubAgentsPage, labelKey: 'settings.tabSubagents' },
	{
		key: 'channels',
		Icon: IconChannels,
		Node: RavenChannelsPage,
		labelKey: 'settings.tabChannels',
	},
	{ key: 'cron', Icon: IconAlarm, Node: RavenCronPage, labelKey: 'settings.tabCron' },
	{
		key: 'preferences',
		Icon: IconSettings,
		Node: PreferencesPanel,
		labelKey: 'settings.tabPreferences',
	},
] as const;

export function SettingsPage() {
	const { t } = useTranslation();
	const [sp, setSp] = useSearchParams();
	const active = sp.get('tab') || 'models';
	const current = TABS.find((x) => x.key === active) ?? TABS[0];
	const Node = current.Node;

	return (
		<div className="flex h-full min-h-0">
			<aside className="hidden w-52 shrink-0 flex-col gap-1 border-r bg-muted/20 p-3 sm:flex">
				<div className="em-kicker mb-2 px-2">{t('settings.title')}</div>
				{TABS.map((tb) => (
					<button
						key={tb.key}
						onClick={() => setSp({ tab: tb.key })}
						className={cn(
							'flex items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition',
							active === tb.key
								? 'bg-background font-semibold text-foreground shadow-sm ring-1 ring-inset ring-border'
								: 'text-muted-foreground hover:bg-muted/60',
						)}
					>
						<tb.Icon className="size-4" />
						<span>{t(tb.labelKey)}</span>
					</button>
				))}
			</aside>
			<div className="min-w-0 flex-1 overflow-hidden">
				<Node />
			</div>
		</div>
	);
}
