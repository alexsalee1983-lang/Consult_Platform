import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 本地开发：npm run dev，/api 转发到本机 8000 端口的后端
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
