/// <reference types="vitest/config" />
import { execSync } from "node:child_process";

import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const buildDate = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
let buildSha = "dev";
try {
  buildSha = execSync("git rev-parse --short HEAD").toString().trim();
} catch {
  // not a git checkout (e.g. tarball build)
}

const port = process.env.PORT ? Number(process.env.PORT) : undefined;

// `/api/*` is served by the Windy FastAPI backend (../backend). In `vite dev`
// it is proxied to the locally running backend; point elsewhere with
// API_PROXY=http://host:port. The proxy also keeps Vite's SPA fallback from
// answering an api path with index.html.
const apiProxy = {
  "/api": {
    target: process.env.API_PROXY ?? "http://127.0.0.1:8000",
    changeOrigin: true,
  },
};

export default defineConfig({
  server: {
    port,
    strictPort: port !== undefined,
    proxy: apiProxy,
  },
  preview: { port, strictPort: port !== undefined, proxy: apiProxy },
  build: {
    // echarts is large by nature and sits in its own group; the limit sits just
    // above it, so any other chunk that grows past it still warns.
    chunkSizeWarningLimit: 800,
    rolldownOptions: {
      output: {
        codeSplitting: {
          // Group heavy vendor deps into stable chunks. Deliberately no `src`
          // catch-all group: a `{ test: /src/ }` group would fold every app
          // module — including the lazy route views — into one chunk, defeating
          // the router's dynamic import()s. (Chunk strategy inherited from
          // upstream meteocompare; the posthog group is gone — Windy ships no
          // third-party analytics.)
          groups: [
            { name: "vue", test: /@vue|vue-router|@vueuse/, priority: 60 },
            { name: "echarts", test: /echarts|vue-echarts|zrender/, priority: 40 },
            { name: "vendor", test: /node_modules/, priority: 10 },
          ],
        },
      },
    },
  },
  define: {
    __BUILD_DATE__: JSON.stringify(buildDate),
    __BUILD_SHA__: JSON.stringify(buildSha),
  },
  plugins: [
    vue(),
    tailwindcss(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      registerType: "prompt",
      pwaAssets: {
        htmlPreset: "2023",
        preset: {
          transparent: { sizes: [64, 192, 512], favicons: [[48, "favicon.ico"]] },
          maskable: { sizes: [512], padding: 0.15, resizeOptions: { background: "#050810" } },
          apple: { sizes: [180], padding: 0.15, resizeOptions: { background: "#050810" } },
        },
        image: "public/logo.svg",
      },
      manifest: {
        name: "Windy",
        short_name: "Windy",
        description: "Private wind-farm forecast and forecast-accuracy application (Krnovo).",
        theme_color: "#050810",
        background_color: "#050810",
        display: "standalone",
        start_url: "/",
        scope: "/",
        id: "windy",
        orientation: "natural",
      },
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,ico,woff,woff2}"],
        // weather-icons also ships its font as a 159 kB SVG for pre-2016 iOS. Every browser
        // with a service worker takes the woff, so precaching the SVG only costs bandwidth.
        globIgnores: ["**/weathericons-regular-webfont-*.svg"],
      },
      devOptions: {
        // enabled: true,
        type: "module",
      },
    }),
  ],
  resolve: {
    tsconfigPaths: true,
  },
  test: {
    // Tests that need a DOM opt in per file with `// @vitest-environment jsdom`.
    environment: "node",
    include: ["src/**/*.test.ts", "scripts/**/*.test.ts"],
  },
});
