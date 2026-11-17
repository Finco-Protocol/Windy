<script setup lang="ts">
import { computed, ref } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { buildDailyMatrix, buildHourlyRows, MODEL_LABELS_HR, type DailyRow } from "@/utils/forecastTable";
import { modelColor, windyApi, type ForecastResponse, type ForecastModel, type SiteResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// SCREEN 1 (P0): "Očekivana proizvodnja po danima" — dnevna tablica MWh po
// modelu, CET fiksan dan (24 h), djelomični dani označeni. Zatim "Satni
// detalji" 24/48 h. Grafikoni i metodologija su sekundarni (collapsed).

const { siteId } = useActiveSite();
const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(siteId.value, signal), () => [siteId.value]);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;

const HORIZON_OPTIONS = [
  { value: 7, label: "7 dana" },
  { value: 10, label: "10 dana" },
  { value: 15, label: "15 dana" },
] as const;
const days = ref<7 | 10 | 15>(7);
const fcResource = useAbortableResource<ForecastResponse>(
  (signal) => windyApi.forecast(days.value, siteId.value, signal),
  () => [days.value, siteId.value],
);
const { data: fcData, loading: fcLoading, error: fcError } = fcResource;

const modelsWithRun = computed<ForecastModel[]>(() => (fcData.value?.models ?? []).filter((m) => m.run !== null));
const dailyRows = computed<DailyRow[]>(() => buildDailyMatrix(modelsWithRun.value));
const modelColumns = computed(() => modelsWithRun.value.map((m) => ({ id: m.model_id, label: MODEL_LABELS_HR[m.model_id] ?? m.model_id })));

const collecting = ref(false);
const collectMessage = ref<string | null>(null);
const collectError = ref<string | null>(null);

async function fetchNow(): Promise<void> {
  collecting.value = true;
  collectError.value = null;
  collectMessage.value = null;
  try {
    const res = await windyApi.collect(days.value, siteId.value);
    collectMessage.value = `Ažurirano ${res.collected_at_utc}`;
    await fcResource.refresh();
  } catch (e) {
    collectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    collecting.value = false;
  }
}

// ---- satni detalji (24/48) -------------------------------------------------

const HOURS_OPTIONS = [
  { value: 24, label: "24 h" },
  { value: 48, label: "48 h" },
] as const;
const hourlyHours = ref<24 | 48>(24);
const hourlyModel = ref<string>("ecmwf_ifs");

const hourlyResource = useAbortableResource<ForecastModel | null>(
  async () => {
    if (!fcData.value) return null;
    return fcData.value.models.find((m) => m.model_id === hourlyModel.value && m.run !== null) ?? null;
  },
  () => [hourlyModel.value, hourlyHours.value, fcData.value],
);
const hourlyRows = computed(() => {
  const m = hourlyResource.data.value;
  if (!m) return [];
  // shared as-of anchor across models: generation time rounded up to the next
  // full hour — the window is the NEXT 24/48 h, never hours from the past
  const anchor = fcData.value?.generated_at_utc ?? new Date().toISOString();
  return buildHourlyRows(m.points, anchor, hourlyHours.value, siteData.value?.site.timezone ?? "Europe/Podgorica");
});

// ---- charts (secondary, collapsed) -----------------------------------------

const axisLabel = { color: "#93896f", fontSize: 10, fontFamily: "JetBrains Mono, monospace" };
const splitLine = { lineStyle: { color: "#1a2638" } };

interface BaseOption {
  [key: string]: unknown;
  backgroundColor: string;
  grid: Record<string, unknown>;
  tooltip: Record<string, unknown>;
  xAxis: Record<string, unknown>;
  yAxis: Record<string, unknown>;
}

function baseOption(yName: string, minInterval = 3600_000): BaseOption {
  return {
    backgroundColor: "transparent",
    grid: { left: 44, right: 12, top: 12, bottom: 24 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#0e1622",
      borderColor: "#1a2638",
      textStyle: { color: "#f4ecd8", fontSize: 11, fontFamily: "JetBrains Mono, monospace" },
      valueFormatter: (v: unknown) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : String(Math.round(Number(v) * 100) / 100)),
    },
    xAxis: {
      type: "time",
      axisLabel: {
        ...axisLabel,
        formatter: (v: number) => (v === undefined || v === null ? "" : new Date(v).toISOString().slice(5, 16).replace("T", " ")),
      },
      axisLine: { lineStyle: { color: "#1a2638" } },
      splitLine: { show: false },
      minInterval,
    },
    yAxis: { type: "value", name: yName, nameTextStyle: { color: "#93896f", fontSize: 10 }, axisLabel, splitLine },
  };
}

const withRun = computed<ForecastModel[]>(() => (fcData.value?.models ?? []).filter((m) => m.run !== null));

function seriesOf(model: ForecastModel, key: "wind_speed_mps" | "modeled_power_mw"): [string, number | null][] {
  return model.points.map((p) => [p.valid_at_utc, p[key]]);
}

const windOption = computed<BaseOption>(() => ({
  ...baseOption("m/s"),
  series: withRun.value.map((m) => ({
    name: m.label,
    type: "line",
    showSymbol: false,
    connectNulls: false,
    data: seriesOf(m, "wind_speed_mps"),
    lineStyle: { width: 1.4, color: modelColor(m.model_id) },
    itemStyle: { color: modelColor(m.model_id) },
  })),
}));

const powerOption = computed<BaseOption>(() => ({
  ...baseOption("MW"),
  series: withRun.value.map((m) => ({
    name: m.label,
    type: "line",
    showSymbol: false,
    connectNulls: false,
    data: seriesOf(m, "modeled_power_mw"),
    lineStyle: { width: 1.4, color: modelColor(m.model_id) },
    itemStyle: { color: modelColor(m.model_id) },
  })),
}));

const freshness = computed(() => {
  const runs = modelsWithRun.value.map((m) => m.run?.fetched_at_utc ?? "").filter(Boolean);
  return runs.length ? runs.sort().at(-1) : null;
});
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-5 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">Backend nedostupan — pokrenite ga s <code>uvicorn app.main:app</code>. {{ siteError }}</StateBlock>
      <StateBlock v-else-if="siteLoading" kind="loading" caption="Spajanje na Windy backend…" />

      <template v-else-if="siteData">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <SiteSelector />
            <div>
              <h1 class="text-paper-50 font-mono text-lg tracking-wide">{{ siteData.site.name }}</h1>
              <p class="text-paper-300 font-mono text-[11px]">{{ siteData.site.capacity_mw }} MW · {{ siteData.site.turbine_count }} × {{ siteData.site.turbine_rated_mw }} MW</p>
            </div>
          </div>
          <div class="flex items-center gap-3">
            <SegmentedToggle v-model="days" :options="[...HORIZON_OPTIONS]" inline divided />
            <button
              type="button"
              class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]"
              :disabled="collecting"
              @click="fetchNow"
            >{{ collecting ? "Dohvaćam…" : "Osvježi" }}</button>
          </div>
        </div>

        <p v-if="freshness" class="text-paper-400 font-mono text-[10px]">
          Podaci zadnje preuzeti: <span class="text-paper-100">{{ freshness }}</span> (UTC) · vremena po satima prikazana u CET
        </p>

        <StateBlock v-if="collectError" kind="error">Dohvat nije uspio: {{ collectError }}</StateBlock>
        <StateBlock v-if="fcError" kind="error">{{ fcError }}</StateBlock>
        <StateBlock v-else-if="fcLoading && !fcData" kind="loading" caption="Učitavam arhivirane prognoze…" />

        <template v-if="fcData">
          <!-- ===== P0: dnevna tablica — TABLE FIRST ===== -->
          <section aria-label="Očekivana proizvodnja po danima">
            <h2 class="text-paper-50 mb-2 font-mono text-base tracking-wide">Očekivana proizvodnja po danima (MWh)</h2>
            <StateBlock v-if="dailyRows.length === 0" kind="empty" :relaxed="true">
              Nema arhiviranih prognoza — pritisnite „Osvježi".
            </StateBlock>
            <div v-else class="overflow-x-auto">
              <table class="w-full font-mono text-[11px]">
                <thead>
                  <tr class="text-paper-400 border-ink-700 border-b text-left">
                    <th class="py-1.5 pr-4 sticky left-0 bg-ink-950">Datum</th>
                    <th v-for="c in modelColumns" :key="c.id" class="py-1.5 pr-4 text-right">
                      <span class="inline-flex items-center gap-1.5">
                        <span class="inline-block size-2" :style="{ background: modelColor(c.id) }" />
                        {{ c.label }}
                      </span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in dailyRows" :key="row.date" class="border-ink-800 border-b">
                    <td class="text-paper-100 py-1.5 pr-4 sticky left-0 bg-ink-950">{{ row.date }}</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-1.5 pr-4 text-right" :class="row.values[c.id] !== null ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="row.values[c.id] !== null">
                        {{ row.values[c.id] }}<span v-if="row.flags[c.id]" class="text-paper-400" :title="row.flags[c.id]"> ⚠</span>
                      </template>
                      <template v-else :title="row.flags[c.id] || 'nema podataka'">—</template>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p class="text-paper-400 mt-2 font-mono text-[10px]">
                Vrijedi za CET dane (24 h). ⚠ = djelomičan dan (prvi/zadnji dan ili nedostajući sat) — kliknite „Detalji o podacima" za pojašnjenje. Procjena na temelju vjetra i krivulje snage; nije jamstvo proizvodnje.
              </p>
            </div>
          </section>

          <!-- ===== P0: satni detalji 24/48 ===== -->
          <section aria-label="Satni detalji" class="mt-4">
            <div class="mb-2 flex flex-wrap items-center gap-3">
              <h2 class="text-paper-50 font-mono text-base tracking-wide">Satni detalji</h2>
              <SegmentedToggle v-model="hourlyHours" :options="[...HOURS_OPTIONS]" inline divided />
              <SegmentedToggle
                v-model="hourlyModel"
                :options="modelColumns.map((c) => ({ value: c.id, label: c.label }))"
                inline
                divided
              />
            </div>
            <div class="overflow-x-auto">
              <table class="w-full max-w-xl font-mono text-[11px]">
                <thead>
                  <tr class="text-paper-400 border-ink-700 border-b text-left">
                    <th class="py-1.5 pr-4">Datum i sat ({{ siteData.site.timezone }})</th>
                    <th class="py-1.5 pr-4 text-right">Vjetar (m/s)</th>
                    <th class="py-1.5 pr-4 text-right">Snaga (MW)</th>
                    <th class="py-1.5 pr-4 text-right">Energija (MWh)</th>
                    <th class="py-1.5 pr-4 text-right">Interval</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in hourlyRows" :key="r.label + r.offset" class="border-ink-800 border-b" :class="r.missing ? 'opacity-50' : ''">
                    <td class="text-paper-100 py-1 pr-4">{{ r.label }} <span class="text-paper-400">{{ r.offset }}</span></td>
                    <td class="py-1 pr-4 text-right" :class="r.wind === null ? 'text-heat-300' : 'text-paper-100'">{{ r.wind ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mw === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mw ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mwh === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mwh ?? "nedostaje" }}</td>
                    <td class="text-paper-400 py-1 pr-4 text-right">{{ r.interval_hours ? r.interval_hours + " h" : "—" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="text-paper-400 mt-2 font-mono text-[10px]">MW = prosječna snaga intervala; MWh = energija intervala (trajanje u zadnjem stupcu). „nedostaje" = podatka nema (nikad 0).</p>
          </section>

          <!-- ===== sekundarno: grafikoni ===== -->
          <CollapsibleSection title="Grafikoni (vjetar i snaga po satima)" :default-open="false" lazy>
            <p class="text-paper-400 font-mono text-[10px]">Praznine ostaju praznine — nikad se ne popunjavaju nulama. ICON-EU završava na 120 h, ICON Global na 192 h.</p>
            <VChart style="height: 18rem" :option="windOption" autoresize />
            <VChart style="height: 16rem" :option="powerOption" autoresize />
          </CollapsibleSection>

          <!-- ===== metodologija (collapsed) ===== -->
          <CollapsibleSection title="Detalji o podacima i metodologiji" :default-open="false">
            <dl class="space-y-2 font-mono text-[10px] leading-relaxed">
              <div v-for="m in fcData.models" :key="m.model_id" class="border-ink-700 border-b pb-2">
                <dt class="text-sodium-200">{{ m.label }} — {{ m.provider }} (preko Open-Meteo)</dt>
                <dd class="text-paper-300">
                  {{ m.height_note }}
                  <template v-if="m.run">Izdano: {{ m.run.run_issued_utc ?? "nepoznato" }} · preuzeto: {{ m.run.fetched_at_utc }} · sati: {{ m.coverage.n_hours }}.</template>
                  <template v-else>Još nije arhivirano.</template>
                  <span v-for="w in m.warnings" :key="w" class="text-heat-300 block">{{ w }}</span>
                </dd>
              </div>
              <div class="border-ink-700 border-b pb-2">
                <dt class="text-sodium-200">Krivulja snage</dt>
                <dd class="text-paper-300">{{ fcData.power_curve.label }} — {{ fcData.power_curve.provenance }}</dd>
              </div>
              <div class="pb-2">
                <dt class="text-sodium-200">Izvor podataka</dt>
                <dd class="text-paper-300">{{ siteData.source_note }} Vremenske oznake u UTC; dnevni redovi po CET (+1 h fiksno).</dd>
              </div>
            </dl>
          </CollapsibleSection>
        </template>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
