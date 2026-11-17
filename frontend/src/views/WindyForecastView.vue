<script setup lang="ts">
import { computed, ref, watch } from "vue";
import VChart from "vue-echarts";

import AppFooter from "@/components/AppFooter.vue";
import CollapsibleSection from "@/components/CollapsibleSection.vue";
import SegmentedToggle from "@/components/SegmentedToggle.vue";
import SiteSelector from "@/components/SiteSelector.vue";
import StateBlock from "@/components/StateBlock.vue";
import { nowLocalForTz } from "@/utils/availability";
import {
  buildDailyMatrix,
  buildEnsembleDaily,
  buildHourlyRows,
  loadAvgPreference,
  MODEL_LABELS_HR,
  modelTotal,
  productionModelIds,
  saveAvgPreference,
  visibleDailyRows,
  type DailyRow,
  type DailySlot,
  type EnsembleInput,
} from "@/utils/forecastTable";
import { modelColor, windyApi, type AdjustedPoint, type ForecastModel, type ForecastResponse, type Inventory, type OperationalConstraint, type OperationalStatus, type SiteResponse, type UnitAssignment, type WindNowResponse } from "@/api/windy";
import { useAbortableResource } from "@/composables/useAbortableResource";
import { useActiveSite } from "@/composables/useActiveSite";

// V1.6 + PR#8 Correction A: dnevna tablica MWh po modelu (UKUPNO = zbroj
// PRIKAZANIH dnevnih MWh; Prosjek = hourly-aligned ensemble po odabiru
// vlasnika, per-site perzistiran), satni detalji 24/48, Rad elektrane s
// PER-TURBINE uređivanjem (ON / % ograničenja / Max MW) kroz JEDNU atomarnu
// reviziju (constraints/replace-units — grupisana ograničenja se sigurno
// razdvajaju), i SADA kartica (modelirano, nikad stvarni vjetar).
// Prilagođeni prikaz NIKAD ne fallbacka na bazu: model bez adjusted vrijednosti
// ostaje prazan s vidljivim razlogom.

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
const userChoseView = ref(false);
function setViewMode(v: "baseline" | "adjusted"): void {
  viewMode.value = v;
  userChoseView.value = true;
}
// Correction A §4: kada postoji inventar i bilo kakva aktivna restrikcija,
// primarni prikaz je PRILAGOĐENI (vlasnikova promjena odmah vidljiva);
// vlasnik može eksplicitno natrag na Bazno.
const statusResource = useAbortableResource<OperationalStatus>((signal) => windyApi.operatingStatus(siteId.value, signal), () => [siteId.value]);
const { data: opStatus, refresh: refreshStatus } = statusResource;
watch(
  [opStatus, siteId],
  ([st]) => {
    if (userChoseView.value || !st || st.error) return;
    const restricted = st.offline_count > 0 || st.curtailed_count > 0 || st.farm_export_cap_mw !== null;
    viewMode.value = st.inventory_present && restricted ? "adjusted" : viewMode.value === "adjusted" ? viewMode.value : "baseline";
  },
  { immediate: true },
);

const fcResource = useAbortableResource<ForecastResponse>(
  (signal) => windyApi.forecast(days.value, siteId.value, signal, viewMode.value === "adjusted"),
  () => [days.value, siteId.value, viewMode.value],
);
const { data: fcData, loading: fcLoading, error: fcError } = fcResource;

const modelsWithRun = computed<ForecastModel[]>(() => (fcData.value?.models ?? []).filter((m) => m.run !== null));
const productionIds = computed(() => productionModelIds(modelsWithRun.value));

// Correction A §1.3: u prilagođenom prikazu model BEZ adjusted vrijednosti
// prikazuje PRAZNO s razlogom — nikad tihu zamjenu baznom prognozom
const dailyRows = computed<DailyRow[]>(() =>
  buildDailyMatrix(
    modelsWithRun.value.map((m) => {
      const slots: DailySlot[] = viewMode.value === "adjusted" && m.adjusted?.available ? (m.adjusted.daily as DailySlot[]) : m.daily_local;
      return { model_id: m.model_id, daily_local: slots };
    }),
  ),
);
const adjustedUnavailable = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  if (viewMode.value !== "adjusted") return out;
  for (const m of modelsWithRun.value) {
    if (m.adjusted && !m.adjusted.available && m.adjusted.reason) out[m.model_id] = `prilagođeno nedostupno: ${m.adjusted.reason} — bazna vrijednost Nije prikazana`;
  }
  return out;
});
const visibleRows = computed<DailyRow[]>(() => visibleDailyRows(dailyRows.value, days.value, tz.value));
const modelColumns = computed(() => modelsWithRun.value.map((m) => ({ id: m.model_id, label: MODEL_LABELS_HR[m.model_id] ?? m.model_id, metOnly: m.met_only })));

// ---- Prosjek: hourly ensemble (Correction A §5) ------------------------------
const avgPref = ref<{ enabled: boolean; models: string[] }>({ enabled: true, models: [] });
watch(
  [siteId, productionIds],
  ([sid, ids]) => {
    avgPref.value = loadAvgPreference(sid, ids);
  },
  { immediate: true },
);
watch(
  avgPref,
  (v) => {
    saveAvgPreference(siteId.value, v);
  },
  { deep: true },
);
const avgModels = computed(() => {
  const chosen = avgPref.value.models.filter((id) => productionIds.value.includes(id));
  return chosen.length ? chosen : productionIds.value;
});
function hourlyInputs(pointsOf: (m: ForecastModel) => { valid_at_utc: string; mw: number | null; interval_hours: number }[]): EnsembleInput[] {
  return modelsWithRun.value
    .filter((m) => avgModels.value.includes(m.model_id))
    .map((m) => ({ model_id: m.model_id, points: pointsOf(m) }));
}
const ensembleRows = computed(() => {
  if (!avgPref.value.enabled || avgModels.value.length === 0) return [];
  const inputs =
    viewMode.value === "adjusted"
      ? hourlyInputs((m) => (m.adjusted?.available ? m.adjusted.points.map((p) => ({ valid_at_utc: p.valid_at_utc, mw: p.adjusted_mw, interval_hours: p.interval_hours })) : []))
      : hourlyInputs((m) => m.points.map((p) => ({ valid_at_utc: p.valid_at_utc, mw: p.modeled_power_mw, interval_hours: p.interval_hours })));
  return visibleDailyRows(buildEnsembleDaily(inputs, tz.value), days.value, tz.value);
});
const avgByDate = computed(() => new Map(ensembleRows.value.map((r) => [r.date, r] as const)));
const totalsAverage = computed(() => {
  // UKUPNO Average = ZBROJ prikazanih dnevnih Average vrijednosti (identity
  // requirement) — nikad prosjek period-totala po modelu
  if (!avgPref.value.enabled) return null;
  const total = ensembleRows.value.reduce((acc, r) => acc + r.mwh, 0);
  return Math.round(total * 100) / 100;
});
const avgCoverageNote = computed(() => {
  const rows = ensembleRows.value;
  if (rows.length === 0) return null;
  const mins = new Set(rows.map((r) => r.contributors_min));
  const maxs = new Set(rows.map((r) => r.contributors_max));
  if (mins.size === 1 && maxs.size === 1 && rows[0] && rows[0].contributors_min === rows[0].contributors_max) {
    return `Prosjek: ${rows[0].contributors_min} model(a) po satu`;
  }
  return `Prosjek: broj modela po satu varira (${Math.min(...mins)}–${Math.max(...maxs)}) — pokrivenost nije jednolika`;
});
function toggleAvgModel(id: string): void {
  const set = new Set(avgPref.value.models);
  if (set.has(id)) set.delete(id);
  else set.add(id);
  avgPref.value = { ...avgPref.value, models: [...set] };
}

const totalsByModel = computed(() => {
  const out: Record<string, { total: number | null; partial: boolean; days: number }> = {};
  for (const c of modelColumns.value) out[c.id] = modelTotal(visibleRows.value, c.id);
  return out;
});

// ---- Rad elektrane: per-unit editor + atomarni replace -----------------------
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
  perUnitState.value = {};
}

const nowUtc = computed(() => new Date().toISOString().slice(0, 19) + "Z");
const activeUnitConstraints = computed(() =>
  (constraintsData.value?.constraints ?? []).filter((c) => c.kind !== "export_cap" && c.start_utc <= nowUtc.value && (c.end_utc === null || c.end_utc > nowUtc.value)),
);
function unitState(unit: string): { off: boolean; capMw: number | null; capPercent: number | null } {
  let off = false;
  let capMw: number | null = null;
  let capPercent: number | null = null;
  for (const c of activeUnitConstraints.value) {
    if (!c.unit_ids?.includes(unit)) continue;
    if (c.kind === "offline") off = true;
    if (c.kind === "curtailment") {
      if (c.cap_mw !== null && c.cap_mw !== undefined && (capMw === null || c.cap_mw < capMw)) capMw = c.cap_mw;
      if (c.cap_percent !== null && c.cap_percent !== undefined && (capPercent === null || c.cap_percent < capPercent)) capPercent = c.cap_percent;
    }
  }
  return { off, capMw: off ? null : capMw, capPercent: off ? null : capPercent };
}
function stateLabel(unit: string): string {
  const st = unitState(unit);
  if (st.off) return "OFF";
  if (st.capMw !== null) return `max ${st.capMw} MW`;
  if (st.capPercent !== null) return `max ${st.capPercent}% nazivnog`;
  return "ON";
}

// per-unit desired state: bulk defaults + per-unit overrides
const WTG_ACTION_OPTIONS = [
  { value: "on", label: "ON" },
  { value: "off", label: "OFF" },
  { value: "cap", label: "Ograniči" },
] as const;
type UnitAction = "on" | "off" | "cap";
interface UnitEdit {
  action: UnitAction;
  reductionPct: number | null; // % SMANJENJE (0 = bez ograničenja, 100 = nula) → backend cap_percent = 100 − reduction
  capMw: number | null;
}
const bulkAction = ref<UnitAction>("cap");
const bulkReductionPct = ref<number>(12.28); // 2,50 MW dopušteno na 2,85 MW nazivnog
const bulkCapMw = ref<number>(2.5);
const perUnitState = ref<Record<string, UnitEdit>>({});

function unitEdit(unit: string): UnitEdit {
  const o = perUnitState.value[unit];
  if (o) return o;
  return { action: bulkAction.value, reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value };
}
function setUnitEdit(unit: string, edit: UnitEdit): void {
  perUnitState.value = { ...perUnitState.value, [unit]: edit };
}
function applyBulkToSelection(): void {
  const next: Record<string, UnitEdit> = {};
  for (const u of wtgSelected.value) next[u] = { action: bulkAction.value, reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value };
  perUnitState.value = next;
}
function allowedMw(unit: string, edit: UnitEdit): number | null {
  const rated = ratedById.value.get(unit);
  if (!rated) return null;
  if (edit.action === "off") return 0;
  if (edit.action === "on") return rated;
  if (edit.capMw !== null && edit.capMw !== undefined) return Math.min(edit.capMw, rated);
  if (edit.reductionPct !== null && edit.reductionPct !== undefined) return Math.round(((100 - edit.reductionPct) / 100) * rated * 1000) / 1000;
  return rated;
}
function mwFromReduction(unit: string, edit: UnitEdit): number | null {
  const rated = ratedById.value.get(unit);
  if (!rated || edit.reductionPct === null) return null;
  return Math.round(((100 - edit.reductionPct) / 100) * rated * 1000) / 1000;
}

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

// assignments differ from the CURRENT active state (Correction A §4)
const pendingAssignments = computed<UnitAssignment[]>(() => {
  const out: UnitAssignment[] = [];
  for (const unit of [...wtgSelected.value].sort()) {
    const edit = unitEdit(unit);
    const cur = unitState(unit);
    if (edit.action === "on") {
      if (!cur.off && cur.capMw === null && cur.capPercent === null) continue;
      out.push({ unit_id: unit, state: "on" });
      continue;
    }
    if (edit.action === "off") {
      if (cur.off) continue;
      out.push({ unit_id: unit, state: "off" });
      continue;
    }
    // cap: percent SMANJENJE → backend cap_percent = 100 − reduction (ALLOWED)
    const reduction = edit.reductionPct ?? 0;
    const allowedPct = Math.round((100 - reduction) * 10000) / 10000;
    const allowedMwNow = mwFromReduction(unit, edit);
    if (cur.capPercent !== null && Math.abs(cur.capPercent - allowedPct) < 1e-6) continue;
    if (cur.capMw !== null && allowedMwNow !== null && Math.abs(cur.capMw - allowedMwNow) < 1e-6 && cur.capPercent === null) continue;
    if (allowedMwNow !== null && allowedMwNow >= ratedById.value.get(unit)!) continue; // no-op limit
    out.push({ unit_id: unit, state: "cap", cap_percent: allowedPct });
  }
  return out;
});

const pendingPreviewMw = computed(() => {
  const st = opStatus.value;
  if (!st || !st.inventory_present) return null;
  const map = new Map(pendingAssignments.value.map((a) => [a.unit_id, a] as const));
  let sum = 0;
  for (const t of turbines.value) {
    const a = map.get(t.unit_id);
    if (a) {
      if (a.state === "off") continue;
      if (a.state === "cap" && a.cap_percent !== null && a.cap_percent !== undefined) sum += (a.cap_percent / 100) * t.rated_mw;
      else if (a.state === "cap" && a.cap_mw !== null && a.cap_mw !== undefined) sum += Math.min(a.cap_mw, t.rated_mw);
      else sum += t.rated_mw;
      continue;
    }
    const cur = unitState(t.unit_id);
    if (cur.off) continue;
    sum += Math.min(cur.capMw ?? t.rated_mw, t.rated_mw);
  }
  return Math.round(sum * 100) / 100;
});

function requestApply(): void {
  applyError.value = null;
  applyMessage.value = null;
  if (pendingAssignments.value.length === 0) {
    applyError.value = "Nema stvarne promjene stanja za odabrane jedinice.";
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
    // Correction A §4: ONE atomic replace-units revision (grouped active
    // constraints are split server-side; no delete+add failure window)
    const res = await windyApi.constraintsReplaceUnits(targetSite, {
      assignments: pendingAssignments.value,
      start_local: startLocal.value,
      end_local: null,
    });
    applyMessage.value = `Spremljeno atomarno (revizija ${res.revision}, ${pendingAssignments.value.length} jedinica). Osvježavam prilagođene vrijednosti…`;
    wtgSelected.value = new Set();
    perUnitState.value = {};
    if (opStatus.value?.inventory_present) setViewMode("adjusted"); // change must be visible immediately
    await Promise.all([refreshConstraints(), refreshStatus(), refreshInventory(), fcResource.refresh()]);
  } catch (e) {
    applyError.value = e instanceof Error ? e.message : String(e);
  } finally {
    applyBusy.value = false;
  }
}

async function revertAll(): Promise<void> {
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
    const res = await windyApi.constraintsReplaceUnits(target_site(), {
      assignments: [...new Set(units)].map((u) => ({ unit_id: u, state: "on" as const })),
      start_local: startLocal.value,
      end_local: null,
    });
    applyMessage.value = `Sve per-turbine restrikcije uklonjene (revizija ${res.revision}).`;
    wtgSelected.value = new Set();
    perUnitState.value = {};
    await Promise.all([refreshConstraints(), refreshStatus(), fcResource.refresh()]);
  } catch (e) {
    applyError.value = e instanceof Error ? e.message : String(e);
  } finally {
    applyBusy.value = false;
  }
}
function target_site(): string {
  return siteId.value;
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

watch(siteId, (newSite, oldSite) => {
  if (newSite === oldSite) return;
  wtgSelected.value = new Set();
  perUnitState.value = {};
  confirmOpen.value = false;
  previewSiteId.value = null;
  applyError.value = null;
  applyMessage.value = null;
  userChoseView.value = false;
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

function utcLabel(iso: string | null): string {
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
            <SegmentedToggle v-model="viewMode" :options="[...VIEW_OPTIONS]" inline divided @update:model-value="(v) => setViewMode(v as 'baseline' | 'adjusted')" />
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
              <span class="text-paper-500">{{ m.met_only ? "(10 m, samo usporedba)" : utcLabel(m.valid_at_utc) }}</span>
              <span v-if="m.stale" class="text-heat-300" title="podatak stariji od 3 h ili nepostoji"> zastarjelo</span>
            </span>
          </div>
          <StateBlock v-else kind="loading" caption="Učitavam…" loading-py="py-2" />
        </section>

        <!-- ===== Rad elektrane (Addendum C + Correction A §4) ===== -->
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

          <CollapsibleSection title="Brzo uređivanje turbina (ON / % ograničenja / Max MW — po jedinici ili bulk)" :default-open="false" lazy>
            <StateBlock v-if="turbines.length === 0" kind="empty" :relaxed="true">
              Inventar turbina nije generiran za ovaj site — generirajte ga u „Uredi detalje".
            </StateBlock>
            <template v-else>
              <div class="mb-2 flex flex-wrap items-center gap-2">
                <input v-model="wtgSearch" placeholder="traži WTG…" class="border-ink-700 bg-ink-900 text-paper-100 w-32 border px-2 py-0.5 font-mono text-[11px]" />
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" @click="selectAllFiltered">Odaberi sve prikazane</button>
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" @click="clearSelection">Očisti</button>
                <span class="text-paper-400 font-mono text-[10px]">{{ wtgSelected.size }} odabrano</span>
              </div>
              <div class="grid max-h-56 grid-cols-4 gap-1 overflow-y-auto sm:grid-cols-6 md:grid-cols-8">
                <button
                  v-for="t in filteredTurbines"
                  :key="t.unit_id"
                  type="button"
                  class="border px-1.5 py-1 font-mono text-[10px]"
                  :class="[wtgSelected.has(t.unit_id) ? 'border-sodium-300 bg-sodium-300/10 text-sodium-100' : 'border-ink-600 text-paper-300 hover:bg-ink-800', unitState(t.unit_id).off ? 'opacity-50 line-through' : '']"
                  :title="`${t.unit_id} · nazivno ${t.rated_mw} MW · trenutno: ${stateLabel(t.unit_id)}`"
                  @click="toggleWtg(t.unit_id)"
                >
                  {{ t.unit_id }}
                  <span class="block text-[9px] opacity-70">{{ stateLabel(t.unit_id) }}</span>
                </button>
              </div>

              <!-- bulk preset za odabrane -->
              <div class="mt-3 flex flex-wrap items-center gap-2">
                <span class="text-paper-400 font-mono text-[10px]">Bulk na odabrane:</span>
                <SegmentedToggle v-model="bulkAction" :options="[...WTG_ACTION_OPTIONS]" inline divided />
                <template v-if="bulkAction === 'cap'">
                  <label class="text-paper-400 font-mono text-[10px]">
                    % smanjenja
                    <input v-model.number="bulkReductionPct" type="number" step="0.01" min="0" max="100" class="border-ink-700 bg-ink-900 text-paper-100 w-20 mx-1 border px-2 py-0.5 font-mono text-[11px]" />
                  </label>
                  <span class="text-paper-500 font-mono text-[10px]">≈ {{ Math.round(((100 - (bulkReductionPct || 0)) / 100) * (siteData.site.turbine_rated_mw || 0) * 1000) / 1000 }} MW dopušteno</span>
                  <span class="text-paper-500 font-mono text-[10px]">ili Max MW:</span>
                  <input v-model.number="bulkCapMw" type="number" step="0.05" min="0" class="border-ink-700 bg-ink-900 text-paper-100 w-20 border px-2 py-0.5 font-mono text-[11px]" />
                </template>
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" @click="applyBulkToSelection">Primijeni bulk na odabrane</button>
              </div>
              <p class="text-paper-500 mt-1 font-mono text-[10px]">
                % smanjenja ≠ % dopušteno: 12,28% smanjenje na 2,85 MW nazivnog = 2,50 MW dopušteno (backend sprema puni MW/„% dopušteno" limit).
              </p>

              <!-- per-jedinica uređivanje -->
              <div v-if="wtgSelected.size > 0" class="border-ink-700 mt-2 max-h-52 space-y-1 overflow-y-auto border px-2 py-2">
                <div v-for="u in [...wtgSelected].sort()" :key="u" class="flex flex-wrap items-center gap-2">
                  <span class="text-paper-100 w-20 font-mono text-[11px]">{{ u }}</span>
                  <span class="text-paper-500 font-mono text-[10px]">sada: {{ stateLabel(u) }}</span>
                  <SegmentedToggle
                    :model-value="unitEdit(u).action"
                    :options="[...WTG_ACTION_OPTIONS]"
                    inline
                    divided
                    padding="px-1.5 py-0.5"
                    @update:model-value="(v) => setUnitEdit(u, { ...unitEdit(u), action: v as UnitAction })"
                  />
                  <template v-if="unitEdit(u).action === 'cap'">
                    <label class="text-paper-400 font-mono text-[10px]">
                      %smanj.
                      <input :value="unitEdit(u).reductionPct ?? 0" type="number" step="0.01" min="0" max="100" class="border-ink-700 bg-ink-900 text-paper-100 w-16 mx-1 border px-1 py-0.5 font-mono text-[11px]" @input="(e) => setUnitEdit(u, { ...unitEdit(u), reductionPct: Number((e.target as HTMLInputElement).value) })" />
                    </label>
                    <span class="text-sodium-200 font-mono text-[10px]">= {{ mwFromReduction(u, unitEdit(u)) ?? "—" }} MW dopušteno</span>
                    <span class="text-paper-500 font-mono text-[10px]">ili MW:</span>
                    <input :value="unitEdit(u).capMw ?? ''" type="number" step="0.05" min="0" class="border-ink-700 bg-ink-900 text-paper-100 w-16 border px-1 py-0.5 font-mono text-[11px]" @input="(e) => setUnitEdit(u, { ...unitEdit(u), capMw: (e.target as HTMLInputElement).value === '' ? null : Number((e.target as HTMLInputElement).value) })" />
                  </template>
                  <span class="text-paper-400 font-mono text-[10px]">→ {{ allowedMw(u, unitEdit(u)) }} MW dopušteno</span>
                </div>
              </div>

              <div class="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy || wtgSelected.size === 0" @click="requestApply">
                  {{ applyBusy ? "Spremam…" : "Primijeni (atomarno)" }}
                </button>
                <button type="button" class="border-heat-400/60 text-heat-300 hover:bg-heat-400/10 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy" @click="revertAll">Poništi sve restrikcije</button>
              </div>
              <p v-if="pendingAssignments.length > 0" class="text-paper-300 mt-1 font-mono text-[10px]">
                Promjena za {{ pendingAssignments.length }} jedinica:
                <template v-for="(a, i) in pendingAssignments.slice(0, 8)" :key="a.unit_id">
                  <span class="text-sodium-200">{{ a.unit_id }}</span> → {{ a.state === "on" ? "ON" : a.state === "off" ? "OFF" : `max ${a.cap_percent}% dopušteno` }}{{ i < Math.min(pendingAssignments.length, 8) - 1 ? ", " : "" }}
                </template>
                <span v-if="pendingAssignments.length > 8"> … ({{ pendingAssignments.length - 8 }} više)</span>
                <span v-if="pendingPreviewMw !== null" class="text-paper-400"> · očekivani RASP. MAKS. ≈ {{ pendingPreviewMw }} MW (pregled; konačno izračunava engine)</span>
              </p>
              <StateBlock v-if="applyError" kind="error">{{ applyError }}</StateBlock>
              <p v-if="applyMessage" class="text-sodium-200 mt-1 font-mono text-[10px]">{{ applyMessage }}</p>
              <p class="text-paper-400 mt-1 font-mono text-[10px]">
                Spremanje je JEDNA atomarna revizija: grupisana aktivna ograničenja se sigurno razdvoju (ne-obraćene jedinice zadržavaju svoje redove), stare revizije ostaju u audit trailu. Vremenski ograničena pravila za ne-obraćene jedinice ostaju netaknuta.
              </p>

              <div v-if="confirmOpen" class="border-sodium-300/40 mt-2 border px-3 py-2">
                <p class="text-paper-100 font-mono text-[11px]">Potvrdi primjenu na {{ pendingAssignments.length }} jedinica?</p>
                <p class="text-paper-400 mt-1 font-mono text-[10px]">Nove vrijednosti vrijede od {{ startLocal }} ({{ tz }}), bez krajnjeg datuma. Stare revizije ostaju arhivirane.</p>
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
            <div v-if="Object.keys(adjustedUnavailable).length" class="text-heat-300 mb-2 font-mono text-[10px]">
              ⚠ {{ Object.values(adjustedUnavailable).join(" · ") }}
            </div>
            <StateBlock v-if="visibleRows.length === 0" kind="empty" :relaxed="true">
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
                    <th class="text-sodium-300 py-1.5 pr-4 text-right" :title="avgCoverageNote ?? ''">
                      <button type="button" class="hover:underline" :class="avgPref.enabled ? '' : 'text-paper-500 line-through'" @click="avgPref = { ...avgPref, enabled: !avgPref.enabled }">Prosjek</button>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in visibleRows" :key="row.date" class="border-ink-800 border-b">
                    <td class="text-paper-100 py-1.5 pr-4 sticky left-0 bg-ink-950">{{ row.date }}</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-1.5 pr-4 text-right" :class="row.values[c.id] !== null ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="row.values[c.id] !== null">
                        {{ row.values[c.id] }}<span v-if="row.flags[c.id] || adjustedUnavailable[c.id]" class="text-paper-400" :title="adjustedUnavailable[c.id] || row.flags[c.id]"> ⚠</span>
                      </template>
                      <template v-else :title="adjustedUnavailable[c.id] || row.flags[c.id] || 'nema podataka'">—</template>
                    </td>
                    <td class="text-sodium-100 py-1.5 pr-4 text-right" :title="avgByDate.get(row.date) ? `${avgByDate.get(row.date)!.contributors_min}–${avgByDate.get(row.date)!.contributors_max} modela po satu; pokriveno ${avgByDate.get(row.date)!.hours_counted} h` : 'nema ensemble vrijednosti'">
                      <template v-if="avgPref.enabled && avgByDate.get(row.date)">{{ avgByDate.get(row.date)!.mwh }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
                <tfoot>
                  <tr class="border-ink-600 text-paper-50 border-t-2">
                    <td class="py-2 pr-4 sticky left-0 bg-ink-950 font-bold" title="UKUPNO = zbroj PRIKAZANIH dnevnih MWh po modelu (djelomični dani uključeni, označeni ⚠)">UKUPNO (MWh)</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-2 pr-4 text-right" :class="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined">
                        {{ totalsByModel[c.id]?.total }}<span v-if="totalsByModel[c.id]?.partial" class="text-paper-400" title="uključuje djelomične dane"> ⚠</span>
                      </template>
                      <template v-else>—</template>
                    </td>
                    <td class="text-sodium-100 py-2 pr-4 text-right" :title="'UKUPNO Prosjek = zbroj PRIKAZANIH dnevnih Average vrijednosti (identitet redova, nikad prosjek period-totala)'">
                      <template v-if="totalsAverage !== null">{{ totalsAverage }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tfoot>
              </table>
              <div class="mt-2 flex flex-wrap items-center gap-2" v-if="avgPref.enabled">
                <span class="text-paper-400 font-mono text-[10px]">Prosjek modeli:</span>
                <button
                  v-for="pid in productionIds"
                  :key="pid"
                  type="button"
                  class="border px-1.5 py-0.5 font-mono text-[10px]"
                  :class="avgPref.models.includes(pid) ? 'border-sodium-300/60 text-sodium-200' : 'border-ink-600 text-paper-500'"
                  @click="toggleAvgModel(pid)"
                >{{ modelColumns.find((c) => c.id === pid)?.label ?? pid }}</button>
                <span class="text-paper-500 font-mono text-[10px]">{{ avgCoverageNote }}</span>
              </div>
              <p class="text-paper-400 mt-2 font-mono text-[10px]">
                Prikazano točno {{ visibleRows.length }} lokalnih dana od {{ visibleRows[0]?.date }} ({{ tz }}); UKUPNO je zbroj upravo tih redova. ⚠ = djelomičan dan. * = met-only (bez MWh).
                <template v-if="viewMode === 'adjusted'"> Prilagođeno: scenario engine na backendu (revizije inventara/ograničenja).</template>
                Procjena na temelju vjetra i referentne krivulje; nije jamstvo proizvodnje.
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
            <p class="text-paper-400 mt-2 font-mono text-[10px]">MW = prosječna snaga intervala; MWh = energija intervala. „nedostaje" = podatka nema (nikad 0).</p>
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
                <dd class="text-paper-300">{{ siteData.source_note }} Vremenske oznake u UTC; dnevni redovi po site vremenskoj zoni ({{ tz }}). Besplatni javni Open-Meteo modeli — bez plaćenih/probatnih API-ja.</dd>
              </div>
            </dl>
          </CollapsibleSection>
        </template>
      </template>
    </main>
    <AppFooter />
  </div>
</template>
