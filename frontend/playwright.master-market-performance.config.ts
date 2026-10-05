import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export default defineConfig({
  testDir: resolve(root, 'frontend', 'tests'),
  testMatch: 'master-trade-analysis-v2.spec.ts',
  grep: /650-symbol universe stays/,
  outputDir: process.env.MASTER_TEST_RESULTS ?? resolve(tmpdir(), `protrebot-master-analysis-${randomUUID()}`),
  workers: 1,
  reporter: 'line',
  use: {baseURL: 'http://127.0.0.1:4174', trace: 'off'},
  webServer: {
    command: 'npm run preview -- --host 127.0.0.1 --port 4174 --strictPort',
    cwd: root,
    url: 'http://127.0.0.1:4174',
    reuseExistingServer: false,
  },
  projects: [{name: 'chromium-master-analysis-production', use: {...devices['Desktop Chrome']}}],
})
