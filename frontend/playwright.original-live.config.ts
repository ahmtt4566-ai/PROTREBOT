import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export default defineConfig({
  testDir: resolve(root, 'frontend', 'tests'),
  testMatch: ['original-v2-live.spec.ts', 'original-risk-admin.spec.ts', 'master-trade-live-ui.spec.ts', 'live-trading-panel.spec.ts'],
  outputDir: resolve(tmpdir(), `kaistrade-original-live-${randomUUID()}`),
  workers: 1,
  retries: 0,
  timeout: 60000,
  reporter: 'line',
  use: {baseURL: 'http://127.0.0.1:4174', trace: 'retain-on-failure'},
  webServer: [{
    command: 'npm run dev -- --mode test --host 127.0.0.1 --port 4174 --strictPort',
    cwd: root,
    url: 'http://127.0.0.1:4174',
    reuseExistingServer: false,
    timeout: 60000,
  }, {
    command: 'npm run dev -- --host 127.0.0.1 --port 4177 --strictPort',
    cwd: root,
    url: 'http://127.0.0.1:4177',
    reuseExistingServer: false,
    timeout: 60000,
  }],
  projects: [{name: 'chromium-original-live', use: {...devices['Desktop Chrome']}}],
})
