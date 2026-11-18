/** Forecast-first helpers — pure, tested builders for the Croatian daily/hourly
 *  tables. Daily grouping and hourly labels use the SELECTED SITE's IANA
 *  timezone (real 23/24/25 h DST days); repeated local hours are
 *  distinguished by their UTC offset. */

export interface DailySlot {
  date: string;
  mwh: number;
  hours_counted: number;
  hours_missing: number;
  hours_expected?: number;
  partial?: boolean;
}

export interface DailyRow {
  date: string;
  values: Record<string, number | null>; // model_id -> MWh (null = iza horizonta / nedostaje)
  flags: Record<string, string>; // model_id -> "djelomičan" / "nepotpun" note
}

export const MODEL_LABELS_HR: Record<string, string> = {
  ecmwf_ifs: "ECMWF",
  gfs_seamless: "GFS",
  icon_global: "ICON Global",
  icon_eu: "ICON EU",
  meteofrance_arpege_world: "ARPEGE World",
  meteofrance_arpege_europe: "ARPEGE Europe",
  gem_seamless: "GEM",
  ukmo_seamless: "UKMO (10 m)",
};

/** Builds one row per CET day across the union of model daily_local arrays.
 *  A model's value shows only within its own coverage; days outside a model's
 *  horizon stay null (— in UI), never zero. Partial days carry a flag. */
export function buildDailyMatrix(models: { model_id: string; daily_local: DailySlot[] }[]): DailyRow[] {
  const dates = new Set<string>();
  for (const m of models) for (const d of m.daily_local) dates.add(d.date);
  const rows: DailyRow[] = [];
  for (const date of [...dates].sort()) {
    const values: Record<string, number | null> = {};
    const flags: Record<string, string> = {};
    for (const m of models) {
      const slot = m.daily_local.find((d) => d.date === date);
      if (!slot) {
        values[m.model_id] = null;
        flags[m.model_id] = "iza horizonta modela";
        continue;
      }
      if (slot.hours_counted === 0) {
        values[m.model_id] = null;
        flags[m.model_id] = "nema izračunatih sati (davatelj ne dostavlja vjetar za ovaj dan)";
        continue;
      }
      values[m.model_id] = slot.mwh;
      if (slot.partial) {
        flags[m.model_id] =
          slot.hours_missing > 0
            ? `djelomičan: ${slot.hours_missing} sat(i) nedostaju`
            : `djelomičan: ${slot.hours_counted}/${slot.hours_expected ?? 24} h`;
      }
    }
    rows.push({ date, values, flags });
  }
  return rows;
}

/** Hourly rows for the NEXT `hours` hours from the shared as-of anchor
 *  (rounded up to the next full hour — the current incomplete hour is
 *  excluded). Rows are a full hour grid in the site's IANA timezone, so a
 *  model lacking an hour shows an explicit missing row (never 0, never
 *  shifted). Repeated local hours across DST are distinguished by their UTC
 *  offset suffix (e.g. CET vs CEST). All models share the same anchor. */
export function buildHourlyRows(
  points: { valid_at_utc: string; wind_speed_mps: number | null; modeled_power_mw: number | null; interval_energy_mwh: number | null; interval_hours: number | null; status: string }[],
  anchorUtc: string,
  hours: 24 | 48,
  tz: string,
): { label: string; offset: string; validAtUtc: string; wind: number | null; mw: number | null; mwh: number | null; interval_hours: number | null; missing: boolean }[] {
  const anchorMs = new Date(anchorUtc).getTime();
  if (Number.isNaN(anchorMs)) return [];
  const startMs = Math.ceil(anchorMs / 3_600_000) * 3_600_000; // next full hour
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const fmtOffset = new Intl.DateTimeFormat("en-GB", { timeZone: tz, timeZoneName: "shortOffset" });
  const byKey = new Map<string, (typeof points)[number]>();
  for (const p of points) byKey.set(p.valid_at_utc.slice(0, 13), p);

  const rows: { label: string; offset: string; validAtUtc: string; wind: number | null; mw: number | null; mwh: number | null; interval_hours: number | null; missing: boolean }[] = [];
  for (let i = 0; i < hours; i++) {
    const t = new Date(startMs + i * 3_600_000);
    const key = t.toISOString().slice(0, 13);
    const p = byKey.get(key);
    const parts = fmt.formatToParts(t);
    const get = (type: string) => parts.find((x) => x.type === type)?.value ?? "";
    const hour24 = get("hour") === "24" ? "00" : get("hour");
    const label = `${get("month")}-${get("day")} ${hour24}:${get("minute")}`;
    const offset = (fmtOffset.formatToParts(t).find((x) => x.type === "timeZoneName")?.value ?? "").replace("GMT", "UTC");
    if (!p) {
      rows.push({ label, offset, validAtUtc: key + ":00:00Z", wind: null, mw: null, mwh: null, interval_hours: null, missing: true });
      continue;
    }
    rows.push({
      label,
      offset,
      validAtUtc: p.valid_at_utc,
      wind: p.wind_speed_mps,
      mw: p.modeled_power_mw,
      mwh: p.interval_energy_mwh,
      interval_hours: p.interval_hours,
      missing: p.wind_speed_mps === null || p.modeled_power_mw === null,
    });
  }
  return rows;
}

/** Addendum B — the UKUPNO (MWh) row is the plain SUM of the per-day MWh that
 *  a model shows above it. Partial days are included (their shown value IS the
 *  partial total) and the flag says so; days where the model has no value
 *  contribute nothing (— is never treated as 0). */
export function modelTotal(rows: DailyRow[], modelId: string): { total: number | null; partial: boolean; days: number } {
  let total = 0;
  let partial = false;
  let days = 0;
  for (const row of rows) {
    const v = row.values[modelId];
    if (v === null || v === undefined) continue;
    total += v;
    days += 1;
    if (row.flags[modelId]) partial = true;
  }
  return { total: days > 0 ? Math.round(total * 100) / 100 : null, partial, days };
}

/** Average column — arithmetic mean of the models' values in one daily row,
 *  over `ids` (production models only: met_only models must be excluded by the
 *  caller). Null only when NO listed model has a value in that row. */
export function rowAverage(values: Record<string, number | null>, ids: string[]): number | null {
  let sum = 0;
  let n = 0;
  for (const id of ids) {
    const v = values[id];
    if (v === null || v === undefined) continue;
    sum += v;
    n += 1;
  }
  return n > 0 ? Math.round((sum / n) * 100) / 100 : null;
}

/** Production model ids (met_only excluded) in display order — the Average
 *  column and UKUPNO totals never include surface-only comparisons. */
export function productionModelIds(models: { model_id: string; met_only: boolean; run: unknown }[]): string[] {
  return models.filter((m) => m.run !== null && !m.met_only).map((m) => m.model_id);
}

/** PR#8 Correction A §5 — hourly-aligned ENSEMBLE average.
 *
 * For every elementary time segment (interval boundaries ∩ local-day
 * boundaries) the average MW across the SELECTED models that cover the whole
 * segment is computed; energy = avgMW × segment_hours is allocated to the
 * site-local day containing the segment start (real 23/24/25 h DST days).
 * This makes the daily Average the genuine mean production estimate at each
 * instant — never an average of period totals, never averaging wind speeds,
 * never zero-filling hours no selected model covers. Per-day contributor
 * min/max make changing model coverage explicit. */
export interface EnsembleInput {
  model_id: string;
  points: { valid_at_utc: string; mw: number | null; interval_hours: number }[];
}

export interface EnsembleDailyRow {
  date: string;
  mwh: number;
  hours_counted: number;
  hours_missing: number;
  hours_expected: number;
  partial: boolean;
  contributors_min: number;
  contributors_max: number;
}

const DAY_FMT_CACHE = new Map<string, Intl.DateTimeFormat>();
function dayFmt(tz: string): Intl.DateTimeFormat {
  let f = DAY_FMT_CACHE.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
    DAY_FMT_CACHE.set(tz, f);
  }
  return f;
}

/** [startMs, endMs) of the LOCAL calendar day `date` in tz. Binary-searched
 * against formatted local dates, so real 23/24/25 h DST days come out exact. */
function localDayBounds(date: string, fmt: Intl.DateTimeFormat): [number, number] {
  const probe = Date.parse(`${date}T12:00:00Z`);
  if (Number.isNaN(probe)) return [NaN, NaN];
  const start = (() => {
    let lo = probe - 40 * 3_600_000;
    let hi = probe + 2 * 3_600_000;
    while (hi - lo > 1000) {
      const mid = Math.round((lo + hi) / 2);
      if (fmt.format(new Date(mid)) >= date) hi = mid;
      else lo = mid;
    }
    return hi;
  })();
  const end = (() => {
    let lo = start + 20 * 3_600_000;
    let hi = start + 32 * 3_600_000;
    while (hi - lo > 1000) {
      const mid = Math.round((lo + hi) / 2);
      if (fmt.format(new Date(mid)) > date) hi = mid;
      else lo = mid;
    }
    return hi;
  })();
  return [start, end];
}

export function buildEnsembleDaily(inputs: EnsembleInput[], tz: string): EnsembleDailyRow[] {
  const fmt = dayFmt(tz);
  const boundaries = new Set<number>();
  const perModel: { intervals: { s: number; e: number; mw: number }[] }[] = [];
  for (const inp of inputs) {
    const intervals: { s: number; e: number; mw: number }[] = [];
    for (const p of inp.points) {
      if (p.mw === null || p.mw === undefined) continue;
      const s = Date.parse(p.valid_at_utc);
      if (Number.isNaN(s)) continue;
      const e = s + Math.max(p.interval_hours, 0.001) * 3_600_000;
      intervals.push({ s, e, mw: p.mw });
      boundaries.add(s);
      boundaries.add(e);
    }
    if (intervals.length) perModel.push({ intervals });
  }
  if (perModel.length === 0) return [];

  const bts = [...boundaries].sort((a, b) => a - b);
  const daily = new Map<string, EnsembleDailyRow & { _segs: { s: number; e: number }[] }>();
  for (let i = 0; i < bts.length - 1; i++) {
    const s = bts[i]!;
    const e = bts[i + 1]!;
    const contributors: number[] = [];
    for (const pm of perModel) {
      const hit = pm.intervals.find((iv) => iv.s <= s && e <= iv.e);
      if (hit !== undefined) contributors.push(hit.mw);
    }
    if (contributors.length === 0) continue; // an hour no selected model covers stays missing
    const avgMw = contributors.reduce((a, c) => a + c, 0) / contributors.length;
    let cur = s;
    while (cur < e - 1) {
      const date = fmt.format(new Date(cur));
      const row = daily.get(date) ?? {
        date,
        mwh: 0,
        hours_counted: 0,
        hours_missing: 0,
        hours_expected: 0,
        partial: false,
        contributors_min: contributors.length,
        contributors_max: contributors.length,
        _segs: [],
      };
      const [dStart, dEnd] = localDayBounds(date, fmt);
      let segEnd = Math.min(e, dEnd);
      // the day boundary is searched to ~1 s; snap it to the exact interval
      // edge when they coincide so energy sums stay exact (never 10.05 MWh)
      if (Math.abs(segEnd - e) <= 120_000) segEnd = e;
      const hours = (segEnd - cur) / 3_600_000;
      if (hours > 0) {
        row.mwh = round2(row.mwh + avgMw * hours);
        row._segs.push({ s: cur, e: segEnd });
        row.contributors_min = Math.min(row.contributors_min, contributors.length);
        row.contributors_max = Math.max(row.contributors_max, contributors.length);
        row.hours_expected = Math.round(Math.max(row.hours_expected, (dEnd - dStart) / 3_600_000) * 1000) / 1000;
        daily.set(date, row);
      }
      cur = segEnd > cur ? segEnd : e;
    }
  }
  const out: EnsembleDailyRow[] = [];
  for (const row of [...daily.values()].sort((a, b) => (a.date < b.date ? -1 : 1))) {
    const merged: { s: number; e: number }[] = [];
    for (const seg of row._segs.sort((a, b) => a.s - b.s)) {
      const last = merged[merged.length - 1];
      if (last && seg.s <= last.e + 1) last.e = Math.max(last.e, seg.e);
      else merged.push({ ...seg });
    }
    const unionH = merged.reduce((acc, m) => acc + (m.e - m.s) / 3_600_000, 0);
    row.hours_counted = round2(unionH);
    row.hours_missing = round2(Math.max(row.hours_expected - unionH, 0));
    row.partial = unionH < row.hours_expected - 1e-6;
    delete (row as unknown as Record<string, unknown>)._segs;
    out.push(row);
  }
  return out;
}

function round2(v: number): number {
  return Math.round(v * 100) / 100;
}

/** PR#8 Correction A §5 — show EXACTLY `days` local calendar dates, anchored
 * at today in the site timezone (never an accidental N+1-th date from a UTC
 * horizon cut). If no row is on/after today, the LAST `days` rows are shown.
 * 7/10/15 totals must equal the sum of the DISPLAYED rows. */
export function visibleDailyRows<T extends { date: string }>(rows: T[], days: number, tz: string, nowUtc?: string): T[] {
  if (rows.length <= days) return rows;
  const today = dayFmt(tz).format(nowUtc ? new Date(nowUtc) : new Date());
  const idx = rows.findIndex((r) => r.date >= today);
  const start = idx === -1 ? rows.length - days : idx;
  return rows.slice(start, start + days);
}

/** Average display preference, persisted PER SITE (Correction A §5): an
 * on/off toggle plus the selected contributor model set. localStorage is the
 * single-user persistence; the backend remains authoritative for numbers. */
export interface AvgPreference {
  enabled: boolean;
  /** true once the user has explicitly chosen a model set (or toggled the
   *  column) — distinguishes never-configured (default: all production
   *  models) from an EXPLICIT empty selection (Prosjek shows an em dash,
   *  never a silent re-enable of every model). Correction B section 3. */
  configured: boolean;
  models: string[];
}

// localStorage when available (browser); in-memory fallback elsewhere
// (tests, SSR) so the preference always round-trips within a process
const memStore = new Map<string, string>();
function prefStorage(): { getItem: (k: string) => string | null; setItem: (k: string, v: string) => void } {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* unreachable in browsers; node throws on the binding itself */
  }
  return { getItem: (k) => memStore.get(k) ?? null, setItem: (k, v) => void memStore.set(k, v) };
}

export function loadAvgPreference(siteId: string, productionIds: string[]): AvgPreference {
  try {
    const raw = prefStorage().getItem(`windy.avg.${siteId}`);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<AvgPreference>;
      // Correction C §3: an EXPLICIT boolean wins; only the LEGACY format
      // (no configured field at all) infers it from the presence of a saved
      // models list. configured:false always means default/all current
      // production models — even when a models array accompanies it.
      if (typeof parsed.configured === "boolean" && !parsed.configured) {
        return { enabled: !!parsed.enabled, configured: false, models: [...productionIds] };
      }
      const configured = typeof parsed.configured === "boolean" ? parsed.configured : Array.isArray(parsed.models);
      let models = Array.isArray(parsed.models) ? [...parsed.models] : [];
      // filter against CURRENT production ids only when they are loaded;
      // while temporarily empty (loading/missing runs) the stored selection
      // is kept untouched, never erased
      if (productionIds.length > 0) models = models.filter((m) => productionIds.includes(m));
      return { enabled: !!parsed.enabled, configured, models };
    }
  } catch {
    /* corrupted preference falls through to the default */
  }
  return { enabled: true, configured: false, models: [...productionIds] };
}

export function saveAvgPreference(siteId: string, pref: AvgPreference): void {
  try {
    prefStorage().setItem(`windy.avg.${siteId}`, JSON.stringify(pref));
  } catch {
    /* storage blocked — persistence is best-effort, UI still works */
  }
}

/** PR#8 Correction D §4 — UKUPNO (Prosjek) sums ONLY the Average values of
 *  rows actually displayed in the daily table (same calendar window), never
 *  an extra ensemble-only day. Missing data stays missing: with no
 *  contributor rows the result is null (rendered as an em dash) — NEVER 0.
 *  A genuine below-cut-in forecast keeps its real 0 (rows exist with
 *  hours_counted > 0 and mwh 0). */
export function sumDisplayedAverages(
  rows: { date: string }[],
  avgByDate: Map<string, { mwh: number }>,
): number | null {
  let total = 0;
  let n = 0;
  for (const row of rows) {
    const avg = avgByDate.get(row.date);
    if (!avg) continue;
    total += avg.mwh;
    n += 1;
  }
  return n > 0 ? Math.round(total * 100) / 100 : null;
}
