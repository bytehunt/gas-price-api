"""gas-price-api: nearby gas station prices for any location."""

from __future__ import annotations

__version__ = "0.1.0"

from .core import fetch_stations
from .models import FUELS, SCHEMA_VERSION, Place, Price, Station, StationsResult

__all__ = [
    "FUELS",
    "SCHEMA_VERSION",
    "Place",
    "Price",
    "Station",
    "StationsResult",
    "__version__",
    "fetch_stations",
]
