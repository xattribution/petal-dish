/// <reference types="vitest/config" />
import { defineConfig, type PluginOption } from 'vite';
import react from '@vitejs/plugin-react';
import { mockApiPlugin } from './mock/vite-plugin.mjs';

// npm run dev                       -> console + mock API (ui/mock) on http://localhost:5173
// PETAL_PROXY=http://127.0.0.1:8000 npm run dev -> console proxied to a real server instead
const proxyTarget = process.env.PETAL_PROXY;

export default defineConfig(({ command }) => ({
  base: './',
  plugins: [react(), (command === 'serve' && !proxyTarget ? mockApiPlugin() : null) as PluginOption],
  build: {
    outDir: '../src/petal_locate/server/static',
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: proxyTarget
      ? {
          '/api': { target: proxyTarget, changeOrigin: true },
          '/ws': { target: proxyTarget.replace(/^http/, 'ws'), ws: true, changeOrigin: true },
        }
      : undefined,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'mock/**/*.test.mjs'],
  },
}));
