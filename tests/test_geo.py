from __future__ import annotations

import pytest

from gasprice.geo import haversine_mi, miles_to_meters, parse_latlon, slugify


def test_haversine_known_distance():
    # Rockville MD -> Baltimore MD is ~33 miles.
    miles = haversine_mi(39.0840, -77.1528, 39.2904, -76.6122)
    assert 30 < miles < 36


def test_haversine_zero():
    assert haversine_mi(39.0, -77.0, 39.0, -77.0) == pytest.approx(0.0)


def test_haversine_is_symmetric():
    a = haversine_mi(39.0840, -77.1528, 38.9784, -76.4922)
    b = haversine_mi(38.9784, -76.4922, 39.0840, -77.1528)
    assert a == pytest.approx(b)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("39.084,-77.1528", (39.084, -77.1528)),
        ("  39.084 , -77.1528 ", (39.084, -77.1528)),
        ("39.084 -77.1528", (39.084, -77.1528)),
        ("39/-77", (39.0, -77.0)),
    ],
)
def test_parse_latlon_accepts(text, expected):
    assert parse_latlon(text) == expected


@pytest.mark.parametrize("text", ["20850", "Rockville, MD", "", "999,-77", "39.0,-200", "abc,def"])
def test_parse_latlon_rejects(text):
    assert parse_latlon(text) is None


def test_miles_to_meters():
    assert miles_to_meters(1) == pytest.approx(1609.344)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rockville, MD", "rockville-md"),
        ("  Silver   Spring  ", "silver-spring"),
        ("20850", "20850"),
        ("!!!", "location"),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected
