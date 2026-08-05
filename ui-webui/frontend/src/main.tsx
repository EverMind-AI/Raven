import { ThemeProvider } from 'next-themes';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import './index.css';
import './i18n';
import App from './App.tsx';
import { TooltipProvider } from '@/components/ui/tooltip.tsx';

createRoot(document.getElementById('root')!).render(
	<StrictMode>
		<ThemeProvider
			attribute="class"
			defaultTheme="light"
			enableSystem={false}
			disableTransitionOnChange
		>
			<TooltipProvider>
				<App />
			</TooltipProvider>
		</ThemeProvider>
	</StrictMode>,
);
