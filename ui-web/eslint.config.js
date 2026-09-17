/* What a module may import, in what order, and what it may leave behind.
 *
 * `make lint-ui` was gen:check plus type-check, and neither sees an import.
 * Import style had already diverged four ways in one tree -- types first, types
 * last, types interleaved, unsorted -- and an unused import or local cost
 * nothing to leave in, because tsconfig had neither noUnusedLocals nor a linter
 * behind it.
 *
 * The same engine and the same base as ui-tui: ../eslint.base.mjs is a factory
 * because the repo root has no node_modules, so each package imports its own
 * plugins and passes them in. What is added here is the React half -- the hooks
 * rules, which are what a component in this tree can get wrong -- and the four
 * places the base's rules do not fit this tree: where a type import goes, the
 * one-line guard `curly` would brace, a test naming the type of a module it
 * loads dynamically, and the gates under scripts/, which are node scripts
 * rather than part of the page.
 */

import js from '@eslint/js'
import tsPlugin from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'
import perfectionist from 'eslint-plugin-perfectionist'
import reactHooks from 'eslint-plugin-react-hooks'
import unusedImports from 'eslint-plugin-unused-imports'
import globals from 'globals'

import base from '../eslint.base.mjs'

export default [
  {
    ignores: [
      /* Written by scripts/gen-rpc-client.mjs from rpc-schema/openrpc.json;
         `npm run gen:check` is what holds it, and a lint rule applied to it
         would be a rule about json-schema-to-typescript's output. */
      'src/rpc/generated.ts',
      'dist/**',
      '.modern/**',
      'node_modules/**',
      '**/*.config.*',
    ],
  },
  ...base({ js, perfectionist, tsParser, tsPlugin, unusedImports }),
  {
    files: ['**/*.{ts,tsx,mjs}'],
    rules: {
      /* Type imports last, where the plugin's default puts them first. Two
         reasons, and both are what this tree already does: a module's purpose
         is a comment at the top of the file (AGENTS.md section 1.1) and it sits
         above the first import, so a group hoisted above it buries the header;
         and the types a module names are the least interesting thing about it,
         which is why the files that were consistent already had them at the
         bottom. */
      'perfectionist/sort-imports': ['error', {
        /* A block comment is a boundary. Without this the sort reaches across
           the file's own header -- a module's purpose comment, and in a test the
           `@vitest-environment` line under it -- and hoists an import above
           both. A comment sits with what it describes. */
        partitionByComment: true,
        groups: [
          ['value-builtin', 'value-external'],
          'value-internal',
          ['value-parent', 'value-sibling', 'value-index'],
          'ts-equals-import',
          'unknown',
          ['type-import', 'type-internal', 'type-parent', 'type-sibling', 'type-index'],
        ],
      }],
      /* Off, where ui-tui has it on: this tree writes a guard as one line
         (`if (!row) return`) in 2,063 places, and bracing them would be a
         restyle of every module rather than the import-and-unused pass this
         config is for. */
      curly: 'off',
      /* The base sets this for .ts and .tsx; the gates are .mjs. */
      'unused-imports/no-unused-imports': 'error',
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      /* A warning rather than an error, for two reasons this pass may not fix:
         features/composer/useInTask.ts is not a hook and only the convention
         says otherwise (six call sites), and transcript/TranscriptPage.tsx's
         CallRow calls useTick after two early returns -- safe only because a
         call's `kind` never changes under its key, and the fix is to split the
         component, which is not a lint sweep's to do. */
      'react-hooks/rules-of-hooks': 'warn',
      /* Also a warning, the way ui-tui has it: several of this tree's effects
         deliberately run once on a value they also read, and the rule cannot
         tell those from a stale closure. */
      'react-hooks/exhaustive-deps': 'warn',
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  {
    /* A test that drives a module through scripts/module-harness.mjs names that
       module's type as `typeof import('./x')`, because the module is loaded
       fresh per case and a static import would be the wrong thing to name. The
       rule's first half still holds everywhere: a type-only import says
       `import type`. */
    files: ['**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/consistent-type-imports': ['error', { disallowTypeAnnotations: false }],
    },
  },
  {
    /* The gates are plain node scripts, not part of the page: they read the
       source tree, spawn the CLI checks, and the ones marked
       `@vitest-environment happy-dom` get a document from the runner. */
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.node, ...globals.browser },
    },
    /* Registered here so the two rules above resolve for a .mjs file: in a flat
       config a rule is only available where its plugin is. */
    plugins: { perfectionist, 'unused-imports': unusedImports },
  },
]
