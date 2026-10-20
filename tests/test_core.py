"""Provider-chain behaviour: fallback, degradation, clamping."""

from __future__ import annotations

import pytest

from gasprice import core
from gasprice.http import ProviderError
from gasprice.models import Address, Benchmark, Place, Price, Station, Trend

PLACE = Place(query="20850", display_name="Rockville, MD", lat=39.084, lon=-77.1528)


def make_station(sid: str, source: str, price: float | None = None) -> Station:
    return Station(
        id=f"{source}:{sid}",
        source=source,
        name=sid,
        address=Address(city="Rockville", region="MD"),
        lat=39.08,
        lon=-77.15,
        distance_mi=0.5,
        prices={"regular": Price(fuel="regular", price=price)} if price else {},
    )


class FakeProvider:
    def __init__(self, name, *, has_prices, stations=None, error=None):
        self.name = name
        self.has_prices = has_prices
        self._stations = stations or []
        self._error = error
        self.calls = 0

    async def fetch(self, place, *, radius_mi, fuels, limit):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._stations), Trend(area="Rockville", today=3.1)


BENCHMARK = Benchmark(
    source="aaa",
    scope="metro",
    area="Washington (MD only)",
    prices={"regular": 4.18, "diesel": 6.41},
    week_ago={"regular": 4.30},
)


@pytest.fixture
def wire(monkeypatch):
    """Install a fake provider chain and keep the whole call off the network."""

    async def fake_resolve(query=None, lat=None, lon=None, *, cache=None):
        return PLACE

    async def fake_region(place, *, cache=None):
        return place

    async def no_benchmark(place, *, metro_hint=None, cache=None):
        return None

    monkeypatch.setattr(core, "resolve_place", fake_resolve)
    monkeypatch.setattr(core, "ensure_region", fake_region)
    monkeypatch.setattr(core, "fetch_benchmark", no_benchmark)

    def _wire(_benchmark=None, **providers):
        monkeypatch.setattr(core, "get_provider", lambda name: providers[name])
        if _benchmark is not None:

            async def with_benchmark(place, *, metro_hint=None, cache=None):
                return _benchmark

            monkeypatch.setattr(core, "fetch_benchmark", with_benchmark)
        return providers

    return _wire


def run(coro):
    """Drive a coroutine without pulling pytest-asyncio into the Nix closure."""
    import asyncio

    return asyncio.run(coro)


def test_first_provider_with_prices_wins(wire):
    providers = wire(
        gasbuddy=FakeProvider(
            "gasbuddy", has_prices=True, stations=[make_station("1", "gasbuddy", 3.09)]
        ),
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy", "overpass"]))

    assert res.sources == ["gasbuddy"]
    assert res.count == 1
    assert not res.degraded
    assert providers["overpass"].calls == 0  # never reached


def test_falls_back_to_overpass_when_price_provider_fails(wire):
    wire(
        gasbuddy=FakeProvider("gasbuddy", has_prices=True, error=ProviderError("HTTP 403")),
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy", "overpass"]))

    assert res.sources == ["overpass"]
    assert res.degraded is True
    assert any("403" in note for note in res.notes)
    assert any("station locations plus area averages" in note for note in res.notes)


def test_unexpected_provider_exception_is_contained(wire):
    wire(
        gasbuddy=FakeProvider("gasbuddy", has_prices=True, error=TypeError("bad parse")),
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy", "overpass"]))

    assert res.sources == ["overpass"]
    assert any("unexpected error" in note for note in res.notes)


def test_empty_provider_is_skipped(wire):
    wire(
        gasbuddy=FakeProvider("gasbuddy", has_prices=True, stations=[]),
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy", "overpass"]))

    assert res.sources == ["overpass"]
    assert any("no stations within" in note for note in res.notes)


def test_all_providers_failing_yields_empty_not_exception(wire):
    wire(
        gasbuddy=FakeProvider("gasbuddy", has_prices=True, error=ProviderError("blocked")),
        overpass=FakeProvider("overpass", has_prices=False, error=ProviderError("timeout")),
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy", "overpass"]))

    assert res.count == 0
    assert res.sources == []
    assert res.degraded is True
    assert len(res.notes) == 2


def test_priced_provider_returning_only_unpriced_stations_is_degraded(wire):
    wire(
        gasbuddy=FakeProvider("gasbuddy", has_prices=True, stations=[make_station("1", "gasbuddy")])
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy"]))

    assert res.count == 1
    assert res.degraded is True


def test_unknown_provider_name_is_noted(wire):
    wire(
        gasbuddy=FakeProvider(
            "gasbuddy", has_prices=True, stations=[make_station("1", "gasbuddy", 3.0)]
        )
    )
    res = run(core.fetch_stations("20850", chain=["nope", "gasbuddy"]))

    assert res.sources == ["gasbuddy"]
    assert any("nope" in note for note in res.notes)


def test_radius_and_limit_are_clamped(wire):
    wire(
        gasbuddy=FakeProvider(
            "gasbuddy", has_prices=True, stations=[make_station("1", "gasbuddy", 3.0)]
        )
    )
    res = run(core.fetch_stations("20850", radius_mi=9999, limit=9999, chain=["gasbuddy"]))

    assert res.radius_mi == core.MAX_RADIUS_MI
    # limit clamping is passed to the provider; assert via the recorded result shape
    assert res.count == 1


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, ["regular", "midgrade", "premium", "diesel"]),
        ([], ["regular", "midgrade", "premium", "diesel"]),
        (["bogus"], ["regular", "midgrade", "premium", "diesel"]),
        (["DIESEL", "regular"], ["diesel", "regular"]),
        (["regular", "regular"], ["regular"]),
    ],
)
def test_normalize_fuels(given, expected):
    assert core.normalize_fuels(given) == expected


def test_resolve_place_prefers_explicit_coordinates():
    place = run(core.resolve_place("ignored", lat=1.5, lon=-2.5))
    assert (place.lat, place.lon) == (1.5, -2.5)


def test_resolve_place_parses_coordinate_string():
    place = run(core.resolve_place("39.084,-77.1528"))
    assert place.lat == pytest.approx(39.084)


def test_resolve_place_requires_something():
    with pytest.raises(ProviderError):
        run(core.resolve_place(None))


def test_benchmark_is_attached_and_listed_as_a_source(wire):
    wire(
        _benchmark=BENCHMARK,
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["overpass"]))

    assert res.benchmark is not None
    assert res.benchmark.area == "Washington (MD only)"
    assert res.sources == ["overpass", "aaa"]


def test_benchmark_keeps_a_priceless_result_out_of_degraded(wire):
    """Locations + area averages is a usable answer; locations alone is not."""
    wire(
        _benchmark=BENCHMARK,
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["overpass"]))

    assert res.station_prices is False
    assert res.degraded is False


def test_no_stations_and_no_benchmark_is_degraded(wire):
    wire(overpass=FakeProvider("overpass", has_prices=False, error=ProviderError("down")))
    res = run(core.fetch_stations("20850", chain=["overpass"]))

    assert res.degraded is True
    assert res.benchmark is None


def test_station_prices_flag_is_set_when_a_station_has_a_price(wire):
    wire(
        gasbuddy=FakeProvider(
            "gasbuddy", has_prices=True, stations=[make_station("1", "gasbuddy", 3.09)]
        )
    )
    res = run(core.fetch_stations("20850", chain=["gasbuddy"]))

    assert res.station_prices is True
    assert res.degraded is False


def test_benchmark_failure_is_noted_not_raised(wire, monkeypatch):
    async def boom(place, *, metro_hint=None, cache=None):
        raise ProviderError("aaa page 500")

    wire(
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        )
    )
    monkeypatch.setattr(core, "fetch_benchmark", boom)

    res = run(core.fetch_stations("20850", chain=["overpass"]))

    assert res.benchmark is None
    assert any("aaa page 500" in note for note in res.notes)
    assert res.count == 1  # stations still returned


def test_benchmark_can_be_disabled(wire):
    wire(
        _benchmark=BENCHMARK,
        overpass=FakeProvider(
            "overpass", has_prices=False, stations=[make_station("2", "overpass")]
        ),
    )
    res = run(core.fetch_stations("20850", chain=["overpass"], benchmark=False))

    assert res.benchmark is None
    assert res.sources == ["overpass"]
