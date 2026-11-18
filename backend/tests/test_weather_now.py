"""Correction E Addendum C — Weather on Site: WMO mapping, endpoint contract,
cache, site isolation, failure safety (Wind NOW and forecast never affected)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.wmo import wmo_condition

# Fresh fixture clock: a hard-coded 2026-11-18 time becomes stale as CI runs later.
NOW_STR = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _payload(code=71, temp=-7.3, is_day=1, time_str=NOW_STR):
    return {
        "current": {
            "time": time_str,
            "temperature_2m": temp,
            "weather_code": code,
            "is_day": is_day,
            "cloud_cover": 88,
            "precipitation": 1.2,
            "rain": 0.0,
            "showers": 0.0,
            "snowfall": 1.2,
        }
    }


@pytest.mark.parametrize(
    "code,is_day,icon,label_contains",
    [
        (0, True, "sun", "Sunčano"),
        (0, False, "moon", "Vedro"),
        (1, True, "partly_day", "Pretežno vedro"),
        (1, False, "partly_night", "Pretežno vedro"),
        (2, True, "partly_day", "Djelomično oblačno"),
        (3, True, "cloud", "Oblačno"),
        (45, True, "fog", "Magla"),
        (48, True, "fog", "inje"),
        (51, True, "drizzle", "Rosulja"),
        (56, True, "sleet", "Ledena"),
        (61, True, "rain", "Kiša"),
        (65, True, "rain", "Jaka"),
        (66, True, "sleet", "ledi"),
        (71, True, "snow", "Snijeg"),
        (77, True, "snow", "zrna"),
        (80, True, "rain", "Pljuskovi"),
        (85, True, "snow", "pljuskovi"),
        (95, True, "thunder", "Grmljavina"),
        (96, True, "thunder", "gradom"),
    ],
)
def test_wmo_mapping(code, is_day, icon, label_contains):
    out = wmo_condition(code, is_day)
    assert out["icon"] == icon
    assert label_contains.lower() in out["label"].lower()


def test_wmo_unknown_and_null_never_sun():
    for code in (None, 999, 1234):
        out = wmo_condition(code, True)
        assert out["icon"] == "unknown"
        assert out["label"] == "Nepoznati uvjeti"


def test_weather_now_ok_contract(client):
    api, fake, _s = client
    fake.current_payload = _payload()
    r = api.get("/api/assets/krnovo/weather-now")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["site_id"] == "krnovo"
    assert d["source_provider"] == "open-meteo"
    assert d["source_model"] == "best_match"
    assert d["weather_code"] == 71
    assert d["condition"] == "Snijeg"
    assert d["icon"] == "snow"
    assert d["temperature_2m_c"] == pytest.approx(-7.3)
    assert d["is_day"] is True
    assert d["valid_at_utc"] == f"{NOW_STR}:00Z"
    assert d["stale"] is False
    assert d["status"] == "ok"
    assert d["snowfall_cm"] == pytest.approx(1.2)
    assert "NIJE mjerenje" in d["note"]


def test_weather_now_cache_within_ttl_no_second_upstream(client):
    api, fake, _s = client
    fake.current_payload = _payload(code=0, temp=17.2)
    r1 = api.get("/api/assets/krnovo/weather-now")
    assert r1.status_code == 200
    assert (r1.json().get("cache") or {}).get("cached") is not True
    r2 = api.get("/api/assets/krnovo/weather-now")
    assert r2.json()["cache"]["cached"] is True
    assert len(fake.current_calls) == 1, "10-min cache: repeated reads do NOT re-hit upstream"


def test_weather_now_site_isolation_no_shared_cache(client):
    api, fake, _s = client
    payloads = {"krnovo": _payload(code=71, temp=-2.4), "site-b": _payload(code=0, temp=25.0)}

    def _per_site(lat, lon):
        return payloads["site-b"] if abs(lat - 45.0) < 0.1 else payloads["krnovo"]

    fake.fetch_current = _per_site  # type: ignore[method-assign]
    api.post(
        "/api/assets",
        json={"id": "site-b", "label": "B", "latitude": 45.0, "longitude": 16.0, "capacity_mw": 4.0, "turbine_count": 2, "turbine_rated_mw": 2.0, "hub_height_m": 90, "timezone": "Europe/Podgorica", "power_curve_id": "ge103-2750-oedb"},
    )
    a = api.get("/api/assets/krnovo/weather-now").json()
    b = api.get("/api/assets/site-b/weather-now").json()
    assert a["temperature_2m_c"] == pytest.approx(-2.4)
    assert b["temperature_2m_c"] == pytest.approx(25.0), "site switch never shows site A's weather"


def test_weather_now_provider_failure_sanitised_wind_now_intact(client):
    api, fake, _s = client
    fake.current_error = RuntimeError("network down")
    r = api.get("/api/assets/krnovo/weather-now")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "unavailable"
    assert "RuntimeError" in d["reason"]
    assert d["temperature_2m_c"] is None and d["icon"] == "unknown"
    # Wind NOW and forecast unaffected by the weather failure
    w = api.get("/api/assets/krnovo/wind-now")
    assert w.status_code == 200
    f = api.get("/api/forecast", params={"days": 2, "site_id": "krnovo"})
    assert f.status_code == 200


def test_weather_now_partial_code_without_temperature(client):
    api, fake, _s = client
    fake.current_payload = {"current": {"time": NOW_STR, "weather_code": 61, "is_day": 0}}
    d = api.get("/api/assets/krnovo/weather-now").json()
    assert d["status"] == "partial"
    assert d["temperature_2m_c"] is None, "null temperature is not 0 °C"
    assert d["condition"] == "Kiša"


def test_weather_now_stale_old_timestamp(client):
    api, fake, _s = client
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    fake.current_payload = _payload(code=3, temp=3.0, time_str=old)
    d = api.get("/api/assets/krnovo/weather-now").json()
    assert d["stale"] is True
