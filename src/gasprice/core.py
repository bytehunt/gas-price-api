"""Orchestration: resolve a location, then walk the provider chain.

This is the single entry point used by the CLI, the live server and the static
collector, so all three return byte-identical JSON for the same query.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .aaa import fetch_benchmark
from .cache import JsonCache
from .geo import parse_latlon
from .geocode import ensure_region, geocode
from .http import ProviderError
from .models import DEFAULT_FUELS, FUELS, Place, PriceCoverage, StationsResult
from .providers import DEFAULT_CHAIN, PRICED_PROVIDERS, get_provider

log = logging.getLogger("gasprice")

MAX_RADIUS_MI = 50.0
MAX_LIMIT = 200


def normalize_fuels(fuels: list[str] | None) -> list[str]:
    """Drop unknown fuel slugs; keep caller order; never return empty."""
    if not fuels:
        return list(DEFAULT_FUELS)
    seen: list[str] = []
    for fuel in fuels:
        slug = fuel.strip().lower()
        if slug in FUELS and slug not in seen:
            seen.append(slug)
    return seen or list(DEFAULT_FUELS)


async def resolve_place(
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    *,
    cache: JsonCache | None = None,
) -> Place:
    """Coordinates win if given; otherwise geocode the query string."""
    if lat is not None and lon is not None:
        return Place(query=query, display_name=query or f"{lat},{lon}", lat=lat, lon=lon)
    if not query:
        raise ProviderError("provide either a location query or both lat and lon")
    if coords := parse_latlon(query):
        return Place(query=query, display_name=query, lat=coords[0], lon=coords[1])
    return await geocode(query, cache=cache)


async def fetch_stations(
    query: str | None = None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    radius_mi: float = 5.0,
    fuels: list[str] | None = None,
    limit: int = 25,
    chain: list[str] | None = None,
    cache: JsonCache | None = None,
    benchmark: bool = True,
    metro_hint: str | None = None,
) -> StationsResult:
    """Fetch nearby stations, their prices where available, and area averages.

    Walks `chain` in order and keeps the first provider that returns stations.
    A provider failure is recorded in `notes` rather than raised, so a blocked
    price source degrades to locations-plus-averages instead of an error.
    """
    radius_mi = max(0.1, min(float(radius_mi), MAX_RADIUS_MI))
    limit = max(1, min(int(limit), MAX_LIMIT))
    fuel_list = normalize_fuels(fuels)
    chain = chain or list(DEFAULT_CHAIN)

    place = await resolve_place(query, lat, lon, cache=cache)

    result = StationsResult(
        generated_at=datetime.now(UTC),
        place=place,
        radius_mi=radius_mi,
        fuels=fuel_list,
    )

    for name in chain:
        try:
            provider = get_provider(name)
        except KeyError as exc:
            result.notes.append(str(exc))
            continue

        try:
            stations, trend = await provider.fetch(
                place, radius_mi=radius_mi, fuels=fuel_list, limit=limit
            )
        except ProviderError as exc:
            log.warning("provider %s failed: %s", name, exc)
            result.notes.append(f"{name}: {exc}")
            continue
        except Exception as exc:  # a parser surprise shouldn't kill the whole run
            log.exception("provider %s raised", name)
            result.notes.append(f"{name}: unexpected error: {exc}")
            continue

        if not stations:
            result.notes.append(f"{name}: no stations within {radius_mi} mi")
            continue

        result.sources.append(name)
        result.stations = stations
        result.trend = trend
        break

    # Area averages come from a separate source, so a station provider without
    # prices still yields a usable answer to "what does gas cost around here".
    if benchmark:
        try:
            place = await ensure_region(place, cache=cache)
            result.place = place
            result.benchmark = await fetch_benchmark(place, metro_hint=metro_hint, cache=cache)
        except ProviderError as exc:
            log.warning("benchmark failed: %s", exc)
            result.notes.append(f"aaa: {exc}")
        except Exception as exc:  # noqa: BLE001 - never fail a run over the benchmark
            log.exception("benchmark raised")
            result.notes.append(f"aaa: unexpected error: {exc}")
        else:
            if result.benchmark:
                result.sources.append(result.benchmark.source)

    result.count = len(result.stations)
    result.station_prices = any(s.has_any_price for s in result.stations)
    result.degraded = not result.station_prices and result.benchmark is None

    if not result.station_prices and result.stations:
        result.notes.append(
            "no per-station prices for this area; station locations plus area averages"
        )

    result.price_coverage = _coverage(result, chain)
    return result


def _coverage(result: StationsResult, chain: list[str]) -> PriceCoverage:
    """Explain the response's price tiers so clients don't parse `notes`."""
    # Derive from the stations themselves: `sources` also carries the benchmark
    # provider, so sources[0] is "aaa" whenever no station provider succeeded.
    station_source = result.stations[0].source if result.stations else None
    price_source = result.benchmark.source if result.benchmark else None

    if result.station_prices:
        return PriceCoverage(
            station_level=True,
            area_level=result.benchmark is not None,
            station_source=station_source,
            price_source=price_source,
        )

    # Checked before NO_STATIONS: when a price-capable provider was asked and
    # broke, that is the useful diagnosis even if it also left us with no
    # stations at all. "no_stations" would hide why.
    if any(name in chain for name in PRICED_PROVIDERS):
        reason, detail = (
            PriceCoverage.PROVIDER_FAILED,
            "A price-capable provider was queried but returned no per-station "
            "prices. Check `notes` for the provider error.",
        )
    elif not result.stations:
        reason, detail = (
            PriceCoverage.NO_STATIONS,
            "No stations were returned, so there is nothing to price. "
            "Check `notes` for the provider error.",
        )
    else:
        reason, detail = (
            PriceCoverage.NO_FREE_SOURCE,
            "US per-station prices are a licensed commercial product (OPIS, "
            "GasBuddy Business), not public data, so no free provider can supply "
            "them. Station locations are complete; prices here are area averages.",
        )

    return PriceCoverage(
        station_level=False,
        area_level=result.benchmark is not None,
        reason=reason,
        detail=detail,
        station_source=station_source,
        price_source=price_source,
    )
