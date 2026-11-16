<script setup lang="ts">
import { computed, ref } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import StateBlock from "@/components/StateBlock.vue";
import {
  modelColor,
  windyApi,
  type AccuracyResponse,
  type ActualsImportResult,
  type ActualsMapping,
  type ActualsPreview,
  type CalibrationEntry,
  type OverlayResponse,
  type SiteResponse,
} from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";

// Screen C — Actual vs Forecast. Actuals are measured generation from the
// user's CSV/XLSX; forecast values come exclusively from archived runs. Where
// actuals are missing the app says so — it never invents accuracy.

const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(signal), () => []);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;

// ---- upload + mapping ---------------------------------------------------------

const file = ref<File | null>(null);
const fieldnames = ref<string[]>([]);
const mapping = ref<ActualsMapping>({
  timestamp_col: "",
  value_col: "",
  value_kind: "energy_mwh",
  timestamp_kind: "auto",
  tz: "Europe/Podgorica",
  interval_hours: null,
});
const VALUE_KIND_OPTIONS = [
  { value: "energy_mwh" as const, label: "Energy MWh/interval" },
  { value: "power_mw" as const, label: "Power MW (interval avg)" },
];
const onConflict = ref<"reject" | "replace">("reject");
const preview = ref<ActualsPreview | null>(null);
const importResult = ref<ActualsImportResult | null>(null);
const importError = ref<string | null>(null);
const busy = ref(false);

/** Peek the header row of a plain-text CSV so the column pickers fill
 *  themselves; XLSX columns must be typed manually (preview still validates). */
async function onFileChange(event: Event): Promise<void> {
  preview.value = null;
  importResult.value = null;
  importError.value = null;
  const input = event.target as HTMLInputElement;
  const f = input.files?.[0] ?? null;
  file.value = f;
  fieldnames.value = [];
  if (!f) return;
  if (f.name.toLowerCase().endsWith(".csv") || f.name.toLowerCase().endsWith(".txt")) {
    const head = (await f.slice(0, 4096).text()).split(/\r?\n/)[0] ?? "";
    const delim = head.includes(";") ? ";" : ",";
    fieldnames.value = head.split(delim).map((c) => c.trim().replace(/^"|"$/g, "")).filter(Boolean);
    const first = fieldnames.value[0];
    const second = fieldnames.value[1];
    if (!mapping.value.timestamp_col && first !== undefined) mapping.value.timestamp_col = first;
    if (!mapping.value.value_col && second !== undefined) mapping.value.value_col = second;
  }
}

async function doPreview(): Promise<void> {
  if (!file.value) return;
  busy.value = true;
  importError.value = null;
  importResult.value = null;
  try {
    preview.value = await windyApi.actualsPreview(file.value, mapping.value);
  } catch (e) {
    preview.value = null;
    importError.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function doImport(): Promise<void> {
  if (!file.value) return;
  busy.value = true;
  importError.value = null;
  try {
    importResult.value = await windyApi.actualsImport(file.value, mapping.value, onConflict.value);
    if (importResult.value.imported) await Promise.all([accuracyResource.refresh(), overlayResource.refresh()]);
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

// ---- accuracy ------------------------------------------------------------------

const day = 86400_000;
const fromUtc = ref<string>(new Date(Date.now() - 7 * day).toISOString().slice(0, 19) + "Z");
const toUtc = ref<string>(new Date(Date.now() + day).toISOString().slice(0, 19) + "Z");
const LEAD_BIN_OPTIONS = [
  { value: "", label: "All leads" },
  { value: "day-ahead (0-24h)", label: "Day-ahead (0-24h)" },
  { value: "2-day ahead (24-48h)", label: "2-day (24-48h)" },
  { value: "3-day ahead (48-72h)", label: "3-day (48-72h)" },
  { value: "5-day ahead (72-120h)", label: "5-day (72-120h)" },
  { value: "7-day ahead (120-168h)", label: "7-day (120-168h)" },
];
const leadBin = ref<string>("");

const useCalibration = ref(false);
const accuracyResource = useAbortableResource<AccuracyResponse>(
  (signal) => windyApi.accuracy(fromUtc.value, toUtc.value, useCalibration.value, signal),
  () => [fromUtc.value, toUtc.value, useCalibration.value],
);
const { data: accuracyData } = accuracyResource;
const overlayResource = useAbortableResource<OverlayResponse>(
  (signal) => windyApi.overlay(fromUtc.value, toUtc.value, leadBin.value || null, useCalibration.value, signal),
  () => [fromUtc.value, toUtc.value, leadBin.value, useCalibration.value],
);
const { data: overlayData } = overlayResource;

// ---- calibration (optional, derived series) -----------------------------------

const calibResource = useAbortableResource<{ models: CalibrationEntry[]; note: string }>(
  (signal) => windyApi.calibration(signal),
  () => [],
);
const { data: calibData } = calibResource;
const calibBusy = ref(false);
const calibMessage = ref<string | null>(null);
const calibError = ref<string | null>(null);

async function trainCalibration(): Promise<void> {
  calibBusy.value = true;
  calibError.value = null;
  calibMessage.value = null;
  try {
    const res = await windyApi.calibrationTrain(fromUtc.value, toUtc.value);
    const models = (res.models as Record<string, { status: string; factor?: number }>) ?? {};
    calibMessage.value = Object.entries(models)
      .map(([id, m]) => `${id}: ${m.status}${m.factor ? ` (factor ${m.factor})` : ""}`)
      .join(" · ");
    await calibResource.refresh();
  } catch (e) {
    calibError.value = e instanceof Error ? e.message : String(e);
  } finally {
    calibBusy.value = false;
  }
}

const CALIB_FALLBACK_NOTE =
  "Optional explainable calibration: one bounded multiplicative factor per model, trained on real measured actuals " +
  "against archived issues with a chronological holdout. Archived forecast values are never modified; the factor only " +
  "scales the derived accuracy view.";

const accuracyRows = computed(() => {
  const models = accuracyData.value?.models ?? {};
  const rows: { model: string; bin: string; binObj: (typeof models)[string]["bins"][string] }[] = [];
  for (const [model, data] of Object.entries(models)) {
    for (const [bin, binObj] of Object.entries(data.bins)) rows.push({ model, bin, binObj });
    if (data.total) rows.push({ model, bin: "all leads", binObj: data.total });
  }
  return rows;
});

const axisLabel = { color: "#93896f", fontSize: 10, fontFamily: "JetBrains Mono, monospace" };

const overlayOption = computed<Record<string, unknown>>(() => {
  const hours = overlayData.value?.hours ?? [];
  const modelIds = [...new Set(hours.flatMap((h) => Object.keys(h.models ?? {})))];
  return {
    backgroundColor: "transparent",
    grid: { left: 48, right: 12, top: 12, bottom: 24 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#0e1622",
      borderColor: "#1a2638",
      textStyle: { color: "#f4ecd8", fontSize: 11, fontFamily: "JetBrains Mono, monospace" },
      valueFormatter: (v: unknown) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : String(Math.round(Number(v) * 100) / 100)),
    },
    xAxis: {
      type: "time",
      axisLabel: { ...axisLabel, formatter: (v: number) => (v === undefined ? "" : new Date(v).toISOString().slice(5, 16).replace("T", " ")) },
      axisLine: { lineStyle: { color: "#1a2638" } },
      splitLine: { show: false },
    },
    yAxis: { type: "value", name: "MWh/h", nameTextStyle: { color: "#93896f", fontSize: 10 }, axisLabel, splitLine: { lineStyle: { color: "#1a2638" } } },
    series: [
      {
        name: "Actual (measured)",
        type: "line",
        showSymbol: false,
        connectNulls: false,
        data: hours.filter((h) => h.actual_mwh !== undefined).map((h) => [h.valid_at_utc, h.actual_mwh]),
        lineStyle: { width: 2, color: "#f4ecd8" },
        itemStyle: { color: "#f4ecd8" },
      },
      ...modelIds.map((id) => ({
        name: id,
        type: "line",
        showSymbol: false,
        connectNulls: false,
        data: hours.filter((h) => h.models?.[id] !== undefined).map((h) => [h.valid_at_utc, h.models?.[id]]),
        lineStyle: { width: 1.3, type: "dashed" as const, color: modelColor(id) },
        itemStyle: { color: modelColor(id) },
      })),
    ],
  };
});
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">Backend unreachable — start it with <code>uvicorn app.main:app</code>. {{ siteError }}</StateBlock>
      <StateBlock v-else-if="siteLoading" kind="loading" caption="Connecting…" />

      <template v-else>
        <h1 class="text-paper-50 font-mono text-lg tracking-wide">Actual vs forecast</h1>
        <p class="text-paper-400 font-mono text-[10px] leading-relaxed">
          Import measured generation (CSV, or XLSX). Timestamps are stored in UTC; naive local times are interpreted in the site
          timezone ({{ siteData?.site.timezone }}) with explicit DST handling. Overwriting existing rows requires the explicit
          “replace” choice.
        </p>

        <CollapsibleSection title="Import actual generation">
          <div class="space-y-3">
            <input type="file" accept=".csv,.xlsx,.xlsm,.txt" class="text-paper-200 font-mono text-[11px]" @change="onFileChange" />

            <div class="grid gap-2 sm:grid-cols-2">
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">timestamp column</span>
                <input
                  v-model="mapping.timestamp_col"
                  list="col-list"
                  class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]"
                />
                <datalist id="col-list">
                  <option v-for="c in fieldnames" :key="c" :value="c" />
                </datalist>
              </label>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">value column</span>
                <input
                  v-model="mapping.value_col"
                  list="col-list"
                  class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]"
                />
              </label>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">timestamps are</span>
                <select v-model="mapping.timestamp_kind" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]">
                  <option value="auto">auto (offset if present, else local tz)</option>
                  <option value="naive-local">naive local (site timezone)</option>
                  <option value="iso-offset">UTC / ISO with offset</option>
                </select>
              </label>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">timezone (for naive local)</span>
                <input v-model="mapping.tz" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]" />
              </label>
              <label class="font-mono text-[10px]">
                <span class="text-paper-400 block">interval hours (empty = infer)</span>
                <input
                  v-model.number="mapping.interval_hours"
                  type="number"
                  step="0.25"
                  min="0.25"
                  placeholder="auto"
                  class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 w-full border px-2 py-1 font-mono text-[11px]"
                />
              </label>
              <div class="font-mono text-[10px]">
                <span class="text-paper-400 block">value kind</span>
                <SegmentedToggle
                  v-model="mapping.value_kind"
                  :options="VALUE_KIND_OPTIONS"
                  padding="px-2 py-1"
                  class="mt-0.5"
                />
              </div>
            </div>

            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="border-ink-600 text-paper-100 hover:border-sodium-300/60 border px-3 py-1.5 font-mono text-[11px] transition-colors disabled:opacity-50"
                :disabled="busy || !file"
                @click="doPreview"
              >Preview</button>
              <span class="text-paper-400 font-mono text-[10px]">on conflict</span>
              <select v-model="onConflict" class="border-ink-700 bg-ink-900 text-paper-100 border px-2 py-1 font-mono text-[11px]">
                <option value="reject">reject (no overwrite)</option>
                <option value="replace">replace (explicit overwrite)</option>
              </select>
              <button
                type="button"
                class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 border px-3 py-1.5 font-mono text-[11px] transition-colors disabled:opacity-50"
                :disabled="busy || !file"
                @click="doImport"
              >Import</button>
            </div>

            <StateBlock v-if="importError" kind="error">{{ importError }}</StateBlock>

            <template v-if="preview">
              <p class="text-paper-300 font-mono text-[10px]">
                parsed {{ preview.rows_parsed }}/{{ preview.rows_total }} rows · interval {{ preview.interval_hours }} h ·
                conflicts with archive: {{ preview.n_conflicts_with_db ?? 0 }}
              </p>
              <div v-if="preview.errors.length" class="border-heat-500/40 bg-heat-500/5 p-2 font-mono text-[10px]">
                <p class="text-heat-300 mb-1">{{ preview.errors.length }} row error(s):</p>
                <p v-for="(e, i) in preview.errors.slice(0, 8)" :key="i" class="text-heat-300">row {{ e.row }} [{{ e.field }}]: {{ e.message }}</p>
                <p v-if="preview.errors.length > 8" class="text-heat-400">…and {{ preview.errors.length - 8 }} more</p>
              </div>
              <div v-if="preview.warnings.length" class="border-ink-700 p-2 font-mono text-[10px]">
                <p class="text-sodium-200 mb-1">{{ preview.warnings.length }} warning(s):</p>
                <p v-for="(w, i) in preview.warnings.slice(0, 8)" :key="i" class="text-paper-300">{{ w }}</p>
              </div>
              <div v-if="preview.rows.length" class="overflow-x-auto">
                <table class="w-full font-mono text-[10px]">
                  <thead>
                    <tr class="text-paper-400 border-ink-700 border-b text-left">
                      <th class="py-1 pr-3">start (UTC)</th>
                      <th class="py-1 pr-3">end (UTC)</th>
                      <th class="py-1 pr-3">MWh</th>
                      <th class="py-1 pr-3">MW</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="r in preview.rows.slice(0, 12)" :key="r.interval_start_utc" class="border-ink-800 border-b">
                      <td class="py-1 pr-3">{{ r.interval_start_utc }}</td>
                      <td class="py-1 pr-3">{{ r.interval_end_utc }}</td>
                      <td class="py-1 pr-3">{{ r.energy_mwh ?? "—" }}</td>
                      <td class="py-1 pr-3">{{ r.power_mw ?? "—" }}</td>
                    </tr>
                  </tbody>
                </table>
                <p v-if="preview.rows.length > 12" class="text-paper-400 mt-1 font-mono text-[10px]">…first 12 of {{ preview.rows.length }} shown</p>
              </div>
            </template>

            <template v-if="importResult">
              <StateBlock v-if="!importResult.imported" kind="error">
                Not imported: {{ importResult.reason }} ({{ importResult.n_conflicts }} conflicting rows — choose “replace” to overwrite explicitly).
              </StateBlock>
              <p v-else class="text-sodium-200 font-mono text-[10px]">
                Imported {{ importResult.rows_imported }} rows (import #{{ importResult.import_id }}, {{ importResult.n_conflicts }} overwritten).
              </p>
            </template>
          </div>
        </CollapsibleSection>

        <CollapsibleSection title="Accuracy (archived forecasts vs actuals)">
          <div class="mb-2 flex flex-wrap items-end gap-2">
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">from (UTC)</span>
              <input v-model="fromUtc" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">to (UTC)</span>
              <input v-model="toUtc" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
            </label>
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">lead bin</span>
              <select v-model="leadBin" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]">
                <option v-for="o in LEAD_BIN_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </label>
          </div>

          <StateBlock
            v-if="accuracyData && !accuracyData.has_measured_actuals"
            kind="empty"
            :relaxed="true"
          >
            No measured actuals — accuracy unavailable. Import real measured production above; hypothetical modelled
            values are never substituted for measurements.
          </StateBlock>
          <StateBlock
            v-else-if="accuracyData && Object.keys(accuracyData.models).length === 0"
            kind="empty"
            :relaxed="true"
          >
            Actuals exist but no archived issue with a known issue time overlaps them yet — keep collecting (or backfill
            from Historical runs); comparisons appear automatically.
          </StateBlock>
          <template v-else-if="accuracyData">
            <p class="text-paper-400 mb-2 font-mono text-[10px]">
              {{ accuracyData.note }}
              Matched hours are complete UTC hours: {{ accuracyData.actual_hours_in_range }} complete,
              {{ accuracyData.actual_hours_partial }} partial (not scored),
              {{ accuracyData.actual_intervals_unaligned }} unaligned intervals.
            </p>
            <div class="mb-2 flex items-center gap-2">
              <label class="text-paper-300 flex items-center gap-1.5 font-mono text-[10px]">
                <input v-model="useCalibration" type="checkbox" class="accent-sodium-300" />
                apply calibrated view (derived series — archive unchanged)
              </label>
              <span
                v-for="(factor, id) in accuracyData.calibration_applied"
                :key="id"
                class="border-sodium-300/40 text-sodium-200 border px-1.5 py-0.5 font-mono text-[10px]"
              >{{ id }} ×{{ factor }}</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full font-mono text-[10px]">
                <thead>
                  <tr class="text-paper-400 border-ink-700 border-b text-left">
                    <th class="py-1.5 pr-3">model</th>
                    <th class="py-1.5 pr-3">lead bin</th>
                    <th class="py-1.5 pr-3">n</th>
                    <th class="py-1.5 pr-3">MAE (MW)</th>
                    <th class="py-1.5 pr-3">bias (MW)</th>
                    <th class="py-1.5 pr-3">WAPE (energy)</th>
                    <th class="py-1.5 pr-3">coverage</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in accuracyRows" :key="i" class="border-ink-800 border-b">
                    <td class="py-1.5 pr-3" :style="{ color: modelColor(r.model) }">{{ r.model }}</td>
                    <td class="text-paper-300 py-1.5 pr-3">{{ r.bin }}</td>
                    <td class="text-paper-100 py-1.5 pr-3">{{ r.binObj.n_pairs }}</td>
                    <td class="py-1.5 pr-3">{{ r.binObj.mae_mw ?? "—" }}</td>
                    <td class="py-1.5 pr-3">{{ r.binObj.bias_mw ?? "—" }}</td>
                    <td class="py-1.5 pr-3">{{ r.binObj.wape_energy !== null ? (r.binObj.wape_energy * 100).toFixed(1) + "%" : "—" }}</td>
                    <td class="py-1.5 pr-3">{{ r.binObj.coverage !== null ? (r.binObj.coverage * 100).toFixed(0) + "%" : "—" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </CollapsibleSection>

        <CollapsibleSection title="Calibration (optional, derived series)" :default-open="false" lazy>
          <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
            {{ calibData?.note ?? CALIB_FALLBACK_NOTE }}
            Site assumptions (loss factor, provisional hub height, manufacturer-class curve) remain visible on the
            Forecast screen — a calibrated factor is not a calibrated plant curve.
          </p>
          <div class="mb-2 flex items-center gap-2">
            <button
              type="button"
              class="border-ink-600 text-paper-100 hover:border-sodium-300/60 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px] transition-colors"
              :disabled="calibBusy"
              @click="trainCalibration"
            >
              {{ calibBusy ? "Training…" : "Train on current range" }}
            </button>
            <span class="text-paper-400 font-mono text-[10px]">{{ fromUtc }} → {{ toUtc }}</span>
          </div>
          <p v-if="calibMessage" class="text-sodium-200 mb-2 font-mono text-[10px]">{{ calibMessage }}</p>
          <p v-if="calibError" class="text-heat-300 mb-2 font-mono text-[10px]">{{ calibError }}</p>
          <div v-if="calibData?.models?.length" class="overflow-x-auto">
            <table class="w-full font-mono text-[10px]">
              <thead>
                <tr class="text-paper-400 border-ink-700 border-b text-left">
                  <th class="py-1.5 pr-3">model</th>
                  <th class="py-1.5 pr-3">status</th>
                  <th class="py-1.5 pr-3">factor</th>
                  <th class="py-1.5 pr-3">train h</th>
                  <th class="py-1.5 pr-3">holdout h</th>
                  <th class="py-1.5 pr-3">val MAE before→after</th>
                  <th class="py-1.5 pr-3">val WAPE before→after</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in calibData.models" :key="c.model_id" class="border-ink-800 border-b">
                  <td class="py-1.5 pr-3" :style="{ color: modelColor(c.model_id) }">{{ c.model_id }}</td>
                  <td class="py-1.5 pr-3">
                    <span :class="c.status === 'calibrated' ? 'text-sodium-200' : 'text-heat-300'">{{ c.status === "calibrated" ? "calibrated" : "NOT CALIBRATED" }}</span>
                  </td>
                  <td class="py-1.5 pr-3">{{ c.factor }}</td>
                  <td class="py-1.5 pr-3">{{ c.n_train_hours }}</td>
                  <td class="py-1.5 pr-3">{{ c.n_val_hours }}</td>
                  <td class="py-1.5 pr-3">{{ c.val_mae_before ?? "—" }} → {{ c.val_mae_after ?? "—" }}</td>
                  <td class="py-1.5 pr-3">{{ c.val_wape_before ?? "—" }} → {{ c.val_wape_after ?? "—" }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="text-paper-400 font-mono text-[10px]">No calibration trained yet.</p>
        </CollapsibleSection>

        <CollapsibleSection title="Aligned series (actual vs archived issues)">
          <p class="text-paper-400 mb-1 font-mono text-[10px]">Solid: measured generation. Dashed: archived forecast issues for the same valid time ({{ overlayData?.lead_bin ?? "all lead bins" }}). {{ overlayData?.note }}</p>
          <StateBlock v-if="overlayData && overlayData.hours.length === 0" kind="empty">
            No aligned hours in this window — import actuals and archive forecast issues first.
          </StateBlock>
          <VChart v-else style="height: 20rem" :option="overlayOption" autoresize />
        </CollapsibleSection>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
