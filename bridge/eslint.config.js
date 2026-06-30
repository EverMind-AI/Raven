import js from '@eslint/js'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'
import perfectionist from 'eslint-plugin-perfectionist'
import unusedImports from 'eslint-plugin-unused-imports'

import base from '../eslint.base.mjs'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', '**/*.config.*'],
  },
  ...base({ js, tsPlugin, tsParser, unusedImports, perfectionist }),
  {
    files: ['src/**/*.{ts,tsx}'],
  },
]
