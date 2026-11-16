"""Turbine power curves and farm power aggregation.

Curve provenance is explicit and always carried through to the API/UI:

- Primary: the manufacturer-class power curve for the **GE 2.75-103** turbine
  (Krnovo: 26 × 2.75 MW) read from the `windpowerlib` bundled local turbine
  database (OpenEnergy Database wind turbine library snapshot, type
  ``GE103/2750``). This is a *manufacturer-class reference* curve — the exact
  as-installed behaviour (site air density, blade condition, losses) is not
  calibrated, so modelled MWh are labelled accordingly.
- Fallback (only if `windpowerlib` cannot be imported): a clearly labelled
  synthetic demonstrative 2.75 MW-class curve — never presented as GE data.

Farm output = per-turbine curve value × turbine count × (1 − loss factor),
capped at the plant capacity (71.5 MW). No density correction is applied
anywhere (no silent double-application of factors).
"""

from __future__ import annotations

from dataclasses import dataclass

GE_CURVE_ID = "ge103-2750-oedb"
GE_TURBINE_TYPE = "GE103/2750"
DEMO_CURVE_ID = "demo-generic-2750"

CURVE_STATUS_MANUFACTURER = "manufacturer-reference"
CURVE_STATUS_DEMO = "demo-illustrative"


@dataclass(frozen=True)
class PowerCurve:
    id: str
    label: str
    status: str  # CURVE_STATUS_*
    source: str
    provenance: str
    rated_power_mw: float
    cut_in_mps: float | None
    rated_mps: float | None
    cut_out_mps: float | None
    # (wind speed m/s, power MW per turbine), ascending by speed
    points: tuple[tuple[float, float], ...]

    def power_mw_per_turbine(self, wind_speed_mps: float | None) -> float | None:
        """Piecewise-linear curve evaluation.

        Below the curve's cut-in and above its last point (cut-out) the output
        is exactly zero — the explicit cut-in clamp matters because the OEDB
        curve's interpolated ramp otherwise leaks a sliver of power just under
        the cut-in speed. Returns None for a None input (unresolved wind speed)
        so callers never mistake "no data" for "no wind".
        """
        if wind_speed_mps is None:
            return None
        pts = self.points
        if not pts:
            return None
        v = wind_speed_mps
        if v < 0:
            return 0.0
        if self.cut_in_mps is not None and v < self.cut_in_mps:
            return 0.0
        if v <= pts[0][0]:
            return pts[0][1]
        if v >= pts[-1][0]:
            # Beyond the last curve point (cut-out): no production.
            return pts[-1][1] if v == pts[-1][0] else 0.0
        for (v1, p1), (v2, p2) in zip(pts, pts[1:]):
            if v1 <= v <= v2:
                if v2 == v1:
                    return p2
                return p1 + (p2 - p1) * (v - v1) / (v2 - v1)
        return 0.0


def curve_from_windpowerlib(turbine_type: str = GE_TURBINE_TYPE, hub_height: float = 90.0) -> PowerCurve:
    """Load the manufacturer-class curve from windpowerlib's local database.

    Values come out in watts; converted to MW per turbine here.
    """
    from windpowerlib import WindTurbine  # imported lazily; optional dependency path

    turbine = WindTurbine(turbine_type=turbine_type, hub_height=hub_height)
    frame = turbine.power_curve
    pts = tuple(
        (float(row["wind_speed"]), round(float(row["value"]) / 1_000_000.0, 6))
        for row in frame.to_dict("records")
    )
    rated = max(p for _, p in pts)
    nonzero = [v for v, p in pts if p > 0]
    cut_in = min(nonzero) if nonzero else None
    rated_v = next((v for v, p in pts if p >= rated), None)
    cut_out = max(nonzero) if nonzero else None
    return PowerCurve(
        id=GE_CURVE_ID,
        label=f"{turbine_type} (GE 2.75-103 class)",
        status=CURVE_STATUS_MANUFACTURER,
        source="windpowerlib bundled local turbine database (OEDB wind turbine library snapshot)",
        provenance=(
            f"Turbine type '{turbine_type}' from the windpowerlib local turbine database "
            "(OpenEnergy Database wind turbine library snapshot), hub height "
            f"{hub_height:g} m; {len(pts)} curve points, {cut_in:g}–{cut_out:g} m/s, "
            f"max {rated:.3f} MW. Manufacturer-class reference data: NOT calibrated for "
            "the specific Krnovo site (density, losses, blade condition)."
        ),
        rated_power_mw=rated,
        cut_in_mps=cut_in,
        rated_mps=rated_v,
        cut_out_mps=cut_out,
        points=pts,
    )


def demo_curve_2p75() -> PowerCurve:
    """Synthetic demonstrative 2.75 MW-class curve (fallback; clearly labelled).

    Cubic ramp between cut-in (3 m/s) and rated (12 m/s), flat to cut-out
    (25 m/s), zero elsewhere. NOT manufacturer data.
    """
    cut_in, rated_v, cut_out, rated = 3.0, 12.0, 25.0, 2.75
    pts: list[tuple[float, float]] = []
    v = 0.0
    while v <= cut_out + 1e-9:
        if v < cut_in:
            p = 0.0
        elif v < rated_v:
            p = rated * (v**3 - cut_in**3) / (rated_v**3 - cut_in**3)
        else:
            p = rated
        pts.append((round(v, 2), round(p, 6)))
        v += 0.5
    pts.append((round(cut_out + 0.01, 2), 0.0))  # explicit zero beyond cut-out
    return PowerCurve(
        id=DEMO_CURVE_ID,
        label="Generic 2.75 MW class (DEMONSTRATIVE)",
        status=CURVE_STATUS_DEMO,
        source="synthetic",
        provenance=(
            "Synthetic demonstrative curve for the 2.75 MW class: cubic ramp between "
            "cut-in 3.0 m/s and rated 12.0 m/s, flat to cut-out 25.0 m/s. This is NOT "
            "manufacturer data and must be replaced with the real turbine curve before "
            "operational use."
        ),
        rated_power_mw=rated,
        cut_in_mps=cut_in,
        rated_mps=rated_v,
        cut_out_mps=cut_out,
        points=tuple(pts),
    )


def load_curve() -> PowerCurve:
    """Manufacturer-class curve when windpowerlib is available, else demo."""
    try:
        return curve_from_windpowerlib()
    except Exception:  # pragma: no cover - exercised only without windpowerlib
        return demo_curve_2p75()


def farm_power_mw(
    wind_speed_mps: float | None,
    curve: PowerCurve,
    turbine_count: int,
    loss_factor: float,
    capacity_mw: float,
) -> float | None:
    """Farm-level power (MW) for one instant/interval-average hub wind speed.

    None input propagates (unknown wind ≠ zero wind). Output is non-negative
    and hard-capped at the plant capacity regardless of the curve's shape.
    """
    per_turbine = curve.power_mw_per_turbine(wind_speed_mps)
    if per_turbine is None:
        return None
    if loss_factor < 0 or loss_factor >= 1:
        raise ValueError("loss_factor must be in [0, 1)")
    raw = per_turbine * turbine_count * (1.0 - loss_factor)
    return round(min(max(raw, 0.0), capacity_mw), 6)


def interval_energy_mwh(power_mw: float | None, interval_hours: float) -> float | None:
    """Energy for one interval: average MW × duration. None stays None."""
    if power_mw is None:
        return None
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return round(power_mw * interval_hours, 6)
