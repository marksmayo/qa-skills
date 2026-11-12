import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Not a finding: `secure: false` here turns off TLS verification for the local dev-server proxy,
// it is not a cookie flag.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "https://localhost:8443",
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
