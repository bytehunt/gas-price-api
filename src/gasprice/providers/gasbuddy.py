"""GasBuddy provider — the only practical source of US retail prices.

GasBuddy has no public/free REST API. Their website is a React app that talks
to a GraphQL endpoint at /graphql, and that is what this provider calls. It is
crowd-sourced data served without authentication, but it is *not* a documented
API: it is unversioned, can change without notice, and scraping it is contrary
to GasBuddy's Terms of Service. This provider exists because there is no free
licensed alternative for the US. If you need something contractually clean,
write another Provider against OPIS / GasBuddy Business and register it in
providers/__init__.py — nothing else in the codebase needs to change.

Expect HTTP 403 from datacenter IPs (including GitHub Actions runners) when
Cloudflare decides to challenge; callers treat that as a degraded run and fall
back to the Overpass provider for station locations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..geo import haversine_mi
from ..http import ProviderError, make_client, request_json
from ..models import (
    GASBUDDY_PRODUCT_TO_FUEL,
    Address,
    Place,
    Price,
    Station,
    Trend,
    fuel_id,
)

GRAPHQL_URL = "https://www.gasbuddy.com/graphql"
STATION_URL = "https://www.gasbuddy.com/station/{id}"
PAGE_SIZE_HINT = 20  # the endpoint decides; used only to cap pagination loops

# Only the fields we actually consume. A narrow selection set is less likely to
# break when they add or rename things.
QUERY = """
query LocationBySearchTerm($cursor: String, $fuel: Int, $lat: Float, $lng: Float,
                           $maxAge: Int, $search: String) {
  locationBySearchTerm(lat: $lat, lng: $lng, search: $search) {
    displayName
    latitude
    longitude
    countryCode
    regionCode
    trends {
      areaName
      today
      todayLow
      trend
    }
    stations(cursor: $cursor, fuel: $fuel, lat: $lat, lng: $lng, maxAge: $maxAge) {
      count
      cursor {
        next
      }
      results {
        id
        name
        latitude
        longitude
        priceUnit
        starRating
        ratingsCount
        address {
          line1
          line2
          locality
          region
          postalCode
          country
        }
        brands {
          name
          brandingType
        }
        prices {
          fuelProduct
          credit {
            nickname
            price
            postedTime
          }
          cash {
            nickname
            price
            postedTime
          }
        }
      }
    }
  }
}
"""


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parse_price(node: Any, fuel: str, payment: str) -> Price | None:
    """A price node with a missing/zero value means 'nobody has reported this'."""
    if not isinstance(node, dict):
        return None
    raw = node.get("price")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return Price(
        fuel=fuel,
        price=round(value, 3),
        payment=payment,
        reported_at=_parse_time(node.get("postedTime")),
        reported_by=node.get("nickname") or None,
    )


def _brand_of(result: dict[str, Any]) -> str | None:
    brands = result.get("brands") or []
    if not isinstance(brands, list):
        return None
    # brandingType is "FUEL" for the actual fuel brand vs. loyalty/other badges.
    for brand in brands:
        if isinstance(brand, dict) and brand.get("brandingType") == "FUEL" and brand.get("name"):
            return str(brand["name"])
    for brand in brands:
        if isinstance(brand, dict) and brand.get("name"):
            return str(brand["name"])
    return None


def parse_station(result: dict[str, Any], origin: tuple[float, float] | None) -> Station | None:
    """Normalize one GraphQL station result. Returns None if it has no usable id."""
    native_id = result.get("id")
    if native_id in (None, ""):
        return None

    raw_addr = result.get("address") or {}
    address = Address(
        line1=raw_addr.get("line1"),
        line2=raw_addr.get("line2"),
        city=raw_addr.get("locality"),
        region=raw_addr.get("region"),
        postal_code=raw_addr.get("postalCode"),
        country=raw_addr.get("country"),
    )

    def as_float(key: str) -> float | None:
        try:
            return float(result[key])
        except (KeyError, TypeError, ValueError):
            return None

    lat, lon = as_float("latitude"), as_float("longitude")
    distance = None
    if origin and lat is not None and lon is not None:
        distance = round(haversine_mi(origin[0], origin[1], lat, lon), 2)

    credit: dict[str, Price] = {}
    cash: dict[str, Price] = {}
    for entry in result.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        fuel = GASBUDDY_PRODUCT_TO_FUEL.get(entry.get("fuelProduct") or "")
        if not fuel:
            continue
        if price := _parse_price(entry.get("credit"), fuel, "credit"):
            credit[fuel] = price
        if price := _parse_price(entry.get("cash"), fuel, "cash"):
            cash[fuel] = price

    try:
        rating = float(result["starRating"]) if result.get("starRating") is not None else None
    except (TypeError, ValueError):
        rating = None
    try:
        count = int(result["ratingsCount"]) if result.get("ratingsCount") is not None else None
    except (TypeError, ValueError):
        count = None

    return Station(
        id=f"gasbuddy:{native_id}",
        source=GasBuddyProvider.name,
        name=str(result.get("name") or _brand_of(result) or f"Station {native_id}"),
        brand=_brand_of(result),
        address=address,
        lat=lat,
        lon=lon,
        distance_mi=distance,
        url=STATION_URL.format(id=native_id),
        prices=credit,
        cash_prices=cash,
        rating=rating,
        ratings_count=count,
    )


def parse_trend(location: dict[str, Any]) -> Trend | None:
    trends = location.get("trends") or []
    if not isinstance(trends, list) or not trends:
        return None
    first = trends[0]
    if not isinstance(first, dict):
        return None

    def num(key: str) -> float | None:
        try:
            return round(float(first[key]), 3)
        except (KeyError, TypeError, ValueError):
            return None

    return Trend(
        area=first.get("areaName"),
        today=num("today"),
        today_low=num("todayLow"),
        trend=num("trend"),
    )


def parse_response(
    payload: dict[str, Any], origin: tuple[float, float] | None
) -> tuple[list[Station], Trend | None, str | None, int | None]:
    """Pure parse of one GraphQL page.

    Returns (stations, trend, next_cursor, total_count). Kept separate from the
    network call so it can be tested against recorded fixtures.
    """
    if errors := payload.get("errors"):
        raise ProviderError(f"gasbuddy graphql errors: {errors}")

    location = ((payload.get("data") or {}).get("locationBySearchTerm")) or {}
    if not location:
        raise ProviderError("gasbuddy returned no location for this query")

    block = location.get("stations") or {}
    results = block.get("results") or []
    stations = [s for r in results if isinstance(r, dict) and (s := parse_station(r, origin))]

    next_cursor = ((block.get("cursor") or {}).get("next")) or None
    total = block.get("count") if isinstance(block.get("count"), int) else None
    return stations, parse_trend(location), next_cursor, total


class GasBuddyProvider:
    name = "gasbuddy"
    has_prices = True

    # A browser-ish UA plus Origin/Referer; the endpoint rejects bare clients.
    HEADERS = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://www.gasbuddy.com",
        "Referer": "https://www.gasbuddy.com/home",
        "apollographql-client-name": "web",
        "apollographql-client-version": "1.0.0",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }

    async def fetch(
        self,
        place: Place,
        *,
        radius_mi: float,
        fuels: list[str],
        limit: int,
    ) -> tuple[list[Station], Trend | None]:
        origin = (place.lat, place.lon)
        sort_fuel = fuel_id(fuels[0] if fuels else "regular")

        collected: dict[str, Station] = {}
        trend: Trend | None = None
        cursor: str | None = None
        # Results arrive nearest-first, so stop as soon as we leave the radius.
        max_pages = max(1, min(10, -(-limit // PAGE_SIZE_HINT) + 2))

        async with make_client(headers=self.HEADERS) as client:
            for _ in range(max_pages):
                variables: dict[str, Any] = {
                    "fuel": sort_fuel,
                    "lat": place.lat,
                    "lng": place.lon,
                    "maxAge": 0,
                    "search": None,
                    "cursor": cursor,
                }
                payload = await request_json(
                    client,
                    "POST",
                    GRAPHQL_URL,
                    json={
                        "operationName": "LocationBySearchTerm",
                        "query": QUERY,
                        "variables": variables,
                    },
                )
                page, page_trend, cursor, _total = parse_response(payload, origin)
                trend = trend or page_trend

                beyond_radius = False
                for station in page:
                    if station.distance_mi is not None and station.distance_mi > radius_mi:
                        beyond_radius = True
                        continue
                    collected.setdefault(station.id, station)

                if not cursor or not page or beyond_radius or len(collected) >= limit:
                    break

        stations = sorted(
            collected.values(),
            key=lambda s: (s.distance_mi if s.distance_mi is not None else 9e9, s.name),
        )
        return stations[:limit], trend
