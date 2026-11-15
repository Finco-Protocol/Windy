import { createApp } from "vue";

import "./style.css";
import { usePWAUpdate } from "@/composables/usePWAUpdate";

import App from "./App.vue";
import { setupECharts } from "./echartsSetup";
import { router } from "./router";

setupECharts();

// Disable mobile Safari's pinch-to-zoom. The viewport meta (`user-scalable=no`)
// already covers Android and installed PWAs, but iOS Safari ignores it for
// manual pinch, so we cancel WebKit's gesture events directly. They only fire
// for multi-finger pinch/rotate, so ordinary scrolling is untouched.
document.addEventListener("gesturestart", (e) => e.preventDefault());

createApp(App).use(router).mount("#app");

usePWAUpdate({ autoUpdate: true });
