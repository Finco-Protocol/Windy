"""Correction E Addendum D — Excel export: row counts, units, adjusted
no-fallback, UKMO met-only, missing ≠ 0, formula-injection guard, re-open."""

import io

import pytest
from openpyxl import load_workbook

from datetime import datetime, timedelta

from tests.conftest import make_raw_run

DAY = "2026-11-18"


def _seed(client):
    api, fake, _s = client
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    h384 = [(datetime.fromisoformat(f"{DAY}T00:00:00+00:00") + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(384)]
    # two production models with 2 days of data
    for mid, ws in (("gfs_seamless", 12.0), ("ecmwf_ifs", 10.0)):
        fake.push_run(mid, f"{DAY}T00:00", make_raw_run(h384, {"80": [ws] * 384, "100": [ws] * 384}))
        r = api.post("/api/backfill", json={"model_id": mid, "run_date": DAY, "run_hour": 0, "days": 16})
        assert r.status_code == 200, r.text
    # UKMO met-only: 10 m wind only
    fake.push_run("ukmo_seamless", f"{DAY}T00:00", make_raw_run(h384, {"10": [8.0] * 384}))
    r2 = api.post("/api/backfill", json={"model_id": "ukmo_seamless", "run_date": DAY, "run_hour": 0, "days": 7})
    assert r2.status_code == 200
    return api, fake


def _fetch_xlsx(api, days=7, mode="baseline", unit="mwh", avg_models=None, avg_enabled=True):
    q = f"days={days}&mode={mode}&unit={unit}&avg_enabled={'true' if avg_enabled else 'false'}"
    if avg_models is not None:
        q += f"&avg_models={avg_models}"
    r = api.get(f"/api/assets/krnovo/forecast-export.xlsx?{q}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "attachment" in r.headers.get("content-disposition", "")
    return load_workbook(io.BytesIO(r.content)), r


def test_export_daily_rows_7_10_15_and_totals(client):
    api, _fake = _seed(client)
    for days in (7, 10, 15):
        wb, _r = _fetch_xlsx(api, days=days)
        ws = wb["Dnevna prognoza"]
        # header + exactly `days` daily rows + UKUPNO row
        n_data_rows = days
        assert ws.cell(row=1, column=1).value == "Datum"
        dates = [ws.cell(row=r, column=1).value for r in range(2, 2 + n_data_rows)]
        assert all(dates), f"expected {days} date rows"
        assert ws.cell(row=2 + days, column=1).value == "UKUPNO"
        # UKUPNO = sum of the displayed rows only (GFS column)
        gfs_col = 2  # first production model
        nums = [ws.cell(row=r, column=gfs_col).value for r in range(2, 2 + days)]
        nums = [v for v in nums if isinstance(v, (int, float))]
        total = ws.cell(row=2 + days, column=gfs_col).value
        assert total is not None
        assert abs(total - sum(nums)) < 1e-6, "UKUPNO sums exactly the displayed rows"


def test_export_models_columns_and_ukmo_no_energy(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7)
    ws = wb["Dnevna prognoza"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    assert "UKMO Global (10 m samo)" not in headers, "met-only has NO energy column in daily production"
    wind = wb["Satni vjetar"]
    wind_headers = [wind.cell(row=1, column=c).value for c in range(1, wind.max_column + 1)]
    assert "UKMO Global (10 m samo)" in wind_headers, "UKMO IS in the wind comparison"
    ukmo_col = wind_headers.index("UKMO Global (10 m samo)") + 1
    assert wind.cell(row=2, column=ukmo_col).value == 8.0, "UKMO 10 m wind archived value present"
    prod = wb["Satna proizvodnja"]
    prod_headers = [prod.cell(row=1, column=c).value for c in range(1, prod.max_column + 1)]
    assert "UKMO Global (10 m samo)" not in prod_headers, "UKMO never in production sheet"


def test_export_gwh_numeric_conversion(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, unit="gwh")
    ws = wb["Dnevna prognoza"]
    mwh_wb, _ = _fetch_xlsx(api, days=7, unit="mwh")
    mwh_ws = mwh_wb["Dnevna prognoza"]
    gfs_gwh = ws.cell(row=2, column=2).value
    gfs_mwh = mwh_ws.cell(row=2, column=2).value
    assert isinstance(gfs_gwh, (int, float)), "real numeric cell, not a formatted string"
    assert abs(gfs_gwh * 1000 - gfs_mwh) < 0.01, "GWh = raw MWh / 1000 at the END"


def test_export_adjusted_no_baseline_fallback(client):
    api, _fake = _seed(client)
    api.delete("/api/assets/krnovo/turbines")  # no inventory → adjusted unavailable
    wb, _r = _fetch_xlsx(api, days=7, mode="adjusted")
    ws = wb["Dnevna prognoza"]
    # every production cell must be EMPTY (None) — never baseline numbers
    vals = [ws.cell(row=r, column=c).value for r in range(2, 8) for c in range(2, ws.max_column - 2)]
    assert all(v is None for v in vals), "adjusted mode without inventory: no baseline masquerade"
    cov = wb["Pokrivenost"]
    statuses = {cov.cell(row=r, column=1).value: cov.cell(row=r, column=9).value for r in range(2, cov.max_row + 1)}
    assert statuses.get("gfs_seamless") in ("ADJUSTED_UNAVAILABLE", "INCOMPATIBLE_CURVE", "NO_RUN"), statuses


def test_export_formula_injection_guard(client):
    api, _fake = _seed(client)
    # rename site label to an injection attempt via config revision
    r = api.post("/api/assets/krnovo/config-revision", json={"changes": {"turbine_model": "=cmd|'/c calc'!A0"}}, )
    assert r.status_code == 200, r.text
    wb, _r = _fetch_xlsx(api, days=7)
    meta = wb["Metapodaci"]
    # every string cell in Metapodaci must not start with '=' raw
    dangerous = [c.value for row in meta.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert dangerous == [], "formula-injection strings are neutralised"


def test_export_missing_model_gaps(client):
    api, _fake = _seed(client)
    # ARPEGE/GEM/ICON have no runs → daily cells empty, Pokrivenost NO_RUN
    wb, _r = _fetch_xlsx(api, days=7)
    ws = wb["Dnevna prognoza"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    gem_col = headers.index("GEM Global") + 1
    assert ws.cell(row=2, column=gem_col).value is None, "NO_RUN model is blank, never 0"
    cov = wb["Pokrivenost"]
    statuses = {cov.cell(row=r, column=1).value: cov.cell(row=r, column=9).value for r in range(2, cov.max_row + 1)}
    assert statuses.get("gem_seamless") == "NO_RUN"
    assert statuses.get("meteofrance_arpege_world") == "NO_RUN"


def test_export_average_column_hourly_aligned(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, avg_models="gfs_seamless,ecmwf_ifs")
    ws = wb["Dnevna prognoza"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    prosjek_col = headers.index("Prosjek") + 1
    gfs_col = headers.index("NOAA GFS") + 1
    ecm_col = headers.index("ECMWF IFS HRES") + 1
    for r in range(2, 4):
        g = ws.cell(row=r, column=gfs_col).value
        e = ws.cell(row=r, column=ecm_col).value
        p = ws.cell(row=r, column=prosjek_col).value
        if g is not None and e is not None:
            assert abs(p - (g + e) / 2) < 0.01, "hourly-aligned average of the selected set"


def test_export_adjusted_hourly_uses_scenario_mw_and_energy(client):
    """Correction F P0-4: with a valid inventory + constraints (WTG-001 OFF),
    adjusted export hourly sheet must carry SCENARIO MW/energy (never archived
    baseline), and the daily production column reflects the OFF unit."""
    api, fake = _seed(client)
    api.post("/api/assets/krnovo/turbines/generate", json={"count": 26, "rating_mw": 2.75})
    api.post(
        "/api/assets/krnovo/constraints",
        json={"rows": [{"kind": "offline", "unit_ids": ["WTG-001"], "start_local": "2026-11-17T00:00", "end_local": None}]},
    )
    wb, _r = _fetch_xlsx(api, days=7, mode="adjusted")
    ws = wb["Satna proizvodnja"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    gfs_mw = headers.index("NOAA GFS MW") + 1
    gfs_e = headers.index("NOAA GFS energija (MWH)") + 1
    mw_val = ws.cell(row=2, column=gfs_mw).value
    e_val = ws.cell(row=2, column=gfs_e).value
    # wind 12 m/s → per-unit baseline P(12); adjusted with WTG-001 OFF = 25 × P(12),
    # which differs from the 26-unit baseline — proving scenario (not archived) source
    from app.power import load_curve
    pu = load_curve().power_mw_per_turbine(12.0)
    assert mw_val == pytest.approx(25 * pu, abs=0.01), "scenario MW with WTG-001 OFF"
    assert mw_val != pytest.approx(26 * pu, abs=0.01), "NOT the archived 26-unit baseline"
    assert e_val == pytest.approx(25 * pu, abs=0.01), "adjusted_mwh column present (1 h interval)"
    # daily sheet also adjusted
    daily = wb["Dnevna prognoza"]
    gfs_daily = daily.cell(row=2, column=2).value
    assert gfs_daily is not None and gfs_daily < 26 * pu * 24, "daily adjusted reflects OFF unit"


def _prosjek_cells(wb, rows=3):
    """Prosjek + N min + N max column values for the first `rows` daily rows."""
    ws = wb["Dnevna prognoza"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    p_col = headers.index("Prosjek") + 1
    nmin_col = headers.index("N min") + 1
    nmax_col = headers.index("N max") + 1
    ukupno_col = p_col
    return {
        "prosjek": [ws.cell(row=r, column=p_col).value for r in range(2, 2 + rows)],
        "n_min": [ws.cell(row=r, column=nmin_col).value for r in range(2, 2 + rows)],
        "n_max": [ws.cell(row=r, column=nmax_col).value for r in range(2, 2 + rows)],
        "ukupno": ws.cell(row=2 + rows, column=p_col).value,
        "meta": {wb["Metapodaci"].cell(row=r, column=1).value: wb["Metapodaci"].cell(row=r, column=2).value for r in range(1, wb["Metapodaci"].max_row + 1)},
    }


def test_export_avg_models_omitted_defaults_to_all_production(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, avg_models=None)
    d = _prosjek_cells(wb)
    assert d["prosjek"][0] is not None, "omitted avg_models -> default all production models -> numeric Prosjek"
    meta_ids = set(d["meta"]["average_models"].split(","))
    assert {"gfs_seamless", "ecmwf_ifs"} <= meta_ids
    assert "ukmo_seamless" not in meta_ids, "met-only never in Average default"


def test_export_avg_models_explicit_empty_blank_prosjek(client):
    """Correction G P1-A: explicitly empty selection must NOT become all models."""
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, avg_models="")
    d = _prosjek_cells(wb)
    assert all(v is None for v in d["prosjek"]), "blank Prosjek — never silently all-models"
    assert all(v is None for v in d["n_min"]) and all(v is None for v in d["n_max"])
    assert d["ukupno"] is None, "UKUPNO Prosjek blank"
    assert "(prazan odabir" in str(d["meta"]["average_models"]), "metadata reflects empty selection"


def test_export_avg_models_single_model_gfs_only(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, avg_models="gfs_seamless")
    d = _prosjek_cells(wb)
    ws = wb["Dnevna prognoza"]
    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    gfs = ws.cell(row=2, column=headers.index("NOAA GFS") + 1).value
    ecm = ws.cell(row=2, column=headers.index("ECMWF IFS HRES") + 1).value
    assert d["prosjek"][0] is not None
    assert d["prosjek"][0] == pytest.approx(gfs), "GFS-only Prosjek equals GFS, not the mean"
    assert d["prosjek"][0] != pytest.approx((gfs + ecm) / 2)


def test_export_avg_enabled_false_blanks_prosjek_even_with_models(client):
    api, _fake = _seed(client)
    wb, _r = _fetch_xlsx(api, days=7, avg_models="gfs_seamless,ecmwf_ifs", avg_enabled=False)
    d = _prosjek_cells(wb)
    assert all(v is None for v in d["prosjek"]), "avg_enabled=false blanks Prosjek"
    assert d["meta"]["average_enabled"].startswith("NE")
