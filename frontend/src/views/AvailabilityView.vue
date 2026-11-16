<script setup lang="ts">
import { computed, ref, watch } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { localToUtc, nowLocalForTz } from "@/utils/availability";
import { scenarioSeries } from "@/utils/chartGaps";
import {
  windyApi,
  type Inventory,
  type OperationalConstraint,
  type RunSummary,
  type ScenarioResponse,
  type ScenarioRevision,
  type SiteOperatingStatus,
  type SiteResponse,
} from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// Availability & Curtailment — practical single-user control over time-
// dependent operating constraints. A "50% cap" means 50% of rated MW as a
// MAXIMUM permitted output (1.375 MW for a 2.75 MW unit), never a multiplier
// of instantaneous output. Baseline archives stay untouched; adjusted values
// are a derived scenario pinned to append-only inventory/constraint revisions.

const { siteId } = useActiveSite();
const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(siteId.value, signal), () => [siteId.value]);
const { data: siteData, error: siteError, loading: siteLoading } = siteResource;

const statusResource = useAbortableResource<SiteOperatingStatus>(
  (signal) => windyApi.operatingStatus(siteId.value, signal),
  () => [siteId.value],
);
const { data: status } = statusResource;

const invResource = useAbortableResource<Inventory>((signal) => windyApi.inventory(siteId.value, signal), () => [siteId.value]);
const { data: inventory, refresh: refreshInventory } = invResource;

const consResource = useAbortableResource<{ revision: number; constraints: OperationalConstraint[] }>(
  (signal) => windyApi.constraints(siteId.value, signal),
  () => [siteId.value],
);
const { data: constraintsData, refresh: refreshConstraints } = consResource;

const revisionsResource = useAbortableResource<{ revisions: ScenarioRevision[] }>(
  (signal) => windyApi.scenarioRevisions(siteId.value, signal),
  () => [siteId.value],
);
const { data: revisionsData, refresh: refreshRevisions } = revisionsResource;

const runsResource = useAbortableResource<{ runs: RunSummary[] }>(
  (signal) => windyApi.runs(null, siteId.value, signal),
  () => [siteId.value],
);
const latestRuns = computed(() => {
  const seen = new Set<string>();
  return (runsResource.data.value?.runs ?? []).filter((r) => !seen.has(r.model_id) && seen.add(r.model_id));
});
const selectedRunId = ref<number | null>(null);
// pinned reopen: when set, the scenario is computed against THESE revisions
// (audit view of a published snapshot), not the currently active configuration
const pinned = ref<{ inventory_revision: number; constraints_revision: number } | null>(null);
const runOptions = computed(() => latestRuns.value.map((r) => ({ value: r.id, label: `${r.model_id} · issued ${r.run_issued_utc ?? "unknown"}` })));

const scenarioResource = useAbortableResource<ScenarioResponse | null>(
  async (signal) => {
    if (selectedRunId.value === null) return null;
    return windyApi.scenarioPinned(siteId.value, selectedRunId.value, pinned.value, signal);
  },
  () => [selectedRunId.value, siteId.value, pinned.value?.inventory_revision ?? null, pinned.value?.constraints_revision ?? null],
);
const { data: scenario, loading: scenarioLoading, error: scenarioError } = scenarioResource;

function selectRun(id: number | null): void {
  selectedRunId.value = id;
  pinned.value = null; // fresh selection follows the live configuration
}

// Site change resets ALL site-scoped transient state: selected WTG ids, bulk
// preview/confirmation, selected run, pinned revision, messages and the local
// time defaults. Abortable resources abort their stale in-flight requests, so
// no response computed for the previous site can land for the new one.
watch(siteId, (newSite, oldSite) => {
  if (newSite === oldSite) return;
  selected.value = new Set();
  confirmOpen.value = false;
  previewSiteId.value = null;
  bulkError.value = null;
  bulkMessage.value = null;
  selectRun(null);
  refreshDefaultStart();
});

// ---- bulk edit form --------------------------------------------------------------

const search = ref("");
const selected = ref<Set<string>>(new Set());
const bulkKind = ref<"offline" | "curtailment">("offline");
const capMode = ref<"percent" | "mw">("percent");
const capPercent = ref<number>(100);
const capMw = ref<number>(1.375);
const startLocal = ref<string>(nowLocalForTz(siteData.value?.site.timezone ?? "Europe/Podgorica"));
const endLocal = ref<string>("");
const bulkBusy = ref(false);
const bulkError = ref<string | null>(null);
const bulkMessage = ref<string | null>(null);
const exportCapMw = ref<number | null>(null);
const confirmOpen = ref(false);

const tz = computed(() => siteData.value?.site.timezone ?? "Europe/Podgorica");

function refreshDefaultStart(): void {
  startLocal.value = nowLocalForTz(tz.value);
  endLocal.value = "";
}

const startUtcPreview = computed(() => localToUtc(startLocal.value, tz.value));
const endUtcPreview = computed(() => (endLocal.value ? localToUtc(endLocal.value, tz.value) : null));

// two-step preview/confirm: affected units, cap meaning and computed UTC times
// are shown BEFORE anything is persisted
interface BulkPreview {
  units: string[];
  kind: string;
  cap: string;
  start_local: string;
  start_utc?: string;
  end_local: string;
  end_utc?: string;
  warnings: string[];
}

const bulkPreview = computed<BulkPreview | { error: string } | null>(() => {
  if (selected.value.size === 0) return null;
  if (startUtcPreview.value.error) return { error: startUtcPreview.value.error };
  if (endLocal.value && endUtcPreview.value?.error) return { error: endUtcPreview.value.error };
  const cap =
    bulkKind.value === "offline"
      ? "offline → 0 MW"
      : capMode.value === "percent"
        ? `max ${capPercent.value}% of rated MW`
        : `max ${capMw.value} MW`;
  return {
    units: [...selected.value].sort(),
    kind: bulkKind.value,
    cap,
    start_local: startLocal.value,
    start_utc: startUtcPreview.value.utc,
    end_local: endLocal.value || "(open-ended)",
    end_utc: endUtcPreview.value?.utc ?? "(open-ended)",
    warnings: [startUtcPreview.value.warning, endUtcPreview.value?.warning].filter(Boolean) as string[],
  };
});

// site the pending confirmation was prepared for — a Site A confirmation must
// never be submitted against Site B
const previewSiteId = ref<string | null>(null);

function requestPreview(): void {
  bulkError.value = null;
  bulkMessage.value = null;
  if (bulkPreview.value && "error" in bulkPreview.value && bulkPreview.value.error) {
    bulkError.value = bulkPreview.value.error;
    return;
  }
  previewSiteId.value = siteId.value; // snap the site at preview time
  confirmOpen.value = true;
}

async function confirmApply(): Promise<void> {
  if (previewSiteId.value !== null && previewSiteId.value !== siteId.value) {
    confirmOpen.value = false;
    selected.value = new Set();
    bulkError.value = "site changed since preview — selection reset, prepare the change again";
    return;
  }
  const targetSite = siteId.value;
  confirmOpen.value = false;
  bulkBusy.value = true;
  bulkError.value = null;
  bulkMessage.value = null;
  try {
    const row: Record<string, unknown> =
      bulkKind.value === "offline"
        ? { kind: "offline", unit_ids: [...selected.value], start_local: startLocal.value, end_local: endLocal.value || null }
        : {
            kind: "curtailment",
            unit_ids: [...selected.value],
            start_local: startLocal.value,
            end_local: endLocal.value || null,
            ...(capMode.value === "percent" ? { cap_percent: capPercent.value } : { cap_mw: capMw.value }),
          };
    const res = await windyApi.constraintsAdd(targetSite, [row]);
    bulkMessage.value = `revision ${res.revision}: ${bulkKind.value} applied to ${selected.value.size} unit(s)` +
      (res.warnings.length ? ` — ${res.warnings.join("; ")}` : "");
    selected.value = new Set();
    await Promise.all([refreshConstraints(), statusResource.refresh(), scenarioResource.refresh()]);
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bulkBusy.value = false;
  }
}

const turbines = computed(() => inventory.value?.turbines ?? []);
const filteredTurbines = computed(() => {
  const q = search.value.trim().toLowerCase();
  if (!q) return turbines.value;
  return turbines.value.filter((t) => t.unit_id.toLowerCase().includes(q) || (t.name ?? "").toLowerCase().includes(q));
});

function toggle(unit: string): void {
  const next = new Set(selected.value);
  if (next.has(unit)) next.delete(unit);
  else next.add(unit);
  selected.value = next;
}

function selectAllFiltered(): void {
  selected.value = new Set(filteredTurbines.value.map((t) => t.unit_id));
}

function clearSelection(): void {
  selected.value = new Set();
}

async function generateInventory(): Promise<void> {
  const site = siteData.value?.site;
  if (!site) return;
  bulkBusy.value = true;
  bulkError.value = null;
  try {
    await windyApi.inventoryGenerate(siteId.value, site.turbine_count, site.turbine_rated_mw);
    await Promise.all([refreshInventory(), statusResource.refresh()]);
    bulkMessage.value = `inventory generated (${site.turbine_count} × ${site.turbine_rated_mw} MW, WTG-001…)`;
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bulkBusy.value = false;
  }
}

// bulk persistence goes through requestPreview() → confirmApply()

async function applyExportCap(): Promise<void> {
  bulkBusy.value = true;
  bulkError.value = null;
  bulkMessage.value = null;
  try {
    if (exportCapMw.value === null || exportCapMw.value <= 0) {
      // remove all farm caps via delete
      const caps = (constraintsData.value?.constraints ?? []).filter((c) => c.kind === "export_cap").map((c) => c.id);
      if (caps.length) await windyApi.constraintsDelete(siteId.value, caps);
      bulkMessage.value = "farm export cap removed (new revision)";
    } else {
      const res = await windyApi.constraintsAdd(siteId.value, [
        { kind: "export_cap", cap_mw: exportCapMw.value, start_local: startLocal.value, end_local: endLocal.value || null },
      ]);
      bulkMessage.value = `export cap ${exportCapMw.value} MW (revision ${res.revision})`;
    }
    await refreshConstraints();
    await statusResource.refresh();
    await scenarioResource.refresh();
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bulkBusy.value = false;
  }
}

async function deleteConstraint(id: number): Promise<void> {
  bulkBusy.value = true;
  bulkError.value = null;
  try {
    await windyApi.constraintsDelete(siteId.value, [id]);
    await Promise.all([refreshConstraints(), statusResource.refresh(), scenarioResource.refresh()]);
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bulkBusy.value = false;
  }
}

// ---- scenario chart ---------------------------------------------------------------

const axisLabel = { color: "#93896f", fontSize: 10, fontFamily: "JetBrains Mono, monospace" };

const scenarioOption = computed<Record<string, unknown> | undefined>(() => {
  const sc = scenario.value;
  if (!sc) return undefined;
  const series = scenarioSeries(sc.points);
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
      axisLabel: { ...axisLabel, formatter: (v: number) => (v == null ? "" : new Date(v).toISOString().slice(5, 16).replace("T", " ")) },
      axisLine: { lineStyle: { color: "#1a2638" } },
      splitLine: { show: false },
    },
    yAxis: { type: "value", name: "MW", nameTextStyle: { color: "#93896f", fontSize: 10 }, axisLabel, splitLine: { lineStyle: { color: "#1a2638" } } },
    series: [
      {
        name: "base (archived)",
        type: "line",
        showSymbol: false,
        connectNulls: false,
        // ALL timestamps kept — missing hours stay [ts, null] so the line
        // visually breaks instead of bridging the gap
        data: series.base,
        lineStyle: { width: 1.6, color: "#f4ecd8" },
        itemStyle: { color: "#f4ecd8" },
      },
      {
        name: "availability-adjusted",
        type: "line",
        showSymbol: false,
        connectNulls: false,
        data: series.adjusted,
        lineStyle: { width: 1.4, color: "#c792ea" },
        itemStyle: { color: "#c792ea" },
        areaStyle: { opacity: 0.08, color: "#c792ea" },
      },
    ],
  };
});

const publishBusy = ref(false);
const publishMessage = ref<string | null>(null);

async function publish(): Promise<void> {
  if (selectedRunId.value === null) return;
  publishBusy.value = true;
  publishMessage.value = null;
  try {
    const res = await windyApi.scenarioPublish(siteId.value, selectedRunId.value);
    publishMessage.value = `published scenario revision #${res.scenario_revision_id} (inputs pinned; idempotent on republish)`;
    await refreshRevisions();
    // reopen the just-published revision pinned to ITS inputs
    pinned.value = { inventory_revision: res.retrieval.inventory_revision, constraints_revision: res.retrieval.constraints_revision };
    await scenarioResource.refresh();
  } catch (e) {
    publishMessage.value = e instanceof Error ? e.message : String(e);
  } finally {
    publishBusy.value = false;
  }
}

function reopen(rev: ScenarioRevision): void {
  // audit view of an older published snapshot pinned to ITS revisions —
  // current live constraints are NOT used for this display
  selectedRunId.value = rev.run_id;
  pinned.value = { inventory_revision: rev.inventory_revision, constraints_revision: rev.constraints_revision };
}

function capLabel(c: OperationalConstraint): string {
  if (c.kind === "offline") return "offline";
  if (c.kind === "export_cap") return `export ≤ ${c.cap_mw} MW`;
  const pct = c.cap_percent !== null ? `${c.cap_percent}% of rated` : null;
  const mw = c.cap_mw !== null ? `${c.cap_mw} MW max` : null;
  return `curtailed ≤ ${[mw, pct].filter(Boolean).join(" / ")}`;
}
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <main class="mx-auto w-full max-w-5xl flex-1 space-y-3 px-4 py-3 sm:space-y-6 sm:px-6 sm:py-6">
      <StateBlock v-if="siteError" kind="error">{{ siteError }}</StateBlock>
      <StateBlock v-else-if="siteLoading" kind="loading" caption="Connecting…" />

      <template v-else-if="siteData">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <SiteSelector />
            <h1 class="text-paper-50 font-mono text-lg tracking-wide">Availability &amp; curtailment</h1>
          </div>
        </div>
        <p class="text-paper-400 font-mono text-[10px] leading-relaxed">
          Time-dependent operating constraints only — installed nameplate stays a physical attribute. A cap is a maximum
          permitted MW (or % of rated), offline dominates, the most restrictive cap wins, and the farm export cap applies
          after per-unit limits. Baseline archives and actuals are never modified; adjusted values are a derived scenario
          pinned to append-only revisions.
        </p>

        <!-- site-level summary -->
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div class="border-ink-700 bg-ink-900/40 border p-3 font-mono text-[10px]">
            <p class="text-paper-400">installed</p>
            <p class="text-paper-50 text-sm">{{ status?.installed_mw ?? "—" }} MW</p>
          </div>
          <div class="border-ink-700 bg-ink-900/40 border p-3 font-mono text-[10px]">
            <p class="text-paper-400">available max</p>
            <p class="text-sodium-200 text-sm">{{ status?.available_max_mw ?? "—" }} MW</p>
          </div>
          <div class="border-ink-700 bg-ink-900/40 border p-3 font-mono text-[10px]">
            <p class="text-paper-400">offline / curtailed</p>
            <p class="text-paper-50 text-sm">{{ status?.offline_count ?? "—" }} / {{ status?.curtailed_count ?? "—" }}</p>
          </div>
          <div class="border-ink-700 bg-ink-900/40 border p-3 font-mono text-[10px]">
            <p class="text-paper-400">farm export cap</p>
            <p class="text-paper-50 text-sm">{{ status?.farm_export_cap_mw ?? "none" }}</p>
          </div>
        </div>

        <CollapsibleSection title="Turbine inventory (WTG grid)">
          <StateBlock v-if="inventory && inventory.n_turbines === 0" kind="empty" :relaxed="true">
            No turbine inventory yet — generate it from the site's count × rating (editable later via bulk rating edits).
          </StateBlock>
          <template v-else-if="inventory">
            <p class="text-paper-300 mb-2 font-mono text-[10px]">
              revision {{ inventory.revision }} · {{ inventory.n_turbines }} units · installed {{ inventory.installed_mw }} MW
            </p>
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <input v-model="search" placeholder="search WTG…" class="border-ink-700 bg-ink-900 text-paper-100 border px-2 py-1 font-mono text-[11px]" />
              <button type="button" class="border-ink-700 text-paper-300 border px-2 py-1 font-mono text-[10px]" @click="selectAllFiltered">select all shown</button>
              <button type="button" class="border-ink-700 text-paper-300 border px-2 py-1 font-mono text-[10px]" @click="clearSelection">clear</button>
              <span class="text-paper-400 font-mono text-[10px]">{{ selected.size }} selected</span>
              <button
                type="button"
                class="border-ink-600 text-paper-100 hover:border-sodium-300/60 border px-2 py-1 font-mono text-[10px]"
                @click="generateInventory"
              >regenerate from site config</button>
            </div>
            <div class="border-ink-700 grid max-h-56 grid-cols-4 gap-1 overflow-y-auto border p-2 sm:grid-cols-8">
              <button
                v-for="t in filteredTurbines"
                :key="t.unit_id"
                type="button"
                class="border px-1 py-0.5 font-mono text-[9px] transition-colors"
                :class="selected.has(t.unit_id) ? 'border-sodium-300/70 text-sodium-200 bg-sodium-300/10' : 'border-ink-700 text-paper-300'"
                @click="toggle(t.unit_id)"
              >{{ t.unit_id }}</button>
            </div>

            <div class="mt-3 space-y-2">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-paper-400 font-mono text-[10px]">bulk action</span>
                <select v-model="bulkKind" class="border-ink-700 bg-ink-900 text-paper-100 border px-2 py-1 font-mono text-[11px]">
                  <option value="offline">offline → 0 MW</option>
                  <option value="curtailment">curtail (cap)</option>
                </select>
                <template v-if="bulkKind === 'curtailment'">
                  <select v-model="capMode" class="border-ink-700 bg-ink-900 text-paper-100 border px-2 py-1 font-mono text-[11px]">
                    <option value="percent">% of rated</option>
                    <option value="mw">max MW</option>
                  </select>
                  <input
                    v-if="capMode === 'percent'"
                    v-model.number="capPercent"
                    type="number" min="0" max="100" step="1"
                    class="border-ink-700 bg-ink-900 text-paper-100 w-20 border px-2 py-1 font-mono text-[11px]"
                  />
                  <input
                    v-else
                    v-model.number="capMw"
                    type="number" min="0" step="0.1"
                    class="border-ink-700 bg-ink-900 text-paper-100 w-24 border px-2 py-1 font-mono text-[11px]"
                  />
                </template>
                <label class="font-mono text-[10px]">
                  <span class="text-paper-400 block">start (local)</span>
                  <input v-model="startLocal" type="datetime-local" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
                </label>
                <label class="font-mono text-[10px]">
                  <span class="text-paper-400 block">end (local, empty = open)</span>
                  <input v-model="endLocal" type="datetime-local" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]" />
                </label>
                <button
                  type="button"
                  class="border-ink-600 text-paper-100 hover:border-sodium-300/60 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px]"
                  :disabled="selected.size === 0"
                  @click="refreshDefaultStart"
                >now ({{ tz }})</button>
                <button
                  type="button"
                  class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px]"
                  :disabled="bulkBusy || selected.size === 0"
                  @click="requestPreview"
                >Preview {{ selected.size }} unit(s)</button>
              </div>
              <div v-if="bulkPreview && !('error' in bulkPreview) && confirmOpen" class="border-sodium-300/40 bg-sodium-300/5 space-y-1 border p-2 font-mono text-[10px]">
                <p class="text-sodium-200">Confirm before persisting:</p>
                <p class="text-paper-100">{{ bulkPreview.cap }}</p>
                <p class="text-paper-300">units: {{ bulkPreview.units.join(", ") }}</p>
                <p class="text-paper-300">
                  {{ bulkPreview.start_local }} ({{ tz }}) → <span class="text-sodium-200">{{ bulkPreview.start_utc }}</span>
                  → {{ bulkPreview.end_local }} → <span class="text-sodium-200">{{ bulkPreview.end_utc }}</span>
                </p>
                <p v-for="w in bulkPreview.warnings" :key="w" class="text-heat-300">{{ w }}</p>
                <button
                  type="button"
                  class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 border px-2 py-1"
                  :disabled="bulkBusy"
                  @click="confirmApply"
                >Confirm &amp; persist</button>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-paper-400 font-mono text-[10px]">farm export cap MW (empty/0 = remove)</span>
                <input
                  v-model.number="exportCapMw"
                  type="number" min="0" step="0.5"
                  class="border-ink-700 bg-ink-900 text-paper-100 w-24 border px-2 py-1 font-mono text-[11px]"
                />
                <button
                  type="button"
                  class="border-ink-600 text-paper-100 hover:border-sodium-300/60 border px-2 py-1 font-mono text-[10px]"
                  :disabled="bulkBusy"
                  @click="applyExportCap"
                >Apply farm cap</button>
              </div>
              <p v-if="bulkMessage" class="text-sodium-200 font-mono text-[10px]">{{ bulkMessage }}</p>
              <p v-if="bulkError" class="text-heat-300 font-mono text-[10px]">{{ bulkError }}</p>
              <p class="text-paper-400 font-mono text-[9px]">
                site timezone: {{ siteData.site.timezone }} — local times convert to UTC with explicit DST handling
                (nonexistent times rejected, ambiguous resolved to first occurrence)
              </p>
            </div>
          </template>
        </CollapsibleSection>

        <CollapsibleSection title="Active constraints (current revision)">
          <p class="text-paper-300 mb-2 font-mono text-[10px]">revision {{ constraintsData?.revision ?? "—" }}</p>
          <StateBlock v-if="constraintsData && constraintsData.constraints.length === 0" kind="empty">No active constraints.</StateBlock>
          <div v-else-if="constraintsData" class="space-y-1">
            <div
              v-for="c in constraintsData.constraints"
              :key="c.id"
              class="border-ink-700 flex items-center justify-between gap-2 border px-2 py-1 font-mono text-[10px]"
            >
              <span class="text-paper-100">{{ capLabel(c) }}</span>
              <span class="text-paper-400 flex items-center gap-3">
                <span>{{ c.kind === "export_cap" ? "farm" : `${(c.unit_ids ?? []).length} unit(s)` }}</span>
                <span>{{ c.start_utc.slice(5, 16).replace("T", " ") }} → {{ c.end_utc ? c.end_utc.slice(5, 16).replace("T", " ") : "open" }} UTC</span>
                <button type="button" class="text-heat-300 hover:text-heat-400" @click="deleteConstraint(c.id)">remove</button>
              </span>
            </div>
          </div>
        </CollapsibleSection>

        <CollapsibleSection title="Scenario: base vs availability-adjusted" lazy>
          <div class="mb-2 flex flex-wrap items-center gap-2">
            <label class="font-mono text-[10px]">
              <span class="text-paper-400 block">archived run</span>
              <select
                :value="selectedRunId"
                class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]"
                @change="selectRun(Number(($event.target as HTMLSelectElement).value))"
              >
                <option :value="null" disabled>select a run…</option>
                <option v-for="o in runOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </label>
            <button
              type="button"
              class="border-ink-600 text-paper-100 hover:border-sodium-300/60 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px]"
              :disabled="publishBusy || selectedRunId === null"
              @click="publish"
            >Publish scenario revision</button>
          </div>
          <p v-if="publishMessage" class="text-sodium-200 mb-2 font-mono text-[10px]">{{ publishMessage }}</p>
          <StateBlock v-if="scenarioError" kind="error">{{ scenarioError }}</StateBlock>
          <StateBlock v-else-if="scenarioLoading" kind="loading" loading-py="py-8" caption="Computing scenario…" />
          <StateBlock v-else-if="!scenario" kind="empty">Select an archived run to compute the adjusted scenario.</StateBlock>
          <template v-else-if="scenario">
            <p v-if="pinned" class="text-sodium-200 mb-2 font-mono text-[10px]">
              AUDIT VIEW — pinned to inventory revision {{ pinned.inventory_revision }} and constraints revision
              {{ pinned.constraints_revision }} (not the live configuration).
              <button type="button" class="text-paper-100 underline" @click="selectRun(selectedRunId)">back to live</button>
            </p>
            <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
              {{ scenario.note }} Curve mode: <span class="text-paper-100">{{ scenario.summary.curve_mode }}</span>.
              <span v-if="scenario.summary.curve_mode === 'scaled-reference'" class="text-heat-300">REFERENCE/UNVERIFIED — scaled normalized shape.</span>
              Pinned inputs: inventory revision <span class="text-paper-100">{{ scenario.inventory_revision }}</span>,
              constraints revision <span class="text-paper-100">{{ scenario.constraints_revision }}</span>,
              model {{ scenario.model_id }} issued {{ scenario.run_issued_utc ?? "unknown" }},
              algorithm v{{ scenario.algorithm_version }}.
              <span v-if="scenario.baseline_comparable === false" class="text-heat-300">
                {{ scenario.baseline_comparable_reason }}
              </span>
            </p>
            <div class="mb-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <div v-for="(w, name) in scenario.summary.windows" :key="name" class="border-ink-700 bg-ink-900/40 border p-2 font-mono text-[10px]">
                <p class="text-paper-400">
                  {{ name }}
                  <span class="text-paper-100">{{ w.valid_hours }}/{{ w.window_hours }} h</span>
                  <span v-if="w.short_horizon" class="text-heat-300">short-horizon</span>
                </p>
                <p class="text-paper-100">
                  base
                  <template v-if="w.complete">{{ w.base_mwh }} MWh</template>
                  <template v-else>n/a ({{ w.missing_hours }} missing)</template>
                </p>
                <p class="text-sodium-200">
                  adjusted
                  <template v-if="w.complete">{{ w.adjusted_mwh }} MWh</template>
                  <template v-else>n/a (incomplete)</template>
                </p>
                <p class="text-paper-400">
                  unit-loss {{ w.lost_unit_mwh ?? "—" }} · export-loss {{ w.lost_export_mwh ?? "—" }}
                  <template v-if="!w.complete"> · subtotal {{ w.available_hours_subtotal.hours }} h: {{ w.available_hours_subtotal.adjusted_mwh }} MWh</template>
                </p>
                <p class="text-paper-400">coverage {{ w.coverage ?? "—" }}</p>
              </div>
            </div>
            <VChart style="height: 18rem" :option="scenarioOption" autoresize />
            <a
              v-if="selectedRunId !== null"
              :href="`/api/assets/${encodeURIComponent(siteId)}/scenario/export.csv?run_id=${selectedRunId}${
                pinned ? `&inventory_revision=${pinned.inventory_revision}&constraints_revision=${pinned.constraints_revision}` : ''
              }`"
              class="border-ink-700 text-paper-300 hover:border-sodium-300/60 hover:text-sodium-200 mt-2 inline-block border px-3 py-1 font-mono text-[11px] transition-colors"
            >Export scenario CSV{{ pinned ? " (pinned revision)" : "" }}</a>
          </template>
        </CollapsibleSection>

        <CollapsibleSection title="Published scenario revisions" :default-open="false" lazy>
          <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
            Each row is a published snapshot pinned to its own inventory/constraint revisions. Opening an older revision
            shows the AUDIT view of that snapshot — the live configuration is not applied to it.
          </p>
          <StateBlock v-if="revisionsData && revisionsData.revisions.length === 0" kind="empty">
            Nothing published yet — select a run and press “Publish scenario revision”.
          </StateBlock>
          <div v-else-if="revisionsData" class="space-y-1">
            <div
              v-for="r in revisionsData.revisions"
              :key="r.id"
              class="border-ink-700 flex flex-wrap items-center justify-between gap-2 border px-2 py-1 font-mono text-[10px]"
            >
              <span class="text-paper-100">
                #{{ r.id }} · {{ r.model_id }} · {{ r.run_issued_utc ?? "unknown" }}
                <span class="text-paper-400">inv r{{ r.inventory_revision }} · cons r{{ r.constraints_revision }} · alg v{{ r.algorithm_version }} · {{ r.created_at_utc }}</span>
              </span>
              <span class="flex items-center gap-2">
                <span class="text-paper-300">
                  24h adj: {{ r.summary?.windows?.["24h"]?.complete ? `${r.summary.windows["24h"].adjusted_mwh} MWh` : `n/a (subtotal ${r.summary?.windows?.["24h"]?.available_hours_subtotal?.adjusted_mwh ?? "—"} MWh / ${r.summary?.windows?.["24h"]?.available_hours_subtotal?.hours ?? 0} h)` }}
                </span>
                <button type="button" class="border-ink-600 text-paper-100 hover:border-sodium-300/60 border px-2 py-0.5" @click="reopen(r)">reopen</button>
                <a
                  :href="r.retrieve_at"
                  class="border-ink-700 text-paper-300 border px-2 py-0.5"
                  title="pinned JSON endpoint"
                >json</a>
              </span>
            </div>
          </div>
        </CollapsibleSection>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
