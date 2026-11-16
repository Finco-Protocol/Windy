<script setup lang="ts">
import { computed, ref } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import {
  windyApi,
  type Inventory,
  type OperationalConstraint,
  type RunSummary,
  type ScenarioResponse,
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

const runsResource = useAbortableResource<{ runs: RunSummary[] }>(
  (signal) => windyApi.runs(null, siteId.value, signal),
  () => [siteId.value],
);
const latestRuns = computed(() => {
  const seen = new Set<string>();
  return (runsResource.data.value?.runs ?? []).filter((r) => !seen.has(r.model_id) && seen.add(r.model_id));
});
const selectedRunId = ref<number | null>(null);
const runOptions = computed(() => latestRuns.value.map((r) => ({ value: r.id, label: `${r.model_id} · issued ${r.run_issued_utc ?? "unknown"}` })));

const scenarioResource = useAbortableResource<ScenarioResponse | null>(
  async (signal) => (selectedRunId.value === null ? null : windyApi.scenario(siteId.value, selectedRunId.value, signal)),
  () => [selectedRunId.value, siteId.value],
);
const { data: scenario, loading: scenarioLoading, error: scenarioError } = scenarioResource;

// ---- bulk edit form --------------------------------------------------------------

const search = ref("");
const selected = ref<Set<string>>(new Set());
const bulkKind = ref<"offline" | "curtailment">("offline");
const capMode = ref<"percent" | "mw">("percent");
const capPercent = ref<number>(100);
const capMw = ref<number>(1.375);
const startLocal = ref<string>("2026-11-13T00:00");
const endLocal = ref<string>("");
const bulkBusy = ref(false);
const bulkError = ref<string | null>(null);
const bulkMessage = ref<string | null>(null);
const exportCapMw = ref<number | null>(null);

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

async function applyBulk(): Promise<void> {
  if (selected.value.size === 0) return;
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
    const res = await windyApi.constraintsAdd(siteId.value, [row]);
    bulkMessage.value = `revision ${res.revision}: ${bulkKind.value} applied to ${selected.value.size} unit(s)` +
      (res.warnings.length ? ` — ${res.warnings.join("; ")}` : "");
    selected.value = new Set();
    await Promise.all([refreshConstraints(), statusResource.refresh()]);
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e);
  } finally {
    bulkBusy.value = false;
  }
}

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
    await Promise.all([refreshConstraints(), statusResource.refresh()]);
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
        data: sc.points.filter((p) => p.base_mw !== null).map((p) => [p.valid_at_utc, p.base_mw]),
        lineStyle: { width: 1.6, color: "#f4ecd8" },
        itemStyle: { color: "#f4ecd8" },
      },
      {
        name: "availability-adjusted",
        type: "line",
        showSymbol: false,
        connectNulls: false,
        data: sc.points.filter((p) => p.adjusted_mw !== null).map((p) => [p.valid_at_utc, p.adjusted_mw]),
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
  } catch (e) {
    publishMessage.value = e instanceof Error ? e.message : String(e);
  } finally {
    publishBusy.value = false;
  }
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
                  class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1.5 font-mono text-[11px]"
                  :disabled="bulkBusy || selected.size === 0"
                  @click="applyBulk"
                >Apply to {{ selected.size }} unit(s)</button>
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
              <select v-model.number="selectedRunId" class="border-ink-700 bg-ink-900 text-paper-100 mt-0.5 border px-2 py-1 font-mono text-[11px]">
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
            <p class="text-paper-400 mb-2 font-mono text-[10px] leading-relaxed">
              {{ scenario.note }} Curve mode: <span class="text-paper-100">{{ scenario.summary.curve_mode }}</span>.
              <span v-if="scenario.summary.curve_mode === 'scaled-reference'" class="text-heat-300">REFERENCE/UNVERIFIED — scaled normalized shape.</span>
            </p>
            <div class="mb-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <div v-for="(w, name) in scenario.summary.windows" :key="name" class="border-ink-700 bg-ink-900/40 border p-2 font-mono text-[10px]">
                <p class="text-paper-400">{{ name }} ({{ w.hours }} h archived)</p>
                <p class="text-paper-100">base {{ w.base_mwh ?? "—" }} MWh</p>
                <p class="text-sodium-200">adjusted {{ w.adjusted_mwh ?? "—" }} MWh</p>
                <p class="text-paper-400">unit-loss {{ w.lost_unit_mwh ?? "—" }} · export-loss {{ w.lost_export_mwh ?? "—" }}</p>
              </div>
            </div>
            <VChart style="height: 18rem" :option="scenarioOption" autoresize />
          </template>
        </CollapsibleSection>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
