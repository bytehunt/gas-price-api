"""locations.yaml — the list of places the GitHub Action keeps refreshed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core import normalize_fuels
from .geo import slugify
from .models import DEFAULT_FUELS
from .providers import DEFAULT_CHAIN

DEFAULT_CONFIG_PATH = Path("locations.yaml")


@dataclass(slots=True)
class LocationSpec:
    name: str
    slug: str
    query: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_mi: float = 5.0
    fuels: list[str] = field(default_factory=lambda: list(DEFAULT_FUELS))
    limit: int = 25
    aaa_metro: str | None = None
    """Explicit AAA metro name, when the town's name does not match one."""


@dataclass(slots=True)
class Config:
    locations: list[LocationSpec]
    providers: list[str] = field(default_factory=lambda: list(DEFAULT_CHAIN))
    out_dir: Path = Path("public")
    site_title: str = "Gas Price API"


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no config at {path}")

    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    defaults = raw.get("defaults") or {}

    d_radius = _as_float(defaults.get("radius_miles"), 5.0)
    d_limit = int(defaults.get("max_stations") or 25)
    d_fuels = normalize_fuels(defaults.get("fuels"))

    entries = raw.get("locations") or []
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'locations' must be a non-empty list")

    locations: list[LocationSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            entry = {"query": entry}
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each location must be a string or mapping, got {entry!r}")

        name = entry.get("name") or entry.get("query")
        if not name:
            raise ValueError(f"{path}: location needs a 'name' or 'query': {entry!r}")

        slug = entry.get("slug") or slugify(str(name))
        if slug in seen:
            raise ValueError(f"{path}: duplicate location slug {slug!r}")
        seen.add(slug)

        lat = entry.get("lat")
        lon = entry.get("lon")
        if (lat is None) != (lon is None):
            raise ValueError(f"{path}: {slug!r} needs both lat and lon, or neither")

        # Coordinates are authoritative; a query is only needed for geocoding, and
        # there the name doubles as the search term ("Rockville, MD" geocodes fine).
        query = entry.get("query") if lat is not None else (entry.get("query") or name)

        locations.append(
            LocationSpec(
                name=str(name),
                slug=slug,
                query=str(query) if query else None,
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                radius_mi=_as_float(entry.get("radius_miles"), d_radius),
                fuels=normalize_fuels(entry.get("fuels")) if entry.get("fuels") else d_fuels,
                limit=int(entry.get("max_stations") or d_limit),
                aaa_metro=(str(m) if (m := entry.get("aaa_metro")) else None),
            )
        )

    providers = raw.get("providers") or list(DEFAULT_CHAIN)
    if isinstance(providers, str):
        providers = [providers]

    return Config(
        locations=locations,
        providers=[str(p) for p in providers],
        out_dir=Path(str(raw.get("out_dir") or "public")),
        site_title=str(raw.get("site_title") or "Gas Price API"),
    )
