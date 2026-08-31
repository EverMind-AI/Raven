// ESLint flat config for the vscode-ext package. Composes the shared repo
// base (typescript, unused-imports, perfectionist ordering) and relaxes
// no-explicit-any for tests.

import js from '@eslint/js'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'
import perfectionist from 'eslint-plugin-perfectionist'
import unusedImports from 'eslint-plugin-unused-imports'

import base from '../eslint.base.mjs'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', '**/*.config.*']
  },
  ...base({ js, tsPlugin, tsParser, unusedImports, perfectionist }),
  {
    files: ['src/__tests__/**', '**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off'
    }
  }
]
