from __future__ import annotations

import pytest

from gasprice.http import ProviderError
from gasprice.providers.overpass import parse_element, parse_response

ORIGIN = (39.084, -77.1528)

PAYLOAD = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 39.0850,
            "lon": -77.1500,
            "tags": {
                "amenity": "fuel",
                "name": "Exxon",
                "brand": "Exxon",
                "addr:housenumber": "100",
                "addr:street": "Rockville Pike",
                "addr:city": "Rockville",
                "addr:state": "MD",
                "addr:postcode": "20852",
            },
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 39.0900, "lon": -77.1600},
            "tags": {"amenity": "fuel", "operator": "Wawa"},
        },
        {"type": "node", "id": 3, "tags": {"amenity": "fuel"}},  # no coords -> dropped
        {"type": "relation", "id": 4},  # wrong type -> dropped
    ]
}


def test_parses_nodes_and_ways():
    stations = parse_response(PAYLOAD, ORIGIN)
    assert [s.id for s in stations] == ["osm:node/1", "osm:way/2"]


def test_way_uses_center_coordinates():
    stations = parse_response(PAYLOAD, ORIGIN)
    way = stations[1]
    assert way.lat == pytest.approx(39.09)
    assert way.lon == pytest.approx(-77.16)


def test_address_is_assembled_from_tags():
    stations = parse_response(PAYLOAD, ORIGIN)
    assert stations[0].address.line1 == "100 Rockville Pike"
    assert stations[0].address.city == "Rockville"
    assert stations[0].address.postal_code == "20852"


def test_name_falls_back_to_operator():
    stations = parse_response(PAYLOAD, ORIGIN)
    assert stations[1].name == "Wawa"


def test_unnamed_station_gets_placeholder():
    station = parse_element({"type": "node", "id": 7, "lat": 39.0, "lon": -77.0}, ORIGIN)
    assert station is not None
    assert station.name == "Unnamed fuel station"


def test_overpass_stations_have_no_prices():
    """The whole point of the fallback: locations yes, prices no."""
    stations = parse_response(PAYLOAD, ORIGIN)
    assert all(not s.has_any_price for s in stations)


def test_sorted_by_distance():
    stations = parse_response(PAYLOAD, ORIGIN)
    distances = [s.distance_mi for s in stations]
    assert distances == sorted(distances)


def test_missing_elements_raises():
    with pytest.raises(ProviderError, match="no elements"):
        parse_response({}, ORIGIN)
