"""AAA daily fuel averages — the legitimate, key-less source of *current* prices.

There is no free US feed of per-station prices (see providers/gasbuddy.py for why),
but AAA publishes daily state and metro-area averages per grade on a public page.
That is a genuine current price signal: it tells you what a fair pump price is in
your area today, and whether prices are rising or falling.

These are area averages, never station prices. They are attached to a result as a
`Benchmark`, separate from `Station.prices`, so the two can never be confused.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from .cache import JsonCache
from .http import ProviderError, make_client, request_text
from .models import Benchmark, Place

SOURCE = "aaa"
STATE_URL = "https://gasprices.aaa.com/?state={code}"
BENCHMARK_TTL = 60 * 60 * 3  # averages move once a day; 3h is plenty

#: Column header -> our fuel slug. AAA writes "Mid-Grade" on the state table and
#: "Mid" on the metro tables.
COLUMN_TO_FUEL = {
    "regular": "regular",
    "mid-grade": "midgrade",
    "mid": "midgrade",
    "premium": "premium",
    "diesel": "diesel",
}

#: Row label -> the Benchmark field it populates.
ROW_TO_FIELD = {
    "current avg.": "prices",
    "yesterday avg.": "yesterday",
    "week ago avg.": "week_ago",
    "month ago avg.": "month_ago",
    "year ago avg.": "year_ago",
}

STATE_CODES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
_ROW_RE = re.compile(r"<tr.*?</tr>", re.S)
_CELL_RE = re.compile(r"<t[hd].*?</t[hd]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# <h3 data-cost="4.0737000" data-title>Hagerstown</h3>
_METRO_RE = re.compile(r"<h3[^>]*data-title[^>]*>\s*([^<]{2,60}?)\s*</h3>", re.I)
_STATE_NAME_RE = re.compile(r"<span>([^<]{2,40})</span>\s*average gas prices", re.I)
_AS_OF_RE = re.compile(r"as of (\d{1,2}/\d{1,2}/\d{2,4})")


def state_code(region: str | None) -> str | None:
    """'Maryland' -> 'MD'. Accepts an already-valid two-letter code."""
    if not region:
        return None
    text = region.strip()
    if len(text) == 2 and text.upper() in set(STATE_CODES.values()):
        return text.upper()
    return STATE_CODES.get(text.lower())


def _text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _money(cell: str) -> float | None:
    m = re.search(r"\$?\s*(\d+\.\d+)", cell)
    if not m:
        return None
    value = float(m.group(1))
    # AAA prints four decimals ($4.0887); round to the usual three.
    return round(value, 3) if 0 < value < 100 else None


def _parse_as_of(text: str) -> datetime | None:
    m = _AS_OF_RE.search(text)
    if not m:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(m.group(1), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_averages_table(table: str) -> dict[str, dict[str, float]] | None:
    """Parse one 'Current/Yesterday/Week/Month/Year' table.

    Returns {field_name: {fuel: price}} or None if this is not such a table.
    """
    rows = _ROW_RE.findall(table)
    if len(rows) < 2:
        return None

    header = [_text(c).lower() for c in _CELL_RE.findall(rows[0])]
    # Column 0 is the row label; the rest name fuel grades.
    fuels = [COLUMN_TO_FUEL.get(h) for h in header[1:]]
    if not any(fuels):
        return None

    out: dict[str, dict[str, float]] = {}
    for row in rows[1:]:
        cells = [_text(c) for c in _CELL_RE.findall(row)]
        if not cells:
            continue
        field = ROW_TO_FIELD.get(cells[0].lower())
        if not field:
            continue
        values: dict[str, float] = {}
        for fuel, cell in zip(fuels, cells[1:], strict=False):
            if fuel and (price := _money(cell)) is not None:
                values[fuel] = price
        if values:
            out[field] = values
    return out or None


def parse_state_page(page: str, url: str | None = None) -> tuple[Benchmark | None, list[Benchmark]]:
    """Extract the state average and every metro average from an AAA state page.

    The state table appears before the first metro heading; each later averages
    table belongs to the nearest preceding ``<h3 data-title>`` metro heading.
    """
    state_name = (m.group(1).strip() if (m := _STATE_NAME_RE.search(page)) else None) or "Statewide"
    as_of = _parse_as_of(page)
    metro_marks = [(m.start(), html.unescape(m.group(1)).strip()) for m in _METRO_RE.finditer(page)]

    state: Benchmark | None = None
    metros: list[Benchmark] = []

    for match in _TABLE_RE.finditer(page):
        parsed = _parse_averages_table(match.group(0))
        if not parsed:
            continue

        preceding = [name for pos, name in metro_marks if pos < match.start()]
        area = preceding[-1] if preceding else state_name
        benchmark = Benchmark(
            source=SOURCE,
            scope="metro" if preceding else "state",
            area=area,
            as_of=as_of,
            url=url,
            **parsed,
        )
        if preceding:
            metros.append(benchmark)
        elif state is None:
            state = benchmark

    return state, metros


def pick_metro(metros: list[Benchmark], place: Place, hint: str | None = None) -> Benchmark | None:
    """Choose the metro whose name best matches the place.

    An explicit `hint` (locations.yaml `aaa_metro:`) wins, because AAA's metro
    names do not always contain the town you searched for — "Rockville, MD" is
    inside AAA's "Washington (MD only)" metro, which no string match would find.
    """
    if not metros:
        return None

    if hint:
        wanted = hint.strip().lower()
        for metro in metros:
            if metro.area.lower() == wanted:
                return metro
        for metro in metros:
            if wanted in metro.area.lower():
                return metro

    haystack = " ".join(filter(None, [place.display_name, place.query])).lower()
    if not haystack:
        return None
    # Longest metro name first, so "Bowie-St. Charles" beats a stray "Bowie".
    for metro in sorted(metros, key=lambda b: -len(b.area)):
        core = re.split(r"[(\-]", metro.area)[0].strip().lower()
        if core and core in haystack:
            return metro
    return None


async def fetch_benchmark(
    place: Place,
    *,
    metro_hint: str | None = None,
    cache: JsonCache | None = None,
) -> Benchmark | None:
    """Current area-average prices for a place, metro-level when identifiable.

    Returns None (never raises) when the state is unknown or the page is
    unavailable — a missing benchmark degrades the response, it does not fail it.
    """
    code = state_code(place.region)
    if not code:
        raise ProviderError(f"no US state for {place.display_name or place.query!r}")

    url = STATE_URL.format(code=code)
    cache = cache if cache is not None else JsonCache(ttl=BENCHMARK_TTL)

    payload = cache.get(SOURCE, url)
    if payload is None:
        async with make_client(headers={"Accept": "text/html,*/*"}) as client:
            page = await request_text(client, "GET", url)
        state, metros = parse_state_page(page, url)
        payload = {
            "state": state.model_dump(mode="json") if state else None,
            "metros": [m.model_dump(mode="json") for m in metros],
        }
        cache.set(SOURCE, url, payload)

    state = Benchmark.model_validate(payload["state"]) if payload.get("state") else None
    metros = [Benchmark.model_validate(m) for m in payload.get("metros") or []]

    return pick_metro(metros, place, metro_hint) or state
