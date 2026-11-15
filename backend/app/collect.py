"""Forecast collection: fetch → resolve hub height → model power → archive.

Core integrity rules enforced here:

- Each forecast *issue/run* is stored immutably; a new collection of the same
  run is an idempotent upsert (no duplicates), a new issue cycle is a new row
  alongside the old one.
- The run's issue time comes from the Single Runs archive (`&run=`), never from
  the fetch time. The live-forecast fallback marks issue time explicitly
  unknown instead of assuming it.
- Hub wind speed is resolved per model's documented height policy; points whose
  source levels are null are kept with null speed + `missing-height` status —
  never interpolated from 10 m, never zero-filled.
- Modeled power uses the configured curve; energy integrates the actual
  interval duration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import db as dbm
from .height import resolve_hub_speed
from .openmeteo import OMError, OpenMeteoClient, extract_series
from .power import PowerCurve, farm_power_mw, interval_energy_mwh
from .registry import KRNOVO_ASSET, SOURCE_PROVIDER, ModelSpec, get_model

log = logging.getLogger("windy.collect")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> datetime:
    """Parse open-meteo UTC timestamps (`2026-11-14T00:00`, Z-less, naive=UTC)."""
    if s.endswith("Z"):
        return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class PointRow:
    valid_at_utc: str
    wind_speed_mps: float | None
    wind_direction_deg: float | None
    modeled_power_mw: float | None
    interval_energy_mwh: float | None
    interval_hours: float
    status: str


@dataclass
class ModelCollectResult:
    model_id: str
    status: str  # ok | ok-issue-unknown | UNAVAILABLE | error
    run_id: int | None = None
    run_issued_utc: str | None = None
    points: int = 0
    error: str | None = None
    height_method: str = ""
    note: str = ""
    height_sources: list[int] = field(default_factory=list)


def build_points(
    raw: dict,
    spec: ModelSpec,
    hub_height: float,
    curve: PowerCurve,
    turbine_count: int,
    loss_factor: float,
    capacity_mw: float,
) -> list[PointRow]:
    """Turn a raw open-meteo hourly payload into resolved point rows."""
    hourly = raw.get("hourly") or {}
    times = [parse_utc(t) for t in hourly.get("time") or []]
    if not times:
        return []

    def speeds_at(h: int) -> list:
        return extract_series(hourly, f"wind_speed_{h}m", spec.id)

    dirs: dict[int, list] = {}
    for h in spec.direction_heights:
        try:
            dirs[h] = extract_series(hourly, f"wind_direction_{h}m", spec.id)
        except OMError:
            dirs[h] = [None] * len(times)

    speed_arrays = {h: speeds_at(h) for h in spec.speed_heights}
    dir_height = min(spec.direction_heights, key=lambda h: abs(h - hub_height))

    rows: list[PointRow] = []
    for i, valid in enumerate(times):
        level_speeds: dict[float, float | None] = {}
        for h, arr in speed_arrays.items():
            level_speeds[float(h)] = arr[i] if i < len(arr) else None
        hub_speed, status = resolve_hub_speed(level_speeds, hub_height, spec.height_method)

        dir_arr = dirs.get(dir_height) or []
        direction = dir_arr[i] if i < len(dir_arr) else None

        power = farm_power_mw(hub_speed, curve, turbine_count, loss_factor, capacity_mw)
        interval_h = 1.0 if i == 0 else max((valid - times[i - 1]).total_seconds() / 3600.0, 0.0)
        energy = interval_energy_mwh(power, interval_h)
        rows.append(
            PointRow(
                valid_at_utc=iso_z(valid),
                wind_speed_mps=round(hub_speed, 4) if hub_speed is not None else None,
                wind_direction_deg=direction,
                modeled_power_mw=power,
                interval_energy_mwh=energy,
                interval_hours=interval_h,
                status=status,
            )
        )
    return rows


def _dedupe_key(run_issued_utc: str | None, fetched_at: datetime) -> str:
    if run_issued_utc:
        return run_issued_utc
    # Live-forecast fallback: issue time genuinely unknown. Dedupe per fetch hour
    # so a manual double-click doesn't duplicate rows either; the trade-off (two
    # collections in different hours of the same cycle create two rows) is
    # disclosed in the UI rather than papered over with a guessed issue time.
    return iso_z(fetched_at.replace(minute=0, second=0, microsecond=0))


def store_run(
    conn,
    site: dict,
    spec: ModelSpec,
    *,
    run_issued_utc: str | None,
    dedupe_key: str,
    issue_source: str,
    fetched_at_utc: str,
    source_url: str,
    forecast_days: int,
    points: list[PointRow],
    raw_json: str | None,
) -> tuple[int, bool]:
    """Insert a run (or find the existing identical one) and upsert its points.

    Returns (run_id, created). On re-collection the existing run row and its
    original raw payload are preserved; points are upserted idempotently.
    """
    cur = conn.execute(
        """
        INSERT INTO forecast_runs (
            site_id, source_provider, model_id, run_issued_utc, dedupe_key,
            issue_source, requested_height_m, height_method, height_sources_json,
            fetched_at_utc, source_url, forecast_days, status, error, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', NULL, ?)
        ON CONFLICT(site_id, model_id, dedupe_key, requested_height_m) DO NOTHING
        """,
        (
            site["id"],
            SOURCE_PROVIDER,
            spec.id,
            run_issued_utc,
            dedupe_key,
            issue_source,
            float(hub_from(site)),
            spec.height_method,
            dbm.j(list(spec.speed_heights)),
            fetched_at_utc,
            source_url,
            forecast_days,
            raw_json,
        ),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT id FROM forecast_runs WHERE site_id=? AND model_id=? AND dedupe_key=? AND requested_height_m=?",
            (site["id"], spec.id, dedupe_key, float(hub_from(site))),
        ).fetchone()
        run_id = int(row["id"])
        created = False
    else:
        run_id = int(cur.lastrowid)
        created = True

    conn.executemany(
        """
        INSERT INTO forecast_points (
            run_id, valid_at_utc, wind_speed_mps, wind_direction_deg,
            height_method, modeled_power_mw, interval_energy_mwh, interval_hours, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, valid_at_utc) DO UPDATE SET
            wind_speed_mps=excluded.wind_speed_mps,
            wind_direction_deg=excluded.wind_direction_deg,
            height_method=excluded.height_method,
            modeled_power_mw=excluded.modeled_power_mw,
            interval_energy_mwh=excluded.interval_energy_mwh,
            interval_hours=excluded.interval_hours,
            status=excluded.status
        """,
        [
            (
                run_id,
                p.valid_at_utc,
                p.wind_speed_mps,
                p.wind_direction_deg,
                spec.height_method,
                p.modeled_power_mw,
                p.interval_energy_mwh,
                p.interval_hours,
                p.status,
            )
            for p in points
        ],
    )
    return run_id, created


def hub_from(site: dict) -> float:
    return float(site["hub_height_m"])


def normalize_run_iso(run_iso: str) -> str:
    """Canonical `...Z` form for run issue stamps (storage + string comparisons)."""
    return iso_z(parse_utc(run_iso))


def collect_model(
    db_path,
    client: OpenMeteoClient,
    site: dict,
    curve: PowerCurve,
    spec: ModelSpec,
    days: int,
    now: datetime | None = None,
) -> ModelCollectResult:
    """Collect the newest available archived run for one model; fall back to the
    live forecast API only when the archive has nothing, marking issue time
    unknown. Never silently substitutes current forecast for a past issue."""
    now = now or datetime.now(timezone.utc)
    fetched = iso_z(now)
    result = ModelCollectResult(model_id=spec.id, status="error", height_method=spec.height_method, note=spec.note, height_sources=list(spec.speed_heights))

    params_hint = f"lat={site['latitude']},lon={site['longitude']},model={spec.id}"
    try:
        run_iso, raw = client.latest_available_run(site["latitude"], site["longitude"], spec, days, now=now)
        run_iso = normalize_run_iso(run_iso)
        issue_source = "single-runs-archive"
        source_url = f"single-runs-api.open-meteo.com (run={run_iso}, {params_hint})"
    except OMError as exc:
        log.warning("single-runs fetch failed for %s: %s — falling back to live forecast", spec.id, exc)
        result.status = "ok-issue-unknown"
        try:
            raw = client.fetch_forecast(site["latitude"], site["longitude"], spec, days)
        except OMError as exc2:
            result.status = "error"
            result.error = str(exc2)
            return result
        run_iso = None
        issue_source = "live-forecast-issue-unknown"
        source_url = f"api.open-meteo.com/v1/forecast ({params_hint})"

    points = build_points(raw, spec, hub_from(site), curve, int(site["turbine_count"]), float(site["loss_factor"]), float(site["capacity_mw"]))
    key = _dedupe_key(run_iso, now)
    with dbm.session(db_path) as conn:
        run_id, _created = store_run(
            conn,
            site,
            spec,
            run_issued_utc=run_iso,
            dedupe_key=key,
            issue_source=issue_source,
            fetched_at_utc=fetched,
            source_url=source_url,
            forecast_days=days,
            points=points,
            raw_json=dbm.j(raw),
        )
    result.status = "ok" if run_iso else "ok-issue-unknown"
    result.run_id = run_id
    result.run_issued_utc = run_iso
    result.points = len(points)
    return result


def collect_site(
    db_path,
    client: OpenMeteoClient,
    site_id: str = KRNOVO_ASSET["id"],
    days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Collect the newest run of every registered model for one site."""
    from .registry import WINDY_MODELS
    from .store import load_site_and_curve

    now = now or datetime.now(timezone.utc)
    site, curve = load_site_and_curve(db_path, site_id)
    days = days or 10
    results = [
        collect_model(db_path, client, site, curve, spec, min(days, spec.request_days), now=now)
        for spec in WINDY_MODELS
    ]
    return {
        "site_id": site_id,
        "collected_at_utc": iso_z(now),
        "models": [r.__dict__ for r in results],
    }
