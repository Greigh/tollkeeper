import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    build: { outDir: '../router/app/static', emptyOutDir: true },
    server: {
        port: 5173,
        host: '127.0.0.1',
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8080',
                changeOrigin: true,
            },
        },
    },
});
