"""Correction E Addendum C — WMO weather interpretation codes → display.

Central, fully testable mapping for the Weather on Site card. Unknown/null
codes map to a neutral "unknown" condition — never a default sun.
"""

from __future__ import annotations

# icon keys the frontend renders as distinct SVGs
ICONS = ("sun", "moon", "partly_day", "partly_night", "cloud", "fog", "drizzle", "rain", "sleet", "snow", "thunder", "unknown")

_LABELS: dict[int, tuple[str, str]] = {
    0: ("sun", "Sunčano"),
    1: ("partly_day", "Pretežno vedro"),
    2: ("partly_day", "Djelomično oblačno"),
    3: ("cloud", "Oblačno"),
    45: ("fog", "Magla"),
    48: ("fog", "Magla s injem"),
    51: ("drizzle", "Rosulja"),
    53: ("drizzle", "Rosulja"),
    55: ("drizzle", "Gusta rosulja"),
    56: ("sleet", "Ledena rosulja"),
    57: ("sleet", "Gusta ledena rosulja"),
    61: ("rain", "Kiša"),
    63: ("rain", "Kiša"),
    65: ("rain", "Jaka kiša"),
    66: ("sleet", "Kiša koja se ledi"),
    67: ("sleet", "Jaka kiša koja se ledi"),
    71: ("snow", "Snijeg"),
    73: ("snow", "Snijeg"),
    75: ("snow", "Gusti snijeg"),
    77: ("snow", "Snježna zrna"),
    80: ("rain", "Pljuskovi"),
    81: ("rain", "Pljuskovi"),
    82: ("rain", "Jaki pljuskovi"),
    85: ("snow", "Snježni pljuskovi"),
    86: ("snow", "Snježni pljuskovi"),
    95: ("thunder", "Grmljavina"),
    96: ("thunder", "Grmljavina s gradom"),
    99: ("thunder", "Grmljavina s gradom"),
}

NIGHT_LABELS = {0: "Vedro", 1: "Pretežno vedro", 2: "Djelomično oblačno"}


def wmo_condition(code: int | None, is_day: bool | None) -> dict:
    """Map a WMO code + day/night flag to the display contract.

    Returns {icon, label}. Unknown/null code → neutral unknown condition
    (never sun). Night variants apply only to the clear/partly codes; all
    precipitation/fog/thunder icons are day/night identical (icon is
    recognizable regardless).
    """
    if code is None:
        return {"icon": "unknown", "label": "Nepoznati uvjeti"}
    entry = _LABELS.get(code)
    if entry is None:
        return {"icon": "unknown", "label": "Nepoznati uvjeti"}
    icon, label = entry
    if is_day is False:
        if code == 0:
            return {"icon": "moon", "label": NIGHT_LABELS[0]}
        if code in (1, 2):
            return {"icon": "partly_night", "label": NIGHT_LABELS[code]}
    return {"icon": icon, "label": label}
