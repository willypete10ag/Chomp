"""Hevy's public API (Hevy Pro, #113): https://api.hevyapp.com/docs.

A personal key from hevy.com/settings?developer goes in the `api-key` header. Lists come 10 to
a page. Chomp reads two of them: every workout (the first sync) and the workout events feed,
which lists workouts updated or deleted since a time. The sync runs in a background thread, so
this client is synchronous; it retries transport errors, 429 and 5xx like sources/http.py.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from chomp.sources import SourceError

log = logging.getLogger("chomp.sources")

BASE_URL = "https://api.hevyapp.com"
PAGE_SIZE = 10  # the most Hevy allows
MAX_PAGES = 5000  # 50,000 workouts; a guard against a feed that never ends
RETRY_STATUS = {429, 500, 502, 503, 504}


class HevyKeyError(SourceError):
    """Hevy refused the API key (wrong key, or Pro has lapsed)."""


def _items(body: dict, key: str, path: str) -> list:
    """The list in a page. The live server doesn't always match the spec: with nothing to
    list, the events feed answers without an `events` list at all, so a page with no list in
    it is empty, and one list under another name is taken as the items."""
    items = body.get(key)
    if isinstance(items, list):
        return items
    lists = {k: v for k, v in body.items() if isinstance(v, list)}
    if not lists:
        log.info(
            "hevy %s: no %s in the reply (keys: %s); taking it as empty", path, key, sorted(body)
        )
        return []
    if len(lists) == 1:
        ((other, items),) = lists.items()
        log.warning("hevy %s: items under %r instead of %r", path, other, key)
        return items
    raise SourceError(f"Hevy's {path} returned no {key} list (keys: {', '.join(sorted(body))})")


class HevyClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        attempts: int = 3,
        backoff_s: float = 1.0,
        max_wait_s: float = 30.0,
    ) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=30.0)
        self._base = base_url.rstrip("/")
        self.attempts = attempts
        self.backoff_s = backoff_s
        self.max_wait_s = max_wait_s

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HevyClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        """GET JSON; None for a 404, which Hevy sends for a page past the last one (and for
        page 1 of an empty list). Logs carry the path and status only, never the key."""
        delay = self.backoff_s
        for attempt in range(1, self.attempts + 1):
            try:
                r = self._client.get(
                    self._base + path, params=params, headers={"api-key": self._key}
                )
            except httpx.TransportError as e:
                log.warning(
                    "hevy %s: %s (attempt %d/%d)", path, type(e).__name__, attempt, self.attempts
                )
                if attempt == self.attempts:
                    raise SourceError("Hevy is unreachable right now") from e
            else:
                if r.status_code == 404:
                    return None
                if r.status_code in (401, 403):
                    log.warning("hevy %s: HTTP %d", path, r.status_code)
                    raise HevyKeyError(
                        f"Hevy refused the API key (HTTP {r.status_code}). Check HEVY_API_KEY"
                        " and that Hevy Pro is active."
                    )
                if r.status_code in RETRY_STATUS:
                    log.warning(
                        "hevy %s: HTTP %d (attempt %d/%d)",
                        path,
                        r.status_code,
                        attempt,
                        self.attempts,
                    )
                    if attempt == self.attempts:
                        raise SourceError(f"Hevy is unavailable (HTTP {r.status_code})")
                    if r.status_code == 429:
                        with contextlib.suppress(ValueError):
                            wait = float(r.headers.get("Retry-After", delay))
                            delay = min(wait, self.max_wait_s)
                elif r.is_error:
                    log.warning("hevy %s: HTTP %d", path, r.status_code)
                    raise SourceError(f"Hevy rejected the request (HTTP {r.status_code})")
                else:
                    try:
                        body = r.json()
                    except ValueError as e:
                        raise SourceError("Hevy returned invalid JSON") from e
                    if not isinstance(body, dict):
                        raise SourceError("Hevy returned something unexpected")
                    return body
            time.sleep(delay)
            delay = min(delay * 2, self.max_wait_s)
        raise AssertionError("unreachable")

    def _pages(self, path: str, key: str, params: dict[str, Any]) -> Iterator[dict]:
        page = 1
        while page <= MAX_PAGES:
            body = self._get(path, {**params, "page": page, "pageSize": PAGE_SIZE})
            if body is None:
                return
            items = _items(body, key, path)
            yield from (x for x in items if isinstance(x, dict))
            count = body.get("page_count")
            if not items or not isinstance(count, int) or page >= count:
                return
            page += 1
        raise SourceError(f"Hevy's {path} has more than {MAX_PAGES} pages")

    def workouts(self) -> Iterator[dict]:
        """Every workout on the account, newest first."""
        return self._pages("/v1/workouts", "workouts", {})

    def events(self, since: str) -> Iterator[dict]:
        """Workout events since a UTC time: {"type": "updated", "workout": {...}} or
        {"type": "deleted", "id": ..., "deleted_at": ...}."""
        return self._pages("/v1/workouts/events", "events", {"since": since})
