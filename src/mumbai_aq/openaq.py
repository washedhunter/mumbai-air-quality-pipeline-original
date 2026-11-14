"""Thin HTTP clients for the OpenAQ v3 API and OpenAQ's public S3 archive.

Both return the response bytes untouched; parsing happens in parse.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from .config import IST, Settings

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    content_type: str | None
    body: bytes | None
    fetched_at: datetime


class OpenAQError(RuntimeError):
    pass


def _get(
    client: httpx.Client,
    url: str,
    params: dict | None,
    max_retries: int,
    sleep: Callable[[float], None],
    ok_statuses: frozenset[int] = frozenset({200}),
) -> Fetched:
    """GET with retries on rate limits and server errors; honours OpenAQ's rate-limit headers."""
    attempt = 0
    while True:
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise OpenAQError(f"network error for {url}: {exc}") from exc
            sleep(min(2**attempt, 60))
            attempt += 1
            continue
        if resp.status_code in RETRY_STATUSES and attempt < max_retries:
            reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after")
            delay = float(reset) if reset and reset.replace(".", "", 1).isdigit() else 2**attempt
            sleep(min(max(delay, 1.0), 90.0))
            attempt += 1
            continue
        if resp.status_code not in ok_statuses:
            raise OpenAQError(f"HTTP {resp.status_code} for {resp.url}: {resp.text[:300]}")
        # Stay under 60 requests/minute: pause when the window is nearly used up.
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) <= 1:
            reset = resp.headers.get("x-ratelimit-reset", "60")
            sleep(float(reset) if reset.isdigit() else 60.0)
        return Fetched(
            url=str(resp.url),
            status=resp.status_code,
            content_type=resp.headers.get("content-type"),
            body=resp.content if resp.status_code == 200 else None,
            fetched_at=datetime.now(UTC),
        )


class OpenAQApi:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.openaq_api_key:
            raise OpenAQError("OPENAQ_API_KEY is not set (needed for the OpenAQ API)")
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.openaq_base_url,
            headers={"X-API-Key": settings.openaq_api_key, "Accept": "application/json"},
            timeout=settings.http_timeout_s,
        )
        self.sleep = sleep

    def locations(self) -> Fetched:
        s = self.settings
        params = {
            "coordinates": f"{s.center_lat},{s.center_lon}",
            "radius": s.radius_m,
            "limit": 1000,
        }
        return _get(self.client, "/locations", params, s.http_max_retries, self.sleep)

    def measurements(self, sensor_id: int, day: date, page: int = 1) -> Fetched:
        """Readings for one sensor whose period ends inside the IST day (00:00 exclusive, 24:00 inclusive)."""
        start = datetime.combine(day, datetime.min.time(), IST)
        end = start + timedelta(days=1)
        params = {
            "datetime_from": (start + timedelta(seconds=1)).astimezone(UTC).isoformat(),
            "datetime_to": end.astimezone(UTC).isoformat(),
            "limit": 1000,
            "page": page,
        }
        return _get(
            self.client,
            f"/sensors/{sensor_id}/measurements",
            params,
            self.settings.http_max_retries,
            self.sleep,
        )


class OpenAQArchive:
    """Public bucket, no key. One gzipped CSV per location and local day; ~4 days behind."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_s)
        self.sleep = sleep

    def url(self, location_id: int, day: date) -> str:
        return (
            f"{self.settings.archive_base_url}/records/csv.gz/locationid={location_id}"
            f"/year={day.year}/month={day.month:02d}/location-{location_id}-{day:%Y%m%d}.csv.gz"
        )

    def day(self, location_id: int, day: date) -> Fetched:
        # S3 answers 403 (not 404) for missing keys when listing isn't allowed for the object path.
        return _get(
            self.client,
            self.url(location_id, day),
            None,
            self.settings.http_max_retries,
            self.sleep,
            ok_statuses=frozenset({200, 403, 404}),
        )
