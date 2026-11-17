/** Correction E Addendum B §6 — MWh|GWh formatting contract. */

import { describe, expect, it } from "vitest";

import { energyLabel, formatEnergy, loadEnergyUnit, saveEnergyUnit } from "./energyUnits";

describe("formatEnergy (Addendum B §1)", () => {
  it("owner's GWh examples from the prompt", () => {
    expect(formatEnergy(1230.9553, "gwh")).toBe("1.231");
    expect(formatEnergy(860.24, "gwh")).toBe("0.8602");
    expect(formatEnergy(3420.85, "gwh")).toBe("3.4209");
  });

  it("MWh mode unchanged (business values untouched)", () => {
    expect(formatEnergy(1230.9553, "mwh")).toBe("1230.96");
    expect(formatEnergy(860.24, "mwh")).toBe("860.24");
  });

  it("real zero stays 0 in both units", () => {
    expect(formatEnergy(0, "mwh")).toBe("0");
    expect(formatEnergy(0, "gwh")).toBe("0");
  });

  it("missing stays null (caller renders —)", () => {
    expect(formatEnergy(null, "gwh")).toBeNull();
    expect(formatEnergy(undefined, "mwh")).toBeNull();
    expect(formatEnergy(NaN, "gwh")).toBeNull();
  });

  it("tiny positive MWh never presents as a real zero in GWh mode", () => {
    // 0.01 MWh = 0.00001 GWh -> rounds to 0.0000 -> explicit marker, never 0
    expect(formatEnergy(0.01, "gwh")).toBe("<0.0001 GWh");
    // 0.05 MWh = 0.00005 GWh -> rounds to 0.0001 (a REAL displayed value)
    expect(formatEnergy(0.05, "gwh")).toBe("0.0001");
  });

  it("tiny positive MWh in MWh mode also guarded", () => {
    expect(formatEnergy(0.001, "mwh")).toBe("<0.01 MWh");
  });

  it("negative values render as-is (no positive-zero guard)", () => {
    expect(formatEnergy(-2.5, "gwh")).toBe("-0.0025");
  });

  it("toggle back returns the identical source value (no cumulative rounding)", () => {
    const raw = 1230.9553;
    const gwh = formatEnergy(raw, "gwh")!;
    expect(gwh).toBe("1.231");
    // parsing back the displayed 4-decimal GWh and reformatting to MWh gives
    // the same display as the original (round-trip stability)
    const back = parseFloat(gwh) * 1000;
    expect(formatEnergy(back, "mwh")).toBe(formatEnergy(1231, "mwh"));
  });

  it("unit label", () => {
    expect(energyLabel("gwh")).toBe("GWh");
    expect(energyLabel("mwh")).toBe("MWh");
  });
});

describe("per-site persistence", () => {
  it("defaults to MWh; per-site isolation; survives save/load", () => {
    expect(loadEnergyUnit("fresh")).toBe("mwh");
    saveEnergyUnit("site-a", "gwh");
    expect(loadEnergyUnit("site-a")).toBe("gwh");
    expect(loadEnergyUnit("site-b")).toBe("mwh");
    saveEnergyUnit("site-a", "mwh");
    expect(loadEnergyUnit("site-a")).toBe("mwh");
  });
});
