/** Pure helpers for chart gap handling — tested to lock the contract that
 *  missing data stays a visible gap and never bridges silently. */

/** Availability scenario series: ALL timestamps are kept; missing values
 *  remain [ts, null] so `connectNulls: false` renders a visible break. */
export function scenarioSeries(points: { valid_at_utc: string; base_mw: number | null; adjusted_mw: number | null }[]): {
  base: [string, number | null][];
  adjusted: [string, number | null][];
} {
  return {
    base: points.map((p) => [p.valid_at_utc, p.base_mw] as [string, number | null]),
    adjusted: points.map((p) => [p.valid_at_utc, p.adjusted_mw] as [string, number | null]),
  };
}

/** Cumulative energy over COMPUTED hours only. After a missing-energy point
 *  the trajectory becomes null (gap in the line) — a carried-forward flat
 *  line would silently present a continuous full forecast across missing
 *  data. */
export function cumulativeOverComputed(
  points: { valid_at_utc: string; interval_energy_mwh: number | null }[],
): [string, number | null][] {
  let acc = 0;
  let broken = false;
  return points.map((p) => {
    if (p.interval_energy_mwh === null) broken = true;
    else acc += p.interval_energy_mwh;
    return [p.valid_at_utc, broken ? null : Math.round(acc * 1000) / 1000] as [string, number | null];
  });
}
