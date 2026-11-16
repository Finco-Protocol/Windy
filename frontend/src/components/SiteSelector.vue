<script setup lang="ts">
import { computed, ref } from "vue";

import { windyApi, type AssetSummary } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// Active-site selector shared by Forecast, Historical runs and Actuals.
// The choice lives in the URL query and survives refresh.

const { siteId, setSite } = useActiveSite();

const res = useAbortableResource<{ assets: AssetSummary[] }>((signal) => windyApi.assets(signal), () => []);
const assets = computed(() => res.data.value?.assets ?? []);
const open = ref(false);

function choose(id: string): void {
  open.value = false;
  if (id !== siteId.value) setSite(id);
}
</script>

<template>
  <div class="relative" @mouseleave="open = false">
    <button
      type="button"
      class="border-ink-700 bg-ink-900 text-paper-100 hover:border-sodium-300/60 border px-2.5 py-1 font-mono text-[11px] transition-colors"
      @click="open = !open"
    >
      site: <span class="text-sodium-200">{{ siteId }}</span> ▾
    </button>
    <div v-if="open" class="border-ink-700 bg-ink-950 absolute right-0 z-20 mt-1 min-w-56 border shadow-lg">
      <button
        v-for="a in assets"
        :key="a.id"
        type="button"
        class="text-paper-100 hover:bg-ink-800 flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left font-mono text-[11px]"
        :class="a.id === siteId ? 'bg-sodium-300/10' : ''"
        @click="choose(a.id)"
      >
        <span>{{ a.id }}</span>
        <span v-if="a.curve_reference_only" class="text-paper-400 text-[9px]">reference</span>
      </button>
      <div class="border-ink-700 text-paper-400 border-t px-3 py-1.5 font-mono text-[9px]">manage on the Sites screen</div>
    </div>
  </div>
</template>
