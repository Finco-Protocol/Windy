<script setup lang="ts">
import { computed, ref } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { modelColor, windyApi, type ForecastResponse, type ForecastModel, type SiteResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// Screen A — Forecast. Everything shown here comes from the backend archive:
// the same data Historical Runs stores, so the UI can never display something
// that was not persisted with its provenance.

const { siteId } = useActiveSite();
const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(siteId.value, signal), () => [siteId.value]);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;

const DAYS_OPTIONS = [
  { value: 7, label: "7 days" },
  { value: 10, label: "10 days" },
] as const;
const days = ref<7 | 10>(7);
const fcResource = useAbortableResource<ForecastResponse>(
  (signal) => windyApi.forecast(days.value, siteId.value, signal),
  () => [days.value, siteId.value],
);
const { data: fcData, loading: fcLoading, error: fcError } = fcResource;

// ---- collection -----------------------------------------------------------

const collecting = ref(false);
const collectMessage = ref<string | null>(null);
const collectError = ref<string | null>(null);

async function fetchNow(): Promise<void> {
  collecting.value = true;
  collectError.value = null;
  collectMessage.value = null;
  try {
    const res = await windyApi.collect(days.value, siteId.value);
    const parts = res.models.map((m) => `${m.model_id}: ${m.status}${m.run_issued_utc ? ` (${m.run_issued_utc})` : ""}`);
    collectMessage.value = `Collected ${res.collected_at_utc} — ${parts.join(" · ")}`;
    await fcResource.refresh();
  } catch (e) {
    collectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    collecting.value = false;
  }
}

// ---- chart data -----------------------------------------------------------

const withRun = computed<ForecastModel[]>(() => (fcData.value?.models ?? []).filter((m) => m.run !== null));

function seriesOf(model: ForecastModel, key: "wind_speed_mps" | "modeled_power_mw"): [string, number | null][] {
  return model.points.map((p) => [p.valid_at_utc, p[key]]);
}

const unionDates = computed<string[]>(() => {
  const set = new Set<string>();
  for (const m of withRun.value) for (const d of m.daily_local) set.add(d.date);
  return [...set].sort();
});

function cumulative(model: ForecastModel): [string, number | null][] {
  // Cumulative over COMPUTED hours only. After a missing-energy point the
  // trajectory becomes null (line breaks) — a flat carried-forward line would
  // silently present a continuous full forecast across missing data.
  let acc = 0;
  let broken = false;
  return model.points.map((p) => {
    if (p.interval_energy_mwh === null) broken = true;
    else acc += p.interval_energy_mwh;
    return [p.valid_at_utc, broken ? null : Math.round(acc * 1000) / 1000] as [string, number | null];
  });
}

const axisLabel = { color: "#93896f", fontSize: 10, fontFamily: "JetBrains Mono, monospace" };
const splitLine = { lineStyle: { color: "#1a2638" } };

interface BaseOption {
  [key: string]: unknown; // EChartsOption wants an index signature
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

const windOption = computed<BaseOption>(() => ({
  ...baseOption("m/s"),
  series: withRun.value.map((m) => ({
    name: m.label,
    type: "line",
    showSymbol: false,
    connectNulls: false, // missing levels stay gaps — never bridged or zero-filled
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
    areaStyle: { opacity: 0.08, color: modelColor(m.model_id) },
  })),
}));

const dailyOption = computed<BaseOption>(() => {
  const base = baseOption("MWh/day", undefined);
  return {
    ...base,
    xAxis: {
      ...base.xAxis,
      type: "category",
      data: unionDates.value,
      axisLabel: { ...axisLabel, formatter: (v: string) => v.slice(5) },
    },
    series: withRun.value.map((m) => {
      const byDate = new Map(m.daily_local.map((d) => [d.date, d.mwh]));
      return {
        name: m.label,
        type: "bar",
        barMaxWidth: 14,
        data: unionDates.value.map((d) => byDate.get(d) ?? null), // shorter horizons get no bar, not zero
        itemStyle: { color: modelColor(m.model_id) },
      };
    }),
  };
});

const cumulativeOption = computed<BaseOption>(() => ({
  ...baseOption("MWh (cumulative, own window)"),
  series: withRun.value.map((m) => ({
    name: m.label,
    type: "line",
    showSymbol: false,
    connectNulls: false,
    data: cumulative(m),
    lineStyle: { width: 1.4, color: modelColor(m.model_id) },
    itemStyle: { color: modelColor(m.model_id) },
  })),
}));

const curveUncalibrated = computed(() => fcData.value?.power_curve.status !== "manufacturer-reference");
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">
        Backend unreachable — start it with <code>uvicorn app.main:app</code> (see README). {{ siteError }}
      </StateBlock>

      <StateBlock v-else-if="siteLoading" kind="loading" caption="Connecting to Windy backend…" />

      <template v-else-if="siteData">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <SiteSelector />
            <div>
              <h1 class="text-paper-50 font-mono text-lg tracking-wide">{{ siteData.site.name }}</h1>
              <p class="text-paper-300 font-mono text-[11px]">
                {{ siteData.site.capacity_mw }} MW · {{ siteData.site.turbine_count }} × {{ siteData.site.turbine_rated_mw }} MW · hub {{ siteData.site.hub_height_m }} m (provisional)
              </p>
            </div>
          </div>
          <div class="flex items-center gap-3">
            <SegmentedToggle v-model="days" :options="DAYS_OPTIONS" inline divided />
            <button
              type="button"
              class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px] tracking-wide transition-colors"
              :disabled="collecting"
              @click="fetchNow"
            >
              {{ collecting ? "Fetching…" : "Fetch now" }}
            </button>
          </div>
        </div>

        <p class="text-paper-400 font-mono text-[10px] leading-relaxed">{{ siteData.source_note }}</p>

        <StateBlock v-if="collectError" kind="error">Fetch failed: {{ collectError }}</StateBlock>
        <p v-if="collectMessage" class="text-sodium-200 font-mono text-[10px] leading-relaxed">{{ collectMessage }}</p>

        <StateBlock v-if="fcError" kind="error">{{ fcError }}</StateBlock>
        <StateBlock v-else-if="fcLoading && !fcData" kind="loading" caption="Loading archived forecasts…" />

        <template v-if="fcData">
          <!-- per-model status chips: source, issue time, height policy, coverage -->
          <div class="grid gap-2 sm:grid-cols-2">
            <div
              v-for="m in fcData.models"
              :key="m.model_id"
              class="border-ink-700 bg-ink-900/40 space-y-1 border p-3 font-mono text-[10px] leading-relaxed"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="flex items-center gap-1.5">
                  <span class="inline-block size-2" :style="{ background: modelColor(m.model_id) }" />
                  <span class="text-paper-50">{{ m.label }}</span>
                  <span class="text-paper-400">· {{ m.provider }} via Open-Meteo</span>
                </span>
                <span class="border-ink-700 text-paper-300 border px-1.5 py-0.5">{{ m.height_method }}</span>
              </div>
              <template v-if="m.run">
                <p class="text-paper-300">
                  issued <span class="text-paper-50">{{ m.run.run_issued_utc ?? "UNKNOWN" }}</span>
                  <template v-if="m.run.run_issued_utc"> UTC</template>
                  · {{ m.coverage.n_hours }} h to {{ m.coverage.last_valid_utc ? m.coverage.last_valid_utc.slice(5, 16).replace("T", " ") : "—" }} UTC
                </p>
                <p class="text-paper-400">
                  hub {{ m.requested_height_m }} m from {{ m.height_sources_m.join("+") }} m ·
                  <template v-if="m.height_method === 'proxy'">nearest native level (proxy)</template>
                  <template v-else>log-height interpolation</template>
                </p>
              </template>
              <p v-else class="text-heat-300">No archived run yet — press “Fetch now”.</p>
              <p v-for="w in m.warnings" :key="w" class="text-heat-300">{{ w }}</p>
            </div>
          </div>

          <CollapsibleSection title="Hub-height wind speed (m/s)">
            <p class="text-paper-400 font-mono text-[10px]">Gaps are missing source levels — never interpolated from 10 m, never zero-filled. ICON-EU ends at 120 h, ICON Global at 192 h.</p>
            <VChart style="height: 20rem" :option="windOption" autoresize />
          </CollapsibleSection>

          <CollapsibleSection title="Modeled site power (MW)">
            <p class="text-paper-400 font-mono text-[10px]">
              {{ fcData.power_curve.label }} · {{ fcData.power_curve.provenance }}
              <span v-if="curveUncalibrated" class="text-sodium-200">Uncalibrated for the site — reference values only.</span>
            </p>
            <VChart style="height: 18rem" :option="powerOption" autoresize />
          </CollapsibleSection>

          <CollapsibleSection :title="`Estimated energy (MWh/day, local ${siteData.site.timezone})`">
            <p class="text-paper-400 font-mono text-[10px]">Bars only where the model has data — a 5-day model shows 5 bars, not 10 zero-padded ones.</p>
            <VChart style="height: 16rem" :option="dailyOption" autoresize />
          </CollapsibleSection>

          <CollapsibleSection title="Cumulative MWh (each model's own window)">
            <p class="text-paper-400 font-mono text-[10px]">
              Cross-model totals over the common intersection ({{ fcData.intersection.n_common_hours }} h):
              <span
                v-for="(v, k) in fcData.intersection.total_mwh"
                :key="k"
                class="mr-2"
                :style="{ color: modelColor(String(k)) }"
              >{{ String(k) }} {{ v }} MWh</span>
              — never compare a 5-day total against a 10-day total.
            </p>
            <VChart style="height: 16rem" :option="cumulativeOption" autoresize />
          </CollapsibleSection>

          <CollapsibleSection title="Site assumptions & provenance" :default-open="false">
            <dl class="space-y-2 font-mono text-[10px] leading-relaxed">
              <div v-for="(note, field) in siteData.site.assumptions" :key="field" class="border-ink-700 border-b pb-2">
                <dt class="text-sodium-200">{{ field }}</dt>
                <dd class="text-paper-300">{{ note }}</dd>
              </div>
              <div class="border-ink-700 border-b pb-2">
                <dt class="text-sodium-200">data origin</dt>
                <dd class="text-paper-300">
                  ECMWF / NOAA / DWD model outputs fetched through Open-Meteo ({{ siteData.source_provider }}). They are
                  <strong class="text-paper-50">not</strong> three independent provider APIs. Issue times come from the Open-Meteo
                  Single Runs archive; when that has no run yet, the live forecast is marked “issue time unknown”.
                </dd>
              </div>
            </dl>
          </CollapsibleSection>
        </template>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
