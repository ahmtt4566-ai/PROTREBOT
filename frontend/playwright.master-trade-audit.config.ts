import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export default defineConfig({
  testDir: resolve(root, 'frontend', 'tests'),
  testMatch: [
    'original-v2-live.spec.ts', 'master-trade-live-ui.spec.ts',
    'master-trade-regressions.spec.ts', 'master-trade-readonly.spec.ts',
    'master-trade-analysis-v2.spec.ts', 'live-trading-panel.spec.ts',
  ],
  outputDir: resolve(tmpdir(), `kaistrade-master-audit-${randomUUID()}`),
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
  }],
  projects: [{name: 'chromium-master-audit', use: {...devices['Desktop Chrome']}}],
})
