import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 120_000,
  globalSetup: "./tests/e2e/global-setup.ts",
  globalTeardown: "./tests/e2e/global-teardown.ts",
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure"
  },
  webServer: [
    {
      command: "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-e2e-api.ps1",
      url: "http://127.0.0.1:8765/health",
      timeout: 120_000,
      reuseExistingServer: false
    },
    {
      command: "npm run dev:web",
      url: "http://127.0.0.1:5173",
      timeout: 120_000,
      reuseExistingServer: false
    }
  ],
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] }
    }
  ]
});
