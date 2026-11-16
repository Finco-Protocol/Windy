import { describe, expect, it } from "vitest";

import { localToUtc, nowLocalForTz } from "./availability";

// Executed (vitest) tests for the availability UI helpers — local→UTC
// conversion with DST semantics matching the backend importer, and the
// dynamic default start time. Component wiring is covered by the backend
// API regression tests + a manual browser smoke noted in the handoff.

describe("localToUtc", () => {
  it("converts CET winter time (UTC+1) for Europe/Podgorica", () => {
    expect(localToUtc("2026-11-13T00:00", "Europe/Podgorica").utc).toBe("2026-11-12T23:00:00Z");
  });

  it("converts CEST summer time (UTC+2)", () => {
    expect(localToUtc("2026-06-15T12:00", "Europe/Podgorica").utc).toBe("2026-06-15T10:00:00Z");
  });

  it("rejects nonexistent spring-forward local times explicitly", () => {
    // 2026-03-29 02:30 does not exist in Europe/Podgorica (02:00 → 03:00)
    const res = localToUtc("2026-03-29T02:30", "Europe/Podgorica");
    expect(res.error).toContain("does not exist");
    expect(res.utc).toBeFalsy();
  });

  it("resolves ambiguous fall-back times to the FIRST occurrence with a warning", () => {
    // 2026-10-25 02:30 occurs twice (CEST then CET)
    const res = localToUtc("2026-10-25T02:30", "Europe/Podgorica");
    expect(res.utc).toBe("2026-10-25T00:30:00Z"); // CEST (UTC+2) = first occurrence
    expect(res.warning).toContain("FIRST occurrence");
  });

  it("rejects malformed input", () => {
    expect(localToUtc("not-a-time", "Europe/Podgorica").error).toBeDefined();
    expect(localToUtc("2026-11-13T00:00", "Mars/Olympus").error).toBeDefined();
  });
});

describe("nowLocalForTz", () => {
  it("returns a parseable wall-clock string in the requested timezone", () => {
    const now = nowLocalForTz("Europe/Podgorica");
    expect(now).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    const parsed = new Date(`${now}:00Z`);
    expect(Number.isNaN(parsed.getTime())).toBe(false);
  });

  it("differs across distant timezones near the date line", () => {
    // same instant, different wall clocks (allow equality only if the offset
    // difference is exactly zero hours at that instant — never for these two)
    expect(nowLocalForTz("Pacific/Kiritimati")).not.toBe(nowLocalForTz("Pacific/Midway"));
  });
});
