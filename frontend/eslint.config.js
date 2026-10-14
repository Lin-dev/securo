import js from '@eslint/js'
import globals from 'globals'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'jsx-a11y': jsxA11y,
    },
    rules: {
      'react-refresh/only-export-components': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/preserve-manual-memoization': 'warn',
      'jsx-a11y/control-has-associated-label': [
        'error',
        {
          controlComponents: ['Button'],
          depth: 3,
          ignoreElements: ['input', 'textarea', 'select'],
        },
      ],
      'jsx-a11y/anchor-has-content': [
        'error',
        {
          components: ['Link'],
        },
      ],
    },
  },
  {
    // Standalone Node CLI scripts (i18n sync tool, git merge driver, their
    // tests) run outside the browser bundle, so they need Node globals
    // instead of the app's browser ones -- and their own config block so
    // eslint:recommended actually applies to them at all.
    files: ['scripts/**/*.mjs'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: globals.node,
    },
  },
])
