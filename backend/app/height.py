"""Wind speed height resolution.

Krnovo's hub height (90 m) is a *provisional, editable* site setting. NWP
models publish wind at fixed levels (10/80/100/120/180 m), rarely exactly at
hub height, so hub-level wind is *resolved* from the levels a model actually
provides — and the method is recorded per model and shown in the UI:

- ``log-interp`` — log-height linear interpolation between two levels that
  bracket the hub height (e.g. GFS 80+100 m, ICON 80+120 m). Standard practice
  for the surface layer; uses scalar speeds and ignores stability/atmospheric
  stratification, which is a stated limitation.
- ``proxy`` — the nearest documented native level is used directly and clearly
  labelled (e.g. ECMWF IFS native 100 m for a 90 m hub).

Never silently relabels 10 m wind as hub wind.
"""

from __future__ import annotations

import math

HEIGHT_METHOD_LOG_INTERP = "log-interp"
HEIGHT_METHOD_PROXY = "proxy"


def log_interp_speed(v1: float, h1: float, v2: float, h2: float, h: float) -> float:
    """Interpolate wind speed linearly in log(height) between two bracketing levels.

    ``v(h) = v1 + (v2 - v1) * ln(h/h1) / ln(h2/h1)``
    """
    if h1 <= 0 or h2 <= 0 or h <= 0:
        raise ValueError("heights must be positive")
    if h1 == h2:
        raise ValueError("bracket levels must differ")
    lo, hi = min(h1, h2), max(h1, h2)
    if not (lo <= h <= hi):
        raise ValueError(f"target height {h} m is not bracketed by {h1} m and {h2} m")
    return v1 + (v2 - v1) * (math.log(h / h1) / math.log(h2 / h1))


def resolve_hub_speed(
    speeds_by_height: dict[float, float | None],
    hub_height: float,
    method: str,
) -> tuple[float | None, str]:
    """Resolve a hub-height speed from per-level speeds.

    Returns ``(speed_mps | None, status)`` where status is ``ok``, ``proxy``
    (single-level substitution) or ``missing-height`` (a needed level was null —
    the point is kept with a null speed rather than fabricated).
    """
    if method == HEIGHT_METHOD_PROXY:
        if len(speeds_by_height) != 1:
            raise ValueError("proxy method expects exactly one source height")
        v = next(iter(speeds_by_height.values()))
        return (v, "ok" if v is not None else "missing-height")
    if method == HEIGHT_METHOD_LOG_INTERP:
        if len(speeds_by_height) != 2:
            raise ValueError("log-interp method expects exactly two bracketing heights")
        (h1, v1), (h2, v2) = sorted(speeds_by_height.items())
        if v1 is None or v2 is None:
            return (None, "missing-height")
        return (log_interp_speed(v1, h1, v2, h2, hub_height), "ok")
    raise ValueError(f"unknown height method: {method}")
