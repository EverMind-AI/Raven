import { useTheme } from 'next-themes';
import { useOnborda } from 'onborda';
import type { ReactNode } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';

import RavenLogo from '@/assets/images/raven_logo.svg?react';
import { CHAT_TOUR_NAME } from '@/components/tour/chatTourSteps';
import { Button } from '@/components/ui/button';
import {
	Sidebar,
	SidebarContent,
	SidebarFooter,
	SidebarGroup,
	SidebarGroupContent,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
	SidebarSeparator,
	useSidebar,
} from '@/components/ui/sidebar';
import i18n from '@/i18n';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import IconAlarm from '~icons/solar/alarm-bold-duotone';
import IconBook from '~icons/solar/book-2-bold-duotone';
import IconCalendar from '~icons/solar/calendar-bold-duotone';
import IconChat from '~icons/solar/chat-round-dots-bold-duotone';
import IconChannels from '~icons/solar/chat-square-bold-duotone';
import IconCompass from '~icons/solar/compass-bold-duotone';
import IconCpu from '~icons/solar/cpu-bold-duotone';
import IconEverOS from '~icons/solar/database-bold-duotone';
import IconGlobal from '~icons/solar/global-bold-duotone';
import IconKey from '~icons/solar/key-bold-duotone';
import IconSkills from '~icons/solar/magic-stick-3-bold-duotone';
import IconMoon from '~icons/solar/moon-bold-duotone';
import IconSettings from '~icons/solar/settings-bold-duotone';
import IconPanel from '~icons/solar/siderbar-linear';
import IconSun from '~icons/solar/sun-2-bold-duotone';

/**
 * One rail entry. Collapsed it is the bare icon with `label` as its tooltip;
 * expanded it is icon + label and the tooltip stands down (SidebarMenuButton
 * hides it whenever the sidebar is not collapsed).
 *
 * @param props.icon - Rendered icon element.
 * @param props.label - Tooltip text when collapsed, row text when expanded.
 * @param props.isActive - Whether this entry matches the current route.
 * @param props.onClick - Navigation handler.
 * @returns The menu item JSX.
 */
function RailItem({
	icon,
	label,
	isActive,
	onClick,
}: {
	icon: ReactNode;
	label: string;
	isActive?: boolean;
	onClick: () => void;
}) {
	const { state } = useSidebar();

	return (
		<SidebarMenuItem>
			<SidebarMenuButton
				tooltip={label}
				isActive={isActive}
				onClick={onClick}
				className="px-2"
			>
				{icon}
				{state === 'expanded' && <span>{label}</span>}
			</SidebarMenuButton>
		</SidebarMenuItem>
	);
}

/**
 * The app-wide navigation rail. It stays `collapsible="none"` — the shadcn
 * `icon` mode would take it out of normal flow and position it `fixed`, which
 * the chat page's own in-flow sidebar sits directly beside — so the two widths
 * are driven off the provider's `state` by hand instead.
 *
 * @returns The rail JSX.
 */
export function AppSidebar() {
	const navigate = useNavigate();
	const location = useLocation();
	const { t } = useTranslation();
	const { startOnborda } = useOnborda();
	const { resolvedTheme, setTheme } = useTheme();
	// `open`/`setOpen` rather than `toggleSidebar`: the latter drives the mobile
	// Sheet, which a `collapsible="none"` rail never renders, so on a narrow
	// viewport the button would look dead.
	const { state, open, setOpen } = useSidebar();
	const expanded = state === 'expanded';

	const handleStartTour = () => {
		if (!location.pathname.startsWith('/chat')) {
			// Page not mounted yet — leave a flag, navigate, and let the
			// ChatTourController auto-trigger after ChatPage mounts.
			sessionStorage.setItem('force_tour', '1');
			navigate('/chat');
		} else {
			startOnborda(CHAT_TOUR_NAME);
		}
	};

	const handleToggleLanguage = () => {
		const next = i18n.language.startsWith('zh') ? 'en' : 'zh';
		i18n.changeLanguage(next);
	};

	return (
		<Sidebar
			collapsible="none"
			className={cn(
				'app-nav-rail border-r transition-[width] duration-200 ease-linear',
				expanded ? 'w-(--sidebar-width)!' : 'w-[calc(var(--sidebar-width-icon)+1px)]!',
			)}
		>
			<SidebarHeader>
				<div
					className={cn(
						'flex gap-1 mt-2',
						expanded ? 'h-12 flex-row items-center px-1' : 'flex-col items-center',
					)}
				>
					<RavenLogo className="size-8 shrink-0" />
					{expanded && <span className="truncate text-sm font-semibold">Raven</span>}
					<Button
						variant="ghost"
						size="icon-sm"
						className={expanded ? 'ml-auto' : ''}
						tooltip={expanded ? t('nav.collapse') : t('nav.expand')}
						onClick={() => setOpen(!open)}
					>
						<IconPanel />
					</Button>
				</div>
			</SidebarHeader>
			<SidebarContent>
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<RailItem
								icon={<IconChat />}
								label={t('common.chat')}
								isActive={
									location.pathname === '/chat' ||
									location.pathname.startsWith('/chat/')
								}
								onClick={() => navigate('/chat')}
							/>
							<RailItem
								icon={<IconCalendar />}
								label={t('common.schedule')}
								isActive={location.pathname === '/schedule'}
								onClick={() => navigate('/schedule')}
							/>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
				<SidebarSeparator />
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<RailItem
								icon={<IconKey />}
								label={t('common.credential')}
								isActive={location.pathname === '/credential'}
								onClick={() => navigate('/credential')}
							/>
							<RailItem
								icon={<IconBook />}
								label={t('common.knowledge')}
								isActive={location.pathname === '/knowledge'}
								onClick={() => navigate('/knowledge')}
							/>
							<RailItem
								icon={<IconCpu className="icon-flip" />}
								label={t('common.subagents')}
								isActive={location.pathname === '/subagents'}
								onClick={() => navigate('/subagents')}
							/>
							<RailItem
								icon={<IconAlarm />}
								label={t('nav.ravenCron')}
								isActive={location.pathname === '/raven-cron'}
								onClick={() => navigate('/raven-cron')}
							/>
							<RailItem
								icon={<IconChannels />}
								label={t('nav.ravenChannels')}
								isActive={location.pathname === '/raven-channels'}
								onClick={() => navigate('/raven-channels')}
							/>
							<RailItem
								icon={<IconEverOS />}
								label={t('nav.everOS')}
								isActive={location.pathname === '/everos'}
								onClick={() => navigate('/everos')}
							/>
							<RailItem
								icon={<IconSkills />}
								label={t('nav.ravenSkills')}
								isActive={location.pathname.startsWith('/raven-skills')}
								onClick={() => navigate('/raven-skills')}
							/>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
			</SidebarContent>
			<SidebarFooter>
				<SidebarMenu>
					<RailItem
						icon={<IconGlobal className="icon-flip" />}
						label={
							i18n.language.startsWith('zh')
								? t('common.switchToEn')
								: t('common.switchToZh')
						}
						onClick={handleToggleLanguage}
					/>
					<RailItem
						icon={<IconCompass />}
						label={t('tour.trigger')}
						onClick={handleStartTour}
					/>
					<RailItem
						icon={resolvedTheme === 'dark' ? <IconSun /> : <IconMoon />}
						label={t('common.toggleTheme')}
						onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
					/>
					<RailItem
						icon={<IconSettings />}
						label={t('common.settings')}
						isActive={location.pathname === '/settings'}
						onClick={() => navigate('/settings')}
					/>
				</SidebarMenu>
			</SidebarFooter>
		</Sidebar>
	);
}
