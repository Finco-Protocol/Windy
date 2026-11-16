<script setup lang="ts">
import { computed, ref } from "vue";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import StateBlock from "@/components/StateBlock.vue";
import { modelColor, windyApi, type CompareResponse, type RunDetail, type RunSummary, type SiteResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";

// Screen B — Historical Runs. Every row is an archived forecast issue: what a
// model predicted at a specific run cycle, stored immutably with its raw
// provenance. Backfill is deliberately bounded to ONE run per action.

const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(signal), () => []);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;
const modelFilter = ref<string | null>(null);
const runsResource = useAbortableResource<{ runs: RunSummary[] }>(
  (signal) => windyApi.runs(modelFilter.value, signal),
  () => [modelFilter.value],
);
const { error: runsError } = runsResource;

const runs = computed<RunSummary[]>(() => runsResource.data.value?.runs ?? []);
const hasMultipleIssues = computed(() => new Set(runs.value.map((r) => `${r.model_id}|${r.run_issued_utc}`)).size >= 2);

// ---- selected run + compare -------------------------------------------------

const selectedId = ref<number | null>(null);
const targetDate = ref<string>(new Date(Date.now() - 86400_000).toISOString().slice(0, 10));

const detailResource = useAbortableResource<RunDetail | null>(
  async (signal) => (selectedId.value === null ? null : windyApi.runDetail(selectedId.value, signal)),
  () => [selectedId.value],
);
const { error: detailError } = detailResource;

const compareResource = useAbortableResource<CompareResponse | null>(
  async (signal) => (selectedId.value === null ? null : windyApi.compare(selectedId.value, targetDate.value, signal)),
  () => [selectedId.value, targetDate.value],
);
const { data: compareData, error: compareError } = compareResource;

// ---- backfill ---------------------------------------------------------------

const bfModel = ref<string>("ecmwf_ifs");
const bfDate = ref<string>(new Date(Date.now() - 86400_000).toISOString().slice(0, 10));
const bfHour = ref<0 | 6 | 12 | 18>(0);
const bfBusy = ref(false);
const bfResult = ref<string | null>(null);
const bfError = ref<string | null>(null);

async function backfill(): Promise<void> {
  bfBusy.value = true;
  bfError.value = null;
  bfResult.value = null;
  try {
    const res = await windyApi.backfill(bfModel.value, bfDate.value, bfHour.value);
    bfResult.value = `${res.model_id} run ${res.run_issued_utc} ${res.created ? "archived" : "already archived"} · ${res.points} hourly points`;
    await runsResource.refresh();
  } catch (e) {
    bfError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bfBusy.value = false;
  }
}

function issueBadge(source: string): string {
  if (source === "single-runs-archive") return "archive";
  if (source === "live-forecast-issue-unknown") return "issue unknown";
  return source;
}
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">Backend unreachable — start it with <code>uvicorn app.main:app</code>. {{ siteError }}</StateBlock>
      <StateBlock v-else-if="siteLoading" kind="loading" caption="Connecting…" />

      <template v-else>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h1 class="text-paper-50 font-mono text-lg tracking-wide">Historical runs</h1>
          <a
            href="/api/export/forecast.csv"
            class="border-ink-700 text-paper-300 hover:border-sodium-300/60 hover:text-sodium-200 border px-3 py-1 font-mono text-[11px] transition-colors"
          >Download archive CSV</a>
        </div>
        <p class="text-paper-400 font-mono text-[10px] leading-relaxed">
          Each run is one model issue cycle, stored unchanged. “archive” rows carry the true issue time (Single Runs API);
          “issue unknown” rows came from the live endpoint and can never be scored against actuals.
        </p>
        <StateBlock v-if="runs.length > 0 && !hasMultipleIssues" kind="empty" :relaxed="true">
          Only one issue cycle archived so far. Collect again later (or use backfill below) — a second issue appears alongside this one, never overwriting it.
        </StateBlock>

        <CollapsibleSection title="Archived runs">
          <div class="mb-2 flex flex-wrap items-center gap-2">
            <span class="text-paper-400 font-mono text-[10px]">model</span>
            <select
              v-model="modelFilter"
              class="border-ink-700 bg-ink-900 text-paper-100 font-mono text-[11px] border px-2 py-1"
            >
              <option :value="null">all models</option>
              <option v-for="m in siteData?.models ?? []" :key="m.id" :value="m.id">{{ m.label }}</option>
            </select>
          </div>
          <StateBlock v-if="runsError" kind="error">{{ runsError }}</StateBlock>
          <StateBlock v-else-if="runs.length === 0" kind="empty" :relaxed="true">
            Nothing archived yet — press “Fetch now” on the Forecast screen, or backfill one run below.
          </StateBlock>
          <div v-else class="overflow-x-auto">
            <table class="w-full font-mono text-[10px]">
              <thead>
                <tr class="text-paper-400 border-ink-700 border-b text-left">
                  <th class="py-1.5 pr-3">model</th>
                  <th class="py-1.5 pr-3">issued (UTC)</th>
                  <th class="py-1.5 pr-3">source</th>
                  <th class="py-1.5 pr-3">fetched (UTC)</th>
                  <th class="py-1.5 pr-3">points</th>
                  <th class="py-1.5 pr-3">valid until</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="r in runs"
                  :key="r.id"
                  class="border-ink-800 hover:bg-ink-900/60 cursor-pointer border-b"
                  :class="selectedId === r.id ? 'bg-sodium-300/10' : ''"
                  @click="selectedId = r.id"
                >
                  <td class="py-1.5 pr-3" :style="{ color: modelColor(r.model_id) }">{{ r.model_id }}</td>
                  <td class="text-paper-100 py-1.5 pr-3">{{ r.run_issued_utc ?? "UNKNOWN" }}</td>
                  <td class="py-1.5 pr-3">
                    <span
                      class="border px-1.5 py-0.5"
                      :class="r.issue_source === 'single-runs-archive' ? 'border-sodium-300/40 text-sodium-200' : 'border-heat-500/40 text-heat-300'"
                    >{{ issueBadge(r.issue_source) }}</span>
                  </td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ r.fetched_at_utc }}</td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ r.n_points }}</td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ r.last_valid_utc?.slice(5, 16).replace("T", " ") ?? "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </CollapsibleSection>

        <CollapsibleSection title="What did this issue predict?" :default-open="false" lazy>
          <p class="text-paper-400 mb-2 font-mono text-[10px]">Select a run above, then pick a target date. The table shows that issue's own prediction for the target date — not a reanalysis, not a current forecast.</p>
          <StateBlock v-if="selectedId === null" kind="empty">No run selected.</StateBlock>
          <template v-else>
            <div class="mb-2 flex items-center gap-2">
              <label class="text-paper-400 font-mono text-[10px]" for="target-date">target date (UTC)</label>
              <input id="target-date" v-model="targetDate" type="date" class="border-ink-700 bg-ink-900 text-paper-100 font-mono text-[11px] border px-2 py-1" />
            </div>
            <StateBlock v-if="detailError || compareError" kind="error">{{ detailError ?? compareError }}</StateBlock>
            <StateBlock v-else-if="!compareData" kind="loading" loading-py="py-8" caption="Loading…" />
            <template v-else-if="compareData">
              <p class="text-paper-300 font-mono text-[10px]">
                {{ compareData.model_id }} issued {{ compareData.run_issued_utc }} → target {{ targetDate }}:
                <span class="text-paper-50">{{ compareData.total_mwh }} MWh</span> over {{ compareData.points.length }} h
              </p>
              <div v-if="compareData.points.length > 0" class="overflow-x-auto">
                <table class="w-full font-mono text-[10px]">
                  <thead>
                    <tr class="text-paper-400 border-ink-700 border-b text-left">
                      <th class="py-1.5 pr-3">valid (UTC)</th>
                      <th class="py-1.5 pr-3">wind m/s</th>
                      <th class="py-1.5 pr-3">MW</th>
                      <th class="py-1.5 pr-3">MWh</th>
                      <th class="py-1.5 pr-3">status</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="p in compareData.points" :key="p.valid_at_utc" class="border-ink-800 border-b">
                      <td class="py-1 pr-3">{{ p.valid_at_utc }}</td>
                      <td class="py-1 pr-3">{{ p.wind_speed_mps ?? "—" }}</td>
                      <td class="py-1 pr-3">{{ p.modeled_power_mw ?? "—" }}</td>
                      <td class="py-1 pr-3">{{ p.interval_energy_mwh ?? "—" }}</td>
                      <td class="text-paper-400 py-1 pr-3">{{ p.status }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <StateBlock v-else kind="empty">This issue has no valid hours on the target date.</StateBlock>
            </template>
          </template>
        </CollapsibleSection>

        <CollapsibleSection title="Backfill one archived run" :default-open="false">
          <p class="text-paper-400 mb-2 font-mono text-[10px]">
            Opt-in and bounded: one run per action. If the archive doesn't hold that run (retention is a moving window),
            the result is UNAVAILABLE — the app never substitutes reanalysis or a current forecast for a past issue.
          </p>
          <div class="flex flex-wrap items-end gap-2">
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">model</span>
              <select v-model="bfModel" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]">
                <option v-for="m in siteData?.models ?? []" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">run date (UTC)</span>
              <input v-model="bfDate" type="date" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">run cycle</span>
              <select v-model.number="bfHour" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]">
                <option :value="0">00Z</option>
                <option :value="6">06Z</option>
                <option :value="12">12Z</option>
                <option :value="18">18Z</option>
              </select>
            </label>
            <button
              type="button"
              class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px] transition-colors"
              :disabled="bfBusy"
              @click="backfill"
            >
              {{ bfBusy ? "Backfilling…" : "Backfill run" }}
            </button>
          </div>
          <p v-if="bfResult" class="text-sodium-200 mt-2 font-mono text-[10px]">{{ bfResult }}</p>
          <p v-if="bfError" class="text-heat-300 mt-2 font-mono text-[10px]">{{ bfError }}</p>
        </CollapsibleSection>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
