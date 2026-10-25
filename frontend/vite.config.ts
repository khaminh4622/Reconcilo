/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { Agent } from "node:http";

const backend = "http://127.0.0.1:8000";
// "/bank" also covers "/bank-admin" and "/bank-tools" (prefix match), but the
// payment timeline lives at "/cases/{id}/timeline" and needs its own entry.
const proxied = [
  "/auth", "/workflow", "/admin", "/demo", "/bank", "/cases", "/customers", "/developer", "/health",
];
// Without a keep-alive agent the proxy sends "Connection: close", and uvicorn on
// Windows intermittently resets such connections (ECONNRESET, shown as HTTP 500).
const agent = new Agent({ keepAlive: true });

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: Object.fromEntries(proxied.map((path) => [path, { target: backend, agent }])) },
  test: { environment: "jsdom" },
});
