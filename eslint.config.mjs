import babelParser from '@babel/eslint-parser'

export default [
  {ignores: ['node_modules/**', 'dist/**', 'frontend/dist/**', 'frontend/node_modules/**', 'tradbt458-main/**', '**/test-results/**']},
  {
    files: ['*.ts', '*.tsx', 'frontend/src/**/*.ts', 'frontend/src/**/*.tsx', 'frontend/tests/**/*.ts'],
    languageOptions: {parser: babelParser, parserOptions: {requireConfigFile: false, babelOptions: {babelrc: false, configFile: false, parserOpts: {plugins: ['typescript', 'jsx']}}}},
    rules: {
      'no-constant-condition': 'error',
      'no-dupe-args': 'error',
      'no-duplicate-case': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'valid-typeof': 'error',
    },
  },
  // Legacy compatibility renderers follow the active panel's unconditional return.
  {files: ['frontend/src/LiveTradingPanel.tsx'], rules: {'no-unreachable': 'off'}},
]
