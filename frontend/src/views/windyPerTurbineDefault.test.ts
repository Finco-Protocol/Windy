// @vitest-environment jsdom
/** Owner decision: Per turbine is the default; historical 1 spot is optional. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";

vi.mock("vue-echarts", () => ({ default: { name: "VChart", template: "<div />" } }));
import WindyForecastView from "./WindyForecastView.vue";

const site = {
  id:"krnovo", name:"Krnovo Wind Farm", country:"Montenegro",
  latitude:42.8756, longitude:19.1106, capacity_mw:74.1,
  turbine_count:26, turbine_rated_mw:2.85, turbine_model:"test",
  hub_height_m:90, loss_factor:0, timezone:"Europe/Podgorica",
  power_curve_id:"ge103-2750-oedb", assumptions:{}, notes:"",
};
const location = {
  location_source:"owner-file-provisional", location_revision:0,
  location_fingerprint:"test", provisional_mapping:true,
  virtual_inventory:false, inventory_revision:1, n_turbines:26,
};
const spatial = {
  site_id:"krnovo", mode:"per_turbine", days:7, location_info:location,
  note:"NWP modelled, not measured",
  models:[{
    model_id:"ecmwf_ifs", label:"ECMWF", issue_utc:"2026-11-18T00:00:00Z",
    met_only:false, status:"OK", grid_cells:26, warning:"synthetic separate grid cells",
    daily:[{date:"2026-11-18",base_mwh:120.1,adjusted_mwh:100.2,hours_counted:24,missing_intervals:0,n_turbines:26,partial:false}],
    turbines:[{
      unit_id:"WTG-001", latitude:42.8948, longitude:19.108, elevation_m:1510,
      daily:[{date:"2026-11-18",base_mwh:4.5,adjusted_mwh_pre_export_cap:4.0,wind_mean_mps:9.0,hours:24}],
    }],
  }],
};

function setupFetch() {
  const calls: { url:string; method:string; body?:string }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?:RequestInit):Promise<Response> => {
    const url = typeof input === "string" || input instanceof URL ? String(input) : input.url;
    const method = init?.method ?? "GET";
    calls.push({url,method,body: typeof init?.body === "string" ? init.body : undefined});
    const path = url.split("?")[0]!;
    let result:any = {};
    if (path === "/api/site") result = {
      site, power_curve:{id:"ge103-2750-oedb",label:"GE",status:"reference",provenance:"test",rated_power_mw:2.78},
      models:[], source_provider:"open-meteo", source_note:"synthetic",
    };
    else if (path === "/api/forecast") result = {
      site, power_curve:{id:"ge103-2750-oedb",label:"GE",status:"reference",provenance:"test",rated_power_mw:2.78},
      days:7,models:[], intersection:{n_common_hours:0,total_mwh:{},note:""},
      operational:null, generated_at_utc:"2026-11-18T00:00:00Z",
    };
    else if (path === "/api/turbine-forecast") result = spatial;
    else if (path === "/api/collect/turbines") result = {
      spot:{collected_at_utc:"2026-11-18T01:00:00Z"},per_turbine:{models:[{model_id:"ecmwf_ifs",status:"OK"}]},
    };
    else if (path === "/api/assets/krnovo/turbines") result = {
      site_id:"krnovo",revision:1,n_turbines:26,installed_mw:74.1,turbines:[],
    };
    else if (path === "/api/assets/krnovo/status") result = {
      site_id:"krnovo",installed_mw:74.1,available_max_mw:74.1,offline_count:0,
      curtailed_count:0,farm_export_cap_mw:null,n_turbines:26,inventory_revision:1,constraints_revision:0,
    };
    else if (path === "/api/assets/krnovo/constraints") result = {site_id:"krnovo",revision:0,constraints:[]};
    else if (path === "/api/assets/krnovo/wind-now") result = {
      site_id:"krnovo",modeled:true,disclaimer:"synthetic spot, not a measurement",
      generated_at_utc:"2026-11-18T00:00:00Z",models:[],
    };
    else if (path === "/api/assets/krnovo/weather-now") result = {
      status:"unavailable",condition:"Nedostupno",icon:"unknown",
      temperature_2m_c:null,cloud_cover_percent:null,precipitation_mm:null,
      snowfall_cm:null,source_provider:"open-meteo",source_model:"best_match",valid_at_utc:null,
    };
    else if (path === "/api/assets") result = {assets:[]};
    return new Response(JSON.stringify(result), {status:200,headers:{"Content-Type":"application/json"}});
  });
  vi.stubGlobal("fetch",fetchMock);
  return {calls};
}
async function mount() {
  const router=createRouter({history:createMemoryHistory(),routes:[
    {path:"/",name:"forecast",component:{template:"<div />"}},
    {path:"/history",name:"history",component:{template:"<div />"}},
    {path:"/sites",name:"sites",component:{template:"<div />"}},
    {path:"/actuals",name:"actuals",component:{template:"<div />"}},
    {path:"/availability",name:"availability",component:{template:"<div />"}},
  ]});
  await router.push("/");
  await router.isReady();
  const el=document.createElement("div");
  document.body.appendChild(el);
  const app=createApp(WindyForecastView);
  app.use(router);
  app.mount(el);
}
async function until(predicate:()=>boolean) {
  for (let i=0;i<150;i++) {
    await nextTick();
    if(predicate())return;
    await new Promise((resolve)=>setTimeout(resolve,10));
  }
  throw new Error("view never settled: " + (document.body.textContent??"").slice(0,400));
}
beforeEach(()=>{document.body.innerHTML="";});
afterEach(()=>{vi.unstubAllGlobals();document.body.innerHTML="";});

describe("Per turbine default + one-spot optional",()=>{
  it("starts with the spatial 26-WTG table and leaves legacy 1 spot selectable",async()=>{
    const h=setupFetch();
    await mount();
    await until(()=>document.body.textContent?.includes("Očekivana proizvodnja po danima")??false);
    expect(document.body.textContent).toContain("Izvor: 26 GPS lokacija");
    expect(document.body.querySelector('[aria-label="Prognoza po turbini"]')).toBeNull();
    expect(h.calls.filter(c=>c.url.startsWith("/api/turbine-forecast"))).toHaveLength(0);
    const detailLink = [...document.body.querySelectorAll("a")].find(a=>a.textContent?.includes("Detalji po 26 WTG"));
    expect(detailLink).toBeTruthy();
    expect(detailLink!.getAttribute("href")).toContain("/detailed-forecast");
    expect(h.calls.some(c=>c.url.includes("modeling_mode=per_turbine"))).toBe(true);
    const spot=[...document.body.querySelectorAll("button")].find(b=>b.textContent?.trim()==="1 spot");
    expect(spot).toBeTruthy();
    spot!.click();
    await until(()=>h.calls.some(c=>c.url.includes("modeling_mode=spot")));
    expect(document.body.textContent).toContain("Izvor: 1 referentna točka elektrane");
    expect(document.body.querySelector('[aria-label="Prognoza po turbini"]')).toBeNull();
    const per=[...document.body.querySelectorAll("button")].find(b=>b.textContent?.trim()==="Po turbini (26)");
    expect(per).toBeTruthy();
    per!.click();
    await until(()=>document.body.textContent?.includes("Izvor: 26 GPS lokacija")??false);
    expect(document.body.querySelector('[aria-label="Prognoza po turbini"]')).toBeNull();
  },20000);

  it("manual refresh collects 26 locations, not implicit spot-only forecast",async()=>{
    const h=setupFetch();
    await mount();
    await until(()=>document.body.textContent?.includes("Osvježi 26 turbina")??false);
    const btn=[...document.body.querySelectorAll("button")].find(b=>b.textContent?.trim()==="Osvježi 26 turbina");
    expect(btn).toBeTruthy();
    btn!.click();
    await until(()=>h.calls.some(c=>c.url==="/api/collect/turbines"&&c.method==="POST"));
    const req=h.calls.find(c=>c.url==="/api/collect/turbines"&&c.method==="POST");
    expect(JSON.parse(req!.body??"{}")).toEqual({days:16,site_id:"krnovo"});
    expect(h.calls.some(c=>c.url==="/api/collect"&&c.method==="POST")).toBe(false);
  },20000);
});
