import { Onborda, OnbordaProvider } from 'onborda';
import { useMemo } from 'react';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom';

import { RouteError } from '@/components/error/RouteError';
import { AppLayout } from '@/components/layout/AppLayout';
import { buildChatTour } from '@/components/tour/chatTourSteps';
import { TourCard } from '@/components/tour/TourCard';
import { Toaster } from '@/components/ui/sonner';
import { UploadProvider } from '@/context/UploadContext';
import { useTranslation } from '@/i18n/useI18n';
import { ChatPage } from '@/pages/chat';
import { CredentialPage } from '@/pages/credential';
import { EverOSPage } from '@/pages/everos';
import { KnowledgePage } from '@/pages/knowledge';
import { RavenChannelsPage } from '@/pages/raven-channels';
import { RavenCronPage } from '@/pages/raven-cron';
import { RavenSkillsPage } from '@/pages/raven-skills';
import { SkillDetailPage } from '@/pages/raven-skills/detail';
import { SchedulePage } from '@/pages/schedule';
import { SettingsPage } from '@/pages/settings';
import { SubAgentsPage } from '@/pages/subagent';

const router = createBrowserRouter([
	{
		element: <AppLayout />,
		errorElement: <RouteError />,
		children: [
			{
				// Content-level boundary: a crash in a page replaces only
				// the Outlet area, so AppLayout (the icon rail / nav) stays
				// usable. The parent route keeps its own errorElement as a
				// last-resort catch-all for AppLayout/AppSidebar crashes.
				errorElement: <RouteError />,
				children: [
					{ path: '/', element: <Navigate to="/chat" replace /> },
					{
						path: '/chat/:agentId?/:sessionId?/:memberId?',
						element: <ChatPage />,
					},
					{ path: '/schedule', element: <SchedulePage /> },
					{ path: '/credential', element: <CredentialPage /> },
					{ path: '/knowledge', element: <KnowledgePage /> },
					{ path: '/knowledge/:kbId', element: <KnowledgePage /> },
					{ path: '/subagents', element: <SubAgentsPage /> },
					{ path: '/raven-cron', element: <RavenCronPage /> },
					{ path: '/raven-channels', element: <RavenChannelsPage /> },
						{ path: '/everos', element: <EverOSPage /> },
					{ path: '/raven-skills', element: <RavenSkillsPage /> },
					{ path: '/raven-skills/detail', element: <SkillDetailPage /> },
					{ path: '/settings', element: <SettingsPage /> },
				],
			},
		],
	},
]);

function App() {
	const { t } = useTranslation();
	const tours = useMemo(() => [buildChatTour(t)], [t]);

	return (
		<OnbordaProvider>
			<Onborda
				steps={tours}
				cardComponent={TourCard}
				shadowOpacity="0.6"
				shadowRgb="28,27,27"
				cardTransition={{ type: 'spring', duration: 0.4 }}
			>
				<UploadProvider>
					<RouterProvider router={router} />
				</UploadProvider>
				<Toaster position="top-right" />
			</Onborda>
		</OnbordaProvider>
	);
}

export default App;
