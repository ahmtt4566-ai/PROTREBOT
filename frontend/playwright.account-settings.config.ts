import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export default defineConfig({
  testDir: resolve(root, 'frontend', 'tests'),
  testMatch: 'account-settings.spec.ts',
  outputDir: process.env.ACCOUNT_TEST_RESULTS ?? resolve(tmpdir(), `protrebot-account-${randomUUID()}`),
  workers: 1, reporter: 'line',
  use: {baseURL: 'http://127.0.0.1:4176', trace: 'retain-on-failure'},
  webServer: {command: 'npm run preview -- --host 127.0.0.1 --port 4176 --strictPort', cwd: root, url: 'http://127.0.0.1:4176', reuseExistingServer: false},
  projects: [{name: 'chromium-account-production', use: {...devices['Desktop Chrome']}}],
})
