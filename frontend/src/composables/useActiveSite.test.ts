import { describe, expect, it } from "vitest";

import { buildNavLinks } from "./useActiveSite";

// Executed tests for the navbar site-continuity logic: every tab link must
// carry the active `?site=` query so the selection survives navigation.

describe("buildNavLinks", () => {
  it("attaches the non-default site query to every app tab", () => {
    const links = buildNavLinks("site-b");
    for (const l of links) {
      if (l.path === "/sites") {
        expect(l.query.site).toBeUndefined(); // the registry itself is global
      } else {
        expect(l.query.site).toBe("site-b");
      }
    }
    expect(links.map((l) => l.path)).toEqual(["/", "/detailed-forecast", "/history", "/actuals", "/availability", "/sites"]);
  });

  it("omits the query for the default site (canonical URL)", () => {
    for (const l of buildNavLinks("krnovo")) {
      expect(l.query.site).toBeUndefined();
    }
  });
});
