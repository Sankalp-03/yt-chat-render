import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:5000";
const paths = [
  "/status", "/start", "/stop", "/config",
  "/auth", "/oauth2callback",
  "/kick/auth", "/kick/oauth2callback"
];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(paths.map((path) => [
      path,
      { target: backend, changeOrigin: true, secure: false }
    ]))
  }
});