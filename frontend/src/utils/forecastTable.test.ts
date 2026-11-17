import { describe, expect, it } from "vitest";

import { buildDailyMatrix, buildHourlyRows, MODEL_LABELS_HR, type DailySlot } from "./forecastTable";

const T = (day: number, h: number) => `2026-11-1${day}T${String(h).padStart(2, "0")}:00:00Z`;

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
  it("keeps 24/48 h window, null gaps stay null, out-of-window cut", () => {
    const now = "2026-11-13T12:00:00Z";
    const pts = [
      { valid_at_utc: T(3, 12), wind_speed_mps: 8, modeled_power_mw: 20, interval_energy_mwh: 20, status: "ok" },
      { valid_at_utc: T(3, 13), wind_speed_mps: null, modeled_power_mw: null, interval_energy_mwh: null, status: "missing-height" },
      { valid_at_utc: T(3, 14), wind_speed_mps: 9, modeled_power_mw: 22, interval_energy_mwh: 22, status: "ok" },
      { valid_at_utc: T(3, 20), wind_speed_mps: 7, modeled_power_mw: 15, interval_energy_mwh: 15, status: "ok" }, // beyond 24h? 12+8h → inside
    ];
    const rows24 = buildHourlyRows(pts, now, 24);
    expect(rows24).toHaveLength(4); // all points within +24 h (last is +8 h)
    expect(rows24[0]?.cetLabel).toBe("11-13 13:00 CET");
    expect(rows24[1]?.missing).toBe(true);
    expect(rows24[1]?.mw).toBeNull();

    const rows48 = buildHourlyRows(pts, now, 48);
    expect(rows48).toHaveLength(4);
    expect(rows48[3]?.cetLabel).toBe("11-13 21:00 CET");
  });

  it("CET label = UTC + 1 h fixed", () => {
    const pts = [{ valid_at_utc: "2026-11-13T23:00:00Z", wind_speed_mps: 5, modeled_power_mw: 10, interval_energy_mwh: 10, status: "ok" }];
    const rows = buildHourlyRows(pts, "2026-11-13T23:00:00Z", 24);
    expect(rows[0]?.cetLabel.startsWith("11-14 00:00")).toBe(true);
  });
});

describe("MODEL_LABELS_HR", () => {
  it("covers all four registered models", () => {
    expect(Object.keys(MODEL_LABELS_HR).sort()).toEqual(["ecmwf_ifs", "gfs_seamless", "icon_eu", "icon_global"]);
  });
});
