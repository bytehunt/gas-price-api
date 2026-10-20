from __future__ import annotations

import pytest

from gasprice.config import load_config

MINIMAL = """
locations:
  - name: Rockville, MD
    query: "20850"
"""


def write(tmp_path, text):
    path = tmp_path / "locations.yaml"
    path.write_text(text, "utf-8")
    return path


def test_minimal_config_gets_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    loc = config.locations[0]
    assert loc.slug == "rockville-md"
    assert loc.radius_mi == 5.0
    assert loc.limit == 25
    assert loc.fuels == ["regular", "midgrade", "premium", "diesel"]
    assert config.providers == ["overpass"]
    assert str(config.out_dir) == "public"


def test_defaults_block_applies_to_all_locations(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
defaults:
  radius_miles: 12
  max_stations: 40
  fuels: [diesel]
locations:
  - name: A
    query: "1"
  - name: B
    query: "2"
    radius_miles: 3
""",
        )
    )
    assert [loc.radius_mi for loc in config.locations] == [12.0, 3.0]
    assert all(loc.limit == 40 for loc in config.locations)
    assert config.locations[0].fuels == ["diesel"]


def test_shorthand_string_locations(tmp_path):
    config = load_config(write(tmp_path, "locations:\n  - '20850'\n  - 'Bethesda, MD'\n"))
    assert [loc.slug for loc in config.locations] == ["20850", "bethesda-md"]
    assert config.locations[1].query == "Bethesda, MD"


def test_explicit_latlon(tmp_path):
    config = load_config(
        write(tmp_path, "locations:\n  - name: Home\n    lat: 39.08\n    lon: -77.15\n")
    )
    loc = config.locations[0]
    assert (loc.lat, loc.lon) == (39.08, -77.15)
    assert loc.query is None


def test_unknown_fuels_are_dropped(tmp_path):
    config = load_config(
        write(tmp_path, "locations:\n  - name: A\n    query: '1'\n    fuels: [regular, kerosene]\n")
    )
    assert config.locations[0].fuels == ["regular"]


def test_duplicate_slugs_rejected(tmp_path):
    text = """
locations:
  - name: A
    slug: dup
    query: "1"
  - name: B
    slug: dup
    query: "2"
"""
    with pytest.raises(ValueError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_half_a_coordinate_pair_rejected(tmp_path):
    with pytest.raises(ValueError, match="both lat and lon"):
        load_config(write(tmp_path, "locations:\n  - name: A\n    lat: 39.0\n"))


def test_name_doubles_as_the_geocode_query(tmp_path):
    """A location with a name but no explicit query geocodes by its name."""
    config = load_config(write(tmp_path, "locations:\n  - name: Bethesda, MD\n"))
    assert config.locations[0].query == "Bethesda, MD"


def test_location_without_name_or_query_rejected(tmp_path):
    with pytest.raises(ValueError, match="needs a 'name' or 'query'"):
        load_config(write(tmp_path, "locations:\n  - slug: a\n    radius_miles: 3\n"))


def test_empty_locations_rejected(tmp_path):
    with pytest.raises(ValueError, match="non-empty list"):
        load_config(write(tmp_path, "locations: []\n"))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_repo_config_is_valid():
    """The committed locations.yaml must always parse."""
    config = load_config("locations.yaml")
    assert config.locations
    assert len({loc.slug for loc in config.locations}) == len(config.locations)
