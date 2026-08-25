import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    exclude: ['dist/**', 'node_modules/**'],
    setupFiles: ['./vitest.setup.ts']
  }
})
