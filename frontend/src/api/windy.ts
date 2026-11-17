// Typed client for the Windy FastAPI backend (single-user, local service).
//
// The backend is the single source of truth: every forecast shown in the UI
// was collected and archived server-side first (Screen A reads the archive,
// never an ad-hoc re-fetch), and every accuracy number comes from archived
// runs joined to imported actuals — never from current forecasts.

export interface WindySite {
  id: string;
  name: string;
  country: string;
  latitude: number;
  longitude: number;
  capacity_mw: number;
  turbine_count: number;
  turbine_model: string;
  turbine_rated_mw: number;
  hub_height_m: number;
  loss_factor: number;
  timezone: string;
  assumptions: Record<string, string>;
  notes: string;
  power_curve_id?: string;
}

export interface CurveInfo {
  id: string;
  label: string;
  status: string;
  provenance: string;
  rated_power_mw: number;
  cut_in_mps: number | null;
  rated_mps: number | null;
  cut_out_mps: number | null;
}

export interface ModelInfo {
  id: string;
  provider: string;
  label: string;
  max_lead_hours: number;
  request_days: number;
  height_method: string;
  speed_heights_m: number[];
  note: string;
}

export interface SiteResponse {
  site: WindySite;
  power_curve: CurveInfo;
  models: ModelInfo[];
  source_provider: string;
  source_note: string;
}

export interface ForecastPoint {
  valid_at_utc: string;
  wind_speed_mps: number | null;
  wind_direction_deg: number | null;
  height_method: string;
  modeled_power_mw: number | null;
  interval_energy_mwh: number | null;
  interval_hours: number;
  status: string;
}

export interface ForecastRunInfo {
  id: number;
  run_issued_utc: string | null;
  issue_source: string;
  fetched_at_utc: string;
  forecast_days: number;
  source_url: string;
}

export interface ForecastModel {
  model_id: string;
  provider: string;
  label: string;
  height_method: string;
  height_sources_m: number[];
  requested_height_m: number;
  height_note: string;
  run: ForecastRunInfo | null;
  points: ForecastPoint[];
  met_only: boolean;
  daily_local: { date: string; mwh: number; hours_counted: number; hours_missing: number }[];
  total_mwh_own_window: number | null;
  coverage: { n_hours: number; first_valid_utc: string | null; last_valid_utc: string | null; max_available_hours: number };
  warnings: string[];
  /** Present only when the forecast was fetched with `with_availability=1`. */
  adjusted?: AdjustedBlock;
}

export interface AdjustedPoint {
  valid_at_utc: string;
  adjusted_mw: number | null;
  adjusted_mwh: number | null;
  interval_hours: number;
  status: string;
}

export interface AdjustedDailySlot {
  date: string;
  mwh: number;
  hours_counted: number;
  hours_missing: number;
  hours_expected?: number;
  partial?: boolean;
}

export interface AdjustedBlock {
  available: boolean;
  reason?: string;
  inventory_revision?: number;
  constraints_revision?: number;
  baseline_comparable?: boolean;
  baseline_comparable_reason?: string | null;
  daily: AdjustedDailySlot[];
  points: AdjustedPoint[];
}

export interface OperationalStatus {
  site_id: string;
  installed_mw: number;
  available_max_mw: number;
  offline_count: number;
  curtailed_count: number;
  farm_export_cap_mw: number | null;
  n_turbines: number;
  inventory_revision: number;
  constraints_revision: number;
  inventory_present?: boolean;
  n_turbines_inventory?: number;
  error?: string;
}

export interface WindNowModel {
  model_id: string;
  label: string;
  met_only: boolean;
  height_method: string;
  valid_at_utc: string | null;
  wind_speed_mps: number | null;
  age_minutes: number | null;
  stale: boolean;
  note: string;
  run_issued_utc?: string | null;
}

export interface WindNowResponse {
  site_id: string;
  modeled: true;
  disclaimer: string;
  generated_at_utc: string;
  models: WindNowModel[];
  cache?: { cached: boolean; age_s: number };
}

export interface ForecastResponse {
  site: WindySite;
  power_curve: CurveInfo;
  generated_at_utc: string;
  days: number;
  models: ForecastModel[];
  operational?: OperationalStatus | null;
  intersection: {
    n_common_hours: number;
    first_common_utc: string | null;
    last_common_utc: string | null;
    total_mwh: Record<string, number>;
    note: string;
  };
}

export interface CollectResult {
  site_id: string;
  collected_at_utc: string;
  models: {
    model_id: string;
    status: string;
    run_id: number | null;
    run_issued_utc: string | null;
    points: number;
    error: string | null;
    height_method: string;
    note: string;
    height_sources: number[];
  }[];
}

export interface RunSummary {
  id: number;
  model_id: string;
  run_issued_utc: string | null;
  dedupe_key: string;
  issue_source: string;
  fetched_at_utc: string;
  forecast_days: number;
  status: string;
  requested_height_m: number;
  n_points: number;
  first_valid_utc: string | null;
  last_valid_utc: string | null;
}

export interface RunDetail extends Omit<RunSummary, "n_points"> {
  source_url: string;
  height_sources_json: string;
  points: ForecastPoint[];
}

export interface CompareResponse {
  run_id: number;
  model_id: string;
  run_issued_utc: string | null;
  target_date: string;
  points: ForecastPoint[];
  total_mwh: number;
}

export interface BackfillResult {
  status: string;
  run_id: number;
  created: boolean;
  model_id: string;
  run_issued_utc: string;
  points: number;
  note: string;
}

export interface AccuracyBin {
  n_pairs: number;
  mae_mw: number | null;
  bias_mw: number | null;
  wape_energy: number | null;
  actual_energy_mwh: number | null;
  forecast_energy_mwh: number | null;
  distinct_hours: number;
  coverage: number | null;
}

export interface AccuracyResponse {
  site_id: string;
  from_utc: string;
  to_utc: string;
  actual_hours_in_range: number;
  actual_hours_partial: number;
  actual_intervals_unaligned: number;
  actual_intervals_invalid: number;
  actual_intervals_over_limit: number;
  lead_bins: string[];
  models: Record<string, { bins: Record<string, AccuracyBin>; total: AccuracyBin | null }>;
  calibration_applied: Record<string, number>;
  has_measured_actuals: boolean;
  selection_policy: string;
  note: string;
}

export interface OverlayHour {
  valid_at_utc: string;
  actual_mwh?: number;
  actual_mw?: number;
  actual_partial?: boolean;
  actual_covered_hours?: number;
  actual_reason?: string;
  models?: Record<string, number>;
}

export interface OverlayResponse {
  site_id: string;
  from_utc: string;
  to_utc: string;
  lead_bin: string | null;
  hours: OverlayHour[];
  calibration_applied: Record<string, number>;
  note: string;
}

export interface BatchPreview {
  dry_run: true;
  total_planned: number;
  total_skipped: number;
  est_points: number;
  per_model: { model_id: string; planned: number; skipped: number; est_points: number }[];
  note: string;
}

export interface BatchJobResult {
  seq: number;
  model_id: string;
  run_issued_utc: string;
  status: string;
  detail: string | null;
}

export interface BatchJob {
  id: number;
  created_at_utc: string;
  status: string;
  cursor_index: number;
  n_stored: number;
  n_already: number;
  n_unavailable: number;
  n_failed: number;
  n_skipped: number;
  last_error: string | null;
  site_id: string;
  params: { models: string[]; date_from: string; date_to: string; cycles: number[]; days: number; max_requests: number };
  planned_total: number;
  results: BatchJobResult[];
}

export interface ArchiveHealthModel {
  model_id: string;
  label: string;
  known_issue_cycles: number;
  issue_unknown_runs: number;
  first_issue_utc: string | null;
  last_issue_utc: string | null;
  last_fetched_utc: string | null;
  last_error: string | null;
  total_points: number;
  missing_cycles: string[];
  n_missing_cycles: number | null;
  note: string;
}

export interface PowerCurveSummary {
  id: string;
  label: string;
  status: string;
  source: string;
  provenance: string;
  rated_power_mw: number;
  cut_in_mps: number | null;
  rated_mps: number | null;
  cut_out_mps: number | null;
  site_id: string | null;
  site_specific: boolean;
}

export interface AssetSummary extends WindySite {
  curve_site_id?: string | null;
  curve_label?: string | null;
  curve_status?: string | null;
  curve_site_specific?: boolean;
  curve_reference_only?: boolean;
}

export interface AssetCreate {
  id?: string;
  label: string;
  country?: string;
  latitude: number;
  longitude: number;
  capacity_mw: number;
  turbine_count: number;
  turbine_rated_mw: number;
  hub_height_m: number;
  loss_factor?: number;
  timezone: string;
  power_curve_id: string;
  notes?: string;
  reconcile_nameplate?: boolean;
  allow_scaled_reference?: boolean;
}

export interface Turbine {
  unit_id: string;
  name: string | null;
  rated_mw: number;
  curve_id: string | null;
  created_at_utc: string;
}

export interface Inventory {
  site_id: string;
  revision: number;
  n_turbines: number;
  installed_mw: number;
  turbines: Turbine[];
}

export interface OperationalConstraint {
  id: number;
  kind: "offline" | "curtailment" | "export_cap";
  unit_ids: string[] | null;
  cap_mw: number | null;
  cap_percent: number | null;
  start_utc: string;
  end_utc: string | null;
  revision: number;
  created_at_utc: string;
  created_by: string;
}

export interface SiteOperatingStatus {
  site_id: string;
  installed_mw: number;
  available_max_mw: number;
  offline_count: number;
  curtailed_count: number;
  farm_export_cap_mw: number | null;
  n_turbines: number;
  inventory_revision: number;
  constraints_revision: number;
}

export interface ScenarioPoint {
  valid_at_utc: string;
  base_mw: number | null;
  adjusted_mw: number | null;
  pre_export_mw: number | null;
  lost_to_unit_mwh: number | null;
  lost_to_export_mwh: number | null;
  interval_hours: number;
  status: string;
}

export interface ScenarioWindow {
  window_hours: number;
  start_valid_utc: string | null;
  end_valid_utc: string | null;
  expected_hours: number;
  valid_hours: number;
  missing_hours: number;
  coverage: number | null;
  complete: boolean;
  short_horizon?: boolean;
  base_mwh: number | null;
  adjusted_mwh: number | null;
  lost_unit_mwh: number | null;
  lost_export_mwh: number | null;
  available_hours_subtotal: {
    hours: number;
    base_mwh: number;
    adjusted_mwh: number;
    lost_unit_mwh: number;
    lost_export_mwh: number;
    note: string;
  };
}

export interface ScenarioResponse {
  site_id: string;
  run_id: number;
  model_id: string;
  run_issued_utc: string | null;
  inventory_revision: number;
  constraints_revision: number;
  algorithm_version: string;
  installed_mw: number;
  n_turbines: number;
  baseline_comparable: boolean;
  baseline_comparable_reason: string | null;
  points: ScenarioPoint[];
  summary: {
    installed_mw: number;
    curve_mode: string;
    windows: { "24h": ScenarioWindow; "7d": ScenarioWindow; "10d": ScenarioWindow };
  };
  note: string;
}

export interface ScenarioRevision {
  id: number;
  model_id: string;
  run_id: number;
  run_issued_utc: string | null;
  inventory_revision: number;
  constraints_revision: number;
  algorithm_version: string;
  created_at_utc: string;
  retrieve_at: string;
  summary: { installed_mw?: number; curve_mode?: string; windows?: Record<string, ScenarioWindow> };
}

export interface ConfigRevision {
  id: number;
  revision: number;
  changes: Record<string, { from: unknown; to: unknown }>;
  effective_from_utc: string;
  note: string | null;
  created_at_utc: string;
  created_by: string;
}

export interface ConfigRevisionsResponse {
  site_id: string;
  current_revision: number;
  revisions: ConfigRevision[];
}

export interface ConfigRevisionApplyResponse {
  site_id: string;
  revision: number;
  changes: Record<string, { from: unknown; to: unknown }>;
  effective_from_utc: string;
  note: string | null;
  inventory_revision: number | null;
  site: WindySite;
}

export interface TurbineLocation {
  unit_id: string;
  latitude: number;
  longitude: number;
  elevation_m: number | null;
  source: string;
  created_at_utc: string;
}

export interface LocationsResponse {
  site_id: string;
  revision: number;
  n_locations: number;
  locations: TurbineLocation[];
  revision_history: { revision: number; n_records: number; created_at_utc: string | null }[];
  note: string | null;
}

export interface UnitAssignment {
  unit_id: string;
  state: "on" | "off" | "cap";
  cap_mw?: number;
  cap_percent?: number; // percent OF RATED ALLOWED (backend semantics)
}

export interface CalibrationEntry {
  model_id: string;
  factor: number;
  n_train_hours: number;
  n_val_hours: number;
  val_mae_before: number | null;
  val_mae_after: number | null;
  val_wape_before: number | null;
  val_wape_after: number | null;
  trained_at_utc: string;
  status: string;
  note: string;
}

export interface ActualsPreview {
  fieldnames: string[];
  rows_total: number;
  rows_parsed: number;
  interval_hours: number | null;
  errors: { row: number; field: string; message: string; value: string }[];
  warnings: string[];
  rows: { interval_start_utc: string; interval_end_utc: string; energy_mwh: number | null; power_mw: number | null }[];
  truncated: boolean;
  n_conflicts_with_db?: number;
  conflicts?: string[];
}

export interface ActualsImportResult {
  imported: boolean;
  reason?: string;
  import_id?: number;
  rows_total?: number;
  rows_imported?: number;
  n_conflicts?: number;
  conflicts?: string[];
  errors?: { row: number; field: string; message: string; value: string }[];
  warnings?: string[];
  interval_hours?: number | null;
}

const BASE = "/api";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(BASE + path, { signal });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`windy api ${res.status}: ${text.slice(0, 300) || res.statusText}`);
  }
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`windy api ${res.status}: ${text.slice(0, 300) || res.statusText}`);
  }
  return (await res.json()) as T;
}

async function postMultipart<T>(path: string, file: File, fields: Record<string, string>): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  const res = await fetch(BASE + path, { method: "POST", body: form });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`windy api ${res.status}: ${text.slice(0, 300) || res.statusText}`);
  }
  return (await res.json()) as T;
}

async function delJson<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { method: "DELETE" });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`windy api ${res.status}: ${text.slice(0, 300) || res.statusText}`);
  }
  return (await res.json()) as T;
}

export interface ActualsMapping {
  timestamp_col: string;
  value_col: string;
  value_kind: "energy_mwh" | "power_mw";
  timestamp_kind?: "auto" | "naive-local" | "iso-offset";
  tz?: string;
  interval_hours?: number | null;
}

export const windyApi = {
  site: (siteId: string, signal?: AbortSignal) => getJson<SiteResponse>(`/site?site_id=${encodeURIComponent(siteId)}`, signal),
  assets: (signal?: AbortSignal) => getJson<{ assets: AssetSummary[] }>("/assets", signal),
  assetCreate: (body: AssetCreate) => postJson<WindySite>("/assets", body),
  powerCurves: (signal?: AbortSignal) => getJson<{ curves: PowerCurveSummary[] }>("/power-curves", signal),
  forecast: (days: number, siteId: string, signal?: AbortSignal, withAvailability = false) =>
    getJson<ForecastResponse>(
      `/forecast?days=${days}&site_id=${encodeURIComponent(siteId)}${withAvailability ? "&with_availability=true" : ""}`,
      signal,
    ),
  windNow: (siteId: string, signal?: AbortSignal) =>
    getJson<WindNowResponse>(`/assets/${encodeURIComponent(siteId)}/wind-now`, signal),
  collect: (days = 10, siteId = "krnovo") => postJson<CollectResult>("/collect", { days, site_id: siteId }),
  runs: (modelId: string | null, siteId: string, signal?: AbortSignal) =>
    getJson<{ runs: RunSummary[] }>(
      `/runs?limit=300&site_id=${encodeURIComponent(siteId)}${modelId ? `&model_id=${encodeURIComponent(modelId)}` : ""}`,
      signal,
    ),
  runDetail: (id: number, siteId: string, signal?: AbortSignal) => getJson<RunDetail>(`/runs/${id}?site_id=${encodeURIComponent(siteId)}`, signal),
  compare: (runId: number, targetDate: string, siteId: string, signal?: AbortSignal) =>
    getJson<CompareResponse>(
      `/compare?run_id=${runId}&target_date=${encodeURIComponent(targetDate)}&site_id=${encodeURIComponent(siteId)}`,
      signal,
    ),
  backfill: (modelId: string, runDate: string, runHour: number, days = 10, siteId = "krnovo") =>
    postJson<BackfillResult>("/backfill", { model_id: modelId, run_date: runDate, run_hour: runHour, days, site_id: siteId }),
  accuracy: (fromUtc: string, toUtc: string, useCalibration: boolean, siteId: string, signal?: AbortSignal) =>
    getJson<AccuracyResponse>(
      `/accuracy?from_utc=${encodeURIComponent(fromUtc)}&to_utc=${encodeURIComponent(toUtc)}&site_id=${encodeURIComponent(siteId)}${useCalibration ? "&use_calibration=true" : ""}`,
      signal,
    ),
  overlay: (fromUtc: string, toUtc: string, leadBin: string | null, useCalibration: boolean, siteId: string, signal?: AbortSignal) =>
    getJson<OverlayResponse>(
      `/accuracy/overlay?from_utc=${encodeURIComponent(fromUtc)}&to_utc=${encodeURIComponent(toUtc)}&site_id=${encodeURIComponent(siteId)}${leadBin ? `&lead_bin=${encodeURIComponent(leadBin)}` : ""}${useCalibration ? "&use_calibration=true" : ""}`,
      signal,
    ),
  batchPreview: (body: Record<string, unknown>) => postJson<BatchPreview>("/backfill/batch", { ...body, dry_run: true }),
  batchStart: (body: Record<string, unknown>) => postJson<{ job_id: number; status: string }>("/backfill/batch", body),
  batchList: (siteId: string, signal?: AbortSignal) =>
    getJson<{ jobs: Omit<BatchJob, "results" | "planned_total">[] }>(`/backfill/batch?site_id=${encodeURIComponent(siteId)}`, signal),
  batchGet: (id: number, signal?: AbortSignal) => getJson<BatchJob>(`/backfill/batch/${id}`, signal),
  batchCancel: (id: number) => postJson<{ job_id: number; status: string }>(`/backfill/batch/${id}/cancel`, {}),
  batchResume: (id: number) => postJson<{ job_id: number; status: string }>(`/backfill/batch/${id}/resume`, {}),
  archiveHealth: (siteId: string, signal?: AbortSignal) =>
    getJson<{ models: ArchiveHealthModel[] }>(`/archive/health?site_id=${encodeURIComponent(siteId)}`, signal),
  calibration: (siteId: string, signal?: AbortSignal) =>
    getJson<{ site_id: string; models: CalibrationEntry[]; note: string }>(`/calibration?site_id=${encodeURIComponent(siteId)}`, signal),
  calibrationTrain: (fromUtc: string, toUtc: string, siteId: string) =>
    postJson<Record<string, unknown>>("/calibration/train", { from_utc: fromUtc, to_utc: toUtc, site_id: siteId }),
  actualsPreview: (file: File, mapping: ActualsMapping, siteId: string) =>
    postMultipart<ActualsPreview>("/actuals/preview", file, { mapping: JSON.stringify(mapping), site_id: siteId }),
  actualsImport: (file: File, mapping: ActualsMapping, onConflict: "reject" | "replace", siteId: string) =>
    postMultipart<ActualsImportResult>("/actuals/import", file, {
      mapping: JSON.stringify(mapping),
      on_conflict: onConflict,
      site_id: siteId,
    }),
  actualsList: (siteId: string, signal?: AbortSignal) =>
    getJson<{ rows: { interval_start_utc: string; interval_end_utc: string; energy_mwh: number | null; power_mw: number | null }[] }>(
      `/actuals?site_id=${encodeURIComponent(siteId)}`,
      signal,
    ),
  inventory: (siteId: string, signal?: AbortSignal) => getJson<Inventory>(`/assets/${encodeURIComponent(siteId)}/turbines`, signal),
  inventoryGenerate: (siteId: string, count: number, ratingMw: number, unitPrefix = "WTG") =>
    postJson<Inventory>(`/assets/${encodeURIComponent(siteId)}/turbines/generate`, { count, rating_mw: ratingMw, unit_prefix: unitPrefix }),
  inventoryBulkRatings: (siteId: string, edits: { unit_id: string; rated_mw: number }[]) =>
    postJson<Inventory>(`/assets/${encodeURIComponent(siteId)}/turbines/bulk-ratings`, { edits }),
  inventoryDelete: (siteId: string) => delJson<Inventory>(`/assets/${encodeURIComponent(siteId)}/turbines`),
  constraints: (siteId: string, signal?: AbortSignal) => getJson<{ site_id: string; revision: number; constraints: OperationalConstraint[] }>(`/assets/${encodeURIComponent(siteId)}/constraints`, signal),
  constraintsAdd: (siteId: string, rows: Record<string, unknown>[]) =>
    postJson<{ site_id: string; revision: number; constraints: OperationalConstraint[]; warnings: string[] }>(
      `/assets/${encodeURIComponent(siteId)}/constraints`,
      { rows },
    ),
  constraintsDelete: (siteId: string, ids: number[]) =>
    postJson<{ site_id: string; revision: number; constraints: OperationalConstraint[] }>(`/assets/${encodeURIComponent(siteId)}/constraints/delete`, { ids }),
  operatingStatus: (siteId: string, signal?: AbortSignal) =>
    getJson<SiteOperatingStatus>(`/assets/${encodeURIComponent(siteId)}/status`, signal),
  constraintsReplaceUnits: (siteId: string, body: { assignments: UnitAssignment[]; start_local: string; end_local?: string | null }) =>
    postJson<{ site_id: string; revision: number; constraints: OperationalConstraint[]; warnings: string[] }>(
      `/assets/${encodeURIComponent(siteId)}/constraints/replace-units`,
      body,
    ),
  configRevisionApply: (siteId: string, body: { changes: Record<string, unknown>; note?: string }) =>
    postJson<ConfigRevisionApplyResponse>(`/assets/${encodeURIComponent(siteId)}/config-revision`, body),
  configRevisions: (siteId: string, signal?: AbortSignal) =>
    getJson<ConfigRevisionsResponse>(`/assets/${encodeURIComponent(siteId)}/config-revisions`, signal),
  locationsSet: (siteId: string, body: { records: { unit_id: string; latitude: number; longitude: number; elevation_m?: number | null; source?: string }[]; note?: string }) =>
    postJson<LocationsResponse>(`/assets/${encodeURIComponent(siteId)}/locations`, body),
  locations: (siteId: string, signal?: AbortSignal) =>
    getJson<LocationsResponse>(`/assets/${encodeURIComponent(siteId)}/locations`, signal),
  scenario: (siteId: string, runId: number, signal?: AbortSignal) =>
    getJson<ScenarioResponse>(`/assets/${encodeURIComponent(siteId)}/scenario?run_id=${runId}`, signal),
  scenarioPinned: (siteId: string, runId: number, revisions: { inventory_revision: number; constraints_revision: number } | null, signal?: AbortSignal) => {
    const q = new URLSearchParams({ run_id: String(runId) });
    if (revisions) {
      q.set("inventory_revision", String(revisions.inventory_revision));
      q.set("constraints_revision", String(revisions.constraints_revision));
    }
    return getJson<ScenarioResponse>(`/assets/${encodeURIComponent(siteId)}/scenario?${q.toString()}`, signal);
  },
  scenarioPublish: (siteId: string, runId: number) =>
    postJson<{
      scenario_revision_id: number;
      created_at_utc: string;
      summary: Record<string, unknown>;
      points_available_at: string;
      retrieval: { run_id: number; site_id: string; inventory_revision: number; constraints_revision: number; note: string };
    }>(`/assets/${encodeURIComponent(siteId)}/scenario/publish`, { run_id: runId }),
  scenarioRevisions: (siteId: string, signal?: AbortSignal) =>
    getJson<{ revisions: ScenarioRevision[] }>(`/assets/${encodeURIComponent(siteId)}/scenario/revisions`, signal),
};

/** Model display colors, stable across all three screens. */
export const MODEL_COLORS: Record<string, string> = {
  ecmwf_ifs: "#e8b649",
  gfs_seamless: "#5aa9e6",
  icon_global: "#7bd88f",
  icon_eu: "#c792ea",
};

export function modelColor(id: string): string {
  return MODEL_COLORS[id] ?? "#93896f";
}

/** `2026-11-15T18:00:00Z` → `18:00` (UTC labels; the archive is UTC end-to-end). */
export function hhmm(iso: string): string {
  return iso.slice(11, 16);
}
