import path from 'path';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import Icons from 'unplugin-icons/vite';
import { defineConfig } from 'vite';
import svgr from 'vite-plugin-svgr';

export default defineConfig({
	plugins: [react(), tailwindcss(), svgr(), Icons({ compiler: 'jsx', jsx: 'react' })],
	resolve: {
		alias: {
			'@': path.resolve(__dirname, './src'),
			'next/navigation': path.resolve(__dirname, './src/lib/next-navigation-shim.ts'),
			// Browser deps (e.g. `mime-types`) that `require('path')` resolve to
			// a real implementation instead of Vite's externalized stub, which
			// warns "Cannot access path.extname in client code". See path-shim.ts.
			path: path.resolve(__dirname, './src/lib/path-shim.ts'),
		},
	},
	optimizeDeps: {
		include: ['mime-types'],
	},
});
