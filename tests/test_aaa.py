"""AAA average-price parsing. No network: the fixture mirrors the real markup."""

from __future__ import annotations

from pathlib import Path

import pytest

from gasprice.aaa import parse_state_page, pick_metro, state_code
from gasprice.models import Place

FIXTURE = Path(__file__).parent / "fixtures" / "aaa_state.html"


@pytest.fixture
def parsed():
    return parse_state_page(FIXTURE.read_text("utf-8"), "https://gasprices.aaa.com/?state=MD")


def test_state_average_is_found(parsed):
    state, _ = parsed
    assert state is not None
    assert state.scope == "state"
    assert state.area == "Maryland"
    assert state.prices["regular"] == 4.089
    assert state.prices["diesel"] == 6.366


def test_midgrade_column_is_mapped_from_both_spellings(parsed):
    """The state table says 'Mid-Grade'; metro tables say 'Mid'."""
    state, metros = parsed
    assert state.prices["midgrade"] == 4.697
    assert metros[0].prices["midgrade"] == 4.554


def test_historical_rows_are_captured(parsed):
    state, _ = parsed
    assert state.yesterday["regular"] == 4.118
    assert state.week_ago["regular"] == 4.259
    assert state.month_ago["regular"] == 4.222
    assert state.year_ago["regular"] == 2.996


def test_change_versus_week_ago(parsed):
    state, _ = parsed
    assert state.change("regular", "week_ago") == pytest.approx(-0.17, abs=0.001)
    assert state.change("regular", "year_ago") == pytest.approx(1.093, abs=0.001)


def test_change_is_none_for_missing_data(parsed):
    _, metros = parsed
    bowie = next(m for m in metros if m.area == "Bowie-St. Charles")
    assert bowie.change("regular", "week_ago") is None


def test_as_of_date_is_parsed(parsed):
    state, _ = parsed
    assert state.as_of is not None
    assert (state.as_of.year, state.as_of.month, state.as_of.day) == (2026, 10, 20)


def test_all_metros_are_found(parsed):
    _, metros = parsed
    assert [m.area for m in metros] == [
        "Baltimore",
        "Washington (MD only)",
        "Bowie-St. Charles",
        "Frederick",
    ]
    assert all(m.scope == "metro" for m in metros)


def test_record_high_tables_are_ignored(parsed):
    """Those tables have a 'Price'/'Date' shape and must not become benchmarks."""
    state, metros = parsed
    assert len(metros) == 4
    # $5.0200 is the all-time record; it must not have been read as a current price.
    assert state.prices["regular"] != 5.02


def test_url_is_carried_through(parsed):
    state, _ = parsed
    assert state.url == "https://gasprices.aaa.com/?state=MD"


def test_empty_page_yields_nothing():
    state, metros = parse_state_page("<html><body>nothing here</body></html>")
    assert state is None
    assert metros == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Maryland", "MD"),
        ("maryland", "MD"),
        ("  Maryland  ", "MD"),
        ("MD", "MD"),
        ("md", "MD"),
        ("District of Columbia", "DC"),
        ("Narnia", None),
        (None, None),
        ("", None),
    ],
)
def test_state_code(given, expected):
    assert state_code(given) == expected


def place(display: str) -> Place:
    return Place(query=display, display_name=display, lat=39.0, lon=-77.0, region="Maryland")


def test_pick_metro_prefers_an_explicit_hint(parsed):
    """Rockville is in AAA's 'Washington (MD only)' metro, which no name match finds."""
    _, metros = parsed
    picked = pick_metro(
        metros, place("Rockville, Montgomery County, Maryland"), "Washington (MD only)"
    )
    assert picked is not None and picked.area == "Washington (MD only)"


def test_pick_metro_hint_matches_partially(parsed):
    _, metros = parsed
    picked = pick_metro(metros, place("anywhere"), "washington")
    assert picked is not None and picked.area == "Washington (MD only)"


def test_pick_metro_falls_back_to_name_matching(parsed):
    _, metros = parsed
    picked = pick_metro(metros, place("Baltimore, Maryland"))
    assert picked is not None and picked.area == "Baltimore"


def test_pick_metro_strips_parenthetical_and_hyphen(parsed):
    """'Bowie-St. Charles' should match a place that only says 'Bowie'."""
    _, metros = parsed
    picked = pick_metro(metros, place("Bowie, Prince George's County, Maryland"))
    assert picked is not None and picked.area == "Bowie-St. Charles"


def test_pick_metro_returns_none_when_nothing_matches(parsed):
    _, metros = parsed
    assert pick_metro(metros, place("Ocean City, Maryland")) is None


def test_pick_metro_with_no_metros():
    assert pick_metro([], place("Baltimore")) is None
