"""Canonical data model. Providers normalize into these shapes.

Every provider must emit `Station` objects so that the JSON API has one stable
schema regardless of where the numbers came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"

#: Fuel grades this project knows about, keyed by the slug used in the API.
FUELS: dict[str, dict[str, Any]] = {
    "regular": {"label": "Regular", "gasbuddy_id": 1, "gasbuddy_product": "regular_gas"},
    "midgrade": {"label": "Midgrade", "gasbuddy_id": 2, "gasbuddy_product": "midgrade_gas"},
    "premium": {"label": "Premium", "gasbuddy_id": 3, "gasbuddy_product": "premium_gas"},
    "diesel": {"label": "Diesel", "gasbuddy_id": 4, "gasbuddy_product": "diesel"},
    "e85": {"label": "E85", "gasbuddy_id": 5, "gasbuddy_product": "e85"},
    "unl88": {"label": "UNL88 (E15)", "gasbuddy_id": 12, "gasbuddy_product": "unl88"},
}

DEFAULT_FUELS = ["regular", "midgrade", "premium", "diesel"]

#: Reverse lookup: provider product name -> our fuel slug.
GASBUDDY_PRODUCT_TO_FUEL = {v["gasbuddy_product"]: k for k, v in FUELS.items()}


def fuel_id(fuel: str) -> int:
    """GasBuddy's numeric id for a fuel slug (defaults to regular)."""
    return int(FUELS.get(fuel, FUELS["regular"])["gasbuddy_id"])


class Address(BaseModel):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None

    def one_line(self) -> str:
        bits = [self.line1, self.city, self.region, self.postal_code]
        return ", ".join(b for b in bits if b)


class Price(BaseModel):
    """One observed price for one fuel at one station."""

    fuel: str
    price: float
    currency: str = "USD"
    unit: str = "gallon"
    payment: str = "credit"
    """``credit`` or ``cash``. Cash prices are often lower; both are kept."""
    reported_at: datetime | None = None
    reported_by: str | None = None
    """Crowd-sourced feeds attribute prices to a reporter nickname."""

    @property
    def age_hours(self) -> float | None:
        if self.reported_at is None:
            return None
        now = datetime.now(tz=self.reported_at.tzinfo)
        return (now - self.reported_at).total_seconds() / 3600.0


class Station(BaseModel):
    id: str
    """Stable within a source, formatted ``<source>:<native id>``."""
    source: str
    name: str
    brand: str | None = None
    address: Address = Field(default_factory=Address)
    lat: float | None = None
    lon: float | None = None
    distance_mi: float | None = None
    url: str | None = None
    prices: dict[str, Price] = Field(default_factory=dict)
    """Credit (or only-known) price per fuel slug."""
    cash_prices: dict[str, Price] = Field(default_factory=dict)
    rating: float | None = None
    ratings_count: int | None = None

    def price_of(self, fuel: str, payment: str = "credit") -> float | None:
        table = self.cash_prices if payment == "cash" else self.prices
        p = table.get(fuel)
        return p.price if p else None

    @property
    def has_any_price(self) -> bool:
        return bool(self.prices or self.cash_prices)


class Place(BaseModel):
    """A resolved query location."""

    query: str | None = None
    display_name: str | None = None
    lat: float
    lon: float
    region: str | None = None
    country: str | None = None


class Trend(BaseModel):
    """Area average, when a station provider gives one alongside its stations."""

    area: str | None = None
    today: float | None = None
    today_low: float | None = None
    trend: float | None = None


class Benchmark(BaseModel):
    """Official area-average prices — NOT the price at any individual station.

    This is what free US data actually offers (see aaa.py). `prices` is today's
    average per fuel; the other maps are the same average further back in time,
    which is what makes "prices are falling" answerable.
    """

    source: str
    scope: str
    """``state`` or ``metro``."""
    area: str
    prices: dict[str, float] = Field(default_factory=dict)
    yesterday: dict[str, float] = Field(default_factory=dict)
    week_ago: dict[str, float] = Field(default_factory=dict)
    month_ago: dict[str, float] = Field(default_factory=dict)
    year_ago: dict[str, float] = Field(default_factory=dict)
    as_of: datetime | None = None
    url: str | None = None

    def change(self, fuel: str = "regular", versus: str = "week_ago") -> float | None:
        """Today's average minus an earlier one, e.g. -0.04 = four cents cheaper."""
        now = self.prices.get(fuel)
        then = getattr(self, versus, {}).get(fuel)
        if now is None or then is None:
            return None
        return round(now - then, 3)


class PriceCoverage(BaseModel):
    """What kind of price data this response actually contains, and why.

    Consumers kept having to infer this from prose in `notes`. Stating it
    explicitly means a UI can decide what to render without string matching:
    `station_level` false plus `area_level` true means "show the area average,
    labelled as an average" rather than "prices are broken".
    """

    station_level: bool
    """True when at least one station carries a real per-pump price."""
    area_level: bool
    """True when an area average (`benchmark`) is attached."""
    reason: str | None = None
    """Machine-readable code when `station_level` is false."""
    detail: str | None = None
    """One human-readable sentence explaining `reason`."""
    station_source: str | None = None
    price_source: str | None = None

    #: Why per-station prices are missing. `no_free_source` is the normal US
    #: case and is not an error; the others are genuine failures. ClassVar so
    #: pydantic treats these as constants rather than model fields.
    NO_FREE_SOURCE: ClassVar[str] = "no_free_source"
    PROVIDER_FAILED: ClassVar[str] = "provider_failed"
    NO_STATIONS: ClassVar[str] = "no_stations"


class StationsResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    place: Place
    radius_mi: float
    fuels: list[str] = Field(default_factory=lambda: list(DEFAULT_FUELS))
    sources: list[str] = Field(default_factory=list)
    station_prices: bool = False
    """True when at least one station carries a real per-station price."""
    degraded: bool = False
    """True when there is no price information at all — no station prices and no
    benchmark. Station locations alone still set this, because you cannot answer
    "what does gas cost here" from a list of addresses."""
    notes: list[str] = Field(default_factory=list)
    trend: Trend | None = None
    benchmark: Benchmark | None = None
    """Area-average prices. Present even when per-station prices are not."""
    price_coverage: PriceCoverage | None = None
    """Explicit statement of which price tiers this response carries."""
    count: int = 0
    stations: list[Station] = Field(default_factory=list)

    def cheapest(self, fuel: str = "regular") -> Station | None:
        priced = [s for s in self.stations if s.price_of(fuel) is not None]
        if not priced:
            return None
        return min(priced, key=lambda s: s.price_of(fuel))  # type: ignore[arg-type,return-value]

    def stats(self, fuel: str = "regular") -> dict[str, float | int | None]:
        vals = sorted(p for s in self.stations if (p := s.price_of(fuel)) is not None)
        if not vals:
            return {"count": 0, "min": None, "max": None, "mean": None, "median": None}
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        return {
            "count": len(vals),
            "min": vals[0],
            "max": vals[-1],
            "mean": round(sum(vals) / len(vals), 3),
            "median": round(median, 3),
        }
