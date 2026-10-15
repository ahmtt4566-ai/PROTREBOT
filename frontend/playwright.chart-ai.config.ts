import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export default defineConfig({
  testDir: resolve(root, 'frontend', 'tests'),
  testMatch: 'chart-ai.spec.ts',
  outputDir: resolve(tmpdir(), `kaistrade-chart-ai-${randomUUID()}`),
  workers: 1, retries: 0, timeout: 45000, reporter: 'line',
  use: {baseURL: 'http://127.0.0.1:4184', trace: 'off'},
  webServer: {
    command: 'npm run dev -- --mode test --host 127.0.0.1 --port 4184 --strictPort',
    cwd: root, url: 'http://127.0.0.1:4184', reuseExistingServer: false, timeout: 60000,
  },
  projects: [{name: 'chart-ai-local', use: {...devices['Desktop Chrome']}}],
})
