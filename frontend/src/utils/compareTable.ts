/** Correction E Addendum A §2 — Compare tabs: all-provider side-by-side
 *  hourly view. Rows align on the SAME absolute UTC hour (never per-model
 *  local clocks); labels render in the site's timezone with DST offsets so
 *  CET/CEST transitions stay truthful. Data comes exclusively from the
 *  existing GET /api/forecast response — no refetch on tab switch.
 *
 *  Adjusted mode uses ONLY each model's adjusted.available/adjusted.points —
 *  an unavailable adjusted block stays missing (null), never a silent
 *  baseline substitution. UKMO/met-only is passed by the caller only for the
 *  WIND comparison and is never averaged into production Prosjek. */

export interface CompareInput {
  model_id: string;
  points: { valid_at_utc: string; value: number | null; interval_hours: number }[];
}

export interface CompareRow {
  /** UTC instant key `YYYY-MM-DDTHH` (full valid_at_utc alignment) */
  key: string;
  /** site-local label `MM-DD HH:MM` */
  label: string;
  /** UTC offset suffix (CET/CEST → UTC+1/UTC+2) */
  offset: string;
  values: Record<string, number | null>;
}

/** Shared UTC-hour grid from the as-of anchor (next full hour — the same
 *  window semantics as buildHourlyRows), aligned on full valid_at_utc. */
export function buildCompareRows(inputs: CompareInput[], anchorUtc: string, hours: number, tz: string): CompareRow[] {
  const anchorMs = new Date(anchorUtc).getTime();
  if (Number.isNaN(anchorMs) || inputs.length === 0) return [];
  const startMs = Math.ceil(anchorMs / 3_600_000) * 3_600_000;
  const fmt = new Intl.DateTimeFormat("en-GB", { timeZone: tz, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtOffset = new Intl.DateTimeFormat("en-GB", { timeZone: tz, timeZoneName: "shortOffset" });
  const byModel = new Map<string, Map<string, { value: number | null; interval_hours: number }>>();
  for (const inp of inputs) {
    const m = new Map<string, { value: number | null; interval_hours: number }>();
    for (const p of inp.points) m.set(p.valid_at_utc.slice(0, 13), { value: p.value, interval_hours: p.interval_hours });
    byModel.set(inp.model_id, m);
  }
  const rows: CompareRow[] = [];
  for (let i = 0; i < hours; i++) {
    const t = new Date(startMs + i * 3_600_000);
    const key = t.toISOString().slice(0, 13);
    const parts = fmt.formatToParts(t);
    const get = (type: string) => parts.find((x) => x.type === type)?.value ?? "";
    const hour24 = get("hour") === "24" ? "00" : get("hour");
    const label = `${get("month")}-${get("day")} ${hour24}:${get("minute")}`;
    const offset = (fmtOffset.formatToParts(t).find((x) => x.type === "timeZoneName")?.value ?? "").replace("GMT", "UTC");
    const values: Record<string, number | null> = {};
    for (const [mid, m] of byModel) {
      const hit = m.get(key);
      values[mid] = hit ? hit.value : null;
    }
    rows.push({ key, label, offset, values });
  }
  return rows;
}

/** Prosjek column for the PRODUCTION comparison: arithmetic mean of the
 *  caller-selected production models at the SAME UTC hour, contributors
 *  counted, null when no contributor covers the hour (missing ≠ 0). Never
 *  includes met-only ids (caller passes the already-filtered set). */
export function compareAverage(row: CompareRow, modelIds: string[]): { value: number | null; contributors: number } {
  let sum = 0;
  let n = 0;
  for (const id of modelIds) {
    const v = row.values[id];
    if (v === null || v === undefined) continue;
    sum += v;
    n += 1;
  }
  return { value: n > 0 ? Math.round((sum / n) * 100) / 100 : null, contributors: n };
}
