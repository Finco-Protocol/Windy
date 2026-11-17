<script setup lang="ts">
import { computed, ref, watch } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { nowLocalForTz } from "@/utils/availability";
import { buildDailyMatrix, buildHourlyRows, MODEL_LABELS_HR, modelTotal, productionModelIds, rowAverage, type DailyRow, type DailySlot } from "@/utils/forecastTable";
import { modelColor, windyApi, type AdjustedPoint, type ForecastModel, type ForecastResponse, type Inventory, type OperationalConstraint, type OperationalStatus, type SiteResponse, type WindNowResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// V1.6 forecast-first: dnevna tablica MWh po modelu (UKUPNO red + Prosjek
// stupac), satni detalji 24/48 h, "Rad elektrane" panel (Addendum C: per-
// turbine ON/OFF i Max MW — 6 × 2,50 MW je VLASNIČOV izbor kroz UI, bez
// zadanog farm capa) i SADA kartica (Addendum A: MODELANO, nikad stvarni
// vjetar). Prilagođeni prikaz koristi /api/forecast?with_availability=1 —
// izračun je uvijek na backendu (scenario engine), frontend samo prikazuje.

const { siteId } = useActiveSite();
const siteResource = useAbortableResource<SiteResponse>((signal) => windyApi.site(siteId.value, signal), () => [siteId.value]);
const { data: siteData, loading: siteLoading, error: siteError } = siteResource;
const tz = computed(() => siteData.value?.site.timezone ?? "Europe/Podgorica");

const HORIZON_OPTIONS = [
  { value: 7, label: "7 dana" },
  { value: 10, label: "10 dana" },
  { value: 15, label: "15 dana" },
] as const;
const days = ref<7 | 10 | 15>(7);

// ---- bazni vs prilagođeni prikaz -------------------------------------------
const VIEW_OPTIONS = [
  { value: "baseline", label: "Bazno" },
  { value: "adjusted", label: "Prilagođeno" },
] as const;
const viewMode = ref<"baseline" | "adjusted">("baseline");

const fcResource = useAbortableResource<ForecastResponse>(
  (signal) => windyApi.forecast(days.value, siteId.value, signal, viewMode.value === "adjusted"),
  () => [days.value, siteId.value, viewMode.value],
);
const { data: fcData, loading: fcLoading, error: fcError } = fcResource;

const modelsWithRun = computed<ForecastModel[]>(() => (fcData.value?.models ?? []).filter((m) => m.run !== null));
const productionIds = computed(() => productionModelIds(modelsWithRun.value));

// dnevna matrica: u prilagođenom prikazu model pokazuje adjusted.daily
// (isti slot oblik); model čiji adjusted nije dostupan ostaje prazan s razlogom
const dailyRows = computed<DailyRow[]>(() =>
  buildDailyMatrix(
    modelsWithRun.value.map((m) => {
      const slots: DailySlot[] = viewMode.value === "adjusted" && m.adjusted?.available ? (m.adjusted.daily as DailySlot[]) : m.daily_local;
      return { model_id: m.model_id, daily_local: slots };
    }),
  ),
);
const modelColumns = computed(() => modelsWithRun.value.map((m) => ({ id: m.model_id, label: MODEL_LABELS_HR[m.model_id] ?? m.model_id, metOnly: m.met_only })));

const adjustedDisabledReason = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  if (viewMode.value !== "adjusted") return out;
  for (const m of modelsWithRun.value) {
    if (m.adjusted && !m.adjusted.available && m.adjusted.reason) out[m.model_id] = `prilagođeno nedostupno: ${m.adjusted.reason}`;
  }
  return out;
});

// ---- UKUPNO (Addendum B) i Prosjek ------------------------------------------
const totalsByModel = computed(() => {
  const out: Record<string, { total: number | null; partial: boolean; days: number }> = {};
  for (const c of modelColumns.value) out[c.id] = modelTotal(dailyRows.value, c.id);
  return out;
});
const totalsAverage = computed(() => rowAverage(Object.fromEntries(Object.entries(totalsByModel.value).map(([id, t]) => [id, t.total])), productionIds.value));
function rowAvg(row: DailyRow): number | null {
  return rowAverage(row.values, productionIds.value);
}

// ---- Rad elektrane (status + brzi bulk editor) -------------------------------
const statusResource = useAbortableResource<OperationalStatus>((signal) => windyApi.operatingStatus(siteId.value, signal), () => [siteId.value]);
const { data: opStatus, refresh: refreshStatus } = statusResource;
const invResource = useAbortableResource<Inventory>((signal) => windyApi.inventory(siteId.value, signal), () => [siteId.value]);
const { data: inventory, refresh: refreshInventory } = invResource;
const consResource = useAbortableResource<{ revision: number; constraints: OperationalConstraint[] }>((signal) => windyApi.constraints(siteId.value, signal), () => [siteId.value]);
const { data: constraintsData, refresh: refreshConstraints } = consResource;

const turbines = computed(() => inventory.value?.turbines ?? []);
const ratedById = computed(() => new Map(turbines.value.map((t) => [t.unit_id, t.rated_mw] as const)));

const wtgSearch = ref("");
const wtgSelected = ref<Set<string>>(new Set());
const filteredTurbines = computed(() => {
  const q = wtgSearch.value.trim().toLowerCase();
  if (!q) return turbines.value;
  return turbines.value.filter((t) => t.unit_id.toLowerCase().includes(q) || (t.name ?? "").toLowerCase().includes(q));
});
function toggleWtg(unit: string): void {
  const next = new Set(wtgSelected.value);
  if (next.has(unit)) next.delete(unit);
  else next.add(unit);
  wtgSelected.value = next;
}
function selectAllFiltered(): void {
  wtgSelected.value = new Set(filteredTurbines.value.map((t) => t.unit_id));
}
function clearSelection(): void {
  wtgSelected.value = new Set();
}

// trenutno STANJE po turbini (samo aktivna ograničenja "od sada", otvorena)
interface UnitState {
  off: boolean;
  capMw: number | null; // najrestriktivniji aktivni cap (MW)
}
const nowUtc = computed(() => new Date().toISOString().slice(0, 19) + "Z");
const activeUnitConstraints = computed(() =>
  (constraintsData.value?.constraints ?? []).filter((c) => c.kind !== "export_cap" && c.start_utc <= nowUtc.value && (c.end_utc === null || c.end_utc > nowUtc.value)),
);
function unitState(unit: string): UnitState {
  let off = false;
  let capMw: number | null = null;
  for (const c of activeUnitConstraints.value) {
    if (!c.unit_ids?.includes(unit)) continue;
    if (c.kind === "offline") off = true;
    if (c.kind === "curtailment") {
      const cap = c.cap_mw ?? (c.cap_percent !== null && c.cap_percent !== undefined ? ((c.cap_percent / 100) * (ratedById.value.get(unit) ?? 0)) : null);
      if (cap !== null && (capMw === null || cap < capMw)) capMw = cap;
    }
  }
  return { off, capMw: off ? null : capMw };
}

// željeno stanje: OFF / cap MW / ON (bez ograničenja)
const WTG_ACTION_OPTIONS = [
  { value: "on", label: "ON" },
  { value: "off", label: "OFF" },
  { value: "cap", label: "Max MW" },
] as const;
const bulkCapMw = ref<number>(2.5);
const wtgAction = ref<"off" | "cap" | "on">("cap");
const applyBusy = ref(false);
const applyError = ref<string | null>(null);
const applyMessage = ref<string | null>(null);
const confirmOpen = ref(false);
const previewSiteId = ref<string | null>(null);

const startLocal = ref<string>(nowLocalForTz("Europe/Podgorica"));
watch(
  tz,
  (v) => {
    startLocal.value = nowLocalForTz(v);
  },
  { immediate: true },
);

// spremi promjene samo za odabrane jedinice čije se stanje mijenja
const pendingUnits = computed(() => {
  const changed: { unit: string; from: UnitState; to: { off: boolean; capMw: number | null } }[] = [];
  for (const unit of [...wtgSelected.value].sort()) {
    const from = unitState(unit);
    const to = wtgAction.value === "off" ? { off: true, capMw: null } : wtgAction.value === "cap" ? { off: false, capMw: bulkCapMw.value > 0 ? bulkCapMw.value : null } : { off: false, capMw: null };
    if (from.off !== to.off || (from.capMw ?? null) !== (to.capMw ?? null)) changed.push({ unit, from, to });
  }
  return changed;
});

// puko opis stanja za pregled — KONAČNI izračun uvijek radi backend engine
const pendingPreviewMw = computed(() => {
  const st = opStatus.value;
  if (!st || !st.inventory_present) return null;
  let sum = 0;
  const changedMap = new Map(pendingUnits.value.map((p) => [p.unit, p.to] as const));
  for (const t of turbines.value) {
    const to = changedMap.get(t.unit_id) ?? unitState(t.unit_id);
    if (to.off) continue;
    sum += Math.min(to.capMw ?? t.rated_mw, t.rated_mw);
  }
  return Math.round(sum * 100) / 100;
});

function requestApply(): void {
  applyError.value = null;
  applyMessage.value = null;
  if (pendingUnits.value.length === 0) {
    applyError.value = "Nema jedinica sa stvarnom promjenom stanja (odabrano stanje je već aktivno).";
    return;
  }
  previewSiteId.value = siteId.value;
  confirmOpen.value = true;
}

async function confirmApply(): Promise<void> {
  if (previewSiteId.value !== siteId.value) {
    confirmOpen.value = false;
    wtgSelected.value = new Set();
    applyError.value = "Site je promijenjen od pregleda — odabir poništen.";
    return;
  }
  const targetSite = siteId.value;
  confirmOpen.value = false;
  applyBusy.value = true;
  applyError.value = null;
  try {
    const changedUnits = new Set(pendingUnits.value.map((p) => p.unit));
    // ukloni AKTIVNA ograničenja koja se odnose ISKLJUČIVO na mijenjane jedinice
    // (vremenski ograničena pravila drugih jedinica ostaju netaknuta)
    const deletable = activeUnitConstraints.value.filter((c) => (c.unit_ids ?? []).length > 0 && c.unit_ids!.every((u) => changedUnits.has(u))).map((c) => c.id);
    if (deletable.length) await windyApi.constraintsDelete(targetSite, deletable);
    // dodaj nova stanje: jedna offline grupa + curtailment grupe po cap vrijednosti
    const toOff = pendingUnits.value.filter((p) => p.to.off).map((p) => p.unit);
    const byCap = new Map<number, string[]>();
    for (const p of pendingUnits.value) {
      if (p.to.off || p.to.capMw === null) continue;
      const arr = byCap.get(p.to.capMw) ?? [];
      arr.push(p.unit);
      byCap.set(p.to.capMw, arr);
    }
    const rows: Record<string, unknown>[] = [];
    if (toOff.length) rows.push({ kind: "offline", unit_ids: toOff, start_local: startLocal.value, end_local: null });
    for (const [cap, units] of [...byCap.entries()].sort((a, b) => a[0] - b[0])) rows.push({ kind: "curtailment", unit_ids: units, cap_mw: cap, start_local: startLocal.value, end_local: null });
    if (rows.length) {
      const res = await windyApi.constraintsAdd(targetSite, rows);
      applyMessage.value = `Spremljeno (revizija ${res.revision}). Svježavam prilagođene vrijednosti…`;
    } else {
      applyMessage.value = "Ograničenja uklonjena — jedinice rade bez limita.";
    }
    wtgSelected.value = new Set();
    await Promise.all([refreshConstraints(), refreshStatus(), refreshInventory(), fcResource.refresh()]);
  } catch (e) {
    applyError.value = e instanceof Error ? e.message : String(e);
  } finally {
    applyBusy.value = false;
  }
}

async function revertAll(): Promise<void> {
  // vrati SVE aktivne per-unit restrikcije (OFF/cap) — oprezna dvofazna radnja
  applyError.value = null;
  applyMessage.value = null;
  const units = activeUnitConstraints.value.flatMap((c) => c.unit_ids ?? []);
  if (units.length === 0) {
    applyMessage.value = "Nema aktivnih ograničenja po turbinama.";
    return;
  }
  if (!window.confirm(`Ukloniti SVAKA aktivna ograničenja po turbinama (${new Set(units).size} jedinica)?`)) return;
  applyBusy.value = true;
  try {
    const ids = activeUnitConstraints.value.map((c) => c.id);
    if (ids.length) await windyApi.constraintsDelete(siteId.value, ids);
    applyMessage.value = "Sve per-turbine restrikcije uklonjene (nova revizija).";
    wtgSelected.value = new Set();
    await Promise.all([refreshConstraints(), refreshStatus(), fcResource.refresh()]);
  } catch (e) {
    applyError.value = e instanceof Error ? e.message : String(e);
  } finally {
    applyBusy.value = false;
  }
}

// ---- SADA kartica (Addendum A): modelirani uvjeti ---------------------------
const nowResource = useAbortableResource<WindNowResponse>((signal) => windyApi.windNow(siteId.value, signal), () => [siteId.value]);
const nowRefreshing = ref(false);
async function refreshNow(): Promise<void> {
  nowRefreshing.value = true;
  try {
    await nowResource.refresh();
  } finally {
    nowRefreshing.value = false;
  }
}

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
    await Promise.all([fcResource.refresh(), nowResource.refresh()]);
  } catch (e) {
    collectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    collecting.value = false;
  }
}

// site promjena resetira sve site-ovisno stanje
watch(siteId, (newSite, oldSite) => {
  if (newSite === oldSite) return;
  wtgSelected.value = new Set();
  confirmOpen.value = false;
  previewSiteId.value = null;
  applyError.value = null;
  applyMessage.value = null;
});

// ---- satni detalji (24/48) ---------------------------------------------------

const HOURS_OPTIONS = [
  { value: 24, label: "24 h" },
  { value: 48, label: "48 h" },
] as const;
const hourlyHours = ref<24 | 48>(24);
const hourlyModel = ref<string>("ecmwf_ifs");
const hourlyProductionOptions = computed(() => modelColumns.value.filter((c) => !c.metOnly).map((c) => ({ value: c.id, label: c.label })));
watch(hourlyProductionOptions, (opts) => {
  if (opts.length && !opts.some((o) => o.value === hourlyModel.value)) hourlyModel.value = opts[0]!.value;
});

const hourlyResource = useAbortableResource<ForecastModel | null>(
  async () => {
    if (!fcData.value) return null;
    return fcData.value.models.find((m) => m.model_id === hourlyModel.value && m.run !== null) ?? null;
  },
  () => [hourlyModel.value, hourlyHours.value, fcData.value],
);
const hourlyAdjusted = computed<Map<string, AdjustedPoint>>(() => {
  const m = hourlyResource.data.value;
  if (viewMode.value !== "adjusted" || !m?.adjusted?.available) return new Map();
  return new Map(m.adjusted.points.map((p) => [p.valid_at_utc.slice(0, 13), p] as const));
});
function hourlyAdjPoint(r: { validAtUtc: string }): AdjustedPoint | undefined {
  return hourlyAdjusted.value.get(r.validAtUtc.slice(0, 13));
}
const hourlyRows = computed(() => {
  const m = hourlyResource.data.value;
  if (!m) return [];
  // shared as-of anchor across models: generation time rounded up to the next
  // full hour — the window is the NEXT 24/48 h, never hours from the past
  const anchor = fcData.value?.generated_at_utc ?? new Date().toISOString();
  return buildHourlyRows(m.points, anchor, hourlyHours.value, tz.value);
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

function localToUtcLabel(iso: string | null): string {
  return iso ? iso.slice(11, 16) + " UTC" : "—";
}
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
          <div class="flex flex-wrap items-center gap-3">
            <SegmentedToggle v-model="viewMode" :options="[...VIEW_OPTIONS]" inline divided />
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
          Podaci zadnje preuzeti: <span class="text-paper-100">{{ freshness }}</span> (UTC) · vremena po satima prikazana u {{ tz }}
        </p>

        <StateBlock v-if="collectError" kind="error">Dohvat nije uspio: {{ collectError }}</StateBlock>
        <StateBlock v-if="fcError" kind="error">{{ fcError }}</StateBlock>
        <StateBlock v-else-if="fcLoading && !fcData" kind="loading" caption="Učitavam arhivirane prognoze…" />

        <!-- ===== SADA (Addendum A): modelirani uvjeti — NIKAD stvarni vjetar ===== -->
        <section aria-label="Sada — modelirani uvjeti" class="border-ink-700 rounded border px-3 py-2">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-paper-50 font-mono text-sm tracking-wide">SADA — modelirani uvjeti</h2>
            <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 disabled:opacity-50 border px-2 py-0.5 font-mono text-[10px]" :disabled="nowRefreshing" @click="refreshNow">
              {{ nowRefreshing ? "…" : "Osvježi SADA" }}
            </button>
          </div>
          <p class="text-heat-300 mt-1 font-mono text-[10px]">{{ nowResource.data.value?.disclaimer ?? "INFORMACIJA o modeliranim uvjetima iz arhiviranih prognoza — NIJE izmjereni (stvarni) vjetar." }}</p>
          <StateBlock v-if="nowResource.error.value" kind="error">{{ nowResource.error.value }}</StateBlock>
          <div v-else-if="nowResource.data.value" class="mt-1.5 flex flex-wrap gap-2">
            <span
              v-for="m in nowResource.data.value.models"
              :key="m.model_id"
              class="border-ink-600 inline-flex items-center gap-1.5 border px-2 py-0.5 font-mono text-[10px]"
              :title="`${m.note}${m.valid_at_utc ? ` · važi od ${m.valid_at_utc}` : ''}${m.run_issued_utc ? ` · run ${m.run_issued_utc}` : ''}`"
            >
              <span class="inline-block size-2" :style="{ background: modelColor(m.model_id) }" />
              <span class="text-paper-200">{{ MODEL_LABELS_HR[m.model_id] ?? m.model_id }}</span>
              <span v-if="m.wind_speed_mps !== null" class="text-sodium-200">{{ m.wind_speed_mps }} m/s</span>
              <span v-else class="text-paper-500">—</span>
              <span class="text-paper-500">{{ m.met_only ? "(10 m, samo usporedba)" : localToUtcLabel(m.valid_at_utc) }}</span>
              <span v-if="m.stale" class="text-heat-300" title="podatak stariji od 3 h ili nepostoji"> zastarjelo</span>
            </span>
          </div>
          <StateBlock v-else kind="loading" caption="Učitavam…" loading-py="py-2" />
        </section>

        <!-- ===== Rad elektrane (Addendum C): INSTALIRANO vs RASP. MAKS. ===== -->
        <section aria-label="Rad elektrane" class="border-ink-700 rounded border px-3 py-2">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-paper-50 font-mono text-sm tracking-wide">Rad elektrane</h2>
            <RouterLink :to="{ name: 'availability' }" class="text-sodium-200 hover:underline font-mono text-[10px]">Uredi detalje (vremenska ograničenja, scenariji) →</RouterLink>
          </div>
          <div v-if="opStatus && !opStatus.error" class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px]">
            <span class="text-paper-300">INSTALIRANO <span class="text-paper-100">{{ opStatus.installed_mw }} MW</span></span>
            <span class="text-paper-300">RASP. MAKS. <span class="text-sodium-200">{{ opStatus.available_max_mw }} MW</span></span>
            <span class="text-paper-300">OFF <span :class="opStatus.offline_count > 0 ? 'text-heat-300' : 'text-paper-100'">{{ opStatus.offline_count }}</span></span>
            <span class="text-paper-300">Ograničeno <span :class="opStatus.curtailed_count > 0 ? 'text-sodium-200' : 'text-paper-100'">{{ opStatus.curtailed_count }}</span></span>
            <span v-if="opStatus.farm_export_cap_mw !== null" class="text-paper-300">Farm cap <span class="text-sodium-200">{{ opStatus.farm_export_cap_mw }} MW</span></span>
          </div>
          <p v-if="opStatus && opStatus.inventory_present === false" class="text-heat-300 mt-1 font-mono text-[10px]">
            Inventar turbina nije generiran — prilagođeni izračun nije moguć dok se ne generira (u detaljima).
          </p>

          <CollapsibleSection title="Brzo uređivanje turbina (ON/OFF, Max MW)" :default-open="false" lazy>
            <StateBlock v-if="turbines.length === 0" kind="empty" :relaxed="true">
              Inventar turbina nije generiran za ovaj site — generirajte ga u „Uredi detalje".
            </StateBlock>
            <template v-else>
              <div class="mb-2 flex flex-wrap items-center gap-2">
                <input v-model="wtgSearch" placeholder="traži WTG…" class="border-ink-700 bg-ink-900 text-paper-100 w-32 border px-2 py-0.5 font-mono text-[11px]" />
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" @click="selectAllFiltered">Odaberi sve prikazane</button>
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" @click="clearSelection">Očisti odabir</button>
                <span class="text-paper-400 font-mono text-[10px]">{{ wtgSelected.size }} odabrano</span>
              </div>
              <div class="grid max-h-56 grid-cols-4 gap-1 overflow-y-auto sm:grid-cols-6 md:grid-cols-8">
                <button
                  v-for="t in filteredTurbines"
                  :key="t.unit_id"
                  type="button"
                  class="border px-1.5 py-1 font-mono text-[10px]"
                  :class="[
                    wtgSelected.has(t.unit_id) ? 'border-sodium-300 bg-sodium-300/10 text-sodium-100' : 'border-ink-600 text-paper-300 hover:bg-ink-800',
                    unitState(t.unit_id).off ? 'opacity-50 line-through' : '',
                  ]"
                  :title="`${t.unit_id} · nazivno ${t.rated_mw} MW · trenutno: ${unitState(t.unit_id).off ? 'OFF' : unitState(t.unit_id).capMw !== null ? `max ${unitState(t.unit_id).capMw} MW` : 'bez ograničenja'}`"
                  @click="toggleWtg(t.unit_id)"
                >
                  {{ t.unit_id.replace(/^WTG-?0*/, "WTG-") || t.unit_id }}
                  <span class="block text-[9px] opacity-70">{{ unitState(t.unit_id).off ? "OFF" : unitState(t.unit_id).capMw !== null ? unitState(t.unit_id).capMw + " MW" : "ON" }}</span>
                </button>
              </div>

              <div class="mt-3 flex flex-wrap items-center gap-2">
                <span class="text-paper-400 font-mono text-[10px]">Akcija na odabrane:</span>
                <SegmentedToggle v-model="wtgAction" :options="[...WTG_ACTION_OPTIONS]" inline divided />
                <input v-if="wtgAction === 'cap'" v-model.number="bulkCapMw" type="number" step="0.05" min="0" class="border-ink-700 bg-ink-900 text-paper-100 w-20 border px-2 py-0.5 font-mono text-[11px]" />
                <button type="button" class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy || wtgSelected.size === 0" @click="requestApply">
                  {{ applyBusy ? "Spremam…" : "Primijeni" }}
                </button>
                <button type="button" class="border-heat-400/60 text-heat-300 hover:bg-heat-400/10 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy" @click="revertAll">Poništi sve restrikcije</button>
              </div>
              <p v-if="wtgAction === 'cap' && bulkCapMw > 0 && siteData" class="text-paper-400 mt-1 font-mono text-[10px]">
                Max {{ bulkCapMw }} MW ≈ {{ Math.round((bulkCapMw / siteData.site.turbine_rated_mw) * 10000) / 100 }}% nazivne snage ({{ siteData.site.turbine_rated_mw }} MW); u izračunu koristi se puni MW limit.
              </p>
              <p v-if="pendingUnits.length > 0" class="text-paper-300 mt-1 font-mono text-[10px]">
                Promjena za {{ pendingUnits.length }} jedinica:
                <template v-for="(p, i) in pendingUnits.slice(0, 8)" :key="p.unit">
                  <span class="text-sodium-200">{{ p.unit }}</span> ({{ p.from.off ? "OFF" : p.from.capMw !== null ? p.from.capMw + " MW" : "ON" }} → {{ p.to.off ? "OFF" : p.to.capMw !== null ? p.to.capMw + " MW" : "ON" }}){{ i < Math.min(pendingUnits.length, 8) - 1 ? ", " : "" }}
                </template>
                <span v-if="pendingUnits.length > 8"> … ({{ pendingUnits.length - 8 }} više)</span>
                <span v-if="pendingPreviewMw !== null" class="text-paper-400"> · očekivani RASP. MAKS. ≈ {{ pendingPreviewMw }} MW (pregled; konačno izračunava engine nakon spremanja)</span>
              </p>
              <StateBlock v-if="applyError" kind="error">{{ applyError }}</StateBlock>
              <p v-if="applyMessage" class="text-sodium-200 mt-1 font-mono text-[10px]">{{ applyMessage }}</p>
              <p class="text-paper-400 mt-1 font-mono text-[10px]">
                Spremanje ide kroz auditable revizije ograničenja (stare revizije ostaju). Vremenski ograničena pravila postavljena u „Uredi detalje" za NEmijenjane jedinice ostaju netaknuta.
              </p>

              <!-- dvofazna potvrda -->
              <div v-if="confirmOpen" class="border-sodium-300/40 mt-2 border px-3 py-2">
                <p class="text-paper-100 font-mono text-[11px]">Potvrdi primjenu na {{ pendingUnits.length }} jedinica ({{ [...wtgSelected].length }} odabranih)?</p>
                <p class="text-paper-400 mt-1 font-mono text-[10px]">
                  Uklonit će se aktivna ograničenja za mijenjane jedinice i postaviti novo stanje od {{ startLocal }} ({{ tz }}), bez krajnjeg datuma.
                </p>
                <div class="mt-2 flex gap-2">
                  <button type="button" class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy" @click="confirmApply">Potvrdi</button>
                  <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-3 py-1 font-mono text-[11px]" @click="confirmOpen = false">Odustani</button>
                </div>
              </div>
            </template>
          </CollapsibleSection>
        </section>

        <template v-if="fcData">
          <!-- ===== P0: dnevna tablica — TABLE FIRST ===== -->
          <section aria-label="Očekivana proizvodnja po danima">
            <h2 class="text-paper-50 mb-2 font-mono text-base tracking-wide">
              Očekivana proizvodnja po danima (MWh) — {{ viewMode === "adjusted" ? "prilagođeno na raspoloživost" : "bazno (bez ograničenja)" }}
            </h2>
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
                        {{ c.label }}<span v-if="c.metOnly" class="text-paper-500" title="samo vjetar (10 m) — izuzet iz MWh i Prosjeka">*</span>
                      </span>
                    </th>
                    <th class="text-sodium-300 py-1.5 pr-4 text-right" title="aritmetička sredina proizvodnih modela (bez met-only)">Prosjek</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in dailyRows" :key="row.date" class="border-ink-800 border-b">
                    <td class="text-paper-100 py-1.5 pr-4 sticky left-0 bg-ink-950">{{ row.date }}</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-1.5 pr-4 text-right" :class="row.values[c.id] !== null ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="row.values[c.id] !== null">
                        {{ row.values[c.id] }}<span v-if="row.flags[c.id] || adjustedDisabledReason[c.id]" class="text-paper-400" :title="adjustedDisabledReason[c.id] || row.flags[c.id]"> ⚠</span>
                      </template>
                      <template v-else :title="adjustedDisabledReason[c.id] || row.flags[c.id] || 'nema podataka'">—</template>
                    </td>
                    <td class="text-sodium-100 py-1.5 pr-4 text-right" :title="'prosjek proizvodnih modela'">{{ rowAvg(row) ?? "—" }}</td>
                  </tr>
                </tbody>
                <tfoot>
                  <tr class="border-ink-600 text-paper-50 border-t-2">
                    <td class="py-2 pr-4 sticky left-0 bg-ink-950 font-bold" title="UKUPNO = zbroj prikazanih dnevnih MWh po modelu (djelomični dani uključeni, označeni ⚠)">UKUPNO (MWh)</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-2 pr-4 text-right" :class="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined">
                        {{ totalsByModel[c.id]?.total }}<span v-if="totalsByModel[c.id]?.partial" class="text-paper-400" title="uključuje djelomične dane"> ⚠</span>
                      </template>
                      <template v-else>—</template>
                    </td>
                    <td class="text-sodium-100 py-2 pr-4 text-right" :title="'prosjek UKUPNO po proizvodnim modelima'">{{ totalsAverage ?? "—" }}</td>
                  </tr>
                </tfoot>
              </table>
              <p class="text-paper-400 mt-2 font-mono text-[10px]">
                Dani u {{ tz }} (stvarni 23/24/25 hDani prije prelaska). ⚠ = djelomičan dan. * = met-only model (samo vjetar) — izuzet iz MWh, UKUPNO i Prosjeka.
                <template v-if="viewMode === 'adjusted'"> Prilagođeni izračun: scenario engine na backendu (revizije inventara/ograničenja).</template>
                Procjena na temelju vjetra i krivulje snage; nije jamstvo proizvodnje.
              </p>
            </div>
          </section>

          <!-- ===== P0: satni detalji 24/48 ===== -->
          <section aria-label="Satni detalji" class="mt-4">
            <div class="mb-2 flex flex-wrap items-center gap-3">
              <h2 class="text-paper-50 font-mono text-base tracking-wide">Satni detalji</h2>
              <SegmentedToggle v-model="hourlyHours" :options="[...HOURS_OPTIONS]" inline divided />
              <SegmentedToggle v-if="hourlyProductionOptions.length" v-model="hourlyModel" :options="hourlyProductionOptions" inline divided />
            </div>
            <div class="overflow-x-auto">
              <table class="w-full max-w-2xl font-mono text-[11px]">
                <thead>
                  <tr class="text-paper-400 border-ink-700 border-b text-left">
                    <th class="py-1.5 pr-4">Datum i sat ({{ tz }})</th>
                    <th class="py-1.5 pr-4 text-right">Vjetar (m/s)</th>
                    <th class="py-1.5 pr-4 text-right">Snaga (MW)</th>
                    <th class="py-1.5 pr-4 text-right">Energija (MWh)</th>
                    <th v-if="viewMode === 'adjusted'" class="text-sodium-300 py-1.5 pr-4 text-right" title="prilagođeno na raspoloživost">Prilag. MW</th>
                    <th v-if="viewMode === 'adjusted'" class="text-sodium-300 py-1.5 pr-4 text-right">Prilag. MWh</th>
                    <th class="py-1.5 pr-4 text-right">Interval</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in hourlyRows" :key="r.label + r.offset" class="border-ink-800 border-b" :class="r.missing ? 'opacity-50' : ''">
                    <td class="text-paper-100 py-1 pr-4">{{ r.label }} <span class="text-paper-400">{{ r.offset }}</span></td>
                    <td class="py-1 pr-4 text-right" :class="r.wind === null ? 'text-heat-300' : 'text-paper-100'">{{ r.wind ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mw === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mw ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mwh === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mwh ?? "nedostaje" }}</td>
                    <template v-if="viewMode === 'adjusted'">
                      <td class="py-1 pr-4 text-right" :class="hourlyAdjPoint(r)?.adjusted_mw === null ? 'text-heat-300' : 'text-sodium-200'">
                        {{ hourlyAdjPoint(r)?.adjusted_mw ?? (hourlyAdjusted.size ? "nedostaje" : "—") }}
                      </td>
                      <td class="py-1 pr-4 text-right" :class="hourlyAdjPoint(r)?.adjusted_mwh === null ? 'text-heat-300' : 'text-sodium-200'">
                        {{ hourlyAdjPoint(r)?.adjusted_mwh ?? (hourlyAdjusted.size ? "nedostaje" : "—") }}
                      </td>
                    </template>
                    <td class="text-paper-400 py-1 pr-4 text-right">{{ r.interval_hours ? r.interval_hours + " h" : "—" }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="text-paper-400 mt-2 font-mono text-[10px]">MW = prosječna snaga intervala; MWh = energija intervala. „nedostaje" = podatka nema (nikad 0). Prilag. stupci = {{ viewMode === "adjusted" ? "raspoloživost (OFF→0, cap→min(baza, cap))" : "uključite Prilagođeno prikaz" }}.</p>
          </section>

          <!-- ===== sekundarno: grafikoni ===== -->
          <CollapsibleSection title="Grafikoni (vjetar i snaga po satima)" :default-open="false" lazy>
            <p class="text-paper-400 font-mono text-[10px]">Praznine ostaju praznine — nikad se ne popunjavaju nulama. ICON-EU završava na 120 h, ICON Global na 192 h, ARPEGE ~96 h.</p>
            <VChart style="height: 18rem" :option="windOption" autoresize />
            <VChart style="height: 16rem" :option="powerOption" autoresize />
          </CollapsibleSection>

          <!-- ===== metodologija (collapsed) ===== -->
          <CollapsibleSection title="Detalji o podacima i metodologiji" :default-open="false">
            <dl class="space-y-2 font-mono text-[10px] leading-relaxed">
              <div v-for="m in fcData.models" :key="m.model_id" class="border-ink-700 border-b pb-2">
                <dt class="text-sodium-200">{{ m.label }} — {{ m.provider }} (preko Open-Meteo)<span v-if="m.met_only"> · MET-ONLY (nema hub razine — bez MWh)</span></dt>
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
                <dd class="text-paper-300">{{ siteData.source_note }} Vremenske oznake u UTC; dnevni redovi po site vremenskoj zoni ({{ tz }}).</dd>
              </div>
            </dl>
          </CollapsibleSection>
        </template>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
