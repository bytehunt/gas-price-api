"""Shared HTTP plumbing: one client factory, one retry policy."""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

import httpx

DEFAULT_UA = "gas-price-api/0.1 (+https://github.com/; personal fuel price tracker)"


def user_agent() -> str:
    return os.environ.get("GASPRICE_USER_AGENT") or DEFAULT_UA


def make_client(timeout: float = 20.0, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    base = {"User-Agent": user_agent(), "Accept": "application/json"}
    if headers:
        base.update(headers)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers=base,
        follow_redirects=True,
    )


class ProviderError(RuntimeError):
    """A provider could not produce data. Callers degrade rather than crash."""


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 1.5,
    **kwargs: Any,
) -> httpx.Response:
    """Request with retries on transport errors and 429/5xx.

    Raises ProviderError when every attempt fails, so a caller can fall back to
    another provider instead of the whole run dying.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            # Jittered backoff; shared public endpoints rate-limit aggressively.
            await asyncio.sleep(backoff**attempt + random.uniform(0, 0.4))
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last = exc
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = ProviderError(f"{url} -> HTTP {resp.status_code}")
            continue
        if resp.status_code >= 400:
            # 4xx other than 429 won't improve by retrying (403 = blocked, etc.).
            raise ProviderError(f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp
    raise ProviderError(f"{url} failed after {attempts} attempts: {last}")


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    resp = await request(client, method, url, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        # Cloudflare interstitials come back as HTML with a 200.
        raise ProviderError(f"{url} -> non-JSON response ({resp.text[:120]!r})") from exc


async def request_text(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> str:
    resp = await request(client, method, url, **kwargs)
    if not resp.text.strip():
        raise ProviderError(f"{url} -> empty response body")
    return resp.text
