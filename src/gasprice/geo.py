"""Distance math and query-string parsing."""

from __future__ import annotations

import math
import re

EARTH_RADIUS_MI = 3958.7613
MILES_PER_METER = 1 / 1609.344

_LATLON_RE = re.compile(
    r"^\s*(?P<lat>[-+]?\d{1,2}(?:\.\d+)?)\s*[,/ ]\s*(?P<lon>[-+]?\d{1,3}(?:\.\d+)?)\s*$"
)


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def miles_to_meters(miles: float) -> float:
    return miles * 1609.344


def parse_latlon(text: str) -> tuple[float, float] | None:
    """Accept ``"39.084,-77.152"`` and friends, so a query can skip geocoding.

    Returns None if the text is not a coordinate pair or is out of range.
    """
    m = _LATLON_RE.match(text)
    if not m:
        return None
    lat, lon = float(m.group("lat")), float(m.group("lon"))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return s.strip("-") or "location"
