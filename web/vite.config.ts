/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// En desarrollo, /api va al backend (`ESTUDIA_PORT`, el puerto que anota en la carpeta de datos).
const backend = `http://127.0.0.1:${process.env.ESTUDIA_PORT ?? "8780"}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": {
        target: backend,
        changeOrigin: true,
        // El backend rechaza escrituras con un Origin ajeno; el de Vite lo sería.
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => req.removeHeader("origin"));
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
