/** Correction E Addendum A — pure helpers: same-UTC-hour alignment, missing ≠
 *  0, MW/MWh arithmetic, adjusted no-fallback (caller contract), Prosjek
 *  contributors. */

import { describe, expect, it } from "vitest";

import { buildCompareRows, compareAverage, type CompareInput } from "./compareTable";
import { loadChartVisibility, saveChartVisibility } from "./chartVisibility";

const ANCHOR = "2026-11-18T05:20:00Z"; // → grid starts 06:00Z = 07:00 CET

function input(model: string, pts: [string, number | null, number][]): CompareInput {
  return { model_id: model, points: pts.map(([t, v, ih]) => ({ valid_at_utc: t, value: v, interval_hours: ih })) };
}

describe("buildCompareRows (Addendum A §2)", () => {
  const a = input("gfs_seamless", [["2026-11-18T06:00:00Z", 10, 1], ["2026-11-18T07:00:00Z", 11, 1]]);
  const b = input("ecmwf_ifs", [["2026-11-18T06:00:00Z", 20, 1]]);

  it("aligns the SAME UTC hour across providers; labels in site tz", () => {
    const rows = buildCompareRows([a, b], ANCHOR, 24, "Europe/Podgorica");
    expect(rows[0]?.key).toBe("2026-11-18T06");
    expect(rows[0]?.label).toBe("11-18 07:00");
    expect(rows[0]?.offset).toBe("UTC+1");
    expect(rows[0]?.values).toEqual({ gfs_seamless: 10, ecmwf_ifs: 20 });
    expect(rows[1]?.values.ecmwf_ifs).toBeNull(); // missing hour — never 0
    expect(rows).toHaveLength(24);
  });

  it("CET labels across the pre-DST night (spring shift happens 01:00Z)", () => {
    const night = input("gfs_seamless", [["2026-03-28T22:00:00Z", 5, 1], ["2026-03-28T23:00:00Z", 5, 1], ["2026-03-29T00:00:00Z", 5, 1]]);
    const rows = buildCompareRows([night], "2026-03-28T21:30:00Z", 3, "Europe/Podgorica");
    // all three instants are still CET (UTC+1) — the 2026 spring shift starts
    // 2026-03-29T01:00Z; labels follow the true local clock, never a fixed +1
    expect(rows.map((r) => r.label)).toEqual(["03-28 23:00", "03-29 00:00", "03-29 01:00"]);
    expect(rows.every((r) => r.offset === "UTC+1")).toBe(true);
  });

  it("CEST label after the shift (01:00Z → 03:00 local, UTC+2)", () => {
    const after = input("gfs_seamless", [["2026-03-29T01:00:00Z", 5, 1], ["2026-03-29T02:00:00Z", 5, 1]]);
    const rows = buildCompareRows([after], "2026-03-29T00:30:00Z", 2, "Europe/Podgorica");
    expect(rows[0]?.label).toBe("03-29 03:00");
    expect(rows[0]?.offset).toBe("UTC+2");
  });

  it("MW vs MWh arithmetic is the CALLER's mapping (3 h @ 10 MW = 30 MWh)", () => {
    const power = input("gfs_seamless", [["2026-11-18T06:00:00Z", 10, 3]]);
    const mwRows = buildCompareRows([power], ANCHOR, 1, "Europe/Podgorica");
    expect(mwRows[0]?.values.gfs_seamless).toBe(10);
    const mwh = input("gfs_seamless", [["2026-11-18T06:00:00Z", 30, 3]]);
    const mwhRows = buildCompareRows([mwh], ANCHOR, 1, "Europe/Podgorica");
    expect(mwhRows[0]?.values.gfs_seamless).toBe(30);
  });

  it("adjusted-unavailable model passed with EMPTY points → all null (no fallback)", () => {
    const gone = input("gem_seamless", []);
    const rows = buildCompareRows([gone], ANCHOR, 2, "Europe/Podgorica");
    expect(rows[0]?.values.gem_seamless).toBeNull();
    expect(rows[1]?.values.gem_seamless).toBeNull();
  });

  it("genuine 0 MWh (below cut-in) preserved, not turned into —", () => {
    const zero = input("gfs_seamless", [["2026-11-18T06:00:00Z", 0, 1]]);
    const rows = buildCompareRows([zero], ANCHOR, 1, "Europe/Podgorica");
    expect(rows[0]?.values.gfs_seamless).toBe(0);
  });
});

describe("compareAverage (Addendum A §2, production Prosjek)", () => {
  const row = buildCompareRows(
    [
      input("gfs_seamless", [["2026-11-18T06:00:00Z", 10, 1]]),
      input("ecmwf_ifs", [["2026-11-18T06:00:00Z", 20, 1]]),
      input("ukmo_seamless", [["2026-11-18T06:00:00Z", 999, 1]]), // met-only must be excluded by the set
    ],
    ANCHOR,
    1,
    "Europe/Podgorica",
  )[0]!;

  it("mean over the SELECTED production set only; met-only id excluded by the caller-set contract", () => {
    // the view passes productionModelIds()/avgModels (met-only already
    // filtered); the helper averages exactly the ids it is given
    expect(compareAverage(row, ["gfs_seamless", "ecmwf_ifs"])).toEqual({ value: 15, contributors: 2 });
    expect(compareAverage(row, ["ukmo_seamless"])).toEqual({ value: 999, contributors: 1 }), "helper is set-faithful — exclusion lives in productionModelIds (tested)";
  });

  it("no contributor → null, never 0", () => {
    const empty = buildCompareRows([input("gfs_seamless", [])], ANCHOR, 1, "Europe/Podgorica")[0]!;
    expect(compareAverage(empty, ["gfs_seamless"])).toEqual({ value: null, contributors: 0 });
  });

  it("subset coverage counts contributors truthfully", () => {
    expect(compareAverage(row, ["ecmwf_ifs"])).toEqual({ value: 20, contributors: 1 });
  });
});

describe("chartVisibility persistence (Addendum A §1)", () => {
  it("first run on WIND defaults UKMO OFF; power chart has no met-only defaults", () => {
    expect(loadChartVisibility("wind", "krnovo", ["gfs_seamless", "ukmo_seamless"], ["ukmo_seamless"]).hidden).toEqual(["ukmo_seamless"]);
    expect(loadChartVisibility("power", "krnovo", ["gfs_seamless"], []).hidden).toEqual([]);
  });

  it("explicit user choice persists and filters against current model ids", () => {
    saveChartVisibility("wind", "s1", { hidden: ["gfs_seamless", "ukmo_seamless"], configured: true });
    const vis = loadChartVisibility("wind", "s1", ["gfs_seamless", "ecmwf_ifs"], ["ukmo_seamless"]);
    expect(vis.configured).toBe(true);
    expect(vis.hidden).toEqual(["gfs_seamless"]);
  });

  it("site isolation: krnovo choices never leak to another site", () => {
    saveChartVisibility("wind", "site-a", { hidden: ["gfs_seamless"], configured: true });
    expect(loadChartVisibility("wind", "site-b", ["gfs_seamless", "ukmo_seamless"], ["ukmo_seamless"]).hidden).toEqual(["ukmo_seamless"]);
    expect(loadChartVisibility("wind", "site-a", ["gfs_seamless"]).hidden).toEqual(["gfs_seamless"]);
  });

  it("chart keys are independent (wind choice ≠ power choice)", () => {
    saveChartVisibility("wind", "s2", { hidden: ["gfs_seamless"], configured: true });
    expect(loadChartVisibility("power", "s2", ["gfs_seamless"], []).hidden).toEqual([]);
  });

  it("temporarily empty model ids never wipe the stored selection", () => {
    saveChartVisibility("wind", "s3", { hidden: ["ecmwf_ifs"], configured: true });
    expect(loadChartVisibility("wind", "s3", [], []).hidden).toEqual(["ecmwf_ifs"]);
  });

  it("toggling a legend never requires a new model availability change (GEM repair case)", () => {
    // GEM absent today: hidden list without gem; GEM appears later → active
    saveChartVisibility("wind", "s4", { hidden: [], configured: true });
    const later = loadChartVisibility("wind", "s4", ["gfs_seamless", "gem_seamless"], []);
    expect(later.hidden).toEqual([]); // no stale hidden entry suppresses repaired GEM
  });
});
