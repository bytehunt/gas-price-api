# gas-price-api

A free, key-less gas price API. Give it any location — ZIP code, place name, or
coordinates — and it returns the gas stations near that point plus current fuel
prices for the area, as both a **static JSON API** (refreshed by GitHub Actions
and served from GitHub Pages) and a **live HTTP API** you can run locally.

```console
$ gasprice fetch "20850" -r 4 --metro "Washington (MD only)"
20850, Rockville, Montgomery County, Maryland, United States  ·  4 mi  ·  25 stations  ·  via overpass, aaa
Washington (MD only) metro average (AAA, Oct 20): Regular $4.18 ▼0.12  Midgrade $4.83 ▼0.08  Premium $5.13 ▼0.10  Diesel $6.41 ▲0.01  (vs. a week ago)

Station             Address                              mi  Regular  Midgrade  Premium  Diesel
Shell               1250 West Montgomery Avenue         0.1        —         —        —       —
7-Eleven            1080 Rockville Pike                 0.4        —         —        —       —
Hungerford Marathon 801 Hungerford Drive                1.3        —         —        —       —
...
```

## Read this first: why those price columns are empty

**There is no free source of per-station gas prices in the United States.** This
is not a gap in this tool — it is why every service you found wants money.

US pump prices are not published by anyone. They are collected either by
commercial price-reporting firms (OPIS, Wood Mackenzie) who license the data, or
by crowd-sourcing apps whose users report prices. That data is the product, so
it is sold, not given away. Specifically:

| Source | Per-station prices? | Free? | Usable here? |
| --- | --- | --- | --- |
| OpenStreetMap (Overpass) | no — locations only | yes | ✅ station discovery |
| AAA | no — daily state & metro averages | yes, no key | ✅ current prices |
| EIA (US government) | no — weekly regional averages | yes, free key | plausible addition |
| GasBuddy | **yes** | no public API | ❌ blocks automation |
| OPIS / GasBuddy Business | yes | no, licensed | ✅ if you pay |

I checked GasBuddy specifically, since it is the only free-to-a-human source of
real per-station prices. It actively blocks automated access: its pages return
HTTP 403 to non-browser clients and its internal GraphQL endpoint returns HTTP
400 to anything that is not its own app (the rejection comes from their
application, not the CDN, which suggests a persisted-query allowlist). A
`gasbuddy` provider is included and wired up, but it is **not** in the default
chain because it does not currently work — from a GitHub Actions runner it never
will, as those IP ranges are blocked outright.

### So what do you actually get?

1. **Every station near any location**, with name, brand, full address, distance,
   and a map link — from OpenStreetMap. Complete, free, unlimited, no key.
2. **Today's real prices for your area**, per fuel grade, from AAA's daily
   averages — including yesterday / last week / last month / last year, so
   "are prices rising?" is answerable.
3. **A drop-in slot for per-station prices** if you ever get access to a source
   that has them. Write one `Provider` class, add it to the registry, and the
   station rows fill in — nothing else changes.

Area averages are never mixed with station prices. `Station.prices` only ever
holds genuine per-station data; averages live in a separate `benchmark` object
and are always labelled with their source and area. If a response has no price
information at all, `degraded` is `true`.

## Quick start

Requires [Nix](https://nixos.org/download) with flakes, and
[direnv](https://direnv.net) if you want the shell to load automatically.

```console
$ direnv allow          # or: nix develop
$ gasprice fetch 20850  # prices near a ZIP
$ gasprice collect      # refresh ./public from locations.yaml
$ gasprice serve        # live API at http://127.0.0.1:8000/docs
$ pytest -q             # 122 tests, no network needed
```

Everything is pinned by `flake.nix` — Python, the libraries, ruff, jq, gh. The
dev shell puts `./bin` on `PATH`, so the `gasprice` command works in a checkout
with no install step, and CI runs the exact commands documented here.

## CLI

```console
gasprice fetch <location> [-r MILES] [-n LIMIT] [--fuel regular] [--cash] [--json]
gasprice cheapest <location> [--fuel diesel]    # best single price
gasprice collect [-c locations.yaml] [-o public] [--fail-on-degraded]
gasprice locations                              # list configured locations
gasprice serve [--host H] [--port P] [--reload]
```

`<location>` is a ZIP code, a place name, or `lat,lon`. Coordinates skip
geocoding entirely. Useful flags:

- `--metro "Washington (MD only)"` — pin which AAA metro average to use
- `--provider gasbuddy` — force a single provider (see the caveat above)
- `--no-benchmark` — skip the area-average lookup
- `--no-cache` — bypass the local geocode/response cache

## Live API

`gasprice serve` runs a FastAPI app with interactive docs at `/docs`.

| Endpoint | Returns |
| --- | --- |
| `GET /v1/stations?q=20850&radius=5` | nearby stations, nearest first, with prices where available |
| `GET /v1/cheapest?q=20850&fuel=diesel` | best single price, or the area average when no station is priced |
| `GET /v1/benchmark?q=20850` | today's area averages plus week/month/year change |
| `GET /v1/locations` | the tracked locations from the last collect |
| `GET /v1/locations/{slug}` | one tracked location |
| `GET /v1/fuels`, `GET /v1/health` | metadata |

All of them accept `q=` **or** `lat=` + `lon=`, plus `radius`, `fuel`
(repeatable), `limit`, `provider` and `metro`. Responses are cached in-process
for 5 minutes (`GASPRICE_SERVER_TTL`) so a refresh does not hammer upstream.

`/v1/cheapest` includes a `best_estimate` object that tells you what to display
and whether it is a real station price or an area average:

```json
{
  "fuel": "regular",
  "station_prices": false,
  "best_estimate": { "price": 4.054, "kind": "area_average" },
  "area_average": 4.054,
  "station": null
}
```

## Static API

`gasprice collect` reads `locations.yaml` and writes a plain JSON tree:

```
public/
  index.html                      small dashboard
  api/v1/index.json               every location + its cheapest prices
  api/v1/summary.json             cheapest per fuel across all locations
  api/v1/locations/<slug>.json    full station list for one location
```

These are static files, so GitHub Pages serves them as a free, fast, no-auth
API. Fetch them from anywhere:

```js
const r = await fetch("https://<user>.github.io/gas-price-api/api/v1/locations/rockville-md.json");
```

Because the workflow commits `public/` on every run, **the git history is a free
price history**:

```console
$ git log -p --follow public/api/v1/locations/rockville-md.json
```

## Configuring locations

```yaml
site_title: Maryland Gas Prices
providers: [overpass]        # add `gasbuddy` if you have a working route

defaults:
  radius_miles: 5
  max_stations: 25
  fuels: [regular, midgrade, premium, diesel]

locations:
  - name: Rockville, MD
    slug: rockville-md       # becomes the API path; defaults to a slug of the name
    query: "Rockville, MD"   # ZIP, place name, or "lat,lon"
    aaa_metro: Washington (MD only)
    radius_miles: 4          # per-location overrides

  - "21201"                  # shorthand: just a query string
```

`aaa_metro` matters more than it looks. AAA groups prices into metro areas whose
names often aren't the town you searched for — most of the Maryland DC suburbs
are in `Washington (MD only)`, which no string match would ever guess. Without
the hint you get the statewide average, which is a worse estimate. To see what
you're getting:

```console
$ gasprice fetch "Bethesda, MD" --json | jq '.benchmark | {scope, area}'
```

## GitHub Actions

Two workflows, both running the same Nix shell as your laptop:

- **`.github/workflows/update.yml`** — every 3 hours (`17 */3 * * *`) plus manual
  dispatch. Collects prices, commits `public/`, writes a run summary table, and
  deploys to Pages. Rebases before pushing so concurrent runs don't collide.
- **`.github/workflows/ci.yml`** — lint, format check, tests, `nix build`, and a
  CLI smoke test on every push and PR.

To turn it on:

1. Push the repo to GitHub.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Actions → General → Workflow permissions: Read and write.**
4. Run **Update prices** once manually to seed `public/` and publish the site.

Scheduled runs are skipped on repos with no activity for 60 days — push a commit
or re-run manually to wake it up. If you only want the JSON and not the Pages
site, delete the `deploy` job and the upload step.

### Reliability

The collector is built on the assumption that free upstreams fail:

- Overpass returns 502/504 under load, so there are four mirrors, each retried
  with jittered backoff. An empty response is treated as a failure and the next
  mirror is tried, because a busy server and a genuinely empty area look the
  same.
- Nominatim is rate-limited to 1 req/sec and results are cached for a month.
- One location failing never aborts the run; it is recorded in
  `index.json → failures`.
- **A worse result never overwrites a good one.** If a refresh loses its prices
  or loses every station, the previously published file is kept and flagged
  `"stale": true` with a `stale_as_of` timestamp. Use `--fail-on-degraded` to
  make the workflow go red when that happens.
- Overpass and Nominatim both ask automated clients to identify themselves; set
  `GASPRICE_USER_AGENT` to something with a contact URL. (Overpass's nginx
  answers 406 to requests without a real UA.)

## Layout

```
flake.nix              pinned toolchain; devShell + packaged app
locations.yaml         which places to track
bin/gasprice           dev-shell entry point
src/gasprice/
  core.py              orchestration: resolve location -> providers -> benchmark
  models.py            Station / Price / Benchmark / StationsResult
  providers/
    overpass.py        OpenStreetMap station discovery (default)
    gasbuddy.py        per-station prices; blocked, opt-in, documented
  aaa.py               daily state & metro averages
  geocode.py           Nominatim forward + reverse geocoding
  collect.py           writes the static API tree
  server.py            FastAPI live API
  cli.py               command line
site/index.html        dashboard copied into public/
tests/                 122 tests; parsers pinned against recorded fixtures
```

## Adding a price source

`Provider` is a small protocol — a name, a `has_prices` flag, and one `fetch`
method returning normalized `Station` objects:

```python
class MyProvider:
    name = "mysource"
    has_prices = True

    async def fetch(self, place, *, radius_mi, fuels, limit):
        ...
        return stations, trend
```

Register it in `src/gasprice/providers/__init__.py` and put it ahead of
`overpass` in `providers:`. Because providers are tried in order and the first
one with stations wins, a working price source immediately populates the station
rows across the CLI, the live API and the static files.

## Legal note

OpenStreetMap data is ODbL — attribute it if you republish. AAA averages are
read from their public pages for personal use; check their terms before building
a product on them. The `gasbuddy` provider queries an undocumented endpoint and
scraping it is contrary to GasBuddy's Terms of Service — it is off by default,
and if you need per-station prices commercially, license them.
