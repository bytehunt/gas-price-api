"""Build the static JSON API that GitHub Actions commits and Pages serves.

Layout under the output directory::

    index.html                      tiny browser dashboard
    api/v1/index.json               tracked locations + their cheapest prices
    api/v1/summary.json             cheapest per fuel across every location
    api/v1/locations/<slug>.json    full station list for one location

Because the Action commits this directory, the repository's git history doubles
as a free price history — ``git log -p public/api/v1/locations/<slug>.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, LocationSpec
from .core import fetch_stations
from .models import FUELS, SCHEMA_VERSION, StationsResult

log = logging.getLogger("gasprice.collect")

API_PREFIX = "api/v1"
#: Be polite to the upstream providers; they are free and shared.
DELAY_BETWEEN_LOCATIONS = 2.0


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    # Trailing newline keeps git diffs clean between runs.
    path.write_text(text + "\n", "utf-8")


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def cheapest_map(result: StationsResult) -> dict[str, Any]:
    """{fuel: {price, station_id, station_name, distance_mi}} for the result."""
    out: dict[str, Any] = {}
    for fuel in result.fuels:
        station = result.cheapest(fuel)
        if station is None:
            continue
        out[fuel] = {
            "price": station.price_of(fuel),
            "station_id": station.id,
            "station_name": station.name,
            "brand": station.brand,
            "distance_mi": station.distance_mi,
            "url": station.url,
        }
    return out


def _has_prices(payload: dict[str, Any] | None) -> bool:
    """Whether a payload carries any price information at all."""
    if not payload:
        return False
    if payload.get("benchmark"):
        return True
    return any((s.get("prices") or s.get("cash_prices")) for s in payload.get("stations") or [])


def _is_regression(fresh: dict[str, Any], previous: dict[str, Any] | None) -> str | None:
    """Reason the fresh payload is worse than what is already published, or None.

    A transient upstream failure must not blank out working data, and "degraded"
    alone does not catch it: an Overpass timeout still leaves the area average in
    place, so the result looks healthy while having lost every station.
    """
    if not previous or previous.get("stale"):
        return None
    if _has_prices(previous) and not _has_prices(fresh):
        return "lost price data"
    if (previous.get("count") or 0) > 0 and (fresh.get("count") or 0) == 0:
        return "lost all stations"
    return None


async def collect_location(spec: LocationSpec, config: Config) -> StationsResult:
    log.info("fetching %s (%s)", spec.name, spec.query or f"{spec.lat},{spec.lon}")
    return await fetch_stations(
        spec.query,
        lat=spec.lat,
        lon=spec.lon,
        radius_mi=spec.radius_mi,
        fuels=spec.fuels,
        limit=spec.limit,
        chain=config.providers,
        metro_hint=spec.aaa_metro,
    )


async def collect(
    config: Config,
    *,
    out_dir: Path | None = None,
    stale_fallback: bool = True,
    site_template: Path | None = None,
) -> dict[str, Any]:
    """Fetch every configured location and write the static API tree.

    When a location's fresh run has no prices but the previously committed file
    did, the old file is kept and marked stale — a Cloudflare block should not
    wipe working data out of the published API.
    """
    out = Path(out_dir or config.out_dir)
    api_dir = out / API_PREFIX
    loc_dir = api_dir / "locations"
    loc_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    failures: list[str] = []

    for index, spec in enumerate(config.locations):
        if index:
            await asyncio.sleep(DELAY_BETWEEN_LOCATIONS)

        target = loc_dir / f"{spec.slug}.json"
        previous = _load(target)

        try:
            result = await collect_location(spec, config)
        except Exception as exc:  # noqa: BLE001 - one bad location must not stop the run
            log.error("%s failed: %s", spec.slug, exc)
            failures.append(f"{spec.slug}: {exc}")
            if previous:
                previous["stale"] = True
                previous["stale_as_of"] = now.isoformat()
                _dump(target, previous)
                entries.append(_index_entry(spec, previous, stale=True))
            continue

        payload = result.model_dump(mode="json")
        payload["name"] = spec.name
        payload["slug"] = spec.slug
        payload["cheapest"] = cheapest_map(result)
        payload["stats"] = {f: result.stats(f) for f in result.fuels}
        payload["stale"] = False

        regression = _is_regression(payload, previous) if stale_fallback else None
        if regression:
            log.warning("%s: %s; keeping previous data as stale", spec.slug, regression)
            assert previous is not None
            previous["stale"] = True
            previous["stale_as_of"] = now.isoformat()
            previous["notes"] = [*(previous.get("notes") or []), *result.notes]
            failures.append(f"{spec.slug}: {regression}, served stale")
            _dump(target, previous)
            entries.append(_index_entry(spec, previous, stale=True))
            continue

        _dump(target, payload)
        entries.append(_index_entry(spec, payload, stale=False))

    index_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "title": config.site_title,
        "providers": config.providers,
        "fuels": {k: v["label"] for k, v in FUELS.items()},
        "location_count": len(entries),
        "degraded_count": sum(1 for e in entries if e["degraded"] or e["stale"]),
        "failures": failures,
        "locations": entries,
        "endpoints": {
            "index": f"{API_PREFIX}/index.json",
            "summary": f"{API_PREFIX}/summary.json",
            "location": f"{API_PREFIX}/locations/{{slug}}.json",
        },
    }
    _dump(api_dir / "index.json", index_payload)
    _dump(api_dir / "summary.json", build_summary(entries, now))

    template = site_template or _bundled_template()
    if template and template.exists():
        shutil.copyfile(template, out / "index.html")
    # Pages would otherwise run the output through Jekyll and drop _-prefixed paths.
    (out / ".nojekyll").write_text("", "utf-8")

    log.info("wrote %d locations to %s", len(entries), out)
    return index_payload


def _index_entry(spec: LocationSpec, payload: dict[str, Any], *, stale: bool) -> dict[str, Any]:
    return {
        "name": spec.name,
        "slug": spec.slug,
        "url": f"{API_PREFIX}/locations/{spec.slug}.json",
        "place": payload.get("place"),
        "radius_mi": payload.get("radius_mi", spec.radius_mi),
        "fuels": payload.get("fuels", spec.fuels),
        "count": payload.get("count", 0),
        "degraded": bool(payload.get("degraded")),
        "station_prices": bool(payload.get("station_prices")),
        "stale": stale,
        "generated_at": payload.get("generated_at"),
        "cheapest": payload.get("cheapest") or {},
        "stats": payload.get("stats") or {},
        "benchmark": payload.get("benchmark"),
        "trend": payload.get("trend"),
    }


def build_summary(entries: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Cheapest station per fuel across all tracked locations."""
    best: dict[str, Any] = {}
    for entry in entries:
        for fuel, info in (entry.get("cheapest") or {}).items():
            price = info.get("price")
            if price is None:
                continue
            current = best.get(fuel)
            if current is None or price < current["price"]:
                best[fuel] = {**info, "location": entry["name"], "location_slug": entry["slug"]}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "cheapest_by_fuel": best,
        "locations": [
            {
                "slug": e["slug"],
                "name": e["name"],
                "stale": e["stale"],
                "degraded": e["degraded"],
                "station_prices": e.get("station_prices", False),
                "regular": (e.get("cheapest") or {}).get("regular", {}).get("price"),
                "area_average_regular": ((e.get("benchmark") or {}).get("prices") or {}).get(
                    "regular"
                ),
                "area": (e.get("benchmark") or {}).get("area"),
            }
            for e in entries
        ],
    }


def _bundled_template() -> Path | None:
    """site/index.html from the repo checkout, when running from a clone."""
    candidate = Path(__file__).resolve().parents[2] / "site" / "index.html"
    return candidate if candidate.exists() else None
