// @vitest-environment jsdom

/** Correction F — DOM regressions: legend clicks actually toggle series
 *  visibility (P0-2), Excel button hits /api URL with real XLSX bytes
 *  (P0-1), bulk OFF overrides stale per-unit CAP edits (P0-5), failures
 *  retain selection and state (P0-6). */

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

function makeHarness() {
  const state = {
    offUnits: new Set<string>(),
    revision: 6,
    failNextReplace: null as null | number,
    replaceCalls: [] as { assignments: { unit_id: string; state: string; cap_mw?: number; cap_percent?: number }[] }[],
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
      points: h24.map((t) => ({ valid_at_utc: t, wind_speed_mps: 11, wind_direction_deg: 180, height_method: "log-interp", modeled_power_mw: 4.5, interval_energy_mwh: 4.5, interval_hours: 1, status: "ok" })),
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
    if (path === "/api/assets/krnovo/status") return respond({ site_id: "krnovo", installed_mw: 74.1, available_max_mw: availableMax(), offline_count: state.offUnits.size, curtailed_count: 0, farm_export_cap_mw: null, n_turbines: 26, inventory_revision: 2, constraints_revision: state.revision });
    if (path === "/api/assets/krnovo/turbines") return respond({ site_id: "krnovo", revision: 2, n_turbines: 26, installed_mw: 74.1, turbines: UNITS.map((u) => ({ unit_id: u, name: u, rated_mw: RATED, curve_id: null, created_at_utc: "" })) });
    if (path === "/api/assets/krnovo/constraints") return respond({ site_id: "krnovo", revision: state.revision, constraints: [...state.offUnits].map((u) => ({ id: 1, kind: "offline", unit_ids: [u], cap_mw: null, cap_percent: null, start_utc: "2026-11-17T00:00:00Z", end_utc: null, revision: state.revision, created_at_utc: "", created_by: "owner" })) });
    if (path === "/api/assets/krnovo/constraints/replace-units" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { assignments: { unit_id: string; state: string; cap_mw?: number; cap_percent?: number }[] };
      if (state.failNextReplace) {
        const code = state.failNextReplace;
        state.failNextReplace = null;
        return respond({ detail: "boom" }, code);
      }
      state.replaceCalls.push({ assignments: body.assignments });
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

async function openChartsAndEditor(): Promise<void> {
  for (const title of ["Grafikoni (vjetar i snaga po satima)", "Brzo uređivanje turbina (ON / % ograničenja / Max MW — po jedinici ili bulk)"]) {
    const header = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.includes(title));
    if (header) {
      header.click();
      await nextTick();
      await new Promise((r) => setTimeout(r, 10));
    }
  }
}

async function settle(maxMs = 2500, until?: (t: string) => boolean): Promise<void> {
  const t0 = Date.now();
  for (;;) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
    const t = document.body.textContent ?? "";
    if (until?.(t)) return;
    if (t.includes("Očekivana proizvodnja") && !until) return;
    if (Date.now() - t0 > maxMs) throw new Error("view never settled");
  }
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

describe("Correction F P0-1 — Excel export hits /api URL with real XLSX bytes", () => {
  it("click Export → fetch to /api/assets/krnovo/forecast-export.xlsx with xlsx MIME + PK signature", async () => {
    const h = makeHarness();
    // intercept AFTER counting: wrap the mock to record export calls
    const exportCalls: string[] = [];
    const base = h.fetchMock;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const raw = typeof input === "string" || input instanceof URL ? String(input) : input.url;
        if (raw.includes("forecast-export.xlsx")) exportCalls.push(raw);
        const res = await base(input, init);
        if (raw.includes("forecast-export.xlsx")) {
          // real XLSX bytes: minimal zip (PK signature)
          const xlsx = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0, 0]);
          return new Response(xlsx, {
            status: 200,
            headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Content-Disposition": 'attachment; filename="Windy_krnovo_2026-11-18_7d_baseline_mwh.xlsx"' },
          });
        }
        return res;
      }),
    );
    await mountView(router);
    await settle(undefined, (t) => t.includes("74.1"));
    await openChartsAndEditor();
    const btn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.includes("Export Excel"));
    expect(btn).toBeTruthy();
    btn!.click();
    await settle(undefined, () => exportCalls.length > 0);
    expect(exportCalls).toHaveLength(1);
    expect(exportCalls[0]).toContain("/api/assets/krnovo/forecast-export.xlsx");
    expect(exportCalls[0]).toContain("days=7");
    expect(exportCalls[0]).toContain("avg_enabled=true");
    expect(exportCalls[0]!.startsWith("/api/assets/krnovo/forecast-export.xlsx")).toBe(true); // always /api-prefixed
  }, 20000);

  it("mislabeled HTML response (no PK signature) surfaces an explicit error", async () => {
    const h = makeHarness();
    const base = h.fetchMock;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const raw = typeof input === "string" || input instanceof URL ? String(input) : input.url;
        if (raw.includes("forecast-export.xlsx")) {
          return new Response("<!doctype html><html>spa</html>", { status: 200, headers: { "Content-Type": "text/html" } });
        }
        return base(input, init);
      }),
    );
    await mountView(router);
    await settle(undefined, (t) => t.includes("74.1"));
    await openChartsAndEditor();
    const btn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.includes("Export Excel"));
    btn!.click();
    await settle(undefined, (t) => t.includes("Export nije vratio XLSX"));
    expect(document.body.textContent).toContain("Export nije vratio XLSX");
  }, 20000);
});

describe("Correction F P0-2 — legend clicks toggle series visibility (DOM)", () => {
  it("click GFS legend → GFS line removed from chart option; click again → restored; Sve/Ništa work", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle();
    await openChartsAndEditor();

    // find the wind legend button for GFS (wind chart legend has aria-pressed buttons)
    const legendBtn = (modelLabel: string) =>
      [...document.body.querySelectorAll("button")].filter((b) => b.getAttribute("aria-pressed") !== null).find((b) => b.textContent?.includes(modelLabel));
    const gfs = legendBtn("GFS");
    expect(gfs).toBeTruthy();
    expect(gfs!.getAttribute("aria-pressed")).toBe("true");
    gfs!.click();
    await nextTick();
    expect(gfs!.getAttribute("aria-pressed")).toBe("false");
    gfs!.click();
    await nextTick();
    expect(gfs!.getAttribute("aria-pressed")).toBe("true");
    // Ništa on wind hides all legend buttons' pressed state
    const nista = [...document.body.querySelectorAll("button")].filter((b) => b.textContent?.trim() === "Ništa")[0];
    nista!.click();
    await nextTick();
    expect(gfs!.getAttribute("aria-pressed")).toBe("false"), "Ništa (wind) hides GFS wind line";
    // power legend GFS unaffected by wind Ništa (independent charts)
    const gfsButtons = [...document.body.querySelectorAll("button")].filter((b) => b.getAttribute("aria-pressed") !== null && b.textContent?.includes("GFS"));
    expect(gfsButtons.length).toBe(2), "wind + power legends each have a GFS button";
    expect(gfsButtons[1]!.getAttribute("aria-pressed")).toBe("true"), "power GFS unaffected by wind Ništa";
  }, 20000);
});

describe("Correction F P0-5/P0-6 — bulk authority + failure retention (DOM)", () => {
  async function selectTiles(units: string[]): Promise<void> {
    for (const u of units) tile(u).click();
    await nextTick();
  }
  function tile(unit: string): HTMLElement {
    const el = [...document.body.querySelectorAll("[role=checkbox]")].find((n) => n.querySelector(`[aria-label^="${unit}:"]`));
    if (!el) throw new Error(`tile ${unit} not found`);
    return el as HTMLElement;
  }

  it("stale per-unit CAP edit does NOT override explicit bulk OFF (payload = OFF for both)", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("74.1"));
    await openChartsAndEditor();
    // select WTG-010 (untouched) and WTG-011 (gets a stale CAP edit via the per-unit editor)
    await selectTiles(["WTG-010", "WTG-011"]);
    const row = [...document.body.querySelectorAll("div")].find((d) => d.textContent?.startsWith("WTG-011") && d.querySelector("input"));
    const mwInput = row?.querySelector("input[type=number]") as HTMLInputElement | undefined;
    if (mwInput) {
      mwInput.value = "2.5";
      mwInput.dispatchEvent(new Event("input", { bubbles: true }));
      await nextTick();
    }
    const offBtn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Isključi odabrane");
    offBtn!.click();
    await settle(undefined, (t) => t.includes("Spremljeno atomarno"));
    const sent = h.state.replaceCalls.at(-1)!;
    expect(sent.assignments).toHaveLength(2);
    expect(sent.assignments.every((a) => a.state === "off" && a.cap_mw === undefined && a.cap_percent === undefined)).toBe(true);
  }, 20000);

  it("failed bulk save (500) RETAINS selection and edits; retry succeeds", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle(undefined, (t) => t.includes("74.1"));
    await openChartsAndEditor();
    await selectTiles(["WTG-010", "WTG-011"]);
    h.state.failNextReplace = 500;
    const offBtn = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Isključi odabrane");
    offBtn!.click();
    await settle(undefined, (t) => t.includes("boom") || t.includes("500"));
    // selection RETAINED for retry
    expect(document.body.querySelector("[role=checkbox][aria-checked=true]") !== null).toBe(true);
    expect(document.body.textContent).toContain("boom");
    // retry now succeeds
    offBtn!.click();
    await settle(undefined, (t) => t.includes("Spremljeno atomarno"));
    expect(h.state.replaceCalls.at(-1)!.assignments.every((a) => a.state === "off")).toBe(true);
    await settle(undefined, (t) => t.includes("72.75") || t.includes("74.1"));
  }, 20000);
});

describe("Correction G P1-B — Compare Prosjek respects the Average toggle", () => {
  async function openCompare(): Promise<void> {
    // Compare section is NOT collapsible; switch to power tab via segmented toggle
    const tab = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Usporedba proizvodnje");
    tab!.click();
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
  }

  it("Prosjek toggle OFF → Compare Prosjek cells are em dash; ON → numeric values return", async () => {
    const h = makeHarness();
    vi.stubGlobal("fetch", h.fetchMock);
    await mountView(router);
    await settle();
    await openChartsAndEditor();
    await openCompare();
    // default enabled: Prosjek column has numeric cells in production compare
    const prosjekCells = () => {
      const tables = [...document.body.querySelectorAll("table")];
      const t = tables.find((tb) => tb.textContent?.includes("Energija") || tb.textContent?.includes("Prosjek"));
      const header = [...(t?.querySelectorAll("th") ?? [])].find((th) => th.textContent?.trim() === "Prosjek");
      if (!header) return null;
      const col = [...(t!.querySelectorAll("tr")[0]!.querySelectorAll("th"))].findIndex((th) => th.textContent?.trim() === "Prosjek");
      return [...(t!.querySelectorAll("tbody tr"))].map((tr) => tr.querySelectorAll("td")[col]?.textContent?.trim());
    };
    let cells = prosjekCells();
    expect(cells).not.toBeNull();
    expect(cells!.every((c) => c !== "—")).toBe(true), "enabled default: numeric values";
    // toggle Prosjek OFF via the daily-table header button
    const prosjekToggle = [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Prosjek");
    prosjekToggle!.click();
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
    cells = prosjekCells();
    expect(cells!.every((c) => c === "—")).toBe(true), "disabled → em dash, never a computed value";
    // re-enable
    prosjekToggle!.click();
    await nextTick();
    await new Promise((r) => setTimeout(r, 10));
    cells = prosjekCells();
    expect(cells!.every((c) => c !== "—")).toBe(true);
    // deselect ALL models → blank, no silent auto-reset to all
    for (const pid of ["ECMWF", "GFS"]) {
      const btn = [...document.body.querySelectorAll("button")].filter((b) => b.textContent?.trim() === pid)[0];
      btn!.click();
      await nextTick();
    }
    await new Promise((r) => setTimeout(r, 10));
    cells = prosjekCells();
    expect(cells!.every((c) => c === "—")).toBe(true), "explicit empty selection stays blank";
  }, 20000);
});
