import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  reporter: "line",
  workers: Number(process.env.PW_WORKERS) || undefined,
  use: { baseURL: "http://127.0.0.1:4173", browserName: "chromium" },
  webServer: { command: "npx vite preview --host 127.0.0.1 --port 4173 --strictPort", url: "http://127.0.0.1:4173", reuseExistingServer: false },
});
