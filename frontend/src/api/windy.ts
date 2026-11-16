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
  daily_local: { date: string; mwh: number; hours_counted: number; hours_missing: number }[];
  total_mwh_own_window: number | null;
  coverage: { n_hours: number; first_valid_utc: string | null; last_valid_utc: string | null; max_available_hours: number };
  warnings: string[];
}

export interface ForecastResponse {
  site: WindySite;
  power_curve: CurveInfo;
  generated_at_utc: string;
  days: number;
  models: ForecastModel[];
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
  lead_bins: string[];
  models: Record<string, { bins: Record<string, AccuracyBin>; total: AccuracyBin | null }>;
  calibration_applied: Record<string, number>;
  has_measured_actuals: boolean;
  note: string;
}

export interface OverlayHour {
  valid_at_utc: string;
  actual_mwh?: number;
  actual_mw?: number;
  actual_partial?: boolean;
  actual_covered_hours?: number;
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
  total_points: number;
  missing_cycles: string[];
  n_missing_cycles: number | null;
  note: string;
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

export interface ActualsMapping {
  timestamp_col: string;
  value_col: string;
  value_kind: "energy_mwh" | "power_mw";
  timestamp_kind?: "auto" | "naive-local" | "iso-offset";
  tz?: string;
  interval_hours?: number | null;
}

export const windyApi = {
  site: (signal?: AbortSignal) => getJson<SiteResponse>("/site", signal),
  forecast: (days: number, signal?: AbortSignal) => getJson<ForecastResponse>(`/forecast?days=${days}`, signal),
  collect: (days = 10) => postJson<CollectResult>("/collect", { days }),
  runs: (modelId: string | null, signal?: AbortSignal) =>
    getJson<{ runs: RunSummary[] }>(`/runs?limit=300${modelId ? `&model_id=${encodeURIComponent(modelId)}` : ""}`, signal),
  runDetail: (id: number, signal?: AbortSignal) => getJson<RunDetail>(`/runs/${id}`, signal),
  compare: (runId: number, targetDate: string, signal?: AbortSignal) =>
    getJson<CompareResponse>(`/compare?run_id=${runId}&target_date=${encodeURIComponent(targetDate)}`, signal),
  backfill: (modelId: string, runDate: string, runHour: number, days = 10) =>
    postJson<BackfillResult>("/backfill", { model_id: modelId, run_date: runDate, run_hour: runHour, days }),
  accuracy: (fromUtc: string, toUtc: string, useCalibration: boolean, signal?: AbortSignal) =>
    getJson<AccuracyResponse>(
      `/accuracy?from_utc=${encodeURIComponent(fromUtc)}&to_utc=${encodeURIComponent(toUtc)}${useCalibration ? "&use_calibration=true" : ""}`,
      signal,
    ),
  overlay: (fromUtc: string, toUtc: string, leadBin: string | null, useCalibration: boolean, signal?: AbortSignal) =>
    getJson<OverlayResponse>(
      `/accuracy/overlay?from_utc=${encodeURIComponent(fromUtc)}&to_utc=${encodeURIComponent(toUtc)}${leadBin ? `&lead_bin=${encodeURIComponent(leadBin)}` : ""}${useCalibration ? "&use_calibration=true" : ""}`,
      signal,
    ),
  batchPreview: (body: Record<string, unknown>) => postJson<BatchPreview>("/backfill/batch", { ...body, dry_run: true }),
  batchStart: (body: Record<string, unknown>) => postJson<{ job_id: number; status: string }>("/backfill/batch", body),
  batchList: (signal?: AbortSignal) => getJson<{ jobs: Omit<BatchJob, "results" | "planned_total">[] }>("/backfill/batch", signal),
  batchGet: (id: number, signal?: AbortSignal) => getJson<BatchJob>(`/backfill/batch/${id}`, signal),
  batchCancel: (id: number) => postJson<{ job_id: number; status: string }>(`/backfill/batch/${id}/cancel`, {}),
  batchResume: (id: number) => postJson<{ job_id: number; status: string }>(`/backfill/batch/${id}/resume`, {}),
  archiveHealth: (signal?: AbortSignal) => getJson<{ models: ArchiveHealthModel[] }>("/archive/health", signal),
  calibration: (signal?: AbortSignal) =>
    getJson<{ site_id: string; models: CalibrationEntry[]; note: string }>("/calibration", signal),
  calibrationTrain: (fromUtc: string, toUtc: string) => postJson<Record<string, unknown>>("/calibration/train", { from_utc: fromUtc, to_utc: toUtc }),
  actualsPreview: (file: File, mapping: ActualsMapping) =>
    postMultipart<ActualsPreview>("/actuals/preview", file, { mapping: JSON.stringify(mapping) }),
  actualsImport: (file: File, mapping: ActualsMapping, onConflict: "reject" | "replace") =>
    postMultipart<ActualsImportResult>("/actuals/import", file, {
      mapping: JSON.stringify(mapping),
      on_conflict: onConflict,
    }),
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
