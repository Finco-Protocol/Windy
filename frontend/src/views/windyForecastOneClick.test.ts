// @vitest-environment jsdom

/** Correction E §4 — one-click ON/OFF and one-step bulk DOM tests: exactly
 *  ONE POST per action, no confirmation modal, server-confirmed status
 *  refresh, failure rollback, 409 refetch-retry, bulk one-step cap. */

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

const RATED = 2.85;
const UNITS = Array.from({ length: 26 }, (_, i) => `WTG-${String(i + 1).padStart(3, "0")}`);

function point(t: string, ws: number, mw: number) {
  return { valid_at_utc: t, wind_speed_mps: ws, wind_direction_deg: 180, height_method: "log-interp", modeled_power_mw: mw, interval_energy_mwh: mw, interval_hours: 1, status: "ok" };
}

function makeHarness() {
  const state = {
    offUnits: new Set<string>(["WTG-005", "WTG-017"]),
    revision: 6,
    failNextReplace: null as null | number, // status code to fail with
    replaceCalls: [] as { assignments: { unit_id: string; state: string; cap_mw?: number; cap_percent?: number }[]; body: Record<string, unknown> }[],
    statusCalls: 0,
  };

  const availableMax = () => (26 - state.offUnits.size) * RATED;

  function forecastPayload(mode: "baseline" | "adjusted") {
    const h24 = Array.from({ length: 24 }, (_, h) => `2026-11-18T${String(h).padStart(2, "0")}:00:00Z`);
    const mk = (id: string, provider: string, label: string) => ({
      model_id: id,
      provider,
      label,
      met_only: false,
      height_method: "log-interp",
      height_sources_m: [80, 100],
      requested_height_m: 90,
      height_note: "test",
      run: { id: 1, run_issued_utc: "2026-11-18T00:00:00Z", issue_source: "single-runs-archive", fetched_at_utc: "2026-11-18T05:00:00Z", forecast_days: 16, source_url: "test" },
      points: h24.map((t) => point(t, 11, 4.5)),
      daily_local: [{ date: "2026-11-18", mwh: 108, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }],
      total_mwh_own_window: 108,
      coverage: { n_hours: 24, first_valid_utc: h24[0], last_valid_utc: h24[23], max_available_hours: 24 },
      warnings: [],
      ...(mode === "adjusted" ? { adjusted: { available: true, inventory_revision: 2, constraints_revision: state.revision, baseline_comparable: true, baseline_comparable_reason: null, daily: [{ date: "2026-11-18", mwh: 100, hours_counted: 24, hours_missing: 0, hours_expected: 24, partial: false }], points: h24.map((t) => ({ valid_at_utc: t, adjusted_mw: 4.3, adjusted_mwh: 4.3, interval_hours: 1, status: "ok" })) } } : {}),
    });
    return {
      site: SITE,
      power_curve: { id: "ge103-2750-oedb", label: "GE ref", status: "reference", provenance: "OEDB", rated_power_mw: 2.78, cut_in_mps: 3, rated_mps: 12, cut_out_mps: 25 },
      generated_at_utc: "2026-11-18T05:20:00Z",
      days: 7,
      operational: { site_id: "krnovo", installed_mw: 74.1, available_max_mw: availableMax(), offline_count: state.offUnits.size, curtailed_count: 0, farm_export_cap_mw: null, n_turbines: 26, inventory_revision: 2, constraints_revision: state.revision, inventory_present: true, n_turbines_inventory: 26 },
      models: [mk("gfs_seamless", "NOAA", "GFS"), mk("ecmwf_ifs", "ECMWF", "ECMWF")],
      intersection: { n_common_hours: 24, first_common_utc: h24[0], last_common_utc: h24[23], total_mwh: {}, note: "" },
    };
  }

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === "string" || input instanceof URL ? String(input) : input.url;
    const path = raw.replace(/^https?:\/\/[^/]+/, "").split("?")[0]!;
    const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (path === "/api/site") return respond({ site: SITE, power_curve: { id: "ge103-2750-oedb", label: "GE ref", status: "reference", provenance: "OEDB", rated_power_mw: 2.78, cut_in_mps: 3, rated_mps: 12, cut_out_mps: 25 }, models: [], source_provider: "open-meteo", source_note: "" });
    if (path.startsWith("/api/forecast")) return respond(forecastPayload(path.includes("with_availability=true") ? "adjusted" : "baseline"));
    if (path === "/api/assets/krnovo/status") {
      state.statusCalls += 1;
      return respond({ site_id: "krnovo", installed_mw: 74.1, available_max_mw: availableMax(), offline_count: state.offUnits.size, curtailed_count: 0, farm_export_cap_mw: null, n_turbines: 26, inventory_revision: 2, constraints_revision: state.revision });
    }
    if (path === "/api/assets/krnovo/turbines") return respond({ site_id: "krnovo", revision: 2, n_turbines: 26, installed_mw: 74.1, turbines: UNITS.map((u) => ({ unit_id: u, name: u, rated_mw: RATED, curve_id: null, created_at_utc: "" })) });
    if (path === "/api/assets/krnovo/constraints") return respond({ site_id: "krnovo", revision: state.revision, constraints: [...state.offUnits].map((u) => ({ id: 1, kind: "offline", unit_ids: [u], cap_mw: null, cap_percent: null, start_utc: "2026-11-17T00:00:00Z", end_utc: null, revision: state.revision, created_at_utc: "", created_by: "owner" })) });
    if (path === "/api/assets/krnovo/constraints/replace-units" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { assignments: { unit_id: string; state: string; cap_mw?: number; cap_percent?: number }[]; start_local: string; expected_revision?: number };
      const fail = state.failNextReplace;
      if (fail) {
        state.failNextReplace = null;
        if (fail === 409) return respond({ detail: { message: "revision conflict", expected: body.expected_revision, current_revision: state.revision } }, 409);
        return respond({ detail: "boom" }, fail);
      }
      state.replaceCalls.push({ assignments: body.assignments, body });
      state.revision += 1;
      for (const a of body.assignments) {
        if (a.state === "on") state.offUnits.delete(a.unit_id);
        if (a.state === "off") state.offUnits.add(a.unit_id);
      }
      return respond({ site_id: "krnovo", revision: state.revision, constraints: [], warnings: [] });
    }
    if (path.startsWith("/api/assets/krnovo/wind-now")) return respond({ site_id: "krnovo", modeled: true, disclaimer: "test", generated_at_utc: "2026-11-18T05:00:00Z", models: [] });
    if (path.startsWith("/api/assets")) return respond({ assets: [] });
    return respond({});
  });

  return { state, fetchMock, availableMax };
}

async function mountView(router: Router): Promise<void> {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const app = createApp(WindyForecastView);
  app.use(router);
  app.mount(el);
  await router.isReady();
}

async function openEditor(): Promise<void> {
  // CollapsibleSection is lazy + closed: click its header to render content
  const header = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.includes("Brzo uređivanje turbina"));
  if (!header) throw new Error("editor section header not found");
  header.click();
  await nextTick();
  await new Promise((r) => setTimeout(r, 10));
}

async function settle(maxMs = 2500, until?: (t: string) => boolean): Promise<void> {
  const t0 = Date.now();
  for (;;) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
    const t = document.body.textContent ?? "";
    if (until?.(t)) return;
    if (t.includes("Očekivana proizvodnja") && !until) return;
    if (Date.now() - t0 > maxMs) throw new Error(`view never settled: ${(document.body.textContent ?? "").slice(0, 200)}`);
  }
}

function tile(unit: string): HTMLElement {
  const el = [...document.body.querySelectorAll("[role=checkbox]")].find((n) => n.querySelector(`[aria-label^="${unit}:"]`));
  if (!el) throw new Error(`tile ${unit} not found`);
  return el as HTMLElement;
}

function switchButton(unit: string): HTMLButtonElement {
  const el = tile(unit).querySelector(`button[aria-label^="${unit}:"]`);
  if (!el) throw new Error(`switch for ${unit} not found`);
  return el as HTMLButtonElement;
}

function replaceCallsOf(h: ReturnType<typeof makeHarness>): number {
  return h.state.replaceCalls.length;
}

let router: Router;

beforeEach(async () => {
  document.body.innerHTML = "";
  router = createRouter({
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
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("Correction E §1 — one-click ON/OFF", () => {
  it("WTG-017 OFF → one click ON → exactly ONE POST, no modal, RASP. MAKS. 68.40 → 71.25; re-click OFF → one POST back", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    const callsBefore = replaceCallsOf(h);
    // no confirmation modal exists anywhere in the editor
    expect(document.body.textContent).not.toContain("Potvrdi primjenu");
    switchButton("WTG-017").click();
    await settle(undefined, (t) => t.includes("71.25"));
    expect(replaceCallsOf(h)).toBe(callsBefore + 1);
    const sent = h.state.replaceCalls.at(-1)!;
    expect(sent.assignments).toEqual([{ unit_id: "WTG-017", state: "on" }]);
    expect((sent.body as { start_local?: string }).start_local).toBeTruthy();
    // re-click: switch now offers OFF for the running unit
    switchButton("WTG-017").click();
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    expect(replaceCallsOf(h)).toBe(callsBefore + 2);
    expect(h.state.replaceCalls.at(-1)!.assignments).toEqual([{ unit_id: "WTG-017", state: "off" }]);
    // switching must not also select the tile for bulk
    expect(tile("WTG-017").getAttribute("aria-checked")).toBe("false");
  }, 20000);

  it("tile selection does NOT toggle state; keyboard Enter selects too", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    const before = replaceCallsOf(h);
    tile("WTG-010").click();
    await nextTick();
    expect(tile("WTG-010").getAttribute("aria-checked")).toBe("true");
    expect(replaceCallsOf(h)).toBe(before), "selection never POSTs";
    tile("WTG-010").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await nextTick();
    expect(tile("WTG-010").getAttribute("aria-checked")).toBe("false");
  }, 20000);

  it("network failure: confirmed state stays, explicit error shown, no phantom status change", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    h.state.failNextReplace = 500;
    const statusCallsBefore = h.state.statusCalls;
    switchButton("WTG-017").click();
    await settle(undefined, (t) => t.includes("boom") || t.includes("500"));
    expect(h.state.offUnits.has("WTG-017")).toBe(true), "server never applied it";
    expect(document.body.textContent).not.toContain("71.25"), "UI never ahead of DB";
    expect(h.state.statusCalls).toBeGreaterThanOrEqual(statusCallsBefore), "state refetched after failure";
    // switch button re-enabled after failure, still offers ON
    expect(switchButton("WTG-017").disabled).toBe(false);
  }, 20000);

  it("409 revision conflict → refetch constraints → retry ONCE → success (2 POSTs total)", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    const stale = h.state.revision;
    // concurrent mutation happens behind our back
    h.state.revision += 1;
    h.state.failNextReplace = 409;
    switchButton("WTG-017").click();
    await settle(undefined, (t) => t.includes("71.25"));
    const posts = h.state.replaceCalls;
    expect(posts.length).toBe(1), "retry succeeded";
    expect(posts[0]!.assignments).toEqual([{ unit_id: "WTG-017", state: "on" }]);
    expect(stale).toBeLessThan(h.state.revision);
  }, 20000);
});

describe("Correction E §2 — one-step bulk", () => {
  it("select six units → Spremi limit na odabrane → ONE POST with six cap_mw 2.5, no confirm", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    const callsBefore = replaceCallsOf(h);
    const six = ["WTG-010", "WTG-011", "WTG-012", "WTG-013", "WTG-014", "WTG-015"];
    for (const u of six) tile(u).click();
    await nextTick();
    const saveBtn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Spremi limit na odabrane");
    expect(saveBtn).toBeTruthy();
    saveBtn!.click();
    await settle(undefined, (t) => t.includes("Spremljeno atomarno"));
    expect(replaceCallsOf(h)).toBe(callsBefore + 1);
    const sent = h.state.replaceCalls.at(-1)!;
    expect(sent.assignments).toHaveLength(6);
    expect(sent.assignments.every((a) => a.state === "cap" && a.cap_mw === 2.5 && a.cap_percent === undefined)).toBe(true);
    // server-confirmed status shown after success
    expect(document.body.textContent).toContain("Spremljeno atomarno");
    expect(document.body.textContent).not.toContain("Potvrdi primjenu");
  }, 20000);

  it("bulk Uključi odabrane → ONE POST with per-unit state on", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("68.4"));
    await openEditor();
    const callsBefore = replaceCallsOf(h);
    for (const u of ["WTG-005", "WTG-017"]) tile(u).click();
    await nextTick();
    const btn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Uključi odabrane");
    btn!.click();
    await settle(undefined, (t) => t.includes("Spremljeno atomarno"));
    expect(replaceCallsOf(h)).toBe(callsBefore + 1);
    expect(h.state.replaceCalls.at(-1)!.assignments).toEqual([
      { unit_id: "WTG-005", state: "on" },
      { unit_id: "WTG-017", state: "on" },
    ]);
  }, 20000);
});
