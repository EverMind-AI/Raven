// Shared flat-config base for the repo's TypeScript/Node packages (no React).
//
// This file lives at the repo root, which has no node_modules. ESM resolves a
// module's bare imports from the module's own location upward, so a root file
// importing 'eslint-plugin-*' would fail to resolve. To avoid that, the base is
// a factory: each package imports its own plugins (resolved from that package's
// node_modules) and passes them in. The base never imports a plugin itself.

export default function base({ js, tsPlugin, tsParser, unusedImports, perfectionist }) {
  return [
    js.configs.recommended,
    {
      files: ['**/*.{ts,tsx}'],
      languageOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        parser: tsParser,
        parserOptions: {
          ecmaFeatures: { jsx: true },
        },
      },
      plugins: {
        '@typescript-eslint': tsPlugin,
        'unused-imports': unusedImports,
        perfectionist,
      },
      rules: {
        ...tsPlugin.configs['flat/recommended'].reduce(
          (acc, cfg) => ({ ...acc, ...(cfg.rules ?? {}) }),
          {},
        ),
        '@typescript-eslint/consistent-type-imports': 'error',
        '@typescript-eslint/no-explicit-any': 'warn',
        '@typescript-eslint/no-unused-vars': 'off',
        'unused-imports/no-unused-imports': 'error',
        'perfectionist/sort-imports': 'error',
        curly: ['error', 'all'],
        'no-fallthrough': 'error',
        'no-unused-expressions': 'off',
        '@typescript-eslint/no-unused-expressions': 'warn',
        'no-undef': 'off',
        'no-unused-vars': 'off',
      },
    },
  ]
}
