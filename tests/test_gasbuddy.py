"""Parser tests against a recorded GraphQL page — no network.

The provider's network path is thin; the risk lives in normalizing the payload,
so that is what is pinned here.
"""

from __future__ import annotations

import pytest

from gasprice.http import ProviderError
from gasprice.providers.gasbuddy import parse_response, parse_station

ORIGIN = (39.084, -77.1528)


@pytest.fixture
def page(load_fixture):
    return load_fixture("gasbuddy_page.json")


def test_parses_all_stations(page):
    stations, trend, cursor, total = parse_response(page, ORIGIN)
    assert [s.id for s in stations] == ["gasbuddy:11111", "gasbuddy:22222", "gasbuddy:33333"]
    assert cursor == "cursor-page-2"
    assert total == 3
    assert trend is not None and trend.today == 3.149 and trend.today_low == 2.879


def test_prices_are_split_by_payment_type(page):
    stations, *_ = parse_response(page, ORIGIN)
    costco = stations[0]
    assert costco.prices["regular"].price == 2.879
    assert costco.prices["regular"].payment == "credit"
    assert costco.cash_prices["regular"].price == 2.829
    assert costco.prices["premium"].price == 3.479
    # premium had no cash entry
    assert "premium" not in costco.cash_prices


def test_zero_price_means_unreported(page):
    """A 0.0 price is 'nobody reported it', not 'free gas'."""
    stations, *_ = parse_response(page, ORIGIN)
    assert "diesel" not in stations[0].prices
    assert stations[1].prices["diesel"].price == 3.999


def test_reported_at_is_timezone_aware(page):
    stations, *_ = parse_response(page, ORIGIN)
    posted = stations[0].prices["regular"].reported_at
    assert posted is not None
    assert posted.tzinfo is not None
    assert posted.year == 2026


def test_distance_is_computed_from_origin(page):
    stations, *_ = parse_response(page, ORIGIN)
    assert stations[0].distance_mi == pytest.approx(0.19, abs=0.1)
    assert stations[2].distance_mi > 40  # the Hagerstown decoy


def test_brand_prefers_fuel_branding(page):
    stations, *_ = parse_response(page, ORIGIN)
    assert stations[0].brand == "Costco"  # not "Loyalty Thing"
    assert stations[1].brand == "Shell"
    assert stations[2].brand is None  # empty brands list


def test_station_url_is_built(page):
    stations, *_ = parse_response(page, ORIGIN)
    assert stations[0].url == "https://www.gasbuddy.com/station/11111"


def test_rating_fields(page):
    stations, *_ = parse_response(page, ORIGIN)
    assert stations[0].rating == 4.5
    assert stations[0].ratings_count == 120
    assert stations[1].rating is None


def test_graphql_errors_raise():
    with pytest.raises(ProviderError, match="graphql errors"):
        parse_response({"errors": [{"message": "nope"}]}, ORIGIN)


def test_missing_location_raises():
    with pytest.raises(ProviderError, match="no location"):
        parse_response({"data": {"locationBySearchTerm": None}}, ORIGIN)


def test_station_without_id_is_skipped():
    assert parse_station({"name": "Nameless"}, ORIGIN) is None


def test_station_without_coords_has_no_distance():
    station = parse_station({"id": "9", "name": "X"}, ORIGIN)
    assert station is not None
    assert station.lat is None
    assert station.distance_mi is None


def test_garbage_fields_do_not_crash():
    station = parse_station(
        {
            "id": "9",
            "name": "X",
            "latitude": "not-a-number",
            "starRating": "nope",
            "ratingsCount": "nope",
            "address": {},
            "brands": "not-a-list",
            "prices": [None, {"fuelProduct": "unknown_fuel", "credit": {"price": 1}}],
        },
        ORIGIN,
    )
    assert station is not None
    assert station.prices == {}
    assert station.brand is None
