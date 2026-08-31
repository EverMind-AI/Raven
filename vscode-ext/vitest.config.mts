// Vitest config for the vscode-ext package: run unit tests under src and
// exclude build output and dependencies.

import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    exclude: ['dist/**', 'node_modules/**']
  }
})
