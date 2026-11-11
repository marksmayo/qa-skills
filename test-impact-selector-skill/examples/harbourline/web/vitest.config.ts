import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/**/*.test.ts", "e2e/**/*.test.ts", "visual/**/*.test.ts"],
    setupFiles: ["./test/setup.ts"],
    css: true,
  },
});
