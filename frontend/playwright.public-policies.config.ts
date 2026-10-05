import {defineConfig, devices} from '@playwright/test'
import {randomUUID} from 'node:crypto'
import {tmpdir} from 'node:os'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const frontend = dirname(fileURLToPath(import.meta.url))
const root = resolve(frontend, '..')

export default defineConfig({
  testDir: resolve(frontend, 'tests'),
  testMatch: 'public-policies.spec.ts',
  outputDir: resolve(tmpdir(), `kaistrade-public-policies-${randomUUID()}`),
  workers: 1,
  reporter: 'line',
  use: {trace: 'retain-on-failure'},
  webServer: [
    {command: 'npm run preview -- --host 127.0.0.1 --port 4173 --strictPort', cwd: frontend, url: 'http://127.0.0.1:4173', reuseExistingServer: false},
    {command: 'npm run preview -- --host 127.0.0.1 --port 4175 --strictPort', cwd: root, url: 'http://127.0.0.1:4175', reuseExistingServer: false},
    {command: 'npm run preview -- --host 127.0.0.1 --port 4176 --strictPort', cwd: root, url: 'http://127.0.0.1:4176', reuseExistingServer: false},
  ],
  projects: [{name: 'chromium-public-production', use: {...devices['Desktop Chrome']}}],
})
