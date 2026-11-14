import { defineConfig } from '@playwright/test';

// npm run e2e                                   -> builds, starts the mock (mock/serve.mjs) and walks every screen
// PETAL_URL=http://127.0.0.1:8000 npx playwright test --trace off   -> the same walk against a real server
//   (petal-locate demo); PETAL_TOKEN=<api token> when the server requires one.
// PETAL_SHOTS=<dir> chooses where the screenshots go (default test-results/screens).
const url = process.env.PETAL_URL;
const port = Number(process.env.MOCK_PORT ?? 4174);

export default defineConfig({
  testDir: 'e2e',
  timeout: 180_000,
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: url ?? `http://127.0.0.1:${port}`,
    viewport: { width: 1280, height: 800 },
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
  },
  webServer: url
    ? undefined
    : {
        command: `node mock/serve.mjs --port ${port}`,
        url: `http://127.0.0.1:${port}/api/v1/meta`,
        reuseExistingServer: false,
        timeout: 30_000,
      },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
