import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';

// ⚠️ 用 fileURLToPath(new URL(...)) 而不是 resolve(__dirname, ...):
// package.json 有 "type": "module",本配置被当 ESM 加载,ESM 里 __dirname 未定义,
// 直接写会报 "__dirname is not defined in ES module scope",构建启动即失败。
export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 多页入口:客户端 index.html + 管理端 admin.html,按入口分 chunk
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        admin: fileURLToPath(new URL('./admin.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
});
