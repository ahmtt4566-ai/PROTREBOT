import {defineConfig, devices} from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testMatch: 'site-checkup.spec.ts',
  workers: 1,
  reporter: 'line',
  use: {...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:4176', trace: 'retain-on-failure'},
  webServer: [
    {
      command: 'npm --prefix .. run preview -- --host 127.0.0.1 --port 4176 --strictPort',
      url: 'http://127.0.0.1:4176',
      reuseExistingServer: false,
    },
  ],
})
