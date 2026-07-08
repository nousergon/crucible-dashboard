import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true, // required for testing-library's afterEach auto-cleanup
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["__tests__/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": __dirname,
      "server-only": `${__dirname}/__tests__/stub-server-only.ts`,
    },
  },
});
