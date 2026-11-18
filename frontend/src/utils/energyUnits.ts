/** Correction E Addendum B §1 — display-only energy unit (MWh | GWh).
 *
 *  STRICTLY formatting: every stored number stays MWh (backend, archive,
 *  Average aggregation, CSV exports). Conversion happens only at render:
 *  displayGWh = rawMWh / 1000, up to 4 decimals. A strictly positive value
 *  that would round to 0.0000 GWh renders as `<0.0001 GWh` — never presented
 *  as a real zero. 0 stays 0, missing stays null (the caller renders —),
 *  partial ⚠ flags are orthogonal. Toggling never re-fetches anything and
 *  never mutates business numbers; toggling back returns the identical
 *  source value (no cumulative rounding). */

const memStore = new Map<string, string>();
function prefStorage(): { getItem: (k: string) => string | null; setItem: (k: string, v: string) => void } {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* node/test environments */
  }
  return { getItem: (k) => memStore.get(k) ?? null, setItem: (k, v) => void memStore.set(k, v) };
}

export type EnergyUnit = "mwh" | "gwh";

export function loadEnergyUnit(siteId: string): EnergyUnit {
  try {
    const raw = prefStorage().getItem(`windy.energyUnit.${siteId}`);
    if (raw === "gwh") return "gwh";
    if (raw === "mwh") return "mwh";
  } catch {
    /* corrupted record falls through */
  }
  return "mwh"; // default for compatibility
}

export function saveEnergyUnit(siteId: string, unit: EnergyUnit): void {
  try {
    prefStorage().setItem(`windy.energyUnit.${siteId}`, unit);
  } catch {
    /* best-effort */
  }
}

/** Format a raw MWh value for display under the active unit.
 *  null/undefined → null (caller renders —). GWh: 4 decimals; a strictly
 *  positive raw value that rounds below 0.0001 GWh renders as the explicit
 *  `<0.0001 GWh` marker string, never as a fake zero. */
export function formatEnergy(rawMwh: number | null | undefined, unit: EnergyUnit): string | null {
  if (rawMwh === null || rawMwh === undefined || Number.isNaN(rawMwh)) return null;
  if (unit === "mwh") {
    const rounded = Math.round(rawMwh * 100) / 100;
    return rawMwh > 0 && rounded === 0 ? "<0.01 MWh" : String(rounded);
  }
  const gwh = rawMwh / 1000;
  const rounded = Math.round(gwh * 10000) / 10000;
  if (rawMwh > 0 && rounded === 0) return "<0.0001 GWh";
  return String(rounded);
}

/** Unit suffix for labels/headings. */
export function energyLabel(unit: EnergyUnit): string {
  return unit === "gwh" ? "GWh" : "MWh";
}
