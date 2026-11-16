<script setup lang="ts">
import { computed, onScopeDispose, ref } from "vue";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import {
  modelColor,
  windyApi,
  type ArchiveHealthModel,
  type BatchJob,
  type BatchPreview,
  type CompareResponse,
  type RunDetail,
  type RunSummary,
  type SiteResponse,
} from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// Screen B — Historical Runs. Every row is an archived forecast issue: what a
// model predicted at a specific run cycle, stored immutably with its raw
// provenance. Backfill is deliberately bounded to ONE run per action.

const { siteId } = useActiveSite();
const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(siteId.value, signal), () => [siteId.value]);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;
const modelFilter = ref<string | null>(null);
const runsResource = useAbortableResource<{ runs: RunSummary[] }>(
  (signal) => windyApi.runs(modelFilter.value, siteId.value, signal),
  () => [modelFilter.value, siteId.value],
);
const { error: runsError } = runsResource;

const runs = computed<RunSummary[]>(() => runsResource.data.value?.runs ?? []);
const hasMultipleIssues = computed(() => new Set(runs.value.map((r) => `${r.model_id}|${r.run_issued_utc}`)).size >= 2);

// ---- selected run + compare -------------------------------------------------

const selectedId = ref<number | null>(null);
const targetDate = ref<string>(new Date(Date.now() - 86400_000).toISOString().slice(0, 10));

const detailResource = useAbortableResource<RunDetail | null>(
  async (signal) => (selectedId.value === null ? null : windyApi.runDetail(selectedId.value, siteId.value, signal)),
  () => [selectedId.value, siteId.value],
);
const { error: detailError } = detailResource;

const compareResource = useAbortableResource<CompareResponse | null>(
  async (signal) => (selectedId.value === null ? null : windyApi.compare(selectedId.value, targetDate.value, siteId.value, signal)),
  () => [selectedId.value, targetDate.value, siteId.value],
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
    await healthResource.refresh();
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

// ---- archive health ------------------------------------------------------------

const healthResource = useAbortableResource<{ models: ArchiveHealthModel[] }>(
  (signal) => windyApi.archiveHealth(siteId.value, signal),
  () => [siteId.value],
);
const { data: healthData } = healthResource;

// ---- batch backfill ---------------------------------------------------------------

const ALL_MODELS = ["ecmwf_ifs", "gfs_seamless", "icon_global", "icon_eu"] as const;
const selectedModels = ref<string[]>([...ALL_MODELS]);
const batchFrom = ref<string>(new Date(Date.now() - 7 * 86400_000).toISOString().slice(0, 10));
const batchTo = ref<string>(new Date(Date.now() - 86400_000).toISOString().slice(0, 10));
const selectedCycles = ref<number[]>([0, 12]);
const batchDays = ref<number>(10);
const batchMaxRequests = ref<number>(60);

const preview = ref<BatchPreview | null>(null);
const activeJob = ref<BatchJob | null>(null);
const batchError = ref<string | null>(null);
const batchBusy = ref(false);
let pollTimer: ReturnType<typeof setInterval> | null = null;

onScopeDispose(() => {
  if (pollTimer) clearInterval(pollTimer);
});

const ALL_CYCLES = [0, 6, 12, 18];

function toggleModel(id: string): void {
  selectedModels.value = selectedModels.value.includes(id)
    ? selectedModels.value.filter((m) => m !== id)
    : [...selectedModels.value, id];
}

function toggleCycle(c: number): void {
  selectedCycles.value = selectedCycles.value.includes(c)
    ? selectedCycles.value.filter((x) => x !== c)
    : [...selectedCycles.value.filter((x) => x !== c), c].sort((a, b) => a - b);
}

function batchBody(): Record<string, unknown> {
  return {
    models: selectedModels.value,
    date_from: batchFrom.value,
    date_to: batchTo.value,
    cycles: selectedCycles.value,
    days: batchDays.value,
    max_requests: batchMaxRequests.value,
    site_id: siteId.value,
  };
}

async function runDryRun(): Promise<void> {
  batchError.value = null;
  try {
    preview.value = await windyApi.batchPreview(batchBody());
  } catch (e) {
    preview.value = null;
    batchError.value = e instanceof Error ? e.message : String(e);
  }
}

function startPolling(jobId: number): void {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      activeJob.value = await windyApi.batchGet(jobId);
      if (activeJob.value && !["running", "cancelling"].includes(activeJob.value.status)) {
        stopPolling();
        await runsResource.refresh();
        await healthResource.refresh();
      }
    } catch {
      stopPolling();
    }
  }, 1500);
}

function stopPolling(): void {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function startBatch(): Promise<void> {
  batchError.value = null;
  batchBusy.value = true;
  try {
    const res = await windyApi.batchStart(batchBody());
    activeJob.value = await windyApi.batchGet(res.job_id);
    startPolling(res.job_id);
  } catch (e) {
    batchError.value = e instanceof Error ? e.message : String(e);
  } finally {
    batchBusy.value = false;
  }
}

async function cancelBatch(): Promise<void> {
  if (activeJob.value) await windyApi.batchCancel(activeJob.value.id);
}

async function resumeBatch(): Promise<void> {
  if (!activeJob.value) return;
  batchError.value = null;
  try {
    await windyApi.batchResume(activeJob.value.id);
    startPolling(activeJob.value.id);
  } catch (e) {
    batchError.value = e instanceof Error ? e.message : String(e);
  }
}

const jobProgressPct = computed(() => {
  const job = activeJob.value;
  if (!job || job.planned_total === 0) return 0;
  return Math.round(((job.n_stored + job.n_already + job.n_unavailable + job.n_failed + job.n_skipped) / job.planned_total) * 100);
});

const RECENT_RESULTS = 24;
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">Backend unreachable — start it with <code>uvicorn app.main:app</code>. {{ siteError }}</StateBlock>
      <StateBlock v-else-if="siteLoading" kind="loading" caption="Connecting…" />

      <template v-else>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <SiteSelector />
            <h1 class="text-paper-50 font-mono text-lg tracking-wide">Historical runs</h1>
          </div>
          <a
            :href="`/api/export/forecast.csv?site_id=${encodeURIComponent(siteId)}`"
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

        <CollapsibleSection title="Archive health" :default-open="false" lazy>
          <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
            Verified issue cycles vs live-fallback runs (issue unknown — not scoreable), first/last issue, points, and
            6-hourly cycles missing inside the archived window. Cross-model comparisons only ever use the same valid
            times and comparable lead windows; missing stays missing.
          </p>
          <StateBlock v-if="healthData && healthData.models.every((m) => m.known_issue_cycles === 0 && m.issue_unknown_runs === 0)" kind="empty">
            Nothing archived yet.
          </StateBlock>
          <div v-else-if="healthData" class="overflow-x-auto">
            <table class="w-full font-mono text-[10px]">
              <thead>
                <tr class="text-paper-400 border-ink-700 border-b text-left">
                  <th class="py-1.5 pr-3">model</th>
                  <th class="py-1.5 pr-3">verified cycles</th>
                  <th class="py-1.5 pr-3">issue-unknown</th>
                  <th class="py-1.5 pr-3">first → last issue (UTC)</th>
                  <th class="py-1.5 pr-3">points</th>
                  <th class="py-1.5 pr-3">missing cycles</th>
                  <th class="py-1.5 pr-3">last fetch (UTC)</th>
                  <th class="py-1.5 pr-3">last error</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="m in healthData.models" :key="m.model_id" class="border-ink-800 border-b">
                  <td class="py-1.5 pr-3" :style="{ color: modelColor(m.model_id) }">{{ m.model_id }}</td>
                  <td class="text-paper-100 py-1.5 pr-3">{{ m.known_issue_cycles }}</td>
                  <td class="py-1.5 pr-3" :class="m.issue_unknown_runs > 0 ? 'text-heat-300' : 'text-paper-300'">{{ m.issue_unknown_runs }}</td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ m.first_issue_utc ?? "—" }} → {{ m.last_issue_utc ?? "—" }}</td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ m.total_points }}</td>
                  <td class="py-1.5 pr-3" :class="m.n_missing_cycles ? 'text-sodium-200' : 'text-paper-300'">
                    {{ m.n_missing_cycles ?? "—" }}<span v-if="m.missing_cycles.length" class="text-paper-400"> ({{ m.missing_cycles.join(", ") }})</span>
                  </td>
                  <td class="text-paper-300 py-1.5 pr-3">{{ m.last_fetched_utc ?? "—" }}</td>
                  <td class="py-1.5 pr-3" :class="m.last_error ? 'text-heat-300' : 'text-paper-400'">{{ m.last_error ?? "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <button
            type="button"
            class="border-ink-600 text-paper-100 hover:border-sodium-300/60 mt-2 border px-2.5 py-1 font-mono text-[10px] transition-colors"
            @click="healthResource.refresh()"
          >Refresh</button>
        </CollapsibleSection>

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

        <CollapsibleSection title="Batch backfill (bounded)" :default-open="false">
          <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
            Bounded batch backfill from the Single Runs archive: pick models, a UTC issue-date range and run cycles;
            a hard request cap bounds provider load. Every archived issue keeps its original issue timestamp and is
            immutable — already-archived cycles report as such, unpublished cycles report unavailable, and nothing is
            ever substituted with reanalysis. Progress checkpoints survive reloads and restarts; resume is explicit.
          </p>
          <div class="space-y-2">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-paper-400 font-mono text-[10px]">models</span>
              <button
                v-for="id in ALL_MODELS"
                :key="id"
                type="button"
                class="border px-2 py-0.5 font-mono text-[10px] transition-colors"
                :class="selectedModels.includes(id) ? 'border-sodium-300/60 text-sodium-200 bg-sodium-300/10' : 'border-ink-700 text-paper-400'"
                :style="selectedModels.includes(id) ? { borderColor: modelColor(id) } : {}"
                @click="toggleModel(id)"
              >{{ id }}</button>
              <button
                type="button"
                class="border-ink-700 text-paper-300 border px-2 py-0.5 font-mono text-[10px]"
                @click="selectedModels = selectedModels.length === ALL_MODELS.length ? [] : [...ALL_MODELS]"
              >{{ selectedModels.length === ALL_MODELS.length ? "none" : "all" }}</button>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">issue dates (UTC)</span>
                <input v-model="batchFrom" type="date" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
                <span class="text-paper-400"> → </span>
                <input v-model="batchTo" type="date" class="border-ink-700 bg-ink-900 text-paper-100 border px-2 py-1 font-mono text-[11px]" />
              </label>
              <div class="font-mono text-[10px]">
                <span class="text-paper-400 block">run cycles</span>
                <button
                  v-for="c in ALL_CYCLES"
                  :key="c"
                  type="button"
                  class="mr-1 mt-0.5 border px-2 py-1 font-mono text-[10px] transition-colors"
                  :class="selectedCycles.includes(c) ? 'border-sodium-300/60 text-sodium-200 bg-sodium-300/10' : 'border-ink-700 text-paper-400'"
                  @click="toggleCycle(c)"
                >{{ String(c).padStart(2, "0") }}Z</button>
              </div>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">horizon days</span>
                <input v-model.number="batchDays" type="number" min="1" max="16" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-16 border px-2 py-1 font-mono text-[11px]" />
              </label>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">max requests (1–200)</span>
                <input v-model.number="batchMaxRequests" type="number" min="1" max="200" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-20 border px-2 py-1 font-mono text-[11px]" />
              </label>
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="border-ink-600 text-paper-100 hover:border-sodium-300/60 border px-3 py-1.5 font-mono text-[11px] transition-colors"
                @click="runDryRun"
              >Dry run</button>
              <button
                type="button"
                class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px] transition-colors"
                :disabled="batchBusy || selectedModels.length === 0 || selectedCycles.length === 0"
                @click="startBatch"
              >Backfill</button>
            </div>
            <StateBlock v-if="batchError" kind="error">{{ batchError }}</StateBlock>
            <div v-if="preview" class="border-ink-700 border p-2 font-mono text-[10px]">
              <p class="text-paper-300 mb-1">
                plan: {{ preview.total_planned }} request(s) ({{ preview.total_skipped }} skipped beyond cap),
                est. {{ preview.est_points }} hourly points
              </p>
              <p v-for="pm in preview.per_model" :key="pm.model_id" class="text-paper-400">
                {{ pm.model_id }}: {{ pm.planned }} planned / {{ pm.skipped }} skipped
              </p>
              <p class="text-paper-400 mt-1">{{ preview.note }}</p>
            </div>
            <div v-if="activeJob" class="border-ink-700 space-y-1 border p-2 font-mono text-[10px]">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="text-paper-100">job #{{ activeJob.id }} — {{ activeJob.status }}</span>
                <span class="flex gap-2">
                  <button
                    v-if="activeJob.status === 'running'"
                    type="button"
                    class="border-heat-500/50 text-heat-300 border px-2 py-0.5"
                    @click="cancelBatch"
                  >Cancel</button>
                  <button
                    v-if="['paused', 'cancelled', 'failed'].includes(activeJob.status)"
                    type="button"
                    class="border-sodium-300/60 text-sodium-200 border px-2 py-0.5"
                    @click="resumeBatch"
                  >Resume</button>
                </span>
              </div>
              <div class="border-ink-700 bg-ink-900 h-1.5 w-full border">
                <div class="bg-sodium-300/70 h-full transition-all" :style="{ width: `${jobProgressPct}%` }" />
              </div>
              <p class="text-paper-300">
                stored {{ activeJob.n_stored }} · already {{ activeJob.n_already }} · unavailable {{ activeJob.n_unavailable }} ·
                failed {{ activeJob.n_failed }} · skipped {{ activeJob.n_skipped }} / {{ activeJob.planned_total }} planned
              </p>
              <p v-if="activeJob.last_error" class="text-heat-300">last error: {{ activeJob.last_error }}</p>
              <div v-if="activeJob.results.length" class="overflow-x-auto">
                <table class="w-full font-mono text-[10px]">
                  <thead>
                    <tr class="text-paper-400 border-ink-700 border-b text-left">
                      <th class="py-1 pr-3">run (UTC)</th>
                      <th class="py-1 pr-3">status</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="r in activeJob.results.slice(-RECENT_RESULTS)" :key="r.seq" class="border-ink-800 border-b">
                      <td class="py-1 pr-3" :style="{ color: modelColor(r.model_id) }">{{ r.model_id }} {{ r.run_issued_utc }}</td>
                      <td class="py-1 pr-3" :class="r.status === 'stored' ? 'text-sodium-200' : r.status === 'unavailable' || r.status === 'failed' ? 'text-heat-300' : 'text-paper-400'">{{ r.status }}</td>
                    </tr>
                  </tbody>
                </table>
                <p v-if="activeJob.results.length > RECENT_RESULTS" class="text-paper-400 mt-1">…latest {{ RECENT_RESULTS }} of {{ activeJob.results.length }}</p>
              </div>
            </div>
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
