import { createRouter, createWebHistory } from "vue-router";

import { usePostHog } from "@/composables/usePostHog";
import WindyForecastView from "@/views/WindyForecastView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "forecast",
      // Screen A — Forecast (Krnovo, multi-model, MWh).
      component: WindyForecastView,
    },
    {
      path: "/history",
      name: "history",
      // Screen B — Historical Runs (archived issues + bounded backfill).
      component: () => import("@/views/WindyHistoryView.vue"),
    },
    {
      path: "/actuals",
      name: "actuals",
      // Screen C — Actual vs Forecast (CSV import + accuracy).
      component: () => import("@/views/WindyActualsView.vue"),
    },
    {
      path: "/sites",
      name: "sites",
      // Site registry (list/create) — per-site isolation everywhere else.
      component: () => import("@/views/AssetsView.vue"),
    },
  ],
});

usePostHog();
