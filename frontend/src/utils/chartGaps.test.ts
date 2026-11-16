import { describe, expect, it } from "vitest";

import { cumulativeOverComputed, scenarioSeries } from "./chartGaps";

const T = (h: number) => `2026-11-13T${String(h).padStart(2, "0")}:00:00Z`;

describe("scenarioSeries", () => {
  it("keeps ALL timestamps including null gaps", () => {
    const points = [
      { valid_at_utc: T(0), base_mw: 10, adjusted_mw: 8 },
      { valid_at_utc: T(1), base_mw: null, adjusted_mw: null }, // missing wind
      { valid_at_utc: T(2), base_mw: 12, adjusted_mw: 9 },
    ];
    const s = scenarioSeries(points);
    expect(s.base).toHaveLength(3);
    expect(s.base[1]).toEqual([T(1), null]);
    expect(s.adjusted[1]).toEqual([T(1), null]);
    expect(s.base[2]).toEqual([T(2), 12]);
  });

  it("legitimate zero production is a real 0, not a null", () => {
    const points = [{ valid_at_utc: T(0), base_mw: 0, adjusted_mw: 0 }];
    expect(scenarioSeries(points).base[0]).toEqual([T(0), 0]);
  });
});

describe("cumulativeOverComputed", () => {
  it("accumulates computed hours and breaks to null after a missing one", () => {
    const pts = [
      { valid_at_utc: T(0), interval_energy_mwh: 10 },
      { valid_at_utc: T(1), interval_energy_mwh: 10 },
      { valid_at_utc: T(2), interval_energy_mwh: null }, // missing
      { valid_at_utc: T(3), interval_energy_mwh: 5 },
    ];
    const cum = cumulativeOverComputed(pts);
    expect(cum[0]).toEqual([T(0), 10]);
    expect(cum[1]).toEqual([T(1), 20]);
    expect(cum[2]).toEqual([T(2), null]), "gap breaks the trajectory";
    expect(cum[3]).toEqual([T(3), null]), "after a gap the full trajectory is unknown";
  });

  it("never bridges gaps with carried-forward totals", () => {
    const pts = [
      { valid_at_utc: T(0), interval_energy_mwh: 50 },
      { valid_at_utc: T(1), interval_energy_mwh: null },
      { valid_at_utc: T(2), interval_energy_mwh: 3 },
    ];
    const cum = cumulativeOverComputed(pts);
    // no point after the gap may carry a pre-gap total (50) or a silently
    // resumed one (53) — the honest representation is a visible break
    expect(cum.map((c) => c[1])).toEqual([50, null, null]);
  });
});
