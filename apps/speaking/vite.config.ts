import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const proxy = { '/api': process.env.SPEAKING_API_URL || 'http://127.0.0.1:8090' };

export default defineConfig({
  base: '/speaking/',
  plugins: [react()],
  publicDir: '../demo/public',
  server: { proxy },
  // Root build runs demo first, then adds this product alongside it for deployment.
  build: { outDir: '../demo/dist/speaking', emptyOutDir: true },
});
