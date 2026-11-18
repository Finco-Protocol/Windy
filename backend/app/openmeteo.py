"""Open-Meteo HTTP client (forecast + Single Runs archive).

Retry/backoff-on-429 and the missing-run error attribution are a Python port of
meteocompare's `src/api/openMeteo.ts` and `src/api/omSingleRuns.ts`
(upstream commit 9d6bb7bf7ad33c416229d8afc02447d43b540250, MIT). Adaptations:
httpx instead of fetch, per-model single requests, wind in m/s, explicit UTC.

The Single Runs API (https://single-runs-api.open-meteo.com/v1/forecast, `&run=`)
serves *archived forecast runs* — what each model actually produced at a given
run cycle — which is what makes honest historical comparison possible. It is
distinct from (a) the live forecast API and (b) the reanalysis archive; Windy
never substitutes one for another.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from .registry import ModelSpec

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"

MAX_RETRIES = 3
BASE_DELAY_MS = 1000
MAX_DELAY_MS = 20_000


class OMError(Exception):
    """Any open-meteo failure other than a attributable missing run."""


class ModelRunUnavailable(OMError):
    """The requested model/run combination does not exist in the archive."""


# --- missing-run attribution (port of omSingleRuns.parseMissingModel) ---------

# open-meteo reports a missing run two ways: a JSON 4xx whose reason names
# "...Model: <component>, run: ..." and a streamed 200 abort containing
# "...modelRunUnavailable(model: ...)" mid-body. The component is open-meteo's
# *internal* id, not necessarily the registry id we sent.
_MISSING_MODEL_RE = re.compile(r"model:\s*(?:[A-Za-z0-9_]+\.)*([a-z0-9_]+)", re.IGNORECASE)

_COMPONENT_TO_REGISTRY_ID: dict[str, str] = {
    "ncep_gfs025": "gfs_seamless",
    "ncep_gfs_graphcast025": "gfs_graphcast025",
    "dwd_icon": "icon_global",
    "dwd_icon_eu": "icon_eu",
    "dwd_icon_d2": "icon_d2",
    "ecmwf_ifs025": "ecmwf_ifs",
    "ecmwf_ifs": "ecmwf_ifs",
}


def parse_missing_model(message: str) -> str | None:
    m = _MISSING_MODEL_RE.search(message)
    return m.group(1) if m else None


def resolve_missing_id(named: str, requested: str) -> bool:
    """True when the error is attributable to the single model we requested."""
    return named == requested or _COMPONENT_TO_REGISTRY_ID.get(named) == requested


# --- retry helpers (port of openMeteo.ts backoff) -----------------------------


def _backoff_ms(attempt: int) -> float:
    ceiling = min(MAX_DELAY_MS, BASE_DELAY_MS * 2**attempt)
    return ceiling / 2 + random.random() * (ceiling / 2)


def _retry_after_ms(headers: httpx.Headers) -> float | None:
    header = headers.get("Retry-After")
    if not header:
        return None
    try:
        return min(MAX_DELAY_MS, max(0.0, float(header) * 1000))
    except ValueError:
        return None


class OpenMeteoClient:
    """Thin open-meteo client with 429 retry; injectable for tests."""

    def __init__(self, timeout_s: float = 40.0, max_retries: int = MAX_RETRIES, sleep=time.sleep) -> None:
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s))
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    # -- core ---------------------------------------------------------------

    def _get_json(self, url: str, params: dict[str, str], requested_model: str | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = OMError(f"open-meteo network error: {exc}")
                if attempt >= self._max_retries:
                    raise last_error from exc
                self._sleep(_backoff_ms(attempt) / 1000)
                continue
            if resp.status_code == 429 and attempt < self._max_retries:
                delay = _retry_after_ms(resp.headers) or _backoff_ms(attempt)
                self._sleep(delay / 1000)
                continue
            text = resp.text
            if resp.status_code != 200:
                self._raise_for_body(resp.status_code, text, requested_model)
            try:
                data = resp.json()
            except ValueError as exc:
                # A 200 with an unparseable body can be open-meteo's *streamed*
                # failure shape (see module docstring / upstream notes).
                self._raise_for_body(200, text, requested_model)
                raise OMError(f"open-meteo returned unparseable body: {text[:300]}") from exc
            if data.get("error"):
                self._raise_for_body(400, str(data.get("reason", "unknown error")), requested_model)
            return data
        raise last_error or OMError("open-meteo: retries exhausted")

    def _raise_for_body(self, status: int, text: str, requested_model: str | None) -> None:
        marker = parse_missing_model(text or "")
        if marker is not None and requested_model is not None and resolve_missing_id(marker, requested_model):
            raise ModelRunUnavailable(
                f"run not available for {requested_model}: {(text or '').strip()[:500]}"
            )
        raise OMError(f"open-meteo {status}: {(text or '').strip()[:500]}")

    # -- endpoints ----------------------------------------------------------

    def _base_params(self, lat: float, lon: float, spec: ModelSpec, days: int) -> dict[str, str]:
        hourly_vars = [f"wind_speed_{h}m" for h in spec.speed_heights]
        hourly_vars += [f"wind_direction_{h}m" for h in spec.direction_heights]
        return {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "hourly": ",".join(hourly_vars),
            "models": spec.id,
            "forecast_days": str(days),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }

    def fetch_current(self, lat: float, lon: float) -> dict:
        """Correction E Addendum C: read-only CURRENT weather block
        (temperature_2m, weather_code, is_day, cloud_cover, precipitation,
        rain, showers, snowfall) from the free public /v1/forecast endpoint
        (best_match). One bounded request per call — the endpoint caches."""
        return self._get_json(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": f"{lat:.6f}",
                "longitude": f"{lon:.6f}",
                "current": "temperature_2m,weather_code,is_day,cloud_cover,precipitation,rain,showers,snowfall",
                "timezone": "UTC",
                "wind_speed_unit": "ms",
            },
            requested_model=None,
        )

    def fetch_forecast(self, lat: float, lon: float, spec: ModelSpec, days: int) -> dict:
        """Live forecast (latest run served by the endpoint; issue time unknown)."""
        return self._get_json(FORECAST_URL, self._base_params(lat, lon, spec, days))

    def fetch_single_run(self, lat: float, lon: float, spec: ModelSpec, run_iso: str, days: int) -> dict:
        """One archived run by its UTC init time (`run=YYYY-MM-DDTHH:00`)."""
        params = self._base_params(lat, lon, spec, days)
        params["run"] = run_iso
        return self._get_json(SINGLE_RUNS_URL, params, requested_model=spec.id)

    def latest_available_run(
        self,
        lat: float,
        lon: float,
        spec: ModelSpec,
        days: int,
        now: datetime | None = None,
        max_probes: int = 4,
    ) -> tuple[str, dict]:
        """Find the newest archived run at/before `now` by probing 6-hourly run
        cycles backwards (models publish 00/06/12/18 UTC runs with publication
        latency, so the newest cycle is often not yet in the archive).

        Returns (run_iso, raw_json). Raises ModelRunUnavailable if none of the
        probed cycles exists — callers decide the fallback.
        """
        now = now or datetime.now(timezone.utc)
        base = now.replace(minute=0, second=0, microsecond=0)
        base -= timedelta(hours=base.hour % 6)  # most recent 6h boundary
        last_error: Exception | None = None
        for k in range(max_probes):
            run_dt = base - timedelta(hours=6 * k)
            run_iso = run_dt.strftime("%Y-%m-%dT%H:00")
            try:
                return run_iso, self.fetch_single_run(lat, lon, spec, run_iso, days)
            except ModelRunUnavailable as exc:
                last_error = exc
                continue
        raise ModelRunUnavailable(
            f"no {spec.id} run found in the archive within the last {max_probes} cycles"
        ) from last_error


def extract_series(hourly: dict, variable: str, model_id: str) -> list:
    """Read a per-model hourly series. open-meteo suffixes variable keys with the
    model id when more than one model is requested and returns bare keys for a
    single model — handle both (port of omForecast.extractHourlyByModel)."""
    suffixed = hourly.get(f"{variable}_{model_id}")
    if suffixed is not None:
        return suffixed
    bare = hourly.get(variable)
    if bare is not None:
        return bare
    raise OMError(f"open-meteo response has no series for {variable} ({model_id})")
