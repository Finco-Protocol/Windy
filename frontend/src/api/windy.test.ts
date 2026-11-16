import { describe, expect, it } from "vitest";

import { hhmm, modelColor } from "./windy";

// Focused unit tests for the Windy API helpers. The heavy lifting (archive
// dedupe, height resolution, power curve, DST imports, accuracy) is covered by
// the backend pytest suite in ../backend/tests.

describe("hhmm", () => {
  it("extracts the UTC HH:MM part of an ISO Z timestamp", () => {
    expect(hhmm("2026-11-15T18:00:00Z")).toBe("18:00");
    expect(hhmm("2026-01-01T00:30:00Z")).toBe("00:30");
  });

  it("returns an empty slice for non-timestamp strings", () => {
    expect(hhmm("not-a-date")).toBe("");
  });
});

describe("modelColor", () => {
  it("maps every registered model to a stable color", () => {
    for (const id of ["ecmwf_ifs", "gfs_seamless", "icon_global", "icon_eu"]) {
      expect(modelColor(id)).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it("falls back to the neutral ink color for unknown ids", () => {
    expect(modelColor("future_model")).toBe("#93896f");
  });
});
