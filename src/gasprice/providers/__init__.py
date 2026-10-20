"""Price and station providers.

A provider is anything that can answer "what stations are near this point, and
what do they charge". `gasbuddy` has prices; `overpass` has only locations and
exists so the API still returns something useful when prices are unavailable.
"""

from __future__ import annotations

from .base import Provider
from .gasbuddy import GasBuddyProvider
from .overpass import OverpassProvider

#: Name -> constructor, for config files and the --provider flag.
REGISTRY: dict[str, type[Provider]] = {
    GasBuddyProvider.name: GasBuddyProvider,
    OverpassProvider.name: OverpassProvider,
}

#: Tried in order; the first one that returns stations wins.
#:
#: `gasbuddy` is deliberately NOT in the default chain: it is blocked (HTTP 403 on
#: its pages, 400 on its GraphQL endpoint) from datacenter IPs and probably from
#: yours, so leaving it first would just add a few seconds of doomed retries per
#: location. Opt in with `--provider gasbuddy` or a `providers:` list in
#: locations.yaml if you have a route that works.
DEFAULT_CHAIN = [OverpassProvider.name]


def get_provider(name: str) -> Provider:
    try:
        return REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(REGISTRY)}") from None


__all__ = [
    "DEFAULT_CHAIN",
    "REGISTRY",
    "GasBuddyProvider",
    "OverpassProvider",
    "Provider",
    "get_provider",
]
