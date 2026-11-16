<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";

import PullToRefresh from "@/components/PullToRefresh.vue";

const links = [
  { to: "/", label: "Forecast" },
  { to: "/history", label: "Historical runs" },
  { to: "/actuals", label: "Actuals & accuracy" },
  { to: "/availability", label: "Availability" },
  { to: "/sites", label: "Sites" },
];
</script>

<template>
  <PullToRefresh />
  <header class="border-ink-700 bg-ink-950/95 border-b">
    <div class="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-2 px-4 py-2 sm:px-6">
      <RouterLink to="/" class="flex items-baseline gap-2">
        <span class="text-sodium-200 font-mono text-sm tracking-[0.2em]">WINDY</span>
        <span class="text-paper-300 font-mono text-[11px] tracking-wide">Krnovo · 71.5 MW</span>
      </RouterLink>
      <nav class="flex flex-wrap gap-1" aria-label="Main">
        <RouterLink
          v-for="l in links"
          :key="l.to"
          :to="l.to"
          class="text-paper-300 hover:bg-ink-800 hover:text-paper-50 border-ink-700 border px-2.5 py-1 font-mono text-[11px] tracking-wide transition-colors aria-[current=page]:bg-sodium-300/15 aria-[current=page]:text-sodium-200"
        >
          {{ l.label }}
        </RouterLink>
      </nav>
    </div>
  </header>
  <RouterView />
</template>
