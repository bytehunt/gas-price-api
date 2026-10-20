from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gasprice.models import Address, Place, Price, Station, StationsResult


def station(sid: str, regular: float | None, *, cash: float | None = None, dist: float = 1.0):
    return Station(
        id=sid,
        source="test",
        name=sid,
        address=Address(line1="1 Main St", city="Rockville", region="MD", postal_code="20850"),
        lat=39.0,
        lon=-77.0,
        distance_mi=dist,
        prices={"regular": Price(fuel="regular", price=regular)} if regular else {},
        cash_prices={"regular": Price(fuel="regular", price=cash, payment="cash")} if cash else {},
    )


def result(*stations) -> StationsResult:
    return StationsResult(
        generated_at=datetime.now(UTC),
        place=Place(lat=39.0, lon=-77.0),
        radius_mi=5,
        fuels=["regular"],
        stations=list(stations),
        count=len(stations),
    )


def test_cheapest_picks_lowest():
    res = result(station("a", 3.29), station("b", 2.89), station("c", 3.09))
    assert res.cheapest("regular").id == "b"


def test_cheapest_ignores_unpriced_stations():
    res = result(station("a", None), station("b", 3.19))
    assert res.cheapest("regular").id == "b"


def test_cheapest_returns_none_when_nothing_priced():
    assert result(station("a", None)).cheapest("regular") is None


def test_cheapest_for_unknown_fuel_is_none():
    assert result(station("a", 3.0)).cheapest("diesel") is None


def test_stats_with_odd_count():
    res = result(station("a", 3.0), station("b", 2.0), station("c", 4.0))
    stats = res.stats("regular")
    assert stats == {"count": 3, "min": 2.0, "max": 4.0, "mean": 3.0, "median": 3.0}


def test_stats_with_even_count_averages_middle_two():
    res = result(station("a", 2.0), station("b", 3.0), station("c", 4.0), station("d", 5.0))
    assert res.stats("regular")["median"] == pytest.approx(3.5)


def test_stats_empty():
    stats = result().stats("regular")
    assert stats == {"count": 0, "min": None, "max": None, "mean": None, "median": None}


def test_price_of_respects_payment_type():
    s = station("a", 3.29, cash=3.19)
    assert s.price_of("regular") == 3.29
    assert s.price_of("regular", "cash") == 3.19


def test_has_any_price():
    assert station("a", 3.0).has_any_price
    assert not station("a", None).has_any_price


def test_address_one_line_skips_blanks():
    addr = Address(city="Rockville", region="MD")
    assert addr.one_line() == "Rockville, MD"


def test_price_age_hours():
    posted = datetime.now(UTC).replace(microsecond=0)
    p = Price(fuel="regular", price=3.0, reported_at=posted)
    assert p.age_hours is not None and p.age_hours < 0.01
    assert Price(fuel="regular", price=3.0).age_hours is None
