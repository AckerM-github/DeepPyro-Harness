import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The SPA is served by the Python host in production. In development Vite owns
// the page and proxies the JSON API to the already-running DeepPyro web host.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/deeppyro/web/ui/static',
    assetsDir: 'assets',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.DEEPPYRO_WEB_ORIGIN ?? 'http://127.0.0.1:8765',
        changeOrigin: false,
      },
    },
  },
});
