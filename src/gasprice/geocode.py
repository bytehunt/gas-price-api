"""Turn "20850" / "Rockville, MD" / "39.08,-77.15" into coordinates.

Uses OpenStreetMap Nominatim, which is free and requires no key. Its usage
policy asks for an identifying User-Agent and at most 1 request/second, so
results are cached for a month — a ZIP code does not move.
"""

from __future__ import annotations

import asyncio

from .cache import JsonCache
from .geo import parse_latlon
from .http import ProviderError, make_client, request_json
from .models import Place

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
GEOCODE_TTL = 60 * 60 * 24 * 30  # a month

_rate_limit = asyncio.Lock()


async def geocode(
    query: str,
    *,
    cache: JsonCache | None = None,
    countrycodes: str | None = "us",
) -> Place:
    """Resolve a free-form location string to a Place.

    Coordinate pairs short-circuit without any network call.
    """
    query = query.strip()
    if not query:
        raise ProviderError("empty location query")

    coords = parse_latlon(query)
    if coords:
        return Place(query=query, display_name=query, lat=coords[0], lon=coords[1])

    cache = cache or JsonCache(ttl=GEOCODE_TTL)
    cache_key = f"{query}|{countrycodes}"
    if (hit := cache.get("geocode", cache_key)) is not None:
        return Place.model_validate(hit)

    params: dict[str, str | int] = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes

    async with _rate_limit:
        async with make_client() as client:
            data = await request_json(client, "GET", NOMINATIM_URL, params=params)
        await asyncio.sleep(1.0)  # Nominatim: max 1 req/sec

    if not data:
        raise ProviderError(f"could not geocode {query!r}")

    top = data[0]
    addr = top.get("address") or {}
    place = Place(
        query=query,
        display_name=top.get("display_name"),
        lat=float(top["lat"]),
        lon=float(top["lon"]),
        region=addr.get("state"),
        country=addr.get("country_code", "").upper() or None,
    )
    cache.set("geocode", cache_key, place.model_dump(mode="json"))
    return place


async def ensure_region(place: Place, *, cache: JsonCache | None = None) -> Place:
    """Fill in `region`/`country` for a place that came from raw coordinates.

    A `lat,lon` query skips geocoding entirely, but the area-average lookup needs
    to know which state it is in. Reverse geocoding is cached for a month, and a
    failure returns the place unchanged rather than raising.
    """
    if place.region or place.country:
        return place

    cache = cache if cache is not None else JsonCache(ttl=GEOCODE_TTL)
    cache_key = f"reverse|{place.lat:.4f},{place.lon:.4f}"
    if (hit := cache.get("geocode", cache_key)) is not None:
        return place.model_copy(update=hit)

    params = {
        "lat": place.lat,
        "lon": place.lon,
        "format": "jsonv2",
        "zoom": 10,
        "addressdetails": 1,
    }
    try:
        async with _rate_limit:
            async with make_client() as client:
                data = await request_json(client, "GET", NOMINATIM_REVERSE_URL, params=params)
            await asyncio.sleep(1.0)  # Nominatim: max 1 req/sec
    except ProviderError:
        return place

    addr = (data or {}).get("address") or {}
    update = {
        "region": addr.get("state"),
        "country": (addr.get("country_code") or "").upper() or None,
        "display_name": place.display_name or data.get("display_name"),
    }
    cache.set("geocode", cache_key, update)
    return place.model_copy(update=update)
