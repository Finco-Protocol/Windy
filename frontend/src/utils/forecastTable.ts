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
): { label: string; offset: string; wind: number | null; mw: number | null; mwh: number | null; interval_hours: number | null; missing: boolean }[] {
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

  const rows: { label: string; offset: string; wind: number | null; mw: number | null; mwh: number | null; interval_hours: number | null; missing: boolean }[] = [];
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
      rows.push({ label, offset, wind: null, mw: null, mwh: null, interval_hours: null, missing: true });
      continue;
    }
    rows.push({
      label,
      offset,
      wind: p.wind_speed_mps,
      mw: p.modeled_power_mw,
      mwh: p.interval_energy_mwh,
      interval_hours: p.interval_hours,
      missing: p.wind_speed_mps === null || p.modeled_power_mw === null,
    });
  }
  return rows;
}
