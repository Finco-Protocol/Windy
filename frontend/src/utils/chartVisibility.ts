/** Correction E Addendum A §1 — per-site, per-chart series visibility.
 *  Pure presentation preference: toggling a legend NEVER triggers API calls
 *  or changes data/ensemble calculations. Storage keys:
 *  `windy.chart.wind.<site_id>` / `windy.chart.power.<site_id>`.
 *
 *  UKMO (met-only, 10 m) is OFF by default on the wind chart the first time
 *  a site is seen (explicit 10 m surface comparison); it can never appear on
 *  the power chart (excluded by the caller). Stored selections are never
 *  wiped while model ids are temporarily empty (loading / missing runs). */

const memStore = new Map<string, string>();
function prefStorage(): { getItem: (k: string) => string | null; setItem: (k: string, v: string) => void } {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* node/test environments without the binding */
  }
  return { getItem: (k) => memStore.get(k) ?? null, setItem: (k, v) => void memStore.set(k, v) };
}

export type ChartKey = "wind" | "power";

export interface ChartVisibility {
  /** model ids explicitly HIDDEN by the user */
  hidden: string[];
  /** true once a stored record exists for this site+chart (distinguishes
   *  first-run defaults from an explicit user choice) */
  configured: boolean;
}

export function chartStorageKey(chart: ChartKey, siteId: string): string {
  return `windy.chart.${chart}.${siteId}`;
}

export function loadChartVisibility(chart: ChartKey, siteId: string, modelIds: string[], metOnlyIds: string[] = []): ChartVisibility {
  try {
    const raw = prefStorage().getItem(chartStorageKey(chart, siteId));
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ChartVisibility>;
      let hidden = Array.isArray(parsed.hidden) ? [...parsed.hidden] : [];
      if (modelIds.length > 0) hidden = hidden.filter((id) => modelIds.includes(id));
      return { hidden, configured: parsed.configured === true };
    }
  } catch {
    /* corrupted record falls through to defaults */
  }
  // first run: UKMO-type met-only models default OFF on the WIND chart
  // (explicit 10 m surface comparison, never presented as hub wind)
  const defaultOff = chart === "wind" ? metOnlyIds : [];
  return { hidden: [...defaultOff], configured: false };
}

export function saveChartVisibility(chart: ChartKey, siteId: string, vis: ChartVisibility): void {
  try {
    prefStorage().setItem(chartStorageKey(chart, siteId), JSON.stringify({ hidden: vis.hidden, configured: true }));
  } catch {
    /* best-effort persistence */
  }
}
