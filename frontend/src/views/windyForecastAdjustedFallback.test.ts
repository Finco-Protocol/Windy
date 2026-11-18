// @vitest-environment jsdom

/** PR#8 Correction B §1 — DOM regression: in the PRILAGODJENO view a model
 *  whose adjusted block is unavailable must show em-dash cells (never
 *  baseline numbers relabelled as adjusted), its UKUPNO stays null, and when
 *  NO model has a valid adjusted block the table says so loudly. The BAZNO
 *  tab still shows the baseline numbers. */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter, type Router } from "vue-router";

vi.mock("vue-echarts", () => ({ default: { name: "VChart", template: "<div />" } }));

import WindyForecastView from "./WindyForecastView.vue";

const SITE = {
  id: "krnovo",
  name: "Krnovo Wind Farm",
  country: "Montenegro",
  latitude: 42.8756,
  longitude: 19.1106,
  capacity_mw: 74.1,
  turbine_count: 26,
  turbine_model: "GE-klasa (provisional)",
  turbine_rated_mw: 2.85,
  hub_height_m: 90,
  loss_factor: 0,
  timezone: "Europe/Podgorica",
  assumptions: {},
  notes: "",
  power_curve_id: "ge103-2750-oedb",
};

function point(t: string, ws: number, mw: number) {
  return { valid_at_utc: t, wind_speed_mps: ws, wind_direction_deg: 180, height_method: "log-interp", modeled_power_mw: mw, interval_energy_mwh: mw, interval_hours: 1, status: "ok" };
}

const GFS_BASELINE_DAILY = [{ date: "2026-11-16", mwh: 123.4, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }];
const ECMWF_ADJUSTED_DAILY = [{ date: "2026-11-16", mwh: 77.7, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }];

function forecastPayload(mode: "baseline" | "adjusted") {
  const gfsAdjusted =
    mode === "adjusted"
      ? { available: false, reason: "no turbine inventory for this site - generate it first" }
      : undefined;
  return {
    site: SITE,
    power_curve: { id: "ge103-2750-oedb", label: "GE ref", status: "reference", provenance: "OEDB", rated_power_mw: 2.78, cut_in_mps: 3, rated_mps: 12, cut_out_mps: 25 },
    generated_at_utc: "2026-11-16T12:20:00Z",
    days: 7,
    operational: mode === "adjusted" ? { site_id: "krnovo", installed_mw: 0, available_max_mw: 0, offline_count: 0, curtailed_count: 0, farm_export_cap_mw: null, n_turbines: 0, inventory_revision: 0, constraints_revision: 0, inventory_present: false, n_turbines_inventory: 0 } : null,
    models: [
      {
        model_id: "gfs_seamless",
        provider: "NOAA",
        label: "GFS",
        met_only: false,
        height_method: "log-interp",
        height_sources_m: [80, 100],
        requested_height_m: 90,
        height_note: "test",
        run: { id: 1, run_issued_utc: "2026-11-16T00:00:00Z", issue_source: "single-runs-archive", fetched_at_utc: "2026-11-16T12:00:00Z", forecast_days: 16, source_url: "test" },
        points: Array.from({ length: 24 }, (_, h) => point(`2026-11-16T${String(h).padStart(2, "0")}:00:00Z`, 12, 5.14)),
        daily_local: GFS_BASELINE_DAILY,
        total_mwh_own_window: 123.4,
        coverage: { n_hours: 24, first_valid_utc: "2026-11-16T00:00:00Z", last_valid_utc: "2026-11-16T23:00:00Z", max_available_hours: 24 },
        warnings: [],
        ...(mode === "adjusted" ? { adjusted: gfsAdjusted } : {}),
      },
      {
        model_id: "ecmwf_ifs",
        provider: "ECMWF",
        label: "ECMWF",
        met_only: false,
        height_method: "proxy",
        height_sources_m: [100],
        requested_height_m: 90,
        height_note: "test",
        run: { id: 2, run_issued_utc: "2026-11-16T00:00:00Z", issue_source: "single-runs-archive", fetched_at_utc: "2026-11-16T12:00:00Z", forecast_days: 10, source_url: "test" },
        points: Array.from({ length: 24 }, (_, h) => point(`2026-11-16T${String(h).padStart(2, "0")}:00:00Z`, 12, 3.2)),
        daily_local: GFS_BASELINE_DAILY,
        total_mwh_own_window: 123.4,
        coverage: { n_hours: 24, first_valid_utc: "2026-11-16T00:00:00Z", last_valid_utc: "2026-11-16T23:00:00Z", max_available_hours: 24 },
        warnings: [],
        ...(mode === "adjusted"
          ? { adjusted: { available: true, inventory_revision: 1, constraints_revision: 1, baseline_comparable: true, baseline_comparable_reason: null, daily: ECMWF_ADJUSTED_DAILY, points: [] } }
          : {}),
      },
    ],
    intersection: { n_common_hours: 24, first_common_utc: "2026-11-16T00:00:00Z", last_common_utc: "2026-11-16T23:00:00Z", total_mwh: {}, note: "" },
  };
}

function makeFetch(_mode: "baseline" | "adjusted") {
  return async (url: string | URL | Request): Promise<Response> => {
    const u = typeof url === "string" ? url : url instanceof Request ? url.url : url.toString();
    const path = u.replace(/^https?:\/\/[^/]+/, "").split("?")[0]! + "?" + (u.includes("?") ? u.split("?")[1]! : "");
    let body: unknown = {};
    if (path.startsWith("/api/site")) body = { site: SITE, power_curve: { id: "ge103-2750-oedb", label: "GE ref", status: "reference", provenance: "OEDB", rated_power_mw: 2.78, cut_in_mps: 3, rated_mps: 12, cut_out_mps: 25 }, models: [], source_provider: "open-meteo", source_note: "" };
    else if (path.startsWith("/api/forecast")) body = forecastPayload(path.includes("with_availability=true") ? "adjusted" : "baseline");
    else if (path.startsWith("/api/assets/krnovo/status")) body = { site_id: "krnovo", installed_mw: 0, available_max_mw: 0, offline_count: 0, curtailed_count: 0, farm_export_cap_mw: null, n_turbines: 0, inventory_revision: 0, constraints_revision: 0, inventory_present: false, n_turbines_inventory: 0 };
    else if (path.startsWith("/api/assets/krnovo/turbines")) body = { site_id: "krnovo", revision: 0, n_turbines: 0, installed_mw: 0, turbines: [] };
    else if (path.startsWith("/api/assets/krnovo/constraints")) body = { site_id: "krnovo", revision: 0, constraints: [] };
    else if (path.startsWith("/api/assets/krnovo/wind-now")) body = { site_id: "krnovo", modeled: true, disclaimer: "test", generated_at_utc: "2026-11-16T12:00:00Z", models: [] };
    else if (path.startsWith("/api/assets")) body = { assets: [] };
    else body = {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  };
}

async function mountView(): Promise<{ router: Router; el: HTMLElement }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", name: "forecast", component: { template: "<div />" } },
      { path: "/history", name: "history", component: { template: "<div />" } },
      { path: "/actuals", name: "actuals", component: { template: "<div />" } },
      { path: "/availability", name: "availability", component: { template: "<div />" } },
      { path: "/sites", name: "sites", component: { template: "<div />" } },
    ],
  });
  await router.push("/");
  await router.isReady();
  const el = document.createElement("div");
  document.body.appendChild(el);
  const app = createApp(WindyForecastView);
  app.use(router);
  app.mount(el);
  // Legacy regressions explicitly select the preserved 1-spot mode.
  for (let attempt = 0; attempt < 100; attempt++) {
    await nextTick();
    const spot = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "1 spot");
    if (spot) { spot.click(); await nextTick(); break; }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return { router, el };
}

async function settle(maxMs = 2000): Promise<void> {
  const t0 = Date.now();
  for (;;) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
    if (document.body.textContent?.includes("Očekivana proizvodnja")) return;
    if (Date.now() - t0 > maxMs) throw new Error("view never settled");
  }
}

function clickViewMode(label: string): void {
  const btn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  if (!btn) throw new Error(`view-mode button "${label}" not found`);
  btn.click();
}

beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("Correction B section 1 - adjusted view never shows baseline as adjusted (DOM)", () => {
  it("mixed availability: gfs cells are em-dash + reason visible; ecmwf adjusted value shown; gfs UKUPNO null", async () => {
    vi.stubGlobal("fetch", vi.fn(makeFetch("adjusted")));
    await mountView();
    await settle();
    clickViewMode("Prilagođeno");
    // wait for refetch to settle into the adjusted table
    for (let i = 0; i < 50; i += 1) {
      await nextTick();
      await new Promise((r) => setTimeout(r, 10));
      if (document.body.textContent?.includes("prilagođeno nedostupno")) break;
    }
    const text = document.body.textContent ?? "";
    expect(text).toContain("prilagođeno nedostupno");
    expect(text).toContain("bazne vrijednosti NISU prikazane");
    // ecmwf ADJUSTED value visible
    expect(text).toContain("77.7");
    // gfs BASELINE number must NOT appear anywhere while in adjusted mode
    expect(text).not.toContain("123.4");
    // heading still says adjusted (not all-unavailable state)
    expect(text).toContain("prilagođeno na raspoloživost");
  }, 15000);

  it("all adjusted unavailable: loud banner, no MWh numbers at all, heading declares unavailability", async () => {
    vi.stubGlobal("fetch", vi.fn(makeFetch("adjusted")));
    // force both models unavailable by intercepting forecast payload
    const prev = makeFetch("adjusted");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL | Request) => {
        const u = typeof url === "string" ? url : url instanceof Request ? url.url : url.toString();
        if (u.includes("/api/forecast") && u.includes("with_availability=true")) {
          const payload = forecastPayload("adjusted");
          for (const m of payload.models) m.adjusted = { available: false, reason: "no turbine inventory for this site - generate it first" };
          return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        return prev(url);
      }),
    );
    await mountView();
    await settle();
    clickViewMode("Prilagođeno");
    for (let i = 0; i < 50; i += 1) {
      await nextTick();
      await new Promise((r) => setTimeout(r, 10));
      if (document.body.textContent?.includes("Prilagođeni izračun nije moguć")) break;
    }
    const text = document.body.textContent ?? "";
    expect(text).toContain("prilagođeni prikaz NIJE dostupan");
    expect(text).toContain("Prebacite na");
    // NO baseline numbers leak into the adjusted table
    expect(text).not.toContain("123.4");
    expect(text).not.toContain("77.7");
  }, 15000);

  it("baseline tab still displays the baseline numbers", async () => {
    vi.stubGlobal("fetch", vi.fn(makeFetch("baseline")));
    await mountView();
    await settle();
    clickViewMode("Bazno");
    for (let i = 0; i < 50; i += 1) {
      await nextTick();
      await new Promise((r) => setTimeout(r, 10));
      if (document.body.textContent?.includes("123.4")) break;
    }
    const text = document.body.textContent ?? "";
    expect(text).toContain("bazno (bez ograničenja)");
    expect(text).toContain("123.4");
  }, 15000);
});
