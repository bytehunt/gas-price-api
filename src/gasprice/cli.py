"""Command line interface: fetch, collect, serve."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .cache import JsonCache
from .collect import collect
from .config import DEFAULT_CONFIG_PATH, load_config
from .core import fetch_stations
from .http import ProviderError
from .models import FUELS, StationsResult
from .providers import DEFAULT_CHAIN, REGISTRY


def _age(value: datetime | None) -> str:
    if value is None:
        return "—"
    hours = (datetime.now(UTC) - value).total_seconds() / 3600
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def render_table(result: StationsResult, *, payment: str = "credit") -> None:
    """Pretty terminal output, falling back to plain text without rich."""
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:  # pragma: no cover - rich is a declared dependency
        _render_plain(result, payment)
        return

    console = Console()
    place = result.place.display_name or result.place.query or "location"
    header = f"[bold]{place}[/bold]  ·  {result.radius_mi:g} mi  ·  {result.count} stations"
    if result.sources:
        header += f"  ·  via {', '.join(result.sources)}"
    console.print(header)

    if b := result.benchmark:
        parts = []
        for fuel in result.fuels:
            if (price := b.prices.get(fuel)) is None:
                continue
            change = b.change(fuel, "week_ago")
            arrow = ""
            if change is not None:
                arrow = (
                    f" [green]▼{abs(change):.2f}[/green]"
                    if change < 0
                    else (f" [red]▲{change:.2f}[/red]" if change > 0 else "")
                )
            parts.append(f"{FUELS[fuel]['label']} ${price:.2f}{arrow}")
        if parts:
            as_of = f", {b.as_of:%b %d}" if b.as_of else ""
            console.print(
                f"[bold]{b.area}[/bold] {b.scope} average ({b.source.upper()}{as_of}): "
                + "  ".join(parts)
                + "  [dim](vs. a week ago)[/dim]"
            )

    if result.trend and result.trend.today:
        t = result.trend
        console.print(
            f"[dim]area average ({t.area or 'local'}): ${t.today:.3f}"
            + (f"  ·  area low ${t.today_low:.3f}" if t.today_low else "")
            + "[/dim]"
        )

    if not result.station_prices and result.stations:
        console.print(
            "[yellow]⚠ no per-station prices available for the US — "
            "the list below is stations only, priced by the area average above[/yellow]"
        )
    for note in result.notes:
        console.print(f"[dim]note: {note}[/dim]")

    if not result.stations:
        return

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Station", overflow="ellipsis", max_width=30)
    table.add_column("Address", overflow="ellipsis", max_width=30, style="dim")
    table.add_column("mi", justify="right")
    for fuel in result.fuels:
        table.add_column(FUELS[fuel]["label"], justify="right")
    table.add_column("Age", justify="right", style="dim")

    best = {fuel: result.cheapest(fuel) for fuel in result.fuels}

    for station in result.stations:
        cells = [
            station.name,
            station.address.one_line(),
            f"{station.distance_mi:.1f}" if station.distance_mi is not None else "—",
        ]
        newest: datetime | None = None
        for fuel in result.fuels:
            price = station.price_of(fuel, payment)
            if price is None:
                cells.append("[dim]—[/dim]")
                continue
            entry = (station.cash_prices if payment == "cash" else station.prices).get(fuel)
            if entry and entry.reported_at and (newest is None or entry.reported_at > newest):
                newest = entry.reported_at
            text = f"{price:.2f}"
            if best.get(fuel) is not None and best[fuel].id == station.id:
                text = f"[bold green]{text}[/bold green]"
            cells.append(text)
        cells.append(_age(newest))
        table.add_row(*cells)

    console.print(table)


def _render_plain(result: StationsResult, payment: str) -> None:
    for station in result.stations:
        prices = " ".join(
            f"{fuel}={p:.2f}"
            for fuel in result.fuels
            if (p := station.price_of(fuel, payment)) is not None
        )
        dist = f"{station.distance_mi:.1f}mi" if station.distance_mi is not None else "?"
        print(f"{station.name:<34} {dist:>8}  {prices or 'no prices'}")


def _common_location_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="?", help="ZIP code, place name, or 'lat,lon'")
    parser.add_argument("--lat", type=float, help="latitude (with --lon)")
    parser.add_argument("--lon", type=float, help="longitude (with --lat)")
    parser.add_argument(
        "-r", "--radius", type=float, default=5.0, help="search radius in miles (default: 5)"
    )
    parser.add_argument(
        "--fuel",
        action="append",
        choices=sorted(FUELS),
        help="fuel grade; repeatable (default: regular midgrade premium diesel)",
    )
    parser.add_argument("-n", "--limit", type=int, default=25, help="max stations (default: 25)")
    parser.add_argument(
        "--provider",
        action="append",
        choices=sorted(REGISTRY),
        help=f"provider chain; repeatable (default: {' '.join(DEFAULT_CHAIN)})",
    )
    parser.add_argument("--cash", action="store_true", help="show cash prices instead of credit")
    parser.add_argument(
        "--metro",
        help="AAA metro area name for the average, e.g. 'Washington (MD only)'",
    )
    parser.add_argument("--no-benchmark", action="store_true", help="skip the area-average lookup")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument(
        "--no-cache", action="store_true", help="bypass the local geocode/response cache"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gasprice", description="Nearby gas station prices for any location."
    )
    parser.add_argument("--version", action="version", version=f"gasprice {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log provider activity")

    # Shared so -v works on either side of the subcommand. SUPPRESS keeps the
    # subparser from overwriting a -v that was given before the subcommand.
    verbose_parent = argparse.ArgumentParser(add_help=False)
    verbose_parent.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log provider activity",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser(
        "fetch", help="look up prices near a location", parents=[verbose_parent]
    )
    _common_location_args(fetch_p)

    cheapest_p = sub.add_parser(
        "cheapest", help="cheapest station for one fuel grade", parents=[verbose_parent]
    )
    _common_location_args(cheapest_p)

    collect_p = sub.add_parser(
        "collect", help="refresh the static API from locations.yaml", parents=[verbose_parent]
    )
    collect_p.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to locations.yaml"
    )
    collect_p.add_argument("-o", "--out", type=Path, help="output directory (default: public/)")
    collect_p.add_argument(
        "--no-stale-fallback",
        action="store_true",
        help="overwrite previous data even when the fresh run has no prices",
    )
    collect_p.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="exit non-zero if any location ended up degraded or stale",
    )

    locations_p = sub.add_parser(
        "locations", help="list configured locations", parents=[verbose_parent]
    )
    locations_p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH)

    serve_p = sub.add_parser("serve", help="run the live JSON API", parents=[verbose_parent])
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument("--reload", action="store_true", help="auto-reload on code changes")

    return parser


async def _run_fetch(args: argparse.Namespace) -> int:
    cache = JsonCache(ttl=0) if args.no_cache else None
    try:
        result = await fetch_stations(
            args.query,
            lat=args.lat,
            lon=args.lon,
            radius_mi=args.radius,
            fuels=args.fuel,
            limit=args.limit,
            chain=args.provider,
            cache=cache,
            benchmark=not args.no_benchmark,
            metro_hint=args.metro,
        )
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payment = "cash" if args.cash else "credit"

    if args.command == "cheapest":
        fuel = result.fuels[0]
        station = result.cheapest(fuel)
        if args.json:
            print(
                json.dumps(
                    {
                        "fuel": fuel,
                        "price": station.price_of(fuel) if station else None,
                        "station": station.model_dump(mode="json") if station else None,
                        "stats": result.stats(fuel),
                        "benchmark": result.benchmark.model_dump(mode="json")
                        if result.benchmark
                        else None,
                        "station_prices": result.station_prices,
                        "degraded": result.degraded,
                        "notes": result.notes,
                    },
                    indent=2,
                    default=str,
                )
            )
        elif station is None:
            # No per-station price is the normal US case, so fall back to the
            # area average rather than exiting as if nothing were found.
            avg = result.benchmark.prices.get(fuel) if result.benchmark else None
            if avg is None:
                print(f"no {fuel} price data for this area", file=sys.stderr)
                return 1
            b = result.benchmark
            print(
                f"${avg:.2f}  {b.area} {b.scope} average ({b.source.upper()})  "
                f"— no per-station prices available"
            )
        else:
            print(
                f"${station.price_of(fuel):.2f}  {station.name}  "
                f"({station.distance_mi:.1f} mi)  {station.address.one_line()}"
            )
        return 0

    if args.json:
        payload = result.model_dump(mode="json")
        payload["stats"] = {f: result.stats(f) for f in result.fuels}
        print(json.dumps(payload, indent=2, default=str))
    else:
        render_table(result, payment=payment)

    return 1 if result.count == 0 else 0


async def _run_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    index = await collect(config, out_dir=args.out, stale_fallback=not args.no_stale_fallback)
    degraded = index["degraded_count"]
    print(
        f"wrote {index['location_count']} locations to "
        f"{args.out or config.out_dir}" + (f"  ({degraded} degraded/stale)" if degraded else "")
    )
    for failure in index["failures"]:
        print(f"  ! {failure}", file=sys.stderr)
    if args.fail_on_degraded and degraded:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command in ("fetch", "cheapest"):
        if args.query is None and (args.lat is None or args.lon is None):
            print("error: pass a location, or both --lat and --lon", file=sys.stderr)
            return 2
        return asyncio.run(_run_fetch(args))

    if args.command == "collect":
        try:
            return asyncio.run(_run_collect(args))
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.command == "locations":
        try:
            config = load_config(args.config)
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for loc in config.locations:
            target = loc.query or f"{loc.lat},{loc.lon}"
            print(f"{loc.slug:<24} {loc.radius_mi:>5g} mi  {target}")
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "gasprice.server:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
