import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  appType: "spa",
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("/node_modules/react") || id.includes("/node_modules/react-dom")) {
            return "vendor-react";
          }
          if (id.includes("/src/shell/") || id.includes("/src/workspace/")) {
            return "shell-workbench";
          }
          const featureMatch = id.match(/\/src\/features\/([^/]+)\//);
          if (featureMatch) {
            return `feature-${featureMatch[1]}`;
          }
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: process.env.BACKEND_URL || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/health": {
        target: process.env.BACKEND_URL || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    proxy: {
      "/api": {
        target: process.env.BACKEND_URL || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/health": {
        target: process.env.BACKEND_URL || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    // 该文件是离线 HTTP 契约证据测试：必须由 WEB_CONTRACT_RUNTIME 指向已录制的
    // 证据运行时，用 vitest.sdk-http-contract.config.ts 单独执行。默认套件没有
    // 这份夹具，收集它只会制造与产品代码无关的失败。
    exclude: ["src/features/agents/sdkHttpContract.test.tsx"],
  },
});
