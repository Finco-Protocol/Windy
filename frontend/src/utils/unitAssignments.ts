/** PR#8 Correction D §1–§3 — the EXACT pure logic behind the per-turbine
 *  editor in WindyForecastView. One and only one limiting mode is
 *  authoritative per unit (`pct` = % SMANJENJA, `mw` = Max MW); the two are
 *  never silently mixed, and the payload sent to
 *  `POST /constraints/replace-units` is built here.
 *
 *  Backend semantics preserved: `cap_percent` is percentage ALLOWED
 *  (100 = unrestricted), never reduction; `cap_mw` is the exact permitted MW.
 *  Boundaries: 0% reduction = ON unrestricted (removes an active cap);
 *  100% reduction = OFF (backend rejects cap_percent 0); Max MW ≥ rated =
 *  ON if the unit is constrained, else a no-op. */

export interface UnitAssignment {
  unit_id: string;
  state: "on" | "off" | "cap";
  cap_mw?: number;
  cap_percent?: number;
}

export interface UnitCurrentState {
  off: boolean;
  capMw: number | null; // most restrictive active MW cap (backend cap_mw)
  capPercent: number | null; // most restrictive active %-ALLOWED cap (backend cap_percent)
}

export type CapMode = "pct" | "mw";

export interface UnitEdit {
  action: "on" | "off" | "cap";
  mode: CapMode; // the ONE authoritative limiting mode for this edit
  reductionPct: number | null; // % SMANJENJE (0 = unrestricted, 100 = zero allowed)
  capMw: number | null; // exact permitted MW
}

const EPS = 1e-6;

/** Most restrictive effective allowed MW for a unit's CURRENT state.
 *  null = unrestricted (rated applies). OFF dominates as 0. Handles both cap
 *  kinds coexisting by taking the minimum of their effective limits — this is
 *  what makes the confirmation preview match backend `site_status`. */
export function effectiveCapMw(state: UnitCurrentState, rated: number): number | null {
  if (state.off) return 0;
  const limits: number[] = [];
  if (state.capMw !== null && state.capMw !== undefined && Number.isFinite(state.capMw)) {
    limits.push(Math.min(state.capMw, rated));
  }
  if (state.capPercent !== null && state.capPercent !== undefined && Number.isFinite(state.capPercent)) {
    limits.push((state.capPercent / 100) * rated);
  }
  if (limits.length === 0) return null;
  return Math.round(Math.min(...limits) * 10000) / 10000;
}

/** Allowed MW implied by an EDIT under its AUTHORITATIVE mode. */
export function editAllowedMw(edit: UnitEdit, rated: number): number | null {
  if (edit.action === "off") return 0;
  if (edit.action === "on") return rated;
  if (edit.mode === "mw") {
    if (edit.capMw === null || edit.capMw === undefined || !Number.isFinite(edit.capMw)) return null;
    return Math.round(Math.min(edit.capMw, rated) * 10000) / 10000;
  }
  const reduction = edit.reductionPct;
  if (reduction === null || reduction === undefined || !Number.isFinite(reduction)) return null;
  return Math.round(((100 - reduction) / 100) * rated * 10000) / 10000;
}

function unitConstrained(cur: UnitCurrentState): boolean {
  return cur.off || cur.capMw !== null || cur.capPercent !== null;
}

export interface AssignmentInputs {
  units: string[];
  getEdit: (unit: string) => UnitEdit;
  getCurrent: (unit: string) => UnitCurrentState;
  getRated: (unit: string) => number;
}

/** Build the replace-units payload. Throws with an actionable message on
 *  invalid input (NaN, non-positive MW, MW above rated, pct outside 0–100). */
export function buildAssignments({ units, getEdit, getCurrent, getRated }: AssignmentInputs): UnitAssignment[] {
  const out: UnitAssignment[] = [];
  for (const unit of [...units].sort()) {
    const edit = getEdit(unit);
    const cur = getCurrent(unit);
    const rated = getRated(unit);
    if (!Number.isFinite(rated) || rated <= 0) throw new Error(`${unit}: nazivna snaga jedinice nije validna (${rated} MW)`);

    if (edit.action === "on") {
      if (!unitConstrained(cur)) continue;
      out.push({ unit_id: unit, state: "on" });
      continue;
    }
    if (edit.action === "off") {
      if (cur.off) continue;
      out.push({ unit_id: unit, state: "off" });
      continue;
    }

    // ---- cap: ONE authoritative mode ----
    if (edit.mode === "mw") {
      const mw = edit.capMw;
      if (mw === null || mw === undefined || !Number.isFinite(mw)) {
        throw new Error(`${unit}: Max MW nije unesen ili nije broj — unesite vrijednost ili odaberite drugi mod.`);
      }
      if (mw <= 0) throw new Error(`${unit}: Max MW mora biti pozitivan broj (uneseno ${mw}).`);
      if (mw > rated + EPS) throw new Error(`${unit}: Max MW ${mw} prelazi nazivnu snagu ${rated} MW.`);
      // Max MW ≥ rated = effectively unrestricted
      if (mw >= rated - EPS) {
        if (unitConstrained(cur)) out.push({ unit_id: unit, state: "on" });
        continue;
      }
      const curEff = effectiveCapMw(cur, rated);
      if (!cur.off && curEff !== null && Math.abs(curEff - mw) < EPS) continue; // identical effective limit
      out.push({ unit_id: unit, state: "cap", cap_mw: Math.round(mw * 10000) / 10000 });
      continue;
    }

    // ---- pct mode: % SMANJENJA → backend cap_percent = 100 − reduction ----
    const reduction = edit.reductionPct;
    if (reduction === null || reduction === undefined || !Number.isFinite(reduction)) {
      throw new Error(`${unit}: % smanjenja nije unesen ili nije broj — unesite vrijednost ili odaberite drugi mod.`);
    }
    if (reduction < 0 || reduction > 100) throw new Error(`${unit}: % smanjenja mora biti u rasponu 0–100 (uneseno ${reduction}).`);
    if (reduction <= EPS) {
      // 0% reduction = unrestricted; an ACTIVE cap must be REMOVED, never a
      // silent no-op
      if (unitConstrained(cur)) out.push({ unit_id: unit, state: "on" });
      continue;
    }
    if (reduction >= 100 - 0.005) {
      // ~100% reduction = OFF (backend rejects cap_percent 0 as a cap)
      if (!cur.off) out.push({ unit_id: unit, state: "off" });
      continue;
    }
    const allowedPct = Math.round((100 - reduction) * 10000) / 10000;
    const targetMw = Math.round(((allowedPct / 100) * rated) * 10000) / 10000;
    const curEff = effectiveCapMw(cur, rated);
    if (!cur.off && curEff !== null && Math.abs(curEff - targetMw) < EPS) continue; // identical effective limit
    out.push({ unit_id: unit, state: "cap", cap_percent: allowedPct });
  }
  return out;
}
