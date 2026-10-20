"""OpenStreetMap / Overpass provider — station locations, no prices.

OSM is ODbL-licensed open data with no key and no ToS problem, but almost no US
station carries a `fuel:*:price` tag, so this provider returns stations with
empty price maps. Its job is to keep the API useful when the price provider is
blocked: you still get the correct list of stations near a point, flagged
`degraded` so consumers know prices are missing rather than zero.
"""

from __future__ import annotations

from typing import Any

from ..geo import haversine_mi, miles_to_meters
from ..http import ProviderError, make_client, request_json
from ..models import Address, Place, Station, Trend

#: Mirrors are tried in order. The main instance answers most of the time but
#: returns 502/504 under load, and the mirrors are not reachable from every
#: network, so the list is deliberately longer than it needs to be.
#:
#: Note: overpass-api.de's nginx answers 406 to requests without a real
#: User-Agent, which make_client always sets. Don't strip it.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

OSM_URL = "https://www.openstreetmap.org/{kind}/{id}"

QUERY_TEMPLATE = """
[out:json][timeout:25];
(
  node(around:{radius_m},{lat},{lon})[amenity=fuel];
  way(around:{radius_m},{lat},{lon})[amenity=fuel];
);
out center tags;
"""


def _name_of(tags: dict[str, Any]) -> str:
    for key in ("name", "brand", "operator"):
        if value := tags.get(key):
            return str(value)
    return "Unnamed fuel station"


def parse_element(element: dict[str, Any], origin: tuple[float, float]) -> Station | None:
    kind = element.get("type")
    native_id = element.get("id")
    if kind not in ("node", "way") or native_id is None:
        return None

    # Nodes carry lat/lon directly; ways carry a computed `center` from `out center`.
    if kind == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)

    tags = element.get("tags") or {}
    address = Address(
        line1=" ".join(
            str(tags[k]) for k in ("addr:housenumber", "addr:street") if tags.get(k)
        ).strip()
        or None,
        city=tags.get("addr:city"),
        region=tags.get("addr:state"),
        postal_code=tags.get("addr:postcode"),
        country=tags.get("addr:country"),
    )

    return Station(
        id=f"osm:{kind}/{native_id}",
        source=OverpassProvider.name,
        name=_name_of(tags),
        brand=tags.get("brand") or tags.get("operator"),
        address=address,
        lat=lat,
        lon=lon,
        distance_mi=round(haversine_mi(origin[0], origin[1], lat, lon), 2),
        url=OSM_URL.format(kind=kind, id=native_id),
    )


def parse_response(payload: dict[str, Any], origin: tuple[float, float]) -> list[Station]:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise ProviderError("overpass returned no elements")
    stations = [s for e in elements if isinstance(e, dict) and (s := parse_element(e, origin))]
    return sorted(stations, key=lambda s: (s.distance_mi or 9e9, s.name))


class OverpassProvider:
    name = "overpass"
    has_prices = False

    async def fetch(
        self,
        place: Place,
        *,
        radius_mi: float,
        fuels: list[str],
        limit: int,
    ) -> tuple[list[Station], Trend | None]:
        origin = (place.lat, place.lon)
        query = QUERY_TEMPLATE.format(
            radius_m=int(miles_to_meters(radius_mi)), lat=place.lat, lon=place.lon
        )

        last: Exception | None = None
        async with make_client(timeout=45.0) as client:
            for endpoint in ENDPOINTS:
                try:
                    # Overpass asks for exponential backoff with doubling when a
                    # server is at capacity, which it frequently is: 2s, 4s, 8s.
                    payload = await request_json(
                        client,
                        "POST",
                        endpoint,
                        attempts=4,
                        backoff=2.0,
                        data={"data": query},
                    )
                except ProviderError as exc:
                    last = exc
                    continue
                stations = parse_response(payload, origin)
                if not stations:
                    # An empty result from a busy server is indistinguishable from
                    # a genuinely empty area, so try the next mirror before
                    # concluding there are no stations here.
                    last = ProviderError(f"{endpoint} returned no stations")
                    continue
                return stations[:limit], None

        raise ProviderError(f"all overpass endpoints failed: {last}")
