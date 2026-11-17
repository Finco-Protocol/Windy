import { describe, expect, it } from "vitest";

import { buildDailyMatrix, buildEnsembleDaily, buildHourlyRows, loadAvgPreference, MODEL_LABELS_HR, modelTotal, productionModelIds, rowAverage, saveAvgPreference, visibleDailyRows, type DailySlot } from "./forecastTable";

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

describe("buildEnsembleDaily (Correction A §5)", () => {
  const pts = (model: string, hours: [string, number, number][]) => ({
    model_id: model,
    points: hours.map(([t, mw, ih]) => ({ valid_at_utc: t, mw, interval_hours: ih })),
  });

  it("equal coverage: daily ensemble = mean of per-model hourly energy", () => {
    const a = pts("gfs_seamless", [["2026-11-16T00:00:00Z", 10, 1], ["2026-11-16T01:00:00Z", 10, 1]]);
    const b = pts("ecmwf_ifs", [["2026-11-16T00:00:00Z", 20, 1], ["2026-11-16T01:00:00Z", 20, 1]]);
    const rows = buildEnsembleDaily([a, b], "Europe/Podgorica");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.date).toBe("2026-11-16"); // 01:00Z is still 01:00 local (CET)
    expect(rows[0]?.mwh).toBe(30); // avg 15 MW x 2 h
    expect(rows[0]?.contributors_min).toBe(2);
  });

  it("3 h intervals conserve energy: 10 MW x 3 h = 30 MWh across a day boundary", () => {
    const a = pts("gfs_seamless", [["2026-11-16T22:00:00Z", 10, 3]]); // 1 h D0 + 2 h D1 (midnight 23:00Z)
    const rows = buildEnsembleDaily([a], "Europe/Podgorica");
    const byDate = Object.fromEntries(rows.map((r) => [r.date, r]));
    expect(byDate["2026-11-16"]?.mwh).toBe(10);
    expect(byDate["2026-11-17"]?.mwh).toBe(20);
  });

  it("missing model hour drops contributor count and is flagged, never zero-filled", () => {
    const a = pts("gfs_seamless", [["2026-11-16T00:00:00Z", 10, 1], ["2026-11-16T01:00:00Z", 10, 1]]);
    const b = pts("ecmwf_ifs", [["2026-11-16T00:00:00Z", 30, 1]]); // no 01:00 point
    const rows = buildEnsembleDaily([a, b], "Europe/Podgorica");
    expect(rows[0]?.mwh).toBe(20 + 10); // 00: avg 20; 01: avg 10 (1 contributor)
    expect(rows[0]?.contributors_min).toBe(1);
    expect(rows[0]?.contributors_max).toBe(2);
  });

  it("DST spring day expected hours = 23", () => {
    const t0ms = Date.parse("2026-03-28T23:00:00Z");
    const hours = Array.from({ length: 23 }, (_, i) => [new Date(t0ms + i * 3_600_000).toISOString(), 5, 1] as [string, number, number]);
    const a = pts("gfs_seamless", hours);
    const rows = buildEnsembleDaily([a], "Europe/Podgorica");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.date).toBe("2026-03-29");
    expect(rows[0]?.hours_expected).toBe(23);
    expect(rows[0]?.hours_counted).toBe(23);
    expect(rows[0]?.partial).toBe(false);
  });

  it("met-only and unselected models are excluded by the caller's input set", () => {
    const b = pts("gfs_seamless", [["2026-11-16T00:00:00Z", 10, 1]]);
    const rows = buildEnsembleDaily([b], "Europe/Podgorica"); // met-only excluded by the caller
    expect(rows[0]?.mwh).toBe(10);
  });
});

describe("visibleDailyRows (Correction A §5)", () => {
  const rows = Array.from({ length: 12 }, (_, i) => ({ date: `2026-11-${String(i + 1).padStart(2, "0")}`, v: i }));
  it("anchors at today in site tz and returns exactly N rows", () => {
    const vis = visibleDailyRows(rows, 7, "Europe/Podgorica", "2026-11-05T10:00:00Z");
    expect(vis).toHaveLength(7);
    expect(vis[0]?.date).toBe("2026-11-05");
    expect(vis[6]?.date).toBe("2026-11-11");
  });
  it("no row on/after today -> the LAST N rows (never an N+1-th date)", () => {
    const vis = visibleDailyRows(rows, 7, "Europe/Podgorica", "2026-12-01T10:00:00Z");
    expect(vis).toHaveLength(7);
    expect(vis[0]?.date).toBe("2026-11-06");
    expect(vis[6]?.date).toBe("2026-11-12");
  });
});

describe("avg preference persistence per site (Correction B section 3)", () => {
  it("round-trips and filters against current production ids", () => {
    saveAvgPreference("s1", { enabled: false, configured: true, models: ["gfs_seamless", "ukmo_seamless"] });
    const pref = loadAvgPreference("s1", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.enabled).toBe(false);
    expect(pref.configured).toBe(true);
    expect(pref.models).toEqual(["gfs_seamless"]); // met-only filtered out
  });

  it("EXPLICIT empty selection stays empty (never silently re-enables all)", () => {
    saveAvgPreference("s-none", { enabled: true, configured: true, models: [] });
    const pref = loadAvgPreference("s-none", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.configured).toBe(true);
    expect(pref.models).toEqual([]);
  });

  it("never-configured site defaults to all production models", () => {
    const pref = loadAvgPreference("s-fresh", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.configured).toBe(false);
    expect(pref.enabled).toBe(true);
    expect(pref.models).toEqual(["gfs_seamless", "ecmwf_ifs"]);
  });

  it("Correction C: explicit configured:false + models[] -> default ALL production ids", () => {
    saveAvgPreference("c-false", { enabled: true, configured: false, models: [] });
    const pref = loadAvgPreference("c-false", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.configured).toBe(false);
    expect(pref.models).toEqual(["gfs_seamless", "ecmwf_ifs"]); // default all, NOT the empty list
  });

  it("Correction C: explicit configured:true + models[] stays EMPTY (no silent re-enable)", () => {
    saveAvgPreference("c-true", { enabled: true, configured: true, models: [] });
    const pref = loadAvgPreference("c-true", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.configured).toBe(true);
    expect(pref.models).toEqual([]);
  });

  it("pre-B stored format (no configured flag) migrates to configured=true", () => {
    // simulate a pre-B record: models list present, configured field ABSENT
    // (undefined is dropped by JSON.stringify) — persisted through the same
    // storage path the app uses
    saveAvgPreference("s-old", { enabled: true, configured: undefined as unknown as boolean, models: ["ecmwf_ifs"] });
    const pref = loadAvgPreference("s-old", ["gfs_seamless", "ecmwf_ifs"]);
    expect(pref.configured).toBe(true);
    expect(pref.models).toEqual(["ecmwf_ifs"]);
  });

  it("temporarily empty productionIds keep the stored selection (no wipe while loading)", () => {
    saveAvgPreference("s-keep", { enabled: true, configured: true, models: ["ecmwf_ifs"] });
    expect(loadAvgPreference("s-keep", []).models).toEqual(["ecmwf_ifs"]);
  });

  it("site switching is isolated", () => {
    saveAvgPreference("sw-a", { enabled: true, configured: true, models: ["gfs_seamless"] });
    saveAvgPreference("sw-b", { enabled: true, configured: true, models: [] });
    expect(loadAvgPreference("sw-a", ["gfs_seamless", "icon_eu"]).models).toEqual(["gfs_seamless"]);
    expect(loadAvgPreference("sw-b", ["gfs_seamless", "icon_eu"]).models).toEqual([]);
  });
});
