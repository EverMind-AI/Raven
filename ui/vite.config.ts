import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Builds the island bundle as one classic IIFE script. Final assembly stays
// with ui/build.py: it inlines this output at the /*__MODERN__*/ marker ahead
// of the legacy script, keeping the one-file dist contract the wheel and
// `raven serve` depend on. When the last legacy part is gone this config
// grows the html entry and build.py retires.
export default defineConfig(({ command }) => ({
  plugins: [react()],
  // Lib mode does not substitute NODE_ENV on its own; without this the
  // bundle carries React's development build, three times the size. Scoped
  // to build: vitest shares this config, and tests need the development
  // React (act() only exists there).
  define: command === 'build' ? { 'process.env.NODE_ENV': JSON.stringify('production') } : undefined,
  build: {
    lib: {
      entry: 'src/main.tsx',
      name: 'RavenModern',
      formats: ['iife'],
      fileName: () => 'modern.iife.js',
    },
    outDir: '.modern',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
  },
}))
