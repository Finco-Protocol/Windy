/** Forecast-first helpers — pure, tested builders for the Croatian daily/hourly
 *  tables. CET (UTC+1) FIXED as reference day boundary (owner decision): the
 *  CET date of a UTC hour t is (t + 1 h).date(). */

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

/** Hourly rows for ONE model over the next `hours` from `nowUtc`, in CET
 *  labels. Gaps stay null; beyond the model's archived horizon rows are cut
 *  (no invented hours). `anchorUtc` = window start (usually now). */
export function buildHourlyRows(
  points: { valid_at_utc: string; wind_speed_mps: number | null; modeled_power_mw: number | null; interval_energy_mwh: number | null; status: string }[],
  nowUtc: string,
  hours: 24 | 48,
): { cetLabel: string; wind: number | null; mw: number | null; mwh: number | null; missing: boolean }[] {
  const start = new Date(nowUtc).getTime();
  const end = start + hours * 3_600_000;
  const CET = 3_600_000; // UTC+1 in ms
  const rows: { cetLabel: string; wind: number | null; mw: number | null; mwh: number | null; missing: boolean }[] = [];
  for (const p of points) {
    const t = new Date(p.valid_at_utc).getTime();
    if (t < start || t >= end) continue;
    const cet = new Date(t + CET);
    rows.push({
      cetLabel: cet.toISOString().slice(5, 16).replace("T", " ") + " CET",
      wind: p.wind_speed_mps,
      mw: p.modeled_power_mw,
      mwh: p.interval_energy_mwh,
      missing: p.wind_speed_mps === null || p.modeled_power_mw === null,
    });
  }
  return rows;
}
