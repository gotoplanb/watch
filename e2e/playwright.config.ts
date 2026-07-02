import { defineConfig } from "@playwright/test";

// Post-deploy smoke config. One project (chromium, headless). Retries once in CI (the pipeline
// Smoke stage sets CI) to ride out a slow Step Functions commit. HTML report + trace on failure
// land in ./report and ./test-results (the pipeline uploads them to the artifact bucket).
export default defineConfig({
  testDir: ".",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "report" }]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
