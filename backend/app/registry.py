"""Windy model registry and the default site asset.

Model definitions are adapted from meteocompare's `src/domain/models.ts`
(upstream commit 9d6bb7bf7ad33c416229d8afc02447d43b540250), trimmed to the
wind-farm use case. Heights note *observed + documented* availability: request
parameters were verified against live Open-Meteo responses for the Krnovo
location on 2026-11-15 (all listed wind levels returned values; ECMWF/GFS have
no 180 m wind via open-meteo).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .height import HEIGHT_METHOD_LOG_INTERP, HEIGHT_METHOD_PROXY

SOURCE_PROVIDER = "open-meteo"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    label: str
    max_lead_hours: int
    request_days: int  # clamped to the model's real horizon; never over-extends
    speed_heights: tuple[int, ...]
    direction_heights: tuple[int, ...]
    height_method: str
    note: str
    met_only: bool = False  # surface-only model: comparison, excluded from MWh


WINDY_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="ecmwf_ifs",
        provider="ECMWF",
        label="ECMWF IFS HRES",
        max_lead_hours=240,
        request_days=10,
        speed_heights=(100,),
        direction_heights=(100,),
        height_method=HEIGHT_METHOD_PROXY,
        met_only=False,
        note=(
            "ECMWF IFS publishes 10 m and 100 m wind natively. The 100 m level is used "
            "directly as a PROXY for the 90 m hub (+10 m above hub) — no interpolation "
            "from 10 m surface wind."
        ),
    ),
    ModelSpec(
        id="gfs_seamless",
        provider="NOAA",
        label="NOAA GFS",
        max_lead_hours=384,
        request_days=16,
        speed_heights=(80, 100),
        direction_heights=(100,),
        height_method=HEIGHT_METHOD_LOG_INTERP,
        note=(
            "GFS publishes 80 m and 100 m wind; the 90 m hub value is log-height "
            "interpolated between 80 and 100 m. Direction taken at 100 m."
        ),
    ),
    ModelSpec(
        id="icon_global",
        provider="DWD",
        label="DWD ICON Global",
        max_lead_hours=192,
        request_days=8,
        speed_heights=(80, 120),
        direction_heights=(120,),
        height_method=HEIGHT_METHOD_LOG_INTERP,
        note=(
            "ICON Global publishes 80 m and 120 m wind; the 90 m hub value is "
            "log-height interpolated between 80 and 120 m. Direction taken at 120 m. "
            "Horizon ends at 192 h (8 days) — later hours do not exist."
        ),
    ),
    ModelSpec(
        id="icon_eu",
        provider="DWD",
        label="DWD ICON-EU",
        max_lead_hours=120,
        request_days=5,
        speed_heights=(80, 120),
        direction_heights=(120,),
        height_method=HEIGHT_METHOD_LOG_INTERP,
        note=(
            "ICON-EU publishes 80 m and 120 m wind; the 90 m hub value is log-height "
            "interpolated between 80 and 120 m. Direction taken at 120 m. Horizon "
            "ends at 120 h (5 days) — never extended to 7/10 days."
        ),
    ),
    ModelSpec(
        id="meteofrance_arpege_world",
        provider="Météo-France",
        label="ARPEGE Europe",
        max_lead_hours=96,
        request_days=4,
        speed_heights=(80, 100),
        direction_heights=(100,),
        height_method=HEIGHT_METHOD_LOG_INTERP,
        note=(
            "ARPEGE Europe publishes 80 m and 100 m wind (probe 2026-11-17: all "
            "levels non-null at Krnovo); 90 m hub is log-height interpolated "
            "between 80 and 100 m. Horizon ~4 days."
        ),
    ),
    ModelSpec(
        id="gem_seamless",
        provider="Environment Canada",
        label="GEM Global",
        max_lead_hours=240,
        request_days=10,
        speed_heights=(100,),
        direction_heights=(100,),
        height_method=HEIGHT_METHOD_PROXY,
        note=(
            "GEM publishes 10 m, 100 m and 120 m wind (probe 2026-11-17: 80 m is "
            "null); the native 100 m level is used directly as a PROXY for the "
            "90 m hub (+10 m above hub). Horizon ~10 days."
        ),
    ),
    ModelSpec(
        id="ukmo_seamless",
        provider="UK Met Office",
        label="UKMO Global (10 m samo)",
        max_lead_hours=168,
        request_days=7,
        speed_heights=(10,),
        direction_heights=(10,),
        height_method=HEIGHT_METHOD_PROXY,
        note=(
            "MET_ONLY: UKMO Global provides surface 10 m wind; hub-relevant "
            "levels (80–120 m) are null at Krnovo (probe 2026-11-17). Shown as a "
            "10 m meteorological comparison ONLY — excluded from MWh, Average "
            "and production totals. No invented shear extrapolation."
        ),
        met_only=True,
    ),
)

MODEL_INDEX: dict[str, ModelSpec] = {m.id: m for m in WINDY_MODELS}


def get_model(model_id: str) -> ModelSpec:
    try:
        return MODEL_INDEX[model_id]
    except KeyError:
        raise KeyError(f"unknown model id: {model_id}") from None


KRNOVO_ASSUMPTIONS: dict[str, str] = {
    "coordinates": (
        "Approximate center of the Krnovo wind farm (≈42.8756 N, 19.1106 E) from "
        "public references. Not survey-grade; one representative point is used for "
        "all model queries."
    ),
    "capacity_mw": "74.10 MW = 26 × 2.85 MW (vlasnička ispravka 2026-11-17; fizički nameplate). Za POSTOJEĆU instalaciju vrijedi samo nakon eksplicitne config-revizije — seed ne mijenja već konfigurirane siteove.",
    "turbine_model": (
        "GE 2.85-116 klasa (26 jedinica) prema vlasničkoj ispravci 2026-11-17. "
        "Krivulja je referentna (NE kalibrirana) i koristi skaliranje — oznaka "
        "REFERENCE/UNVERIFIED ostaje vidljiva."
    ),
    "hub_height_m": (
        "90 m PROVISIONAL (editable setting). Publicly reported reference value; "
        "no as-built confirmation is available. Verify against project documentation "
        "before treating MWh as calibrated."
    ),
    "loss_factor": (
        "0.0 by default — no calibrated wake/availability/electrical loss value is "
        "available. Editable per asset."
    ),
    "power_curve": (
        "GE103/2750 manufacturer-class curve (OEDB snapshot, peak 2.78 MW) from the "
        "windpowerlib bundled turbine database, explicitly SCALED to the 2.85 MW unit "
        "rating via owner opt-in (scaled-reference). This is NOT a verified as-built "
        "GE 2.85-116 power curve — no such curve is available free of charge. All "
        "modelled MWh remain REFERENCE/UNVERIFIED until calibrated against actuals."
    ),
    "timezone": "Europe/Podgorica (CET/CEST) used for local-time display and CSV import defaults.",
}


KRNOVO_ASSET: dict = {
    "id": "krnovo",
    "name": "Krnovo Wind Farm",
    "country": "Montenegro",
    "latitude": 42.8756,
    "longitude": 19.1106,
    "capacity_mw": 74.1,
    "turbine_count": 26,
    "turbine_model": "GE-klasa 2,85 MW (provisional) — krivulja je referentna (ge103-2750 scaled), NIJE as-built 2.85-116",
    "turbine_rated_mw": 2.85,
    "hub_height_m": 90.0,
    "loss_factor": 0.0,
    "timezone": "Europe/Podgorica",
    "assumptions": KRNOVO_ASSUMPTIONS,
    "notes": (
        "Initial Windy asset. Configuration values are publicly documented references; "
        "each field's origin is listed under 'assumptions'. All values are editable."
    ),
}
