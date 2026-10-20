# gas-price-api

Find the gas stations near any location and what fuel costs there. Works from a
ZIP code, a place name, or raw coordinates. Runs as a CLI, a live JSON API, or a
static JSON site that GitHub Actions refreshes every 3 hours.

```console
$ gasprice fetch "21201" -r 3
427, West Mulberry Street, Baltimore, Maryland  ·  3 mi  ·  60 stations  ·  via overpass, aaa
Baltimore metro average (AAA, Oct 20): Regular $4.05 ▼0.20  Midgrade $4.62 ▼0.18
                                       Premium $4.93 ▼0.17  Diesel $6.34 ▼0.01

Station              Address                      mi  Regular  Midgrade  Premium  Diesel
Gulf                 300 W Franklin St          0.01        —         —        —       —
Exxon                1 N Howard St              0.08        —         —        —       —
Sunoco               1100 W Baltimore St        0.64        —         —        —       —
```

> **Prices are area averages, not pump prices.** Station locations come from
> OpenStreetMap and are complete; the dollar figures are AAA's daily state and
> metro averages. No free source of per-station US prices exists — that data is
> licensed (OPIS, GasBuddy Business), which is why every app charges for it. The
> per-station columns stay empty until you plug in such a source. See
> [Adding prices](#adding-a-price-source).

## Setup

Needs [Nix](https://nixos.org/download) with flakes. [direnv](https://direnv.net)
is optional but makes the shell load automatically.

```console
$ direnv allow     # or run `nix develop` by hand each time
```

That's it — Python, the libraries, ruff and jq are all pinned in `flake.nix`,
and `./bin` goes on `PATH` so `gasprice` works with no install step.

## Commands

```console
$ gasprice fetch 21201                   # stations + prices near a ZIP
$ gasprice fetch "Baltimore, MD" -r 3    # 3-mile radius
$ gasprice fetch "39.29,-76.62" -n 10    # coordinates, 10 results
$ gasprice cheapest 21201 --fuel diesel  # single best price
$ gasprice collect                       # refresh ./public from locations.yaml
$ gasprice serve                         # live API on http://127.0.0.1:8000/docs
$ gasprice locations                     # list what's being tracked
```

Handy flags on `fetch` and `cheapest`:

| Flag | Does |
| --- | --- |
| `-r, --radius` | search radius in miles (default 5) |
| `-n, --limit` | max stations (default 25) |
| `--fuel` | `regular`, `midgrade`, `premium`, `diesel`, `e85`, `unl88` — repeatable |
| `--metro` | pin the AAA metro, e.g. `"Washington (MD only)"` |
| `--json` | raw JSON instead of a table |
| `--cash` | cash prices instead of credit |

Development: `pytest -q` (124 tests, no network), `ruff check .`, `ruff format .`.

## Tracking locations

Edit `locations.yaml`, then run `gasprice collect`:

```yaml
locations:
  - name: Mulberry St (Downtown Baltimore)
    slug: mulberry-st-baltimore              # becomes the API path
    query: "427 W Mulberry St, Baltimore, MD"  # ZIP, address, or "lat,lon"
    radius_miles: 3
    max_stations: 70
    aaa_metro: Baltimore

  - "21201"                                  # shorthand: query only
```

Two gotchas worth knowing:

- **Qualify addresses with a state.** `"1 Compass Rd"` geocodes to Massachusetts;
  `"1 Compass Rd, Middle River, MD"` is what you want.
- **Set `aaa_metro`.** AAA groups prices into metros whose names often aren't
  your town — most Maryland DC suburbs live under `Washington (MD only)`.
  Without it you get the statewide average, which is a worse estimate. Check
  with `gasprice fetch "<place>" --json | jq '.benchmark | {scope, area}'`.

## Live API

`gasprice serve`, then browse `/docs` for the interactive reference.

| Endpoint | Returns |
| --- | --- |
| `/v1/stations?q=21201&radius=3` | nearby stations, nearest first |
| `/v1/cheapest?q=21201&fuel=diesel` | best price, or the area average if none |
| `/v1/benchmark?q=21201` | area averages + week/month/year change |
| `/v1/locations` | everything from the last `collect` |
| `/v1/locations/{slug}` | one tracked location |

All accept `q=` **or** `lat=`+`lon=`, plus `radius`, `fuel`, `limit` and `metro`.

## Static API

`gasprice collect` writes plain JSON that GitHub Pages serves for free:

```
public/
  index.html                      dashboard
  api/v1/index.json               all locations + cheapest prices
  api/v1/summary.json             cheapest area per fuel
  api/v1/locations/<slug>.json    full station list
```

```js
const r = await fetch("https://bytehunt.github.io/gas-price-api/api/v1/summary.json");
```

Each response carries `station_prices` (are these real pump prices?), `degraded`
(no price data at all), and `stale` (a refresh failed, so this is the last good
data). A worse refresh never overwrites a good one.

## GitHub Actions

- **Update prices** — every 3 hours, commits `public/`, deploys to Pages.
- **CI** — lint, tests and build on every push and PR.

To enable:

1. **Settings → Pages → Source: GitHub Actions** — not "Branch". Branch mode
   only publishes the repo root or `/docs`, so it cannot serve `public/`.
2. **Settings → Actions → General → Workflow permissions: Read and write**
3. Run **Update prices** once manually to seed the site.

The site then lives at <https://bytehunt.github.io/gas-price-api/>.

Because `public/` is committed each run, your git history doubles as a price
history: `git log -p public/api/v1/locations/<slug>.json`.

## Adding a price source

A provider is one class — a name, a `has_prices` flag, and a `fetch` method
returning `Station` objects:

```python
class MyProvider:
    name = "mysource"
    has_prices = True

    async def fetch(self, place, *, radius_mi, fuels, limit):
        return stations, trend
```

Register it in `src/gasprice/providers/__init__.py` and list it ahead of
`overpass` under `providers:` in `locations.yaml`. Providers are tried in order
and the first with stations wins, so the per-station columns fill in everywhere
at once — CLI, live API and static files.

A `gasbuddy` provider ships with the repo but is **off by default**: it returns
403 on its pages and 400 from its GraphQL layer for automated clients, and
GitHub Actions IPs are blocked outright.

## Licensing

OpenStreetMap data is ODbL — attribute it if you republish. AAA averages are
read from their public pages; check their terms before building a product on
them. Scraping GasBuddy is contrary to their ToS; license per-station data if
you need it commercially.
