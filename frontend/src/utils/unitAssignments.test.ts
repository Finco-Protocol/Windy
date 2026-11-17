/** PR#8 Correction D §1–§3 — the exact pure helper used by the Rad elektrane
 *  editor. Assertions cover the PAYLOAD that reaches
 *  `windyApi.constraintsReplaceUnits` (via buildAssignments), effective-cap
 *  preview math, and 0%/100% boundary behavior. */

import { describe, expect, it } from "vitest";

import { buildAssignments, editAllowedMw, effectiveCapMw, type UnitAssignment, type UnitCurrentState, type UnitEdit } from "./unitAssignments";

const RATED = 2.85;

function edit(partial: Partial<UnitEdit>): UnitEdit {
  return { action: "cap", mode: "mw", reductionPct: null, capMw: null, ...partial };
}
function cst(partial: Partial<UnitCurrentState>): UnitCurrentState {
  return { off: false, capMw: null, capPercent: null, ...partial };
}

const RATEDS: Record<string, number> = { "WTG-001": RATED, "WTG-002": RATED };

function build(edits: Record<string, UnitEdit>, states: Record<string, UnitCurrentState> = {}, units?: string[]): UnitAssignment[] {
  return buildAssignments({
    units: units ?? Object.keys(edits),
    getEdit: (u) => edits[u]!,
    getCurrent: (u) => states[u] ?? cst({}),
    getRated: (u) => RATEDS[u] ?? RATED,
  });
}

describe("buildAssignments — authoritative mode (Correction D §1)", () => {
  it("MW mode sends EXACT cap_mw and never cap_percent", () => {
    const out = build({ "WTG-001": edit({ mode: "mw", capMw: 2.5 }) });
    expect(out).toEqual([{ unit_id: "WTG-001", state: "cap", cap_mw: 2.5 }]);
    expect(out[0]?.cap_percent).toBeUndefined();
  });

  it("pct mode sends cap_percent = 100 − reduction and never cap_mw", () => {
    const out = build({ "WTG-001": edit({ mode: "pct", reductionPct: 12.28 }) });
    expect(out).toEqual([{ unit_id: "WTG-001", state: "cap", cap_percent: 87.72 }]);
    expect(out[0]?.cap_mw).toBeUndefined();
  });

  it("user editing ONLY Max MW gets cap_mw even with a stale pct value in the edit", () => {
    const out = build({ "WTG-001": edit({ mode: "mw", capMw: 2.5, reductionPct: 40 }) });
    expect(out).toEqual([{ unit_id: "WTG-001", state: "cap", cap_mw: 2.5 }]);
  });

  it("bulk MW choice REPLACES stale per-unit pct overrides (mode authority)", () => {
    // WTG-001 previously carried a per-unit pct override; a fresh bulk apply
    // overwrites it — the helper receives the new MW-mode edit
    const out = build(
      { "WTG-001": edit({ mode: "mw", capMw: 2.5 }), "WTG-002": edit({ mode: "mw", capMw: 2.5 }) },
      { "WTG-001": cst({ capPercent: 50 }) },
    );
    expect(out).toHaveLength(2);
    expect(out.every((a) => a.state === "cap" && a.cap_mw === 2.5 && a.cap_percent === undefined)).toBe(true);
  });

  it("six × 2.50 MW edits produce six cap_mw 2.5 assignments (72.00 MW proof input)", () => {
    const units = Array.from({ length: 6 }, (_, i) => `WTG-00${i + 1}`);
    const edits = Object.fromEntries(units.map((u) => [u, edit({ mode: "mw", capMw: 2.5 })]));
    const out = build(edits, {}, units);
    expect(out).toHaveLength(6);
    expect(out.every((a) => a.cap_mw === 2.5)).toBe(true);
    const preview = 20 * RATED + 6 * 2.5;
    expect(preview).toBeCloseTo(72.0, 6);
  });
});

describe("buildAssignments — boundaries (Correction D §3)", () => {
  it("0% reduction on a CONSTRAINED unit emits state 'on' (removes the cap)", () => {
    const out = build({ "WTG-001": edit({ mode: "pct", reductionPct: 0 }) }, { "WTG-001": cst({ capMw: 2.5 }) });
    expect(out).toEqual([{ unit_id: "WTG-001", state: "on" }]);
  });

  it("0% reduction on an UNCONSTRAINED unit is a no-op", () => {
    expect(build({ "WTG-001": edit({ mode: "pct", reductionPct: 0 }) })).toEqual([]);
  });

  it("100% reduction becomes OFF (backend rejects cap_percent 0)", () => {
    const out = build({ "WTG-001": edit({ mode: "pct", reductionPct: 100 }) });
    expect(out).toEqual([{ unit_id: "WTG-001", state: "off" }]);
    expect(out[0]?.cap_percent).toBeUndefined();
  });

  it("Max MW == rated on a CONSTRAINED unit emits 'on'; unconstrained → no-op", () => {
    expect(build({ "WTG-001": edit({ mode: "mw", capMw: RATED }) }, { "WTG-001": cst({ capMw: 2.5 }) })).toEqual([{ unit_id: "WTG-001", state: "on" }]);
    expect(build({ "WTG-001": edit({ mode: "mw", capMw: RATED }) })).toEqual([]);
  });

  it("Max MW above rated is an explicit validation error, never a silent no-op", () => {
    expect(() => build({ "WTG-001": edit({ mode: "mw", capMw: 3.5 }) })).toThrow(/prelazi nazivnu/);
  });

  it("invalid input throws with an actionable message (NaN, ≤0, > rated, pct range)", () => {
    expect(() => build({ "WTG-001": edit({ mode: "mw", capMw: NaN }) })).toThrow(/Max MW/);
    expect(() => build({ "WTG-001": edit({ mode: "mw", capMw: 0 }) })).toThrow(/pozitivan/);
    expect(() => build({ "WTG-001": edit({ mode: "mw", capMw: -1 }) })).toThrow(/pozitivan/);
    expect(() => build({ "WTG-001": edit({ mode: "mw", capMw: 3.0 }) })).toThrow(/prelazi nazivnu/);
    expect(() => build({ "WTG-001": edit({ mode: "pct", reductionPct: 120 }) })).toThrow(/rasponu 0–100/);
    expect(() => build({ "WTG-001": edit({ mode: "pct", reductionPct: NaN }) })).toThrow(/% smanjenja/);
  });
});

describe("effectiveCapMw — preview math (Correction D §2)", () => {
  it("most restrictive when both cap kinds coexist", () => {
    expect(effectiveCapMw(cst({ capMw: 2.6, capPercent: 80 }), RATED)).toBeCloseTo(2.28, 6); // 80% of 2.85
    expect(effectiveCapMw(cst({ capMw: 2.0, capPercent: 90 }), RATED)).toBe(2.0);
  });

  it("OFF dominates as 0; unrestricted is null", () => {
    expect(effectiveCapMw(cst({ off: true, capMw: 2.5 }), RATED)).toBe(0);
    expect(effectiveCapMw(cst({}), RATED)).toBeNull();
  });

  it("six %-based caps at 87.72% preview as 72.00 MW — not 71.25", () => {
    const eff = effectiveCapMw(cst({ capPercent: 87.72 }), RATED) ?? RATED;
    const preview = 20 * RATED + 6 * eff;
    expect(preview).toBeCloseTo(72.0, 2);
    expect(preview).not.toBeCloseTo(71.25, 1);
  });
});

describe("editAllowedMw — display sync", () => {
  it("mw mode shows the typed MW; pct mode shows the implied MW", () => {
    expect(editAllowedMw(edit({ mode: "mw", capMw: 2.5 }), RATED)).toBe(2.5);
    expect(editAllowedMw(edit({ mode: "pct", reductionPct: 12.28 }), RATED)).toBeCloseTo(2.5, 2); // 87.72% x 2.85 = 2.49978
    expect(editAllowedMw(edit({ action: "off" }), RATED)).toBe(0);
    expect(editAllowedMw(edit({ action: "on" }), RATED)).toBe(RATED);
  });
});
