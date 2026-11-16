<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, RouterView } from "vue-router";

import PullToRefresh from "@/components/PullToRefresh.vue";
import { buildNavLinks, useActiveSite } from "@/composables/useActiveSite";
import { windyApi, type AssetSummary } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";

const { siteId } = useActiveSite();
const links = computed(() => buildNavLinks(siteId.value));

// header shows the ACTIVE site's real label/capacity, never a fixed nameplate
const assetsRes = useAbortableResource<{ assets: AssetSummary[] }>((signal) => windyApi.assets(signal), () => []);
const activeSite = computed(() => (assetsRes.data.value?.assets ?? []).find((a) => a.id === siteId.value));
</script>

<template>
  <PullToRefresh />
  <header class="border-ink-700 bg-ink-950/95 border-b">
    <div class="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-2 px-4 py-2 sm:px-6">
      <RouterLink :to="{ path: '/', query: siteId !== 'krnovo' ? { site: siteId } : {} }" class="flex items-baseline gap-2">
        <span class="text-sodium-200 font-mono text-sm tracking-[0.2em]">WINDY</span>
        <span v-if="activeSite" class="text-paper-300 font-mono text-[11px] tracking-wide">
          {{ activeSite.name }} · {{ activeSite.capacity_mw }} MW
        </span>
      </RouterLink>
      <nav class="flex flex-wrap gap-1" aria-label="Main">
        <RouterLink
          v-for="l in links"
          :key="l.path"
          :to="{ path: l.path, query: l.query }"
          class="text-paper-300 hover:bg-ink-800 hover:text-paper-50 border-ink-700 border px-2.5 py-1 font-mono text-[11px] tracking-wide transition-colors aria-[current=page]:bg-sodium-300/15 aria-[current=page]:text-sodium-200"
        >
          {{ l.label }}
        </RouterLink>
      </nav>
    </div>
  </header>
  <RouterView />
</template>
