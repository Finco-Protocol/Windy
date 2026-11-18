<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";

import AppFooter from "@/components/AppFooter.vue";
import PerTurbinePanel from "@/components/PerTurbinePanel.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { windyApi, type SpatialForecast, type SiteResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";
import { loadEnergyUnit, saveEnergyUnit, type EnergyUnit } from "@/utils/energyUnits";

// The dedicated WTG drilldown does not alter the main-page forecast or data
// source. Both screens read the same immutable per-coordinate snapshots.
// Explicit POST only on the user's refresh click, never on entering this tab.
const route = useRoute();
const { siteId } = useActiveSite();
const horizonOptions = [
  { value: 7, label: "7 dana" },
  { value: 10, label: "10 dana" },
  { value: 15, label: "15 dana" },
] as const;
const modeOptions = [
  { value: "baseline", label: "Bazno" },
  { value: "adjusted", label: "Prilagođeno" },
] as const;
const unitOptions = [
  { value: "mwh", label: "MWh" },
  { value: "gwh", label: "GWh" },
] as const;
function initialDays(): 7 | 10 | 15 {
  const q = Number(route.query.days);
  return q === 10 || q === 15 ? q : 7;
}
const days = ref<7 | 10 | 15>(initialDays());
const viewMode = ref<"baseline" | "adjusted">(route.query.view === "adjusted" ? "adjusted" : "baseline");
const energyUnit = ref<EnergyUnit>(loadEnergyUnit(siteId.value));
watch(siteId, (sid) => { energyUnit.value = loadEnergyUnit(sid); });
watch(energyUnit, (unit) => { saveEnergyUnit(siteId.value, unit); });

const siteResource = useAbortableResource<SiteResponse>(
  (signal) => windyApi.site(siteId.value, signal), () => [siteId.value],
);
const spatialResource = useAbortableResource<SpatialForecast>(
  (signal) => windyApi.perTurbineForecast(days.value, siteId.value, signal),
  () => [siteId.value, days.value],
);
const { data, loading, error } = spatialResource;
const availableCount = computed(() => data.value?.models.filter((m) => m.status === "OK" && !m.met_only).length ?? 0);
const collecting = ref(false);
const collectError = ref<string | null>(null);
const collectInfo = ref<string | null>(null);
async function collect26(): Promise<void> {
  if (collecting.value) return;
  const sid = siteId.value;
  collecting.value = true;
  collectError.value = null;
  collectInfo.value = null;
  try {
    const result = await windyApi.collectTurbines(sid);
    if (sid !== siteId.value) return;
    const success = result.per_turbine.models.filter((m) => ["OK", "ALREADY_ARCHIVED"].includes(m.status)).length;
    const failed = result.per_turbine.models.length - success;
    collectInfo.value = `Dohvat završen: ${success} modela s podacima, ${failed} nedostupno. Bez zamjene 1-spot vrijednostima.`;
    await spatialResource.refresh();
  } catch (e) {
    if (sid === siteId.value) collectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    collecting.value = false;
  }
}
const exporting = ref(false);
const exportError = ref<string | null>(null);
async function exportDetailed(): Promise<void> {
  if (exporting.value) return;
  exporting.value = true;
  exportError.value = null;
  const sid = siteId.value;
  try {
    const url = windyApi.perTurbineExportUrl(sid, days.value, viewMode.value, energyUnit.value);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Excel ${response.status}`);
    if (!(response.headers.get("content-type") ?? "").startsWith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")) {
      throw new Error("Server nije vratio XLSX datoteku.");
    }
    const file = await response.blob();
    const sig = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    if (sig[0] !== 0x50 || sig[1] !== 0x4b) throw new Error("XLSX nema valjani ZIP/PK potpis.");
    if (sid !== siteId.value) return;
    const link = document.createElement("a");
    const href = URL.createObjectURL(file);
    link.href = href;
    link.download = `Windy_${sid}_Detailed_26WTG_${days.value}d_${viewMode.value}_${energyUnit.value}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
  } catch (e) {
    if (sid === siteId.value) exportError.value = e instanceof Error ? e.message : String(e);
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-4 px-4 py-3 sm:px-6 sm:py-6">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <SiteSelector />
          <div>
            <h1 class="text-paper-50 font-mono text-lg tracking-wide">Detailed forecast (26 WTG)</h1>
            <p class="text-paper-400 font-mono text-[11px]">
              {{ siteResource.data.value?.site.name ?? siteId }} · detalji po turbini
            </p>
          </div>
        </div>
        <RouterLink :to="{ path: '/', query: siteId !== 'krnovo' ? { site: siteId } : {} }"
          class="text-sodium-200 border-ink-600 hover:bg-ink-800 border px-2.5 py-1 font-mono text-[11px]">
          ← Forecast
        </RouterLink>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <SegmentedToggle v-model="viewMode" :options="[...modeOptions]" inline divided />
        <SegmentedToggle v-model="days" :options="[...horizonOptions]" inline divided />
        <SegmentedToggle v-model="energyUnit" :options="[...unitOptions]" inline divided />
        <button type="button"
          class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]"
          :disabled="exporting || !availableCount" @click="exportDetailed">
          {{ exporting ? "Izvoz…" : "⬇ Excel (26 WTG)" }}
        </button>
        <button type="button"
          class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]"
          :disabled="collecting" @click="collect26">
          {{ collecting ? "Dohvaćam…" : "Osvježi 26 turbina" }}
        </button>
      </div>

      <StateBlock v-if="siteResource.error.value" kind="error">{{ siteResource.error.value }}</StateBlock>
      <StateBlock v-if="error" kind="error">{{ error }}</StateBlock>
      <StateBlock v-if="collectError" kind="error">{{ collectError }}</StateBlock>
      <StateBlock v-if="exportError" kind="error">{{ exportError }}</StateBlock>
      <p v-if="collectInfo" role="status" class="text-sodium-200 font-mono text-[11px]">{{ collectInfo }}</p>
      <StateBlock v-if="loading && !data" kind="loading" caption="Učitavam arhivirane prognoze po WTG…" />
      <PerTurbinePanel v-if="data" :forecast="data" :unit="energyUnit" :view-mode="viewMode" />
    </main>
    <AppFooter />
  </div>
</template>
