"""Static-API writer: output layout, summaries, and the stale-fallback rule."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from gasprice import collect as collect_mod
from gasprice.collect import API_PREFIX, build_summary, cheapest_map, collect
from gasprice.config import Config, LocationSpec
from gasprice.models import Address, Benchmark, Place, Price, Station, StationsResult

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

BENCHMARK = Benchmark(
    source="aaa",
    scope="metro",
    area="Washington (MD only)",
    prices={"regular": 4.18, "diesel": 6.41},
    week_ago={"regular": 4.30},
)


def make_result(prices: dict[str, float] | None, *, degraded: bool = False) -> StationsResult:
    stations = []
    if prices is not None:
        stations.append(
            Station(
                id="gasbuddy:1",
                source="gasbuddy",
                name="Costco",
                brand="Costco",
                address=Address(city="Rockville", region="MD"),
                lat=39.08,
                lon=-77.15,
                distance_mi=0.4,
                url="https://example.test/1",
                prices={f: Price(fuel=f, price=p) for f, p in prices.items()},
            )
        )
    return StationsResult(
        generated_at=NOW,
        place=Place(query="20850", display_name="Rockville, MD", lat=39.084, lon=-77.1528),
        radius_mi=5,
        fuels=["regular", "diesel"],
        sources=["gasbuddy"],
        degraded=degraded or not prices,
        stations=stations,
        count=len(stations),
    )


def make_config(tmp_path, *specs) -> Config:
    return Config(
        locations=list(specs)
        or [
            LocationSpec(
                name="Rockville, MD",
                slug="rockville-md",
                query="20850",
                fuels=["regular", "diesel"],
            )
        ],
        out_dir=tmp_path / "public",
    )


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(collect_mod, "DELAY_BETWEEN_LOCATIONS", 0)


def stub_collect(monkeypatch, *results):
    """Return each result in turn from collect_location; exceptions are raised."""
    queue = list(results)

    async def fake(spec, config):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(collect_mod, "collect_location", fake)


def read(path):
    return json.loads(path.read_text("utf-8"))


def test_writes_expected_file_tree(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    stub_collect(monkeypatch, make_result({"regular": 2.89, "diesel": 3.79}))

    index = asyncio.run(collect(config))

    out = config.out_dir
    assert (out / API_PREFIX / "index.json").exists()
    assert (out / API_PREFIX / "summary.json").exists()
    assert (out / API_PREFIX / "locations" / "rockville-md.json").exists()
    assert (out / ".nojekyll").exists()
    assert index["location_count"] == 1
    assert index["degraded_count"] == 0


def test_location_file_carries_name_slug_and_cheapest(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    stub_collect(monkeypatch, make_result({"regular": 2.89, "diesel": 3.79}))
    asyncio.run(collect(config))

    payload = read(config.out_dir / API_PREFIX / "locations" / "rockville-md.json")
    assert payload["slug"] == "rockville-md"
    assert payload["name"] == "Rockville, MD"
    assert payload["cheapest"]["regular"]["price"] == 2.89
    assert payload["stats"]["regular"]["min"] == 2.89
    assert payload["stale"] is False


def test_json_is_stable_between_identical_runs(tmp_path, monkeypatch, no_delay):
    """Sorted keys matter: the Action commits this, and churn would be noise."""
    config = make_config(tmp_path)
    target = config.out_dir / API_PREFIX / "locations" / "rockville-md.json"

    stub_collect(monkeypatch, make_result({"regular": 2.89}))
    asyncio.run(collect(config))
    first = target.read_text("utf-8")

    stub_collect(monkeypatch, make_result({"regular": 2.89}))
    asyncio.run(collect(config))
    assert target.read_text("utf-8") == first


def test_degraded_run_keeps_previous_good_data_as_stale(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    target = config.out_dir / API_PREFIX / "locations" / "rockville-md.json"

    stub_collect(monkeypatch, make_result({"regular": 2.89}))
    asyncio.run(collect(config))

    # Now the price provider gets blocked and only locations come back.
    stub_collect(monkeypatch, make_result(None))
    index = asyncio.run(collect(config))

    payload = read(target)
    assert payload["stale"] is True
    assert payload["stale_as_of"]
    assert payload["stations"][0]["prices"]["regular"]["price"] == 2.89  # preserved
    assert index["degraded_count"] == 1
    assert any("stale" in f for f in index["failures"])


def test_stale_fallback_can_be_disabled(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    target = config.out_dir / API_PREFIX / "locations" / "rockville-md.json"

    stub_collect(monkeypatch, make_result({"regular": 2.89}))
    asyncio.run(collect(config))

    stub_collect(monkeypatch, make_result(None))
    asyncio.run(collect(config, stale_fallback=False))

    payload = read(target)
    assert payload["stations"] == []
    assert payload["degraded"] is True


def test_first_ever_run_that_is_degraded_still_writes(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    stub_collect(monkeypatch, make_result(None))
    index = asyncio.run(collect(config))

    payload = read(config.out_dir / API_PREFIX / "locations" / "rockville-md.json")
    assert payload["degraded"] is True
    assert index["degraded_count"] == 1


def test_one_failing_location_does_not_stop_the_others(tmp_path, monkeypatch, no_delay):
    config = make_config(
        tmp_path,
        LocationSpec(name="A", slug="a", query="1", fuels=["regular"]),
        LocationSpec(name="B", slug="b", query="2", fuels=["regular"]),
    )
    stub_collect(monkeypatch, RuntimeError("boom"), make_result({"regular": 3.19}))

    index = asyncio.run(collect(config))

    assert index["location_count"] == 1  # only B produced an entry
    assert any("a: boom" in f for f in index["failures"])
    assert (config.out_dir / API_PREFIX / "locations" / "b.json").exists()


def test_cheapest_map_skips_fuels_with_no_prices():
    result = make_result({"regular": 2.89})
    mapping = cheapest_map(result)
    assert set(mapping) == {"regular"}
    assert mapping["regular"]["station_name"] == "Costco"


def test_build_summary_picks_global_minimum():
    entries = [
        {
            "name": "A",
            "slug": "a",
            "stale": False,
            "degraded": False,
            "cheapest": {"regular": {"price": 3.10}},
        },
        {
            "name": "B",
            "slug": "b",
            "stale": False,
            "degraded": False,
            "cheapest": {"regular": {"price": 2.95}, "diesel": {"price": 3.80}},
        },
    ]
    summary = build_summary(entries, NOW)
    assert summary["cheapest_by_fuel"]["regular"]["price"] == 2.95
    assert summary["cheapest_by_fuel"]["regular"]["location_slug"] == "b"
    assert summary["cheapest_by_fuel"]["diesel"]["location"] == "B"
    assert len(summary["locations"]) == 2


def test_build_summary_ignores_null_prices():
    entries = [
        {
            "name": "A",
            "slug": "a",
            "stale": True,
            "degraded": True,
            "cheapest": {"regular": {"price": None}},
        }
    ]
    assert build_summary(entries, NOW)["cheapest_by_fuel"] == {}


def test_losing_all_stations_keeps_previous_data(tmp_path, monkeypatch, no_delay):
    """An Overpass timeout still returns an area average, so `degraded` stays
    false while every station is gone. That must not overwrite a good file."""
    config = make_config(tmp_path)
    target = config.out_dir / API_PREFIX / "locations" / "rockville-md.json"

    stub_collect(monkeypatch, make_result({"regular": 2.89}))
    asyncio.run(collect(config))
    assert read(target)["count"] == 1

    empty = make_result(None)
    empty.benchmark = BENCHMARK  # averages survived; stations did not
    empty.degraded = False
    stub_collect(monkeypatch, empty)
    index = asyncio.run(collect(config))

    payload = read(target)
    assert payload["stale"] is True
    assert payload["count"] == 1  # previous stations preserved
    assert any("lost all stations" in f for f in index["failures"])


def test_benchmark_only_result_is_not_treated_as_a_regression(tmp_path, monkeypatch, no_delay):
    """Going from averages-only to averages-only is normal, not a regression."""
    config = make_config(tmp_path)
    target = config.out_dir / API_PREFIX / "locations" / "rockville-md.json"

    first = make_result(None)
    first.benchmark = BENCHMARK
    first.degraded = False
    stub_collect(monkeypatch, first)
    asyncio.run(collect(config))

    second = make_result(None)
    second.benchmark = BENCHMARK
    second.degraded = False
    stub_collect(monkeypatch, second)
    index = asyncio.run(collect(config))

    assert read(target)["stale"] is False
    assert index["failures"] == []


def test_benchmark_reaches_the_index_and_summary(tmp_path, monkeypatch, no_delay):
    config = make_config(tmp_path)
    result = make_result(None)
    result.benchmark = BENCHMARK
    result.degraded = False
    stub_collect(monkeypatch, result)

    index = asyncio.run(collect(config))

    entry = index["locations"][0]
    assert entry["benchmark"]["area"] == "Washington (MD only)"
    summary = read(config.out_dir / API_PREFIX / "summary.json")
    assert summary["locations"][0]["area_average_regular"] == 4.18
    assert summary["locations"][0]["area"] == "Washington (MD only)"
