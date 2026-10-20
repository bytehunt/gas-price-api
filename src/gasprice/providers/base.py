from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Place, Station, Trend


@runtime_checkable
class Provider(Protocol):
    name: str
    has_prices: bool
    """False for inventory-only sources; used to decide whether a run is degraded."""

    async def fetch(
        self,
        place: Place,
        *,
        radius_mi: float,
        fuels: list[str],
        limit: int,
    ) -> tuple[list[Station], Trend | None]:
        """Return stations within radius of place, nearest first, plus an area trend."""
        ...
