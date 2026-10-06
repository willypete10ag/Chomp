"""Shared HTTP plumbing for sources: timeouts, retries with backoff, and a small TTL cache."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

import httpx

from chomp.sources import SourceError

log = logging.getLogger("chomp.sources")

RETRY_STATUS = {429, 500, 502, 503, 504}


class TTLCache:
    """In-process cache for raw upstream responses. Bounded by size and age."""

    def __init__(self, ttl_s: float, max_items: int = 512) -> None:
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._items: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()

    def get(self, key: tuple) -> Any | None:
        hit = self._items.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self.ttl_s:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: tuple, value: Any) -> None:
        self._items[key] = (time.monotonic(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)


def _retry_after(response: httpx.Response, default: float, cap: float) -> float:
    try:
        return min(float(response.headers.get("Retry-After", default)), cap)
    except ValueError:
        return default


async def get_json(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    backoff_s: float = 0.5,
    max_wait_s: float = 5.0,
    not_found_ok: bool = False,
) -> Any | None:
    """GET JSON with retries on transport errors, 429 and 5xx.

    Returns None for a 404 when `not_found_ok`. Raises SourceError otherwise. Logs carry
    the path and status only: never query strings or headers, which may hold keys.
    """
    path = httpx.URL(url).path
    delay = backoff_s
    for attempt in range(1, attempts + 1):
        try:
            r = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as e:
            err = type(e).__name__
            log.warning("%s %s: %s (attempt %d/%d)", name, path, err, attempt, attempts)
            if attempt == attempts:
                raise SourceError(f"{name} is unreachable right now") from e
        else:
            if r.status_code == 404 and not_found_ok:
                return None
            if r.status_code in RETRY_STATUS:
                status = r.status_code
                log.warning("%s %s: HTTP %d (attempt %d/%d)", name, path, status, attempt, attempts)
                if attempt == attempts:
                    what = "rate limit reached" if r.status_code == 429 else "unavailable"
                    raise SourceError(f"{name} {what} (HTTP {r.status_code})")
                delay = _retry_after(r, delay, max_wait_s) if r.status_code == 429 else delay
            elif r.is_error:
                log.warning("%s %s: HTTP %d", name, path, r.status_code)
                raise SourceError(f"{name} rejected the request (HTTP {r.status_code})")
            else:
                try:
                    return r.json()
                except ValueError as e:
                    raise SourceError(f"{name} returned invalid JSON") from e
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_wait_s)
    raise AssertionError("unreachable")
