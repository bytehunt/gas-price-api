"""Live JSON API (FastAPI) for ad-hoc locations.

The static files under ``public/`` cover the locations you track on a schedule.
This server is for "what about right here, right now" — it accepts any query
string or coordinate pair. Responses are cached briefly in-process so a page
refresh does not hammer the upstream providers.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .aaa import fetch_benchmark
from .config import DEFAULT_CONFIG_PATH, Config, load_config
from .core import (
    MAX_LIMIT,
    MAX_RADIUS_MI,
    fetch_stations,
    normalize_fuels,
    resolve_place,
)
from .geocode import ensure_region
from .http import ProviderError
from .models import FUELS, SCHEMA_VERSION, StationsResult
from .providers import REGISTRY

log = logging.getLogger("gasprice.server")

RESPONSE_TTL = float(os.environ.get("GASPRICE_SERVER_TTL", "300"))

app = FastAPI(
    title="gas-price-api",
    version=__version__,
    summary="Nearby gas station prices for any US location.",
    description=(
        "Free, key-less gas price lookups. Prices are crowd-sourced and may be a few "
        "hours old; check `reported_at` on each price and the `degraded` flag on the "
        "response before trusting a number."
    ),
)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    stamp, payload = hit
    if time.time() - stamp > RESPONSE_TTL:
        _cache.pop(key, None)
        return None
    return payload


def _store(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if RESPONSE_TTL > 0:
        _cache[key] = (time.time(), payload)
    return payload


def _config() -> Config | None:
    path = Path(os.environ.get("GASPRICE_CONFIG", str(DEFAULT_CONFIG_PATH)))
    if not path.exists():
        return None
    try:
        return load_config(path)
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable config %s: %s", path, exc)
        return None


def _public_dir() -> Path:
    return Path(os.environ.get("GASPRICE_PUBLIC_DIR", "public"))


async def _lookup(
    q: str | None,
    lat: float | None,
    lon: float | None,
    radius: float,
    fuel: list[str] | None,
    limit: int,
    provider: str | None,
    metro: str | None = None,
) -> StationsResult:
    if q is None and (lat is None or lon is None):
        raise HTTPException(400, "pass ?q=<zip|place|lat,lon> or both ?lat= and ?lon=")

    chain = None
    if provider:
        if provider not in REGISTRY:
            raise HTTPException(400, f"unknown provider {provider!r}; known: {sorted(REGISTRY)}")
        chain = [provider]

    try:
        return await fetch_stations(
            q,
            lat=lat,
            lon=lon,
            radius_mi=radius,
            fuels=fuel,
            limit=limit,
            chain=chain,
            metro_hint=metro,
        )
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


QParam = Annotated[
    str | None, Query(description="ZIP code, place name, or `lat,lon`", examples=["20850"])
]
LatParam = Annotated[float | None, Query(ge=-90, le=90, description="Latitude")]
LonParam = Annotated[float | None, Query(ge=-180, le=180, description="Longitude")]
RadiusParam = Annotated[float, Query(gt=0, le=MAX_RADIUS_MI, description="Search radius in miles")]
LimitParam = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Max stations to return")]
FuelParam = Annotated[
    list[str] | None, Query(description="Repeatable fuel grade filter", examples=[["regular"]])
]
ProviderParam = Annotated[str | None, Query(description="Force a single provider")]
MetroParam = Annotated[
    str | None,
    Query(
        description="AAA metro area for the average, when the place name doesn't match one",
        examples=["Washington (MD only)"],
    ),
]


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/v1/health", tags=["meta"])
async def health() -> dict[str, Any]:
    config = _config()
    return {
        "ok": True,
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "providers": sorted(REGISTRY),
        "tracked_locations": len(config.locations) if config else 0,
        "cache_ttl_seconds": RESPONSE_TTL,
    }


@app.get("/v1/fuels", tags=["meta"])
async def fuels() -> dict[str, str]:
    return {slug: meta["label"] for slug, meta in FUELS.items()}


@app.get("/v1/stations", tags=["prices"])
async def stations(
    q: QParam = None,
    lat: LatParam = None,
    lon: LonParam = None,
    radius: RadiusParam = 5.0,
    fuel: FuelParam = None,
    limit: LimitParam = 25,
    provider: ProviderParam = None,
    metro: MetroParam = None,
) -> JSONResponse:
    """Stations near a location, nearest first, with prices where available.

    US per-station prices are generally unavailable (see `notes`); `benchmark`
    carries the current area average so the response still prices the area.
    """
    key = f"stations|{q}|{lat}|{lon}|{radius}|{sorted(fuel or [])}|{limit}|{provider}|{metro}"
    if cached := _cached(key):
        return JSONResponse(cached, headers={"X-Cache": "hit"})

    result = await _lookup(q, lat, lon, radius, fuel, limit, provider, metro)
    payload = result.model_dump(mode="json")
    payload["stats"] = {f: result.stats(f) for f in result.fuels}
    return JSONResponse(_store(key, payload), headers={"X-Cache": "miss"})


@app.get("/v1/cheapest", tags=["prices"])
async def cheapest(
    q: QParam = None,
    lat: LatParam = None,
    lon: LonParam = None,
    radius: RadiusParam = 5.0,
    fuel: Annotated[str, Query(description="Fuel grade")] = "regular",
    limit: LimitParam = 50,
    provider: ProviderParam = None,
    metro: MetroParam = None,
) -> JSONResponse:
    """The cheapest station for one fuel grade, or the area average if none is priced."""
    grade = normalize_fuels([fuel])[0]
    key = f"cheapest|{q}|{lat}|{lon}|{radius}|{grade}|{limit}|{provider}|{metro}"
    if cached := _cached(key):
        return JSONResponse(cached, headers={"X-Cache": "hit"})

    result = await _lookup(q, lat, lon, radius, [grade], limit, provider, metro)
    station = result.cheapest(grade)
    area_average = result.benchmark.prices.get(grade) if result.benchmark else None
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": result.generated_at.isoformat(),
        "place": result.place.model_dump(mode="json"),
        "fuel": grade,
        "radius_mi": result.radius_mi,
        "station_prices": result.station_prices,
        "degraded": result.degraded,
        "notes": result.notes,
        "stats": result.stats(grade),
        "station": station.model_dump(mode="json") if station else None,
        "price": station.price_of(grade) if station else None,
        "area_average": area_average,
        "benchmark": result.benchmark.model_dump(mode="json") if result.benchmark else None,
        # What to actually show a user: a real station price when one exists,
        # otherwise today's area average, labelled as such.
        "best_estimate": {
            "price": station.price_of(grade) if station else area_average,
            "kind": "station" if station else ("area_average" if area_average else None),
        },
    }
    return JSONResponse(_store(key, payload), headers={"X-Cache": "miss"})


@app.get("/v1/benchmark", tags=["prices"])
async def benchmark(
    q: QParam = None,
    lat: LatParam = None,
    lon: LonParam = None,
    metro: MetroParam = None,
) -> JSONResponse:
    """Today's area-average prices for a location, with week/month/year history."""
    key = f"benchmark|{q}|{lat}|{lon}|{metro}"
    if cached := _cached(key):
        return JSONResponse(cached, headers={"X-Cache": "hit"})

    if q is None and (lat is None or lon is None):
        raise HTTPException(400, "pass ?q=<zip|place|lat,lon> or both ?lat= and ?lon=")

    # Skip the station providers entirely; this endpoint only needs the average.
    try:
        place = await ensure_region(await resolve_place(q, lat, lon))
        b = await fetch_benchmark(place, metro_hint=metro)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    if b is None:
        raise HTTPException(404, "no area average available for this location")

    payload = b.model_dump(mode="json")
    payload["place"] = place.model_dump(mode="json")
    payload["change"] = {
        fuel: {
            period: b.change(fuel, period)
            for period in ("yesterday", "week_ago", "month_ago", "year_ago")
        }
        for fuel in b.prices
    }
    return JSONResponse(_store(key, payload), headers={"X-Cache": "miss"})


@app.get("/v1/locations", tags=["tracked"])
async def locations() -> dict[str, Any]:
    """The locations from locations.yaml, served from the last collected data."""
    index = _public_dir() / "api" / "v1" / "index.json"
    if index.exists():
        import json

        try:
            return json.loads(index.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"could not read collected index: {exc}") from exc

    config = _config()
    if not config:
        raise HTTPException(404, "no locations.yaml and no collected data; run `gasprice collect`")
    return {
        "schema_version": SCHEMA_VERSION,
        "collected": False,
        "locations": [
            {"name": loc.name, "slug": loc.slug, "query": loc.query, "radius_mi": loc.radius_mi}
            for loc in config.locations
        ],
    }


@app.get("/v1/locations/{slug}", tags=["tracked"])
async def location(slug: str) -> JSONResponse:
    """One tracked location: collected data if present, otherwise a live fetch."""
    path = _public_dir() / "api" / "v1" / "locations" / f"{slug}.json"
    if path.exists():
        import json

        try:
            return JSONResponse(json.loads(path.read_text("utf-8")), headers={"X-Source": "static"})
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"could not read {path}: {exc}") from exc

    config = _config()
    spec = next((loc for loc in config.locations if loc.slug == slug), None) if config else None
    if spec is None:
        raise HTTPException(404, f"unknown location {slug!r}")

    result = await _lookup(
        spec.query, spec.lat, spec.lon, spec.radius_mi, spec.fuels, spec.limit, None
    )
    payload = result.model_dump(mode="json")
    payload["name"] = spec.name
    payload["slug"] = spec.slug
    return JSONResponse(payload, headers={"X-Source": "live"})
