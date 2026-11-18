// @vitest-environment jsdom
/** Separate Detailed forecast (26 WTG) screen: no 26-WTG matrix on Forecast. */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";

import WindyDetailedForecastView from "./WindyDetailedForecastView.vue";
import { buildNavLinks } from "@/composables/useActiveSite";

const DAY = "2026-11-18";
const spatial = {
  site_id: "krnovo",
  mode: "per_turbine" as const,
  days: 7,
  location_info: {
    location_source: "owner-file-provisional", location_revision: 0,
    provisional_mapping: true, virtual_inventory: false,
    inventory_revision: 1, n_turbines: 26,
  },
  note: "NWP grid data, not measured; WTG mapping provisional",
  models: [{
    model_id: "ecmwf_ifs", label: "ECMWF IFS",
    status: "OK", met_only: false, issue_utc: "2026-11-18T00:00:00Z",
    grid_cells: 8, warning: "Modelled wind at 26 coordinates",
    daily: [{ date: DAY, base_mwh: 260, adjusted_mwh: 234,
      hours_counted: 24, missing_intervals: 0, n_turbines: 26, partial: false }],
    turbines: Array.from({ length: 26 }, (_, i) => ({
      unit_id: `WTG-${String(i + 1).padStart(3, "0")}`,
      latitude: 42.87 + i * .0001, longitude: 19.11 + i * .0001, elevation_m: 1520 + i,
      daily: [{ date: DAY, base_mwh: 10, adjusted_mwh_pre_export_cap: 9,
        wind_mean_mps: 8 + i / 10, hours: 24 }],
    })),
  }],
};

const site = {
  id: "krnovo", name: "Krnovo Wind Farm", country: "Montenegro",
  latitude: 42.8756, longitude: 19.1106, capacity_mw: 74.1,
  turbine_count: 26, turbine_rated_mw: 2.85, hub_height_m: 90,
  loss_factor: 0, timezone: "Europe/Podgorica", turbine_model: "GE",
  assumptions: {}, notes: "", power_curve_id: "ge103-2750-oedb",
};

const calls: { path: string; method: string }[] = [];
beforeEach(() => {
  document.body.innerHTML = "";
  calls.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (req: RequestInfo | URL, opts?: RequestInit): Promise<Response> => {
    const url = typeof req === "string" || req instanceof URL ? String(req) : req.url;
    const path = url.split("?")[0]!;
    const method = opts?.method ?? "GET";
    calls.push({path, method});
    let payload: unknown = {};
    if (path === "/api/site") payload = {
      site, power_curve: {id: "ge103-2750-oedb", label: "GE", status: "reference", provenance: "synthetic",
        rated_power_mw: 2.78}, models: [], source_provider: "open-meteo", source_note: "synthetic",
    };
    else if (path === "/api/assets") payload = {assets: [site]};
    else if (path === "/api/turbine-forecast") payload = spatial;
    else if (path === "/api/collect/turbines") payload = {
      spot: {collected_at_utc: "2026-11-18T01:00:00Z"},
      per_turbine: {models: [{model_id: "ecmwf_ifs", status: "ALREADY_ARCHIVED"}]},
    };
    return new Response(JSON.stringify(payload), {
      status: 200, headers: {"Content-Type": "application/json"},
    });
  }));
});
afterEach(() => {vi.unstubAllGlobals();document.body.innerHTML = "";});

async function mountDetail(query: Record<string, string> = {}) {
  const router = createRouter({history: createMemoryHistory(), routes: [
    {path: "/", name: "forecast", component: {template: "<div />"}},
    {path: "/detailed-forecast", name: "detailed-forecast", component: {template: "<div />"}},
  ]});
  await router.push({path: "/detailed-forecast", query});
  await router.isReady();
  const host = document.createElement("div");
  document.body.appendChild(host);
  const app = createApp(WindyDetailedForecastView);
  app.use(router);
  app.mount(host);
  return {router, app};
}
async function settle(predicate: () => boolean) {
  for (let attempt=0; attempt<140; attempt++) {
    await nextTick();
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("Detailed forecast did not render: " + (document.body.textContent??"").slice(0,350));
}

describe("Detailed forecast — 26 WTG drilldown", () => {
  it("shows 26 WTG with per-location MWh, model selector and no implicit collection", async () => {
    const {app} = await mountDetail();
    await settle(() => (document.body.textContent??"").includes("WTG-026"));
    const txt = document.body.textContent ?? "";
    expect(txt).toContain("Detailed forecast (26 WTG)");
    expect(txt).toContain("WTG-001");
    expect(txt).toContain("WTG-026");
    expect(txt).toContain("ECMWF IFS");
    expect(txt).toContain("234");
    expect(document.body.querySelectorAll('[aria-label="Prognoza po turbini"]').length).toBe(1);
    expect(calls.filter(c=>c.path==="/api/turbine-forecast"&&c.method==="GET")).toHaveLength(1);
    expect(calls.filter(c=>c.method==="POST")).toHaveLength(0);
    expect(document.body.textContent).toContain("NWP");
    app.unmount();
  }, 20000);

  it("mode + unit toggles use same snapshot, 10/15 days load on demand, site stays in links", async () => {
    const {app} = await mountDetail({site: "site-b", days: "10"});
    await settle(() => (document.body.textContent??"").includes("WTG-026"));
    const back = [...document.body.querySelectorAll("a")].find(a=>a.textContent?.includes("Forecast"));
    expect(back?.getAttribute("href")).toContain("site=site-b");
    const buttons = [...document.body.querySelectorAll("button")];
    const adjusted = buttons.find(x=>x.textContent?.trim()==="Prilagođeno");
    expect(adjusted).toBeTruthy();
    const before = calls.filter(c=>c.path==="/api/turbine-forecast").length;
    adjusted!.click();
    await nextTick();
    expect(calls.filter(c=>c.path==="/api/turbine-forecast")).toHaveLength(before);
    expect(document.body.textContent).toContain("9");
    const fifteen = buttons.find(x=>x.textContent?.trim()==="15 dana");
    fifteen!.click();
    await settle(()=>calls.filter(c=>c.path==="/api/turbine-forecast").length>before);
    expect(calls.every(c=>c.method==="GET")).toBe(true);
    app.unmount();
  }, 20000);

  it("nav exposes a dedicated tab and preserves active site", () => {
    expect(buildNavLinks("krnovo").find(l=>l.path==="/detailed-forecast")?.label).toContain("26 WTG");
    expect(buildNavLinks("another-site").find(l=>l.path==="/detailed-forecast")?.query.site).toBe("another-site");
  });
});
