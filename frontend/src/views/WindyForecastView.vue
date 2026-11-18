<script setup lang="ts">
import { computed, ref, watch, type Ref } from "vue";
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
  sumDisplayedAverages,
} from "@/utils/forecastTable";
import { buildCompareRows, compareAverage, type CompareInput, type CompareRow } from "@/utils/compareTable";
import { energyLabel, formatEnergy, loadEnergyUnit, saveEnergyUnit, type EnergyUnit } from "@/utils/energyUnits";
import { loadChartVisibility, saveChartVisibility, type ChartVisibility } from "@/utils/chartVisibility";
import { buildAssignments, editAllowedMw, effectiveCapMw, type CapMode, type UnitAssignment, type UnitEdit } from "@/utils/unitAssignments";
import { modelColor, windyApi, type AdjustedPoint, type ForecastModel, type ForecastResponse, type Inventory, type OperationalConstraint, type OperationalStatus, type SiteResponse, type WeatherNowResponse, type WindNowResponse } from "@/api/windy";
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
      // Correction B §1: adjusted mode uses ONLY valid adjusted daily rows —
      // when a model's adjusted block is unavailable (no inventory, curve
      // mismatch, no run, met-only, backend error) its cells stay EMPTY with
      // the visible reason. NEVER fall back to baseline daily_local here:
      // baseline numbers must not be displayed as "prilagođeno".
      const slots: DailySlot[] = viewMode.value === "adjusted" ? (m.adjusted?.available ? (m.adjusted.daily as DailySlot[]) : []) : m.daily_local;
      return { model_id: m.model_id, daily_local: slots };
    }),
  ),
);
const adjustedUnavailable = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  if (viewMode.value !== "adjusted") return out;
  for (const m of modelsWithRun.value) {
    if (m.adjusted && !m.adjusted.available && m.adjusted.reason) out[m.model_id] = `prilagođeno nedostupno: ${m.adjusted.reason} — bazne vrijednosti NISU prikazane`;
  }
  return out;
});
// Correction B §1: when NO model has a valid adjusted block, say so loudly
// instead of showing a plausible-looking (baseline) MWh table
const adjustedAllUnavailable = computed(() => viewMode.value === "adjusted" && modelsWithRun.value.length > 0 && modelsWithRun.value.every((m) => !m.adjusted?.available));
const visibleRows = computed<DailyRow[]>(() => visibleDailyRows(dailyRows.value, days.value, tz.value));
const modelColumns = computed(() => modelsWithRun.value.map((m) => ({
  id: m.model_id,
  label: MODEL_LABELS_HR[m.model_id] ?? m.model_id,
  metOnly: m.met_only,
  cycle: m.run_selection?.cycle_kind,
  issue: m.run?.run_issued_utc ?? null,
  lastUsable: m.run_selection?.last_usable_wind_utc ?? null,
  leadHours: m.run_selection?.available_lead_hours ?? null,
  newerIssuePreserved: m.run_selection?.newer_issue_preserved ?? false,
})));
function cycleText(kind?: string): string {
  if (kind === "long") return "DUGI";
  if (kind === "short") return "KRATKI";
  if (kind === "incomplete-long") return "NEPOTPUN DUGI";
  if (kind === "unknown") return "NEPOZNAT RUN";
  return "";
}

// ---- Correction E Addendum A §1: chart legends — per-site, per-chart,
// client-side ONLY (no API calls on toggle). Wind and power selections are
// fully independent. UKMO (met-only, 10 m) is OFF by default on the wind
// chart and can NEVER appear on the power chart. Stored selections survive
// rerenders and refetches; a new model (e.g. repaired GEM) needs no code.
const windVis = ref<ChartVisibility>({ hidden: [], configured: false });
const powerVis = ref<ChartVisibility>({ hidden: [], configured: false });
watch(
  [siteId, modelsWithRun],
  ([sid, models]) => {
    const ids = models.map((m) => m.model_id);
    const metOnly = models.filter((m) => m.met_only).map((m) => m.model_id);
    windVis.value = loadChartVisibility("wind", sid, ids, metOnly);
    powerVis.value = loadChartVisibility("power", sid, ids, metOnly);
  },
  { immediate: true, deep: false },
);
watch(windVis, (v) => saveChartVisibility("wind", siteId.value, v), { deep: true });
watch(powerVis, (v) => saveChartVisibility("power", siteId.value, v), { deep: true });

// Correction F P0-2: template auto-unwraps refs, so handlers take the VALUE
// plus the chart KEY and assign to the correct ref (.value) — the previous
// return-only version was a no-op click
function visRef(chart: "wind" | "power"): Ref<ChartVisibility> {
  return chart === "wind" ? windVis : powerVis;
}
function toggleLegend(chart: "wind" | "power", modelId: string): void {
  const vis = visRef(chart);
  const hidden = new Set(vis.value.hidden);
  if (hidden.has(modelId)) hidden.delete(modelId);
  else hidden.add(modelId);
  vis.value = { ...vis.value, hidden: [...hidden], configured: true };
}
function setLegendAll(chart: "wind" | "power", hideAll: boolean): void {
  const vis = visRef(chart);
  const ids = (fcData.value?.models ?? []).map((m) => m.model_id);
  vis.value = { ...vis.value, hidden: hideAll ? [...ids] : [], configured: true };
}
function legendState(chart: "wind" | "power", modelId: string): { active: boolean; hasData: boolean } {
  const vis = visRef(chart);
  const m = (fcData.value?.models ?? []).find((x) => x.model_id === modelId);
  return { active: !vis.value.hidden.includes(modelId), hasData: !!m?.run };
}

// ---- Correction E Addendum B §1: display energy unit MWh|GWh (per site).
// PURE presentation: stored numbers stay MWh; conversion only at render.
// Power (MW), wind (m/s), installed MW, Max MW are NEVER converted.
const ENERGY_UNIT_OPTIONS = [
  { value: "mwh", label: "MWh" },
  { value: "gwh", label: "GWh" },
] as const;
const energyUnit = ref<EnergyUnit>("mwh");
watch(
  siteId,
  (sid) => {
    energyUnit.value = loadEnergyUnit(sid);
  },
  { immediate: true },
);
watch(energyUnit, (u) => saveEnergyUnit(siteId.value, u));
function fmtE(rawMwh: number | null | undefined): string | null {
  return formatEnergy(rawMwh, energyUnit.value);
}

// ---- Prosjek: hourly ensemble (Correction A §5) ------------------------------
const avgPref = ref<{ enabled: boolean; configured: boolean; models: string[] }>({ enabled: true, configured: false, models: [] });
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
// Correction B §3: distinguish never-configured (default = all production
// models) from EXPLICITLY selected zero models (Prosjek shows "—" with a
// hint). An empty selection never silently re-enables every model.
const avgModels = computed(() => {
  if (!avgPref.value.configured) return productionIds.value;
  return avgPref.value.models.filter((id) => productionIds.value.includes(id));
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
  return buildEnsembleDaily(inputs, tz.value);
});
const avgByDate = computed(() => new Map(ensembleRows.value.map((r) => [r.date, r] as const)));
const totalsAverage = computed(() => {
  // Correction D §4: UKUPNO Average = zbroj Average vrijednosti UPRAVO
  // PRIKAZANIH redova (isti kalendar kao dnevna tablica — nikad dodatni dan
  // koji postoji samo u ensemble-u). Bez ijednog contributor reda -> null
  // (em dash), NIKAD 0: nedostajuca prognoza nije nula. Stvarna 0 (ispod
  // cut-in, s pokrivenim satima) ostaje 0.
  if (!avgPref.value.enabled) return null;
  return sumDisplayedAverages(visibleRows.value, avgByDate.value);
});
const avgCoverageNote = computed(() => {
  const rows = visibleRows.value.map((r) => avgByDate.value.get(r.date)).filter((r): r is NonNullable<typeof r> => !!r);
  if (rows.length === 0) return avgPref.value.enabled ? "Prosjek: nema ensemble vrijednosti za prikazane dane" : null;
  const mins = new Set(rows.map((r) => r.contributors_min));
  const maxs = new Set(rows.map((r) => r.contributors_max));
  if (mins.size === 1 && maxs.size === 1 && rows[0] && rows[0].contributors_min === rows[0].contributors_max) {
    if (rows[0].contributors_min === 1) {
      return "⚠ Prosjek: SAMO JEDAN model pokriva prikazane dane — vrijednost je jednokratni model, ne multi-model ensemble";
    }
    return `Prosjek: ${rows[0].contributors_min} model(a) po satu`;
  }
  const lo = Math.min(...mins);
  if (lo === 1) {
    return `⚠ Prosjek: broj modela pada na SAMO JEDAN u dijelu prikaza (${Math.min(...mins)}–${Math.max(...maxs)}) — ti dani su jednokratni model, ne ensemble`;
  }
  return `Prosjek: broj modela po satu varira (${Math.min(...mins)}–${Math.max(...maxs)}) — pokrivenost nije jednolika`;
});
function toggleAvgModel(id: string): void {
  const set = new Set(avgPref.value.models);
  if (set.has(id)) set.delete(id);
  else set.add(id);
  avgPref.value = { ...avgPref.value, configured: true, models: [...set] };
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
const CAP_MODE_OPTIONS = [
  { value: "mw", label: "Max MW" },
  { value: "pct", label: "% smanjenja" },
] as const;
const bulkAction = ref<UnitAction>("cap");
const bulkMode = ref<CapMode>("mw"); // vlasnicka kanonska vrijednost je 2,50 MW
const bulkReductionPct = ref<number>(12.28); // ≡ 2,50 MW dopusteno na 2,85 MW nazivnog
const bulkCapMw = ref<number>(2.5);
const perUnitState = ref<Record<string, UnitEdit>>({});

function unitEdit(unit: string): UnitEdit {
  const o = perUnitState.value[unit];
  if (o) return o;
  return { action: bulkAction.value, mode: bulkMode.value, reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value };
}
function setUnitEdit(unit: string, edit: UnitEdit): void {
  perUnitState.value = { ...perUnitState.value, [unit]: edit };
}
function applyBulkToSelection(): void {
  const next: Record<string, UnitEdit> = {};
  for (const u of wtgSelected.value) next[u] = { action: bulkAction.value, mode: bulkMode.value, reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value };
  perUnitState.value = next; // bulk izbor ZAMJENJUJE zastarjele per-unit override (Correction D §1)
}
// autoritativni mod: prikaz sinhroniziran, mod se nikad tiho ne prebacuje
function allowedMw(unit: string, edit: UnitEdit): number | null {
  const rated = ratedById.value.get(unit);
  if (!rated) return null;
  return editAllowedMw(edit, rated);
}

const applyBusy = ref(false); // bulk save in flight
let siteGeneration = 0;
let saveOperationId = 0; // invalidate pending writes when switching site
function isCurrentWrite(site: string, opId: number): boolean {
  return siteId.value === site && saveOperationId === opId;
}
const applyError = ref<string | null>(null);
const applyMessage = ref<string | null>(null);
const busyUnits = ref<Set<string>>(new Set()); // per-unit one-click save in flight
const unitErrors = ref<Map<string, string>>(new Map()); // per-unit last error
const startLocal = ref<string>(nowLocalForTz("Europe/Podgorica"));
watch(
  tz,
  (v) => {
    startLocal.value = nowLocalForTz(v);
  },
  { immediate: true },
);

// Correction E §1: fresh timestamp for EVERY click — a page open for hours
// must never retroactively date a new action from mount time
function freshStart(): string {
  return nowLocalForTz(tz.value);
}

// site the write was issued for — a pending response must never refresh a
// different site's UI (Correction E §1, site-switch guard).
// Correction F P0-6: explicit result contract — callers clear selection ONLY
// on confirmed success; on failure user edits and previous confirmed state
// stay, retry remains available. Stale-site responses report ok:false.
async function saveAssignments(assignments: UnitAssignment[], targetUnits: string[]): Promise<{ ok: boolean; revision: number | null }> {
  const targetSite = siteId.value;
  const opId = ++saveOperationId;
  applyBusy.value = true;
  applyError.value = null;
  applyMessage.value = null;
  const attempt = (expected: number | null) =>
    windyApi.constraintsReplaceUnits(targetSite, {
      assignments,
      start_local: freshStart(),
      end_local: null,
      ...(expected !== null ? { expected_revision: expected } : {}),
    });
  try {
    // Only the POST and its revision retry decide whether a write succeeded.
    // A failed follow-up status fetch must NEVER turn a committed write into
    // "not saved" or leave a bulk selection available for duplicate replay.
    let res: Awaited<ReturnType<typeof windyApi.constraintsReplaceUnits>>;
    try {
      res = await attempt(constraintsData.value?.revision ?? null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!msg.includes("409")) throw e;
      await refreshConstraints();
      if (!isCurrentWrite(targetSite, opId)) return { ok: false, revision: null };
      res = await attempt(constraintsData.value?.revision ?? null); // retry only once
    }
    if (!isCurrentWrite(targetSite, opId)) return { ok: false, revision: res.revision };

    let statusNote = "Status elektrane nije potvrđen (pokušajte osvježiti prikaz).";
    try {
      const st = await windyApi.operatingStatus(targetSite);
      if (!isCurrentWrite(targetSite, opId)) return { ok: false, revision: res.revision };
      statusNote = `Server potvrdio RASP. MAKS. ${st.available_max_mw} MW.`;
      if (st.n_turbines > 0) setViewMode("adjusted");
    } catch {
      // The write is already committed: status failure is warning, not a save error.
    }
    if (!isCurrentWrite(targetSite, opId)) return { ok: false, revision: res.revision };
    const refreshed = await Promise.allSettled([
      refreshConstraints(), refreshStatus(), refreshInventory(), fcResource.refresh(),
    ]);
    if (!isCurrentWrite(targetSite, opId)) return { ok: false, revision: res.revision };
    const failedRefreshes = refreshed.filter((x) => x.status === "rejected").length;
    const refreshNote = failedRefreshes
      ? ` ${failedRefreshes} osvježavanja nije uspjelo; podaci mogu biti zastarjeli.`
      : "";
    applyMessage.value =
      `Spremljeno atomarno (revizija ${res.revision}, ${targetUnits.length} jedinica: ${targetUnits.slice(0, 6).join(", ")}${targetUnits.length > 6 ? "…" : ""}). ` +
      statusNote + refreshNote;
    return { ok: true, revision: res.revision };
  } catch (e) {
    if (isCurrentWrite(targetSite, opId)) {
      applyError.value = `Spremanje nije potvrđeno: ${e instanceof Error ? e.message : String(e)}. Provjerite aktualno stanje prije ponavljanja.`;
    }
    return { ok: false, revision: null };
  } finally {
    if (isCurrentWrite(targetSite, opId)) applyBusy.value = false;
  }
}

// Correction E §1: ONE-CLICK per-unit ON/OFF — exactly one atomic request,
// no Apply, no confirmation modal. ON = unrestricted (removes the unit's
// active OFF/limit), OFF = zero. Future scheduled restrictions survive
// (backend keeps them). Per-unit busy lock prevents racing re-clicks.
async function toggleUnit(unit: string, state: "on" | "off"): Promise<void> {
  if (busyUnits.value.has(unit) || applyBusy.value) return;
  const generation = siteGeneration;
  const nextBusy = new Set(busyUnits.value);
  nextBusy.add(unit);
  busyUnits.value = nextBusy;
  unitErrors.value = new Map([...unitErrors.value.entries()].filter(([u]) => u !== unit));
  try {
    const assignments = buildAssignments({
      units: [unit],
      getEdit: () => ({ action: state, mode: "mw", reductionPct: null, capMw: null }),
      getCurrent: unitState,
      getRated: (u) => ratedById.value.get(u) ?? 0,
    });
    const res = await saveAssignments(assignments, [unit]);
    if (generation === siteGeneration && !res.ok && applyError.value) {
      unitErrors.value = new Map([...unitErrors.value.entries(), [unit, applyError.value]]);
    }
  } catch (e) {
    if (generation === siteGeneration) unitErrors.value = new Map([...unitErrors.value.entries(), [unit, e instanceof Error ? e.message : String(e)]]);
  } finally {
    if (generation === siteGeneration) {
      const nb = new Set(busyUnits.value);
      nb.delete(unit);
      busyUnits.value = nb;
    }
  }
}

// Correction E §2 + F P0-5/P0-6: ONE-STEP bulk — the clicked action IS
// authoritative: bulk ON/OFF ignores ALL stale per-unit editor overrides and
// sends explicit state for every selected unit. Selection clears ONLY on
// confirmed success; failures keep edits + selection for retry.
// Validate the complete payload BEFORE posting: bad Max MW/% inputs are
// displayed as actionable errors and never escape from Vue click handlers.
function safeBulkAssignments(units: string[], getEdit: (unit: string) => UnitEdit): UnitAssignment[] | null {
  applyError.value = null;
  try {
    const assignments = buildAssignments({
      units,
      getEdit,
      getCurrent: unitState,
      getRated: (u) => ratedById.value.get(u) ?? 0,
    });
    if (assignments.length === 0) {
      applyError.value = "Nema stvarne promjene stanja za odabrane jedinice.";
      return null;
    }
    return assignments;
  } catch (e) {
    applyError.value = `Nije spremljeno: ${e instanceof Error ? e.message : String(e)}`;
    return null;
  }
}

async function bulkOnOff(state: "on" | "off"): Promise<void> {
  if (applyBusy.value || wtgSelected.value.size === 0) return;
  const units = [...wtgSelected.value];
  const assignments = safeBulkAssignments(units, () => ({ action: state, mode: bulkMode.value, reductionPct: null, capMw: null }));
  if (!assignments) return;
  const res = await saveAssignments(assignments, units);
  if (res.ok) {
    wtgSelected.value = new Set();
    perUnitState.value = {};
  }
}

async function bulkSaveLimit(): Promise<void> {
  if (applyBusy.value || wtgSelected.value.size === 0) return;
  const units = [...wtgSelected.value];
  const assignments = safeBulkAssignments(
    units,
    (u) => perUnitState.value[u] ?? {
      action: bulkAction.value, mode: bulkMode.value,
      reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value,
    },
  );
  if (!assignments) return;
  const res = await saveAssignments(assignments, units);
  if (res.ok) {
    wtgSelected.value = new Set();
    perUnitState.value = {};
  }
}

// kept for power users: remove ALL active per-unit restrictions (destructive —
// keeps its explicit browser confirm, unlike the one-click actions above)
async function revertAll(): Promise<void> {
  if (applyBusy.value) return;
  applyError.value = null;
  applyMessage.value = null;
  const units = activeUnitConstraints.value.flatMap((c) => c.unit_ids ?? []);
  if (units.length === 0) {
    applyMessage.value = "Nema aktivnih ograničenja po turbinama.";
    return;
  }
  if (!window.confirm(`Ukloniti SVAKA aktivna ograničenja po turbinama (${new Set(units).size} jedinica)?`)) return;
  const targetSite = siteId.value;
  const opId = ++saveOperationId;
  applyBusy.value = true;
  try {
    const res = await windyApi.constraintsReplaceUnits(targetSite, {
      assignments: [...new Set(units)].map((u) => ({ unit_id: u, state: "on" as const })),
      start_local: freshStart(),
      end_local: null,
    });
    if (!isCurrentWrite(targetSite, opId)) return;
    wtgSelected.value = new Set();
    perUnitState.value = {};
    const refreshed = await Promise.allSettled([refreshConstraints(), refreshStatus(), fcResource.refresh()]);
    if (!isCurrentWrite(targetSite, opId)) return;
    applyMessage.value = `Sve per-turbine restrikcije uklonjene (revizija ${res.revision}).` +
      (refreshed.some((x) => x.status === "rejected") ? " Osvježavanje nije potpuno uspjelo." : "");
  } catch (e) {
    if (isCurrentWrite(targetSite, opId)) applyError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (isCurrentWrite(targetSite, opId)) applyBusy.value = false;
  }
}

// pregled payload-a PRIJE klika (tekst, ne modal) — Correction D §2 preview
const assignmentError = ref<string | null>(null);
const pendingAssignments = computed<UnitAssignment[]>(() => {
  assignmentError.value = null;
  if (wtgSelected.value.size === 0) return [];
  try {
    return buildAssignments({
      units: [...wtgSelected.value],
      getEdit: (u) =>
        perUnitState.value[u] ?? { action: bulkAction.value, mode: bulkMode.value, reductionPct: bulkReductionPct.value, capMw: bulkCapMw.value },
      getCurrent: unitState,
      getRated: (u) => ratedById.value.get(u) ?? 0,
    });
  } catch (e) {
    assignmentError.value = e instanceof Error ? e.message : String(e);
    return [];
  }
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
      if (a.state === "cap" && a.cap_mw !== null && a.cap_mw !== undefined) sum += Math.min(a.cap_mw, t.rated_mw);
      else if (a.state === "cap" && a.cap_percent !== null && a.cap_percent !== undefined) sum += (a.cap_percent / 100) * t.rated_mw;
      else sum += t.rated_mw;
      continue;
    }
    const cur = unitState(t.unit_id);
    // Correction D §2: preview honours BOTH cap kinds via the most
    // restrictive effective limit — a saved %-based cap is never treated
    // as rated
    const eff = effectiveCapMw(cur, t.rated_mw);
    sum += eff ?? t.rated_mw;
  }
  return Math.round(sum * 100) / 100;
});

// ---- SADA kartica (Addendum A): modelirani uvjeti ---------------------------
const nowResource = useAbortableResource<WindNowResponse>((signal) => windyApi.windNow(siteId.value, signal), () => [siteId.value]);
const nowRefreshing = ref(false);
async function refreshNow(): Promise<void> {
  nowRefreshing.value = true;
  try {
    await Promise.all([nowResource.refresh(), refreshWeather()]);
  } finally {
    nowRefreshing.value = false;
  }
}

// ---- Correction E Addendum C: Weather on Site (modelirano, NIJE mjerenje)
const weatherResource = useAbortableResource<WeatherNowResponse>((signal) => windyApi.weatherNow(siteId.value, signal), () => [siteId.value]);
async function refreshWeather(): Promise<void> {
  await weatherResource.refresh();
}
const WEATHER_ICONS: Record<string, string> = {
  sun: "☀",
  moon: "🌙",
  partly_day: "🌤",
  partly_night: "☁🌙",
  cloud: "☁",
  fog: "🌫",
  drizzle: "🌧",
  rain: "🌧",
  sleet: "🌧❄",
  snow: "❄",
  thunder: "⛈",
  unknown: "？",
};
function fmtTemp(t: number | null | undefined): string {
  if (t === null || t === undefined || Number.isNaN(t)) return "—";
  return `${t < 0 ? "−" : "+"}${Math.abs(t).toFixed(1)} °C`;
}

const collecting = ref(false);
const collectMessage = ref<string | null>(null);
const collectError = ref<string | null>(null);

// ---- Correction E Addendum D: Excel export (7/10/15) — read-only snapshot
const exportBusy = ref(false);
let exportOperationId = 0;
const exportError = ref<string | null>(null);
async function exportExcel(): Promise<void> {
  if (exportBusy.value) return; // no double-click duplication
  exportBusy.value = true;
  exportError.value = null;
  const opId = ++exportOperationId;
  const targetSite = siteId.value;
  const targetDays = days.value;
  const targetMode = viewMode.value;
  const targetUnit = energyUnit.value;
  try {
    const url = windyApi.forecastExportUrl(targetSite, targetDays, targetMode, targetUnit, avgModels.value, avgPref.value.enabled); // Correction F P1-10
    const res = await fetch(url);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`export ${res.status}: ${text || res.statusText}`);
    }
    // Correction F P0-1: reject a mislabeled SPA HTML page — require the real
    // XLSX MIME and the PK zip signature before treating bytes as a workbook
    const ctype = res.headers.get("content-type") ?? "";
    if (!ctype.startsWith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")) {
      throw new Error(`Export nije vratio XLSX (content-type: ${ctype || "nepoznat"}) — vjerojatno HTML stranica umjesto datoteke.`);
    }
    const blob = await res.blob();
    const head = new Uint8Array(await blob.slice(0, 2).arrayBuffer());
    if (!(head[0] === 0x50 && head[1] === 0x4b)) {
      throw new Error("Preuzete datoteke nisu valjani XLSX (nedostaje ZIP/PK potpis).");
    }
    const cd = res.headers.get("content-disposition") ?? "";
    const m = /filename="([^"]+)"/.exec(cd);
    const name = m?.[1] ?? `Windy_${targetSite}_${targetDays}d_${targetMode}_${targetUnit}.xlsx`;
    if (siteId.value !== targetSite || exportOperationId !== opId) return; // site changed while downloading
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objUrl);
  } catch (e) {
    if (siteId.value === targetSite && exportOperationId === opId) exportError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (exportOperationId === opId) exportBusy.value = false;
  }
}

async function fetchNow(): Promise<void> {
  collecting.value = true;
  collectError.value = null;
  collectMessage.value = null;
  try {
    // Display selection does not determine persistence depth: collect full provider horizons.
    const res = await windyApi.collect(16, siteId.value);
    collectMessage.value = `Ažurirano ${res.collected_at_utc}`;
    await Promise.all([fcResource.refresh(), nowResource.refresh(), refreshWeather()]);
  } catch (e) {
    collectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    collecting.value = false;
  }
}

watch(siteId, (newSite, oldSite) => {
  if (newSite === oldSite) return;
  ++siteGeneration;
  ++saveOperationId;
  ++exportOperationId;
  applyBusy.value = false;
  exportBusy.value = false;
  exportError.value = null;
  wtgSelected.value = new Set();
  perUnitState.value = {};
  busyUnits.value = new Set();
  unitErrors.value = new Map();
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

const windSource = computed<ForecastModel[]>(() => withRun.value.filter((m) => !windVis.value.hidden.includes(m.model_id)));
const windOption = computed<BaseOption>(() => ({
  ...baseOption("m/s"),
  series: windSource.value.map((m) => ({
    name: m.label,
    type: "line",
    showSymbol: false,
    connectNulls: false,
    data: seriesOf(m, "wind_speed_mps"),
    // UKMO (10 m surface comparison) renders DASHED — visually distinct
    // from hub-wind estimates
    lineStyle: { width: 1.4, color: modelColor(m.model_id), type: m.met_only ? "dashed" : "solid" },
    itemStyle: { color: modelColor(m.model_id) },
  })),
}));

// Correction E Addendum A §1 (amended by Correction F P0-3): power chart
// follows the page mode TRUTHFULLY — in Prilagođeno a model WITHOUT a valid
// adjusted block is EXCLUDED from the chart entirely (never plotted as
// baseline under the adjusted heading); Bazno plots archived baseline only.
const powerChartAdjusted = computed(() => viewMode.value === "adjusted");
const powerSource = computed<ForecastModel[]>(() =>
  withRun.value.filter((m) => {
    if (m.met_only || powerVis.value.hidden.includes(m.model_id)) return false;
    if (powerChartAdjusted.value && !m.adjusted?.available) return false; // F P0-3: no baseline fallback
    return true;
  }),
);
const powerOption = computed<BaseOption>(() => ({
  ...baseOption("MW"),
  series: powerSource.value.map((m) => {
    const adjusted = powerChartAdjusted.value && m.adjusted?.available;
    const data = adjusted
      ? (m.adjusted!.points.map((pt) => [pt.valid_at_utc, pt.adjusted_mw]) as [string, number | null][])
      : seriesOf(m, "modeled_power_mw");
    return {
      name: m.label,
      type: "line",
      showSymbol: false,
      connectNulls: false,
      data,
      lineStyle: { width: 1.4, color: modelColor(m.model_id) },
      itemStyle: { color: modelColor(m.model_id) },
    };
  }),
}));

// ---- Correction E Addendum A §2: Compare tabs (client-side, no refetch) ----
const COMPARE_TAB_OPTIONS = [
  { value: "wind", label: "Usporedba vjetra (m/s)" },
  { value: "power", label: "Usporedba proizvodnje" },
] as const;
const compareTab = ref<"wind" | "power">("wind");
const COMPARE_HOURS_OPTIONS = [
  { value: 24, label: "24 h" },
  { value: 48, label: "48 h" },
] as const;
const compareHours = ref<24 | 48>(24);
const COMPARE_UNIT_OPTIONS = computed(() => [
  { value: "mw", label: "MW" },
  { value: "mwh", label: `Energija (${energyLabel(energyUnit.value)})` },
] as const);
const comparePowerUnit = ref<"mw" | "mwh">("mw");

function comparePointsOf(m: ForecastModel): { valid_at_utc: string; value: number | null; interval_hours: number }[] {
  if (compareTab.value === "wind") return m.points.map((pt) => ({ valid_at_utc: pt.valid_at_utc, value: pt.wind_speed_mps, interval_hours: pt.interval_hours }));
  if (viewMode.value === "adjusted") {
    // adjusted mode: ONLY available adjusted points — never baseline fallback
    if (!m.adjusted?.available) return [];
    return m.adjusted.points.map((pt) => ({ valid_at_utc: pt.valid_at_utc, value: comparePowerUnit.value === "mw" ? pt.adjusted_mw : pt.adjusted_mwh, interval_hours: pt.interval_hours }));
  }
  return m.points.map((pt) => ({ valid_at_utc: pt.valid_at_utc, value: comparePowerUnit.value === "mw" ? pt.modeled_power_mw : pt.interval_energy_mwh, interval_hours: pt.interval_hours }));
}
// Addendum B §1: Compare energy display under the ACTIVE unit; MW column untouched
const compareIsEnergy = computed(() => compareTab.value === "power" && comparePowerUnit.value === "mwh");
function fmtCmp(v: number | null | undefined): string | null {
  if (v === null || v === undefined) return null;
  return compareIsEnergy.value ? fmtE(v) : String(v);
}

const compareInputs = computed<CompareInput[]>(() =>
  modelsWithRun.value
    .filter((m) => (compareTab.value === "wind" ? true : !m.met_only))
    .map((m) => ({ model_id: m.model_id, points: comparePointsOf(m) })),
);
const compareColumns = computed(() =>
  modelsWithRun.value
    .filter((m) => (compareTab.value === "wind" ? true : !m.met_only))
    .map((m) => ({ id: m.model_id, label: MODEL_LABELS_HR[m.model_id] ?? m.model_id, metOnly: m.met_only, hasRun: !!m.run })),
);
const compareRows = computed<CompareRow[]>(() => buildCompareRows(compareInputs.value, fcData.value?.generated_at_utc ?? new Date().toISOString(), compareHours.value, tz.value));
const compareAvg = computed(() => compareRows.value.map((r) => ({ row: r, avg: compareAverage(r, avgModels.value) })));
const compareAdjustedUnavailable = computed(() =>
  viewMode.value === "adjusted"
    ? modelsWithRun.value.filter((m) => !m.met_only && compareTab.value === "power" && !m.adjusted?.available).map((m) => m.model_id)
    : [],
);

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
            <SegmentedToggle v-model="energyUnit" :options="[...ENERGY_UNIT_OPTIONS]" inline divided title="prikazna jedinica energije (snaga ostaje MW)" />
            <button
              type="button"
              class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 inline-flex items-center gap-1 border px-3 py-1 font-mono text-[11px]"
              :disabled="exportBusy"
              :title="'Export Excel: trenutačni horizont, mod i jedinica'"
              @click="exportExcel"
            >{{ exportBusy ? "Izrađujem…" : "⬇ Export Excel (.xlsx)" }}</button>
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
        <StateBlock v-if="exportError" kind="error">Excel export nije uspio: {{ exportError }}</StateBlock>
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
          <StateBlock v-if="!nowResource.data.value && !nowResource.error.value" kind="loading" caption="Učitavam…" loading-py="py-2" />
          <!-- Addendum C: Weather on Site — modelirano uz WIND NOW -->
          <div class="border-ink-600 mt-2 flex flex-wrap items-stretch gap-3 border-t pt-2">
            <div v-if="weatherResource.error.value" class="text-heat-300 font-mono text-[10px]" role="status">
              Vrijeme na lokaciji trenutačno nedostupno ({{ weatherResource.error.value }}) — vjetar i proizvodnja ostaju netaknuti.
            </div>
            <template v-else-if="weatherResource.data.value">
              <div class="flex items-center gap-3">
                <span
                  class="text-2xl leading-none"
                  role="img"
                  :aria-label="`Vremenska ikona: ${weatherResource.data.value.condition}`"
                  :title="weatherResource.data.value.condition"
                >{{ WEATHER_ICONS[weatherResource.data.value.icon] ?? "？" }}</span>
                <div class="font-mono">
                  <div class="text-paper-100 text-sm">{{ weatherResource.data.value.condition }}</div>
                  <div class="text-paper-300 text-[10px]">
                    Temperatura (2 m): <span class="text-sodium-200">{{ fmtTemp(weatherResource.data.value.temperature_2m_c) }}</span>
                    <span v-if="weatherResource.data.value.stale" class="text-heat-300"> · zastarjelo</span>
                  </div>
                </div>
              </div>
              <div class="text-paper-400 font-mono text-[10px] leading-relaxed">
                <div v-if="weatherResource.data.value.cloud_cover_percent !== null && weatherResource.data.value.cloud_cover_percent !== undefined">Oblačnost: {{ weatherResource.data.value.cloud_cover_percent }}%</div>
                <div v-if="(weatherResource.data.value.precipitation_mm ?? 0) > 0">Oborina: {{ weatherResource.data.value.precipitation_mm }} mm</div>
                <div v-if="(weatherResource.data.value.snowfall_cm ?? 0) > 0">Snijeg: {{ weatherResource.data.value.snowfall_cm }} cm</div>
                <div>Modelirano • {{ weatherResource.data.value.source_provider }}/{{ weatherResource.data.value.source_model }} • {{ weatherResource.data.value.valid_at_utc ?? "—" }} UTC</div>
                <div class="text-heat-300">Nije mjerenje na elektrani</div>
              </div>
            </template>
            <StateBlock v-else kind="loading" caption="Učitavam vrijeme…" loading-py="py-1" />
          </div>
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
              <!-- Correction E §1: tile body = BULK SELECTION (checkbox-like);
                   ON/OFF switch = ONE-CLICK save (stopPropagation, aria-pressed).
                  Selekcija nikad ne mijenja stanje i obrnuto. -->
              <div class="grid max-h-56 grid-cols-4 gap-1 overflow-y-auto sm:grid-cols-6 md:grid-cols-8">
                <div
                  v-for="t in filteredTurbines"
                  :key="t.unit_id"
                  class="border px-1.5 py-1 font-mono text-[10px] cursor-pointer select-none"
                  :class="wtgSelected.has(t.unit_id) ? 'border-sodium-300 bg-sodium-300/10' : 'border-ink-600 hover:bg-ink-800'"
                  role="checkbox"
                  :aria-checked="wtgSelected.has(t.unit_id)"
                  tabindex="0"
                  :aria-label="`Odaberi ${t.unit_id} za bulk`"
                  @click="toggleWtg(t.unit_id)"
                  @keydown.enter.prevent="toggleWtg(t.unit_id)"
                  @keydown.space.prevent="toggleWtg(t.unit_id)"
                >
                  <span :class="wtgSelected.has(t.unit_id) ? 'text-sodium-100' : 'text-paper-300'" :title="`${t.unit_id} · nazivno ${t.rated_mw} MW · trenutno: ${stateLabel(t.unit_id)}`">
                    {{ t.unit_id }}
                  </span>
                  <button
                    type="button"
                    class="mt-0.5 w-full border font-mono text-[9px] leading-tight disabled:opacity-60"
                    :class="unitState(t.unit_id).off ? 'border-heat-400/60 text-heat-300 hover:bg-heat-400/10' : 'border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/10'"
                    :aria-pressed="!unitState(t.unit_id).off"
                    :aria-label="`${t.unit_id}: ${unitState(t.unit_id).off ? 'prebaci u ON (ukloni isključenje)' : 'prebaci u OFF (isključi u modelu)'}`"
                    :disabled="busyUnits.has(t.unit_id) || applyBusy"
                    @click.stop="toggleUnit(t.unit_id, unitState(t.unit_id).off ? 'on' : 'off')"
                  >
                    {{ busyUnits.has(t.unit_id) ? "Spremam…" : unitState(t.unit_id).off ? "OFF ▸ uključi" : "ON ▸ isključi" }}
                    <span class="block text-[8px] opacity-70">{{ stateLabel(t.unit_id) }}</span>
                  </button>
                  <span v-if="unitErrors.get(t.unit_id)" class="text-heat-300 block text-[8px]">{{ unitErrors.get(t.unit_id) }}</span>
                </div>
              </div>
              <p class="text-paper-500 mt-1 font-mono text-[10px]">
                ON/OFF prekidač = trenutna promjena MODELANOG operativnog stanja u Windy (NIJE fizička SCADA komanda) — sprema se odmah, jednim klikom. Klik na pločicu samo odabire jedinicu za bulk akciju.
              </p>

              <!-- Correction E §2: bulk one-step — N odabranih, JEDNA atomska akcija, bez potvrde -->
              <div class="mt-3 flex flex-wrap items-center gap-2">
                <span class="text-paper-400 font-mono text-[10px]">Bulk na {{ wtgSelected.size }} odabranih:</span>
                <button type="button" class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-2 py-0.5 font-mono text-[10px]" :disabled="applyBusy || wtgSelected.size === 0" @click="bulkOnOff('on')">Uključi odabrane</button>
                <button type="button" class="border-heat-400/60 text-heat-300 hover:bg-heat-400/10 disabled:opacity-50 border px-2 py-0.5 font-mono text-[10px]" :disabled="applyBusy || wtgSelected.size === 0" @click="bulkOnOff('off')">Isključi odabrane</button>
                <span class="text-paper-400 font-mono text-[10px]">ili limit:</span>
                <SegmentedToggle v-model="bulkMode" :options="[...CAP_MODE_OPTIONS]" inline divided />
                <template v-if="bulkMode === 'mw'">
                  <label class="text-paper-400 font-mono text-[10px]">
                    Max MW (dopušteno)
                    <input v-model.number="bulkCapMw" type="number" step="0.05" min="0" class="border-sodium-300/60 text-paper-100 bg-ink-900 w-20 mx-1 border px-2 py-0.5 font-mono text-[11px]" />
                  </label>
                  <span class="text-paper-500 font-mono text-[10px]">≈ {{ Math.round(((bulkCapMw || 0) / (siteData.site.turbine_rated_mw || 1)) * 10000) / 100 }}% nazivnog</span>
                </template>
                <template v-else>
                  <label class="text-paper-400 font-mono text-[10px]">
                    % smanjenja
                    <input v-model.number="bulkReductionPct" type="number" step="0.01" min="0" max="100" class="border-sodium-300/60 text-paper-100 bg-ink-900 w-20 mx-1 border px-2 py-0.5 font-mono text-[11px]" />
                  </label>
                  <span class="text-paper-500 font-mono text-[10px]">≈ {{ Math.round(((100 - (bulkReductionPct || 0)) / 100) * (siteData.site.turbine_rated_mw || 0) * 1000) / 1000 }} MW dopušteno</span>
                </template>
                <button type="button" class="border-sodium-300/60 text-sodium-200 hover:bg-sodium-300/15 disabled:opacity-50 border px-2 py-0.5 font-mono text-[10px]" :disabled="applyBusy || wtgSelected.size === 0" @click="bulkSaveLimit">Spremi limit na odabrane</button>
                <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-2 py-0.5 font-mono text-[10px]" :disabled="wtgSelected.size === 0" @click="applyBulkToSelection">Postavi editor na odabrane</button>
              </div>
              <p class="text-paper-500 mt-1 font-mono text-[10px]">
                % smanjenja ≠ % dopušteno: 12,28% smanjenje na 2,85 MW nazivnog = 2,50 MW dopušteno. Sprema se ISKLJUČIVO vrijednost aktivnog moda — Max MW se nikad ne zamjenjuje pohranjenim % vrijednostima ni obrnuto. „Postavi editor na odabrane" samo prefila per-unit uređivač ispod (za pojedinačne prilagode prije sprema).
              </p>

              <!-- per-jedinica uređivanje (opcionalne prilagode prije bulk sprema) -->
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
                    <label class="font-mono text-[10px]" :class="unitEdit(u).mode === 'mw' ? 'text-sodium-300' : 'text-paper-500'" title="autoritativni mod">
                      MW
                      <input :value="unitEdit(u).capMw ?? ''" type="number" step="0.05" min="0" :class="unitEdit(u).mode === 'mw' ? 'border-sodium-300/60' : 'border-ink-700'" class="bg-ink-900 text-paper-100 w-16 mx-1 border px-1 py-0.5 font-mono text-[11px]" @input="(e) => { const v = (e.target as HTMLInputElement).value; setUnitEdit(u, { ...unitEdit(u), mode: 'mw', capMw: v === '' ? null : Number(v) }) }" />
                    </label>
                    <label class="font-mono text-[10px]" :class="unitEdit(u).mode === 'pct' ? 'text-sodium-300' : 'text-paper-500'" title="autoritativni mod">
                      %smanj.
                      <input :value="unitEdit(u).reductionPct ?? 0" type="number" step="0.01" min="0" max="100" :class="unitEdit(u).mode === 'pct' ? 'border-sodium-300/60' : 'border-ink-700'" class="bg-ink-900 text-paper-100 w-16 mx-1 border px-1 py-0.5 font-mono text-[11px]" @input="(e) => { const v = (e.target as HTMLInputElement).value; setUnitEdit(u, { ...unitEdit(u), mode: 'pct', reductionPct: v === '' ? null : Number(v) }) }" />
                    </label>
                  </template>
                  <span class="text-sodium-200 font-mono text-[10px]">→ {{ allowedMw(u, unitEdit(u)) }} MW dopušteno ({{ unitEdit(u).mode === 'mw' ? 'Max MW mod' : '% smanjenja mod' }})</span>
                </div>
              </div>

              <div class="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" class="border-heat-400/60 text-heat-300 hover:bg-heat-400/10 disabled:opacity-50 border px-3 py-1 font-mono text-[11px]" :disabled="applyBusy" @click="revertAll">Poništi sve restrikcije</button>
              </div>
              <p v-if="pendingAssignments.length > 0" class="text-paper-300 mt-1 font-mono text-[10px]">
                Sljedeća bulk akcija mijenja {{ pendingAssignments.length }} jedinica:
                <template v-for="(a, i) in pendingAssignments.slice(0, 8)" :key="a.unit_id">
                  <span class="text-sodium-200">{{ a.unit_id }}</span> → {{ a.state === "on" ? "ON" : a.state === "off" ? "OFF" : a.cap_mw !== undefined ? `max ${a.cap_mw} MW dopušteno` : `max ${a.cap_percent}% dopušteno` }}{{ i < Math.min(pendingAssignments.length, 8) - 1 ? ", " : "" }}
                </template>
                <span v-if="pendingAssignments.length > 8"> … ({{ pendingAssignments.length - 8 }} više)</span>
                <span v-if="pendingPreviewMw !== null" class="text-paper-400"> · očekivani RASP. MAKS. ≈ {{ pendingPreviewMw }} MW (pregled; konačno potvrđuje server nakon sprema)</span>
              </p>
              <StateBlock v-if="assignmentError" kind="error">{{ assignmentError }}</StateBlock>
              <StateBlock v-if="applyError" kind="error">{{ applyError }}</StateBlock>
              <p v-if="applyMessage" class="text-sodium-200 mt-1 font-mono text-[10px]">{{ applyMessage }}</p>
              <p class="text-paper-400 mt-1 font-mono text-[10px]">
                Spremanje je JEDNA atomarna revizija (zaštita od istovremenih izmjena: 409 → osvježi + pokušaj ponovo). Grupisana aktivna ograničenja se sigurno razdvoju, buduća zakazana pravila ostaju, stare revizije ostaju u audit trailu.
              </p>
            </template>
          </CollapsibleSection>
        </section>

        <template v-if="fcData">
          <!-- ===== P0: dnevna tablica — TABLE FIRST ===== -->
          <section aria-label="Očekivana proizvodnja po danima">
            <h2 class="text-paper-50 mb-2 font-mono text-base tracking-wide">
              Očekivana proizvodnja po danima ({{ energyUnit.toUpperCase() }}) — {{ viewMode === "adjusted" ? (adjustedAllUnavailable ? "prilagođeni prikaz NIJE dostupan (nijedna vrijednost nije prilagođena — bazne vrijednosti NISU prikazani)" : "prilagođeno na raspoloživost") : "bazno (bez ograničenja)" }}
            </h2>
            <div v-if="adjustedAllUnavailable" class="text-heat-300 mb-2 font-mono text-[10px]">
              ⚠ Prilagođeni izračun nije moguć za nijedan model ({{ Object.values(adjustedUnavailable)[0] ?? "nema valjanih adjusted vrijednosti" }}). Tablica je PRAZNA namjerno — bazni MWh se ovdje nikad ne prikazuju kao prilagođeni. Prebacite na „Bazno" za bazne vrijednosti.
            </div>
            <div v-else-if="Object.keys(adjustedUnavailable).length" class="text-heat-300 mb-2 font-mono text-[10px]">
              ⚠ {{ Object.values(adjustedUnavailable).join(" · ") }}
            </div>
            <p class="text-paper-400 mb-2 font-mono text-[10px]">
              „Osvježi” uvijek dohvaća maksimalni horizont po modelu; 7/10/15 mijenja samo prikaz. Arhivirani runovi se ne prepisuju.
            </p>
            <div class="mb-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px]" aria-label="Stvarna pokrivenost prognoznih runova">
              <span v-for="c in modelColumns.filter((x) => !x.metOnly)" :key="c.id"
                :class="c.cycle === 'short' || c.cycle === 'incomplete-long' ? 'text-heat-300' : 'text-paper-400'"
                :title="c.newerIssuePreserved ? 'Noviji kratki run ostaje u Povijesti, ovdje se koristi zadnji puni dugi run.' : ''">
                <strong>{{ c.label }}</strong>
                <span v-if="cycleText(c.cycle)"> · {{ cycleText(c.cycle) }}</span>
                · izdano {{ c.issue ?? "nepoznato" }}
                · zadnji valjani sat {{ c.lastUsable ?? "nedostaje" }}
                · lead {{ c.leadHours === null ? "—" : c.leadHours + " h" }}
              </span>
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
                        <span v-else-if="c.cycle === 'long'" class="text-sodium-300 text-[9px]" title="Potvrđen dugi 00/12 UTC run">D</span>
                        <span v-else-if="c.cycle === 'short' || c.cycle === 'incomplete-long'" class="text-heat-300 text-[9px]" title="Kratki ili nepotpuni run, bez lažnog produljivanja">!</span>
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
                        {{ fmtE(row.values[c.id]) }}<span v-if="row.flags[c.id] || adjustedUnavailable[c.id]" class="text-paper-400" :title="adjustedUnavailable[c.id] || row.flags[c.id]"> ⚠</span>
                      </template>
                      <template v-else :title="adjustedUnavailable[c.id] || row.flags[c.id] || 'nema podataka'">—</template>
                    </td>
                    <td class="text-sodium-100 py-1.5 pr-4 text-right" :title="avgByDate.get(row.date) ? `${avgByDate.get(row.date)!.contributors_min}–${avgByDate.get(row.date)!.contributors_max} modela po satu; pokriveno ${avgByDate.get(row.date)!.hours_counted} h` : 'nema ensemble vrijednosti'">
                      <template v-if="avgPref.enabled && avgByDate.get(row.date)">
                        {{ fmtE(avgByDate.get(row.date)!.mwh) }}
                        <span class="text-paper-500 text-[9px]">N={{ avgByDate.get(row.date)!.contributors_min === avgByDate.get(row.date)!.contributors_max ? avgByDate.get(row.date)!.contributors_min : avgByDate.get(row.date)!.contributors_min + "–" + avgByDate.get(row.date)!.contributors_max }}</span>
                      </template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
                <tfoot>
                  <tr class="border-ink-600 text-paper-50 border-t-2">
                    <td class="py-2 pr-4 sticky left-0 bg-ink-950 font-bold" title="UKUPNO = zbroj PRIKAZANIH dnevnih vrijednosti po modelu (djelomični dani uključeni, označeni ⚠)">UKUPNO ({{ energyUnit.toUpperCase() }})</td>
                    <td v-for="c in modelColumns" :key="c.id" class="py-2 pr-4 text-right" :class="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined ? 'text-sodium-200' : 'text-paper-500'">
                      <template v-if="totalsByModel[c.id]?.total !== null && totalsByModel[c.id]?.total !== undefined">
                        {{ fmtE(totalsByModel[c.id]?.total) }}<span v-if="totalsByModel[c.id]?.partial" class="text-paper-400" title="uključuje djelomične dane"> ⚠</span>
                      </template>
                      <template v-else>—</template>
                    </td>
                    <td class="text-sodium-100 py-2 pr-4 text-right" :title="'UKUPNO Prosjek = zbroj PRIKAZANIH dnevnih Average vrijednosti (identitet redova, nikad prosjek period-totala)'">
                      <template v-if="totalsAverage !== null">{{ fmtE(totalsAverage) }}</template>
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
                <span class="text-paper-500 font-mono text-[10px]">{{ avgModels.length === 0 ? "Prosjek: nijedan model nije odabran — stupac prikazuje — umjesto tihog izbora svih modela." : avgCoverageNote }}</span>
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
                    <th class="py-1.5 pr-4 text-right">Energija ({{ energyUnit.toUpperCase() }})</th>
                    <th v-if="viewMode === 'adjusted'" class="text-sodium-300 py-1.5 pr-4 text-right" title="prilagođeno na raspoloživost">Prilag. MW</th>
                    <th v-if="viewMode === 'adjusted'" class="text-sodium-300 py-1.5 pr-4 text-right">Prilag. ({{ energyUnit.toUpperCase() }})</th>
                    <th class="py-1.5 pr-4 text-right">Interval</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in hourlyRows" :key="r.label + r.offset" class="border-ink-800 border-b" :class="r.missing ? 'opacity-50' : ''">
                    <td class="text-paper-100 py-1 pr-4">{{ r.label }} <span class="text-paper-400">{{ r.offset }}</span></td>
                    <td class="py-1 pr-4 text-right" :class="r.wind === null ? 'text-heat-300' : 'text-paper-100'">{{ r.wind ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mw === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mw ?? "nedostaje" }}</td>
                    <td class="py-1 pr-4 text-right" :class="r.mwh === null ? 'text-heat-300' : 'text-paper-100'">{{ r.mwh === null ? "nedostaje" : fmtE(r.mwh) }}</td>
                    <template v-if="viewMode === 'adjusted'">
                      <td class="py-1 pr-4 text-right" :class="hourlyAdjPoint(r)?.adjusted_mw === null ? 'text-heat-300' : 'text-sodium-200'">
                        {{ hourlyAdjPoint(r)?.adjusted_mw ?? (hourlyAdjusted.size ? "nedostaje" : "—") }}
                      </td>
                      <td class="py-1 pr-4 text-right" :class="hourlyAdjPoint(r)?.adjusted_mwh === null ? 'text-heat-300' : 'text-sodium-200'">
                        {{ hourlyAdjPoint(r)?.adjusted_mwh === null || hourlyAdjPoint(r) === undefined ? (hourlyAdjusted.size ? "nedostaje" : "—") : fmtE(hourlyAdjPoint(r)!.adjusted_mwh) }}
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
            <p class="text-paper-400 font-mono text-[10px]">Praznine ostaju praznine — nikad se ne popunjavaju nulama. ICON-EU završava na 120 h, ICON Global na 192 h, ARPEGE ~96 h, GEM ~240 h.</p>
            <VChart style="height: 18rem" :option="windOption" autoresize />
            <!-- Addendum A §1: legenda ISPOD vjetra — klik pali/gasi liniju (client-side) -->
            <div class="mt-1 flex flex-wrap items-center gap-1.5">
              <span class="text-paper-500 font-mono text-[9px]">Vjetar:</span>
              <button
                v-for="m in fcData.models.filter((x) => x.run !== null)"
                :key="`wl-${m.model_id}`"
                type="button"
                class="border px-1.5 py-0.5 font-mono text-[10px] inline-flex items-center gap-1"
                :class="legendState('wind', m.model_id).active ? 'border-paper-300/60 text-paper-100' : 'border-ink-600 text-paper-500 line-through'"
                :aria-pressed="legendState('wind', m.model_id).active"
                :title="m.met_only ? `${m.label}: 10 m površinski vjetar — NIJE usporedivo s vjetrom na 90 m (isključen po defaultu)` : `${m.label}: klik pali/gasi liniju`"
                @click="toggleLegend(&#39;wind&#39;, m.model_id)"
              >
                <span class="inline-block size-2" :style="{ background: modelColor(m.model_id) }" />
                {{ (MODEL_LABELS_HR[m.model_id] ?? m.model_id) + (m.met_only ? ' (10 m)' : '') }}<span v-if="!legendState('wind', m.model_id).hasData" class="text-paper-500"> · nema podataka</span>
              </button>
              <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-1.5 py-0.5 font-mono text-[9px]" @click="setLegendAll(&#39;wind&#39;, false)">Sve</button>
              <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-1.5 py-0.5 font-mono text-[9px]" @click="setLegendAll(&#39;wind&#39;, true)">Ništa</button>
            </div>
            <VChart style="height: 16rem" :option="powerOption" autoresize />
            <div class="mt-1 flex flex-wrap items-center gap-1.5">
              <span class="text-paper-500 font-mono text-[9px]">Snaga ({{ powerChartAdjusted ? 'Prilagođeno — adjusted MW' : 'Bazno — arhivirani MW' }}):</span>
              <button
                v-for="m in fcData.models.filter((x) => x.run !== null && !x.met_only)"
                :key="`pl-${m.model_id}`"
                type="button"
                class="border px-1.5 py-0.5 font-mono text-[10px] inline-flex items-center gap-1"
                :class="legendState('power', m.model_id).active ? 'border-paper-300/60 text-paper-100' : 'border-ink-600 text-paper-500 line-through'"
                :aria-pressed="legendState('power', m.model_id).active"
                :title="`${m.label}: klik pali/gasi liniju`"
                @click="toggleLegend(&#39;power&#39;, m.model_id)"
              >
                <span class="inline-block size-2" :style="{ background: modelColor(m.model_id) }" />
                {{ MODEL_LABELS_HR[m.model_id] ?? m.model_id }}<span v-if="!legendState('power', m.model_id).hasData" class="text-paper-500"> · nema podataka</span>
              </button>
              <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-1.5 py-0.5 font-mono text-[9px]" @click="setLegendAll(&#39;power&#39;, false)">Sve</button>
              <button type="button" class="border-ink-600 text-paper-300 hover:bg-ink-800 border px-1.5 py-0.5 font-mono text-[9px]" @click="setLegendAll(&#39;power&#39;, true)">Ništa</button>
            </div>
          </CollapsibleSection>

          <!-- ===== Addendum A §2: Compare — sve providere na satu ===== -->
          <section aria-label="Compare — usporedba modela" class="mt-4">
            <div class="mb-2 flex flex-wrap items-center gap-3">
              <h2 class="text-paper-50 font-mono text-base tracking-wide">Compare — usporedba modela</h2>
              <SegmentedToggle v-model="compareTab" :options="[...COMPARE_TAB_OPTIONS]" inline divided />
              <SegmentedToggle v-model="compareHours" :options="[...COMPARE_HOURS_OPTIONS]" inline divided />
              <SegmentedToggle v-if="compareTab === 'power'" v-model="comparePowerUnit" :options="[...COMPARE_UNIT_OPTIONS]" inline divided />
            </div>
            <p v-if="compareTab === 'wind'" class="text-paper-400 mb-1 font-mono text-[10px]">
              Ista apsolutna UTC sata kroz sve modele (prikaz u {{ tz }}). Ostali modeli = dokumentirana procjena/proxy za 90 m; <span class="text-sodium-200">UKMO (10 m)</span> = površinski vjetar, NIJE usporedivo s vjetrom na 90 m.
            </p>
            <p v-else class="text-paper-400 mb-1 font-mono text-[10px]">
              Proizvodnja po satu — {{ viewMode === 'adjusted' ? 'Prilagođeno (adjusted MW/MWh; model bez adjusted-a = —, nikad baza)' : 'Bazno (arhivirani MW/MWh)' }}. Met-only modeli (UKMO) su isključeni.
              <span v-if="compareAdjustedUnavailable.length" class="text-heat-300"> Prilagođeno nedostupno: {{ compareAdjustedUnavailable.join(', ') }} (prikazano —).</span>
            </p>
            <div class="overflow-x-auto">
              <table class="w-full font-mono text-[11px]">
                <thead>
                  <tr class="text-paper-400 border-ink-700 border-b text-left">
                    <th class="py-1.5 pr-3 sticky left-0 bg-ink-950">Sat ({{ tz }})</th>
                    <th v-for="c in compareColumns" :key="c.id" class="py-1.5 pr-3 text-right">
                      <span class="inline-flex items-center gap-1">
                        <span class="inline-block size-2" :style="{ background: modelColor(c.id) }" />
                        {{ c.label }}{{ c.metOnly ? ' (10 m)' : '' }}
                      </span>
                    </th>
                    <th v-if="compareTab === 'power'" class="text-sodium-300 py-1.5 pr-3 text-right" title="prosjek odabranih produkcijskih modela (isti UTC sat); poštuje Prosjek toggle iz dnevne tablice">Prosjek</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in compareRows" :key="r.key" class="border-ink-800 border-b">
                    <td class="text-paper-100 py-1 pr-3 sticky left-0 bg-ink-950">{{ r.label }} <span class="text-paper-400">{{ r.offset }}</span></td>
                    <td v-for="c in compareColumns" :key="c.id" class="py-1 pr-3 text-right" :class="r.values[c.id] !== null && r.values[c.id] !== undefined ? 'text-paper-100' : 'text-paper-500'" :title="r.values[c.id] === null || r.values[c.id] === undefined ? `${c.label}: nema podatka za ovaj sat` : `${c.label} @ ${r.key}:00 UTC`">
                      {{ (r.values[c.id] ?? null) === null ? '—' : fmtCmp(r.values[c.id]) }}
                    </td>
                    <td v-if="compareTab === 'power'" class="py-1 pr-3 text-right" :class="avgPref.enabled ? 'text-sodium-100' : 'text-paper-500'" :title="!avgPref.enabled ? 'Prosjek je isključen (Prosjek toggle u dnevnoj tablici)' : `${compareAvg.find((x) => x.row.key === r.key)?.avg.contributors ?? 0} modela pokriva ovaj sat`">
                      {{ avgPref.enabled ? ((compareAvg.find((x) => x.row.key === r.key)?.avg.value ?? null) === null ? '—' : fmtCmp(compareAvg.find((x) => x.row.key === r.key)!.avg.value)) : '—' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="text-paper-400 mt-1 font-mono text-[10px]">
              Podaci dolaze isključivo iz već dohvaćenog /api/forecast odgovora — prebacivanje tabova i legende NE pokreću nove HTTP pozive. Nedostaje = — (nikad 0).
            </p>
          </section>

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
