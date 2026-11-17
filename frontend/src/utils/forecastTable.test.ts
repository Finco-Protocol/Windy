import { describe, expect, it } from "vitest";

import { buildDailyMatrix, buildHourlyRows, MODEL_LABELS_HR, modelTotal, productionModelIds, rowAverage, type DailySlot } from "./forecastTable";

function model(id: string, daily: DailySlot[]): { model_id: string; daily_local: DailySlot[] } {
  return { model_id: id, daily_local: daily };
}

describe("buildDailyMatrix", () => {
  it("unions dates across models; missing days stay null, not zero", () => {
    const models = [
      model("ecmwf_ifs", [
        { date: "2026-11-13", mwh: 100, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false },
        { date: "2026-11-14", mwh: 110, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false },
      ]),
      model("icon_eu", [{ date: "2026-11-13", mwh: 90, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }]),
    ];
    const rows = buildDailyMatrix(models);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.values).toEqual({ ecmwf_ifs: 100, icon_eu: 90 });
    expect(rows[1]?.values).toEqual({ ecmwf_ifs: 110, icon_eu: null }), "ICON-EU horizon ended — blank, never zero";
  });

  it("flags partial days with hours detail", () => {
    const models = [
      model("gfs_seamless", [
        { date: "2026-11-13", mwh: 60, hours_counted: 11, hours_missing: 0, hours_expected: 24, partial: true },
        { date: "2026-11-14", mwh: 5, hours_counted: 20, hours_missing: 4, hours_expected: 24, partial: true },
      ]),
    ];
    const rows = buildDailyMatrix(models);
    expect(rows[0]?.flags.gfs_seamless).toBe("djelomičan: 11/24 h");
    expect(rows[1]?.flags.gfs_seamless).toBe("djelomičan: 4 sat(i) nedostaju");
  });
});

describe("buildHourlyRows", () => {
  it("anchors to NEXT full hour from shared as-of; issued 00:00, now 13:20 → future hours only", () => {
    const pts = [
      { valid_at_utc: "2026-11-13T00:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, interval_hours: 1, status: "ok" },
      { valid_at_utc: "2026-11-13T14:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, interval_hours: 1, status: "ok" },
      { valid_at_utc: "2026-11-13T15:00:00Z", wind_speed_mps: null, modeled_power_mw: null, interval_energy_mwh: null, interval_hours: 1, status: "missing-height" },
      { valid_at_utc: "2026-11-13T16:00:00Z", wind_speed_mps: 9, modeled_power_mw: 22, interval_energy_mwh: 22, interval_hours: 1, status: "ok" },
    ];
    // anchor (generated_at) 13:20Z → first row is the next full hour 14:00Z;
    // the 00:00Z row is in the past and must never appear
    const rows = buildHourlyRows(pts, "2026-11-13T13:20:00Z", 24, "Europe/Podgorica");
    expect(rows).toHaveLength(24);
    expect(rows[0]?.label).toBe("11-13 15:00");
    expect(rows[0]?.missing).toBe(false);
    expect(rows.some((r) => r.label.startsWith("11-13 00:00"))).toBe(false);
    const missingRow = rows.find((r) => r.label === "11-13 16:00"); // 15:00Z = 16:00 CET
    expect(missingRow?.missing).toBe(true);
    expect(missingRow?.mw).toBeNull();
  });

  it("hour grid is complete: absent model hours become explicit missing rows", () => {
    const pts = [
      { valid_at_utc: "2026-11-13T14:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, interval_hours: 1, status: "ok" },
      { valid_at_utc: "2026-11-13T16:00:00Z", wind_speed_mps: 9, modeled_power_mw: 22, interval_energy_mwh: 22, interval_hours: 1, status: "ok" },
    ]; // 15:00 absent → explicit gap row
    const rows = buildHourlyRows(pts, "2026-11-13T13:20:00Z", 24, "Europe/Podgorica");
    expect(rows).toHaveLength(24);
    const gap = rows.find((r) => r.label === "11-13 16:00");
    expect(gap?.missing).toBe(true);
    expect(gap?.wind).toBeNull();
  });

  it("repeated fall-back 02:00 local hours are distinguishable by UTC offset", () => {
    // Europe/Podgorica fall-back 2026-10-25: 02:00 occurs at 00:00Z (CEST, UTC+2)
    // and 01:00Z (CET, UTC+1)
    const pts = [
      { valid_at_utc: "2026-10-25T00:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, interval_hours: 1, status: "ok" },
      { valid_at_utc: "2026-10-25T01:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, interval_hours: 1, status: "ok" },
    ];
    const rows = buildHourlyRows(pts, "2026-10-25T00:00:00Z", 24, "Europe/Podgorica");
    const both = rows.filter((r) => r.label === "10-25 02:00");
    expect(both).toHaveLength(2);
    expect(both[0]?.offset).toBe("UTC+2"); // first occurrence: CEST
    expect(both[1]?.offset).toBe("UTC+1"); // second occurrence: CET
  });

  it("non-1h intervals expose their duration", () => {
    const pts = [{ valid_at_utc: "2026-11-13T14:00:00Z", wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 40, interval_hours: 2, status: "ok" }];
    const rows = buildHourlyRows(pts, "2026-11-13T13:20:00Z", 24, "Europe/Podgorica");
    expect(rows[0]?.interval_hours).toBe(2);
  });
});

describe("MODEL_LABELS_HR", () => {
  it("covers all seven registered models", () => {
    expect(Object.keys(MODEL_LABELS_HR).sort()).toEqual([
      "ecmwf_ifs",
      "gem_seamless",
      "gfs_seamless",
      "icon_eu",
      "icon_global",
      "meteofrance_arpege_world",
      "ukmo_seamless",
    ]);
  });
});

describe("modelTotal (Addendum B — UKUPNO red)", () => {
  const rows = buildDailyMatrix([
    model("gfs_seamless", [
      { date: "2026-11-13", mwh: 100.25, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false },
      { date: "2026-11-14", mwh: 110.5, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false },
    ]),
    model("icon_eu", [{ date: "2026-11-13", mwh: 90, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }]),
  ]);

  it("sums the shown per-day MWh exactly", () => {
    expect(modelTotal(rows, "gfs_seamless")).toEqual({ total: 210.75, partial: false, days: 2 });
  });

  it("a blank (iza horizonta) day contributes nothing — never zero", () => {
    // icon_eu shows only 11-13; its UKUPNO is 90, not 90 + 0 for 11-14
    expect(modelTotal(rows, "icon_eu")).toEqual({ total: 90, partial: false, days: 1 });
  });

  it("a partial day is included and marks the total with ⚠", () => {
    const partialRows = buildDailyMatrix([
      model("gfs_seamless", [
        { date: "2026-11-13", mwh: 60, hours_counted: 11, hours_missing: 0, hours_expected: 24, partial: true },
        { date: "2026-11-14", mwh: 100, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false },
      ]),
    ]);
    expect(modelTotal(partialRows, "gfs_seamless")).toEqual({ total: 160, partial: true, days: 2 });
  });

  it("all-blank model → null total, not 0", () => {
    const none = buildDailyMatrix([model("ecmwf_ifs", [])]);
    expect(modelTotal(none, "ecmwf_ifs")).toEqual({ total: null, partial: false, days: 0 });
  });
});

describe("rowAverage (Average kolona)", () => {
  it("arithmetic mean over production models with values", () => {
    expect(rowAverage({ gfs_seamless: 100, icon_eu: 90, ecmwf_ifs: 110 }, ["gfs_seamless", "icon_eu", "ecmwf_ifs"])).toBe(100);
  });

  it("models without a value in the row are skipped, not zero-filled", () => {
    expect(rowAverage({ gfs_seamless: 100, icon_eu: null }, ["gfs_seamless", "icon_eu"])).toBe(100);
  });

  it("no values at all → null", () => {
    expect(rowAverage({ gfs_seamless: null }, ["gfs_seamless"])).toBeNull();
  });

  it("productionModelIds drops met_only models and models without a run", () => {
    const models = [
      { model_id: "ecmwf_ifs", met_only: false, run: {} },
      { model_id: "ukmo_seamless", met_only: true, run: {} },
      { model_id: "gfs_seamless", met_only: false, run: null },
      { model_id: "icon_eu", met_only: false, run: {} },
    ];
    expect(productionModelIds(models)).toEqual(["ecmwf_ifs", "icon_eu"]);
  });
});
