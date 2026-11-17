"""Shared test fixtures: deterministic fake Open-Meteo client + app harness.

All fixtures are clearly synthetic. The UI/API never serves them as real
forecasts — these tests only exercise code paths.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.openmeteo import ModelRunUnavailable, OMError  # noqa: E402


def make_raw_run(times: list[str], ws_by_height: dict[int, list], dir_by_height: dict[int, list] | None = None) -> dict:
    hourly: dict = {"time": times}
    for h, vals in ws_by_height.items():
        hourly[f"wind_speed_{h}m"] = vals
    for h, vals in (dir_by_height or {}).items():
        hourly[f"wind_direction_{h}m"] = vals
    return {"hourly": hourly}


class FakeOMClient:
    """Stand-in for OpenMeteoClient with a scripted archive.

    Test hooks beyond the happy path:
    - a run not pushed raises ModelRunUnavailable (as the real API does);
    - `om_error_runs` raises a generic OMError (network-ish failure);
    - `gates[(model_id, run_iso)]` blocks fetch_single_run on an Event until
      set, making worker-thread scheduling deterministic in tests.
    """

    def __init__(self) -> None:
        # model_id -> list of (run_iso, raw); the LAST element is "newest".
        self.runs: dict[str, list[tuple[str, dict]]] = {}
        self.live: dict[str, dict] = {}
        self.om_error_runs: set[tuple[str, str]] = set()
        self.gates: dict[tuple[str, str], "threading.Event"] = {}
        self.requests: list[tuple[str, str]] = []
        # Correction E Addendum C: scripted /v1/forecast  weather
        self.current_payload: dict = {
            "current": {"time": "2026-11-18T05:30", "temperature_2m": 6.7, "weather_code": 61, "is_day": 1, "cloud_cover": 100, "precipitation": 0.5, "rain": 0.5, "showers": 0.0, "snowfall": 0.0}
        }
        self.current_error: Exception | None = None
        self.current_calls: list[tuple[float, float]] = []

    def fetch_current(self, lat: float, lon: float) -> dict:
        self.current_calls.append((lat, lon))
        if self.current_error is not None:
            raise self.current_error
        return self.current_payload

    def push_run(self, model_id: str, run_iso: str, raw: dict) -> None:
        self.runs.setdefault(model_id, []).append((run_iso, raw))

    def latest_available_run(self, lat, lon, spec, days, now=None, max_probes=4):
        stack = self.runs.get(spec.id) or []
        if not stack:
            raise ModelRunUnavailable(f"no {spec.id} run found in the archive within the last {max_probes} cycles")
        return stack[-1]

    def fetch_forecast(self, lat, lon, spec, days):
        if spec.id in self.live:
            return self.live[spec.id]
        raise OMError("open-meteo network error: fake client has no live fixture")

    def fetch_single_run(self, lat, lon, spec, run_iso, days):
        self.requests.append((spec.id, run_iso))
        gate = self.gates.get((spec.id, run_iso))
        if gate is not None:
            gate.wait()
        if (spec.id, run_iso) in self.om_error_runs:
            raise OMError(f"fake network failure for {spec.id} {run_iso}")
        for iso, raw in reversed(self.runs.get(spec.id, [])):
            if iso == run_iso:
                return raw
        raise ModelRunUnavailable(f"run {run_iso} not available for {spec.id}: modelRunUnavailable(model: fake)")


@pytest.fixture()
def fake_client() -> FakeOMClient:
    return FakeOMClient()


@pytest.fixture()
def client(tmp_path, fake_client):
    """TestClient around a Windy app with a temp DB and the fake OM client."""
    settings = Settings()
    settings.db_path = tmp_path / "test-windy.db"
    settings.scheduler_enabled = False
    application = create_app(settings=settings, om_client=fake_client)
    with TestClient(application) as test_client:
        yield test_client, fake_client, settings


def wait_for_job_status(api, job_id: int, target: set[str], timeout_s: float = 5.0) -> dict:
    """Poll a batch job until it reaches one of `target` (or time out)."""
    deadline = time.monotonic() + timeout_s
    job = None
    while time.monotonic() < deadline:
        job = api.get(f"/api/backfill/batch/{job_id}").json()
        if job["status"] in target:
            return job
        time.sleep(0.02)
    return job  # last observed state; the caller's assertion will fail loudly
