import {defineConfig, devices} from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  workers: 1,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: 'npm run dev -- --mode test --host 127.0.0.1 --port 4173',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: false,
    },
    {
      command: 'npm --prefix .. run dev -- --mode test --host 127.0.0.1 --port 4174',
      url: 'http://127.0.0.1:4174',
      reuseExistingServer: false,
    },
  ],
  projects: [
    {name: 'chromium', testIgnore: /scanner\.spec\.ts/, use: {...devices['Desktop Chrome']}},
    {name: 'scanner', testMatch: /scanner\.spec\.ts/, use: {...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:4174'}},
  ],
})