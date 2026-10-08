"""Routing providers behind one interface. Keys come from the environment only."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Protocol

import httpx

from .models import Point, RouteEstimate

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """The provider could not answer (bad key, disabled API, quota, no route…)."""


class RoutingProvider(Protocol):
    name: str

    async def route(self, origin: Point, dest: Point, departure: datetime) -> RouteEstimate: ...


class RateLimiter:
    """Spaces calls at least 1/qps seconds apart (shared by all tasks of a provider)."""

    def __init__(self, qps: float):
        self._interval = 1.0 / qps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = _time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


async def _request(client: httpx.AsyncClient, limiter: RateLimiter, method: str, url: str, *,
                   attempts: int = 3, **kw) -> httpx.Response:
    """HTTP with rate limiting and exponential backoff on 429/5xx/network errors.
    Never logs the URL query (it carries the TomTom key)."""
    delay = 1.0
    for n in range(1, attempts + 1):
        await limiter.wait()
        try:
            r = await client.request(method, url, **kw)
        except httpx.TransportError as e:
            if n == attempts:
                raise ProviderError(f"network error: {type(e).__name__}") from None
            log.warning("network error %s, retry %d/%d", type(e).__name__, n, attempts)
        else:
            if r.status_code != 429 and r.status_code < 500:
                return r
            if n == attempts:
                return r
            retry_after = r.headers.get("Retry-After", "")
            log.warning("HTTP %s, retry %d/%d", r.status_code, n, attempts)
            if retry_after.isdigit():
                delay = max(delay, float(retry_after))
        await asyncio.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


def _err(r: httpx.Response, secret: str) -> str:
    return f"HTTP {r.status_code}: {r.text[:300].replace(secret, '***')}"


class TomTomProvider:
    """TomTom Routing API calculateRoute.

    With `departAt` in the future TomTom's travel time uses its historic speed
    profiles; `computeTravelTimeFor=all` also returns no-traffic and live
    variants in the same call (one billable request).
    https://developer.tomtom.com/routing-api/documentation/tomtom-maps/calculate-route
    """

    name = "tomtom"
    BASE = "https://api.tomtom.com"

    def __init__(self, key: str, client: httpx.AsyncClient, qps: float = 5.0):
        if not key:
            raise ValueError("TOMTOM_API_KEY is not set")
        self._key, self._client, self._limiter = key, client, RateLimiter(qps)

    async def route(self, origin: Point, dest: Point, departure: datetime | None) -> RouteEstimate:
        """departure=None means "leave now" (live traffic; used by the collector)."""
        url = f"{self.BASE}/routing/1/calculateRoute/{origin.key()}:{dest.key()}/json"
        params = {"key": self._key, "traffic": "true", "travelMode": "car", "routeType": "fastest",
                  "computeTravelTimeFor": "all"}
        params["departAt"] = "now" if departure is None else departure.isoformat(timespec="seconds")
        r = await _request(self._client, self._limiter, "GET", url, params=params)
        if r.status_code != 200:
            raise ProviderError(_err(r, self._key))
        s = r.json()["routes"][0]["summary"]
        return RouteEstimate(
            provider=self.name,
            departure=datetime.fromisoformat(s["departureTime"]),
            duration_s=s["travelTimeInSeconds"],
            distance_m=s["lengthInMeters"],
            typical_s=s.get("historicTrafficTravelTimeInSeconds"),
            free_flow_s=s.get("noTrafficTravelTimeInSeconds"),
            live_s=s.get("liveTrafficIncidentsTravelTimeInSeconds"),
        )



def _secs(v: str | None) -> int | None:
    return None if v is None else int(float(v.rstrip("s")))


class GoogleRoutesProvider:
    """Google Routes API computeRoutes, TRAFFIC_AWARE_OPTIMAL (Pro SKU).

    `with_bounds` adds PESSIMISTIC and OPTIMISTIC calls (3× cost) to get a range.
    Results are marked non-cacheable: the Maps Platform terms forbid storing them.
    https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes
    """

    name = "google"
    URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

    def __init__(self, key: str, client: httpx.AsyncClient, qps: float = 10.0, with_bounds: bool = False):
        if not key:
            raise ValueError("GOOGLE_MAPS_API_KEY is not set")
        self._key, self._client, self._limiter, self._bounds = key, client, RateLimiter(qps), with_bounds

    async def _one(self, origin: Point, dest: Point, departure: datetime, model: str) -> dict:
        body = {
            "origin": {"location": {"latLng": {"latitude": origin.lat, "longitude": origin.lon}}},
            "destination": {"location": {"latLng": {"latitude": dest.lat, "longitude": dest.lon}}},
            "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE_OPTIMAL", "trafficModel": model,
            "departureTime": departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        r = await _request(self._client, self._limiter, "POST", self.URL, json=body, headers={
            "X-Goog-Api-Key": self._key,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters"})
        if r.status_code != 200:
            raise ProviderError(_err(r, self._key))
        routes = r.json().get("routes")
        if not routes:
            raise ProviderError("no route")
        return routes[0]

    async def route(self, origin: Point, dest: Point, departure: datetime) -> RouteEstimate:
        best = await self._one(origin, dest, departure, "BEST_GUESS")
        low = high = None
        if self._bounds:
            hi, lo = await asyncio.gather(self._one(origin, dest, departure, "PESSIMISTIC"),
                                          self._one(origin, dest, departure, "OPTIMISTIC"))
            low, high = _secs(lo["duration"]), _secs(hi["duration"])
        return RouteEstimate(
            provider=self.name, departure=departure, duration_s=_secs(best["duration"]),
            distance_m=best.get("distanceMeters"),
            # staticDuration: Google's docs define it both as "no traffic" and as
            # "historical only"; we expose it as typical_s and say so in the README.
            typical_s=_secs(best.get("staticDuration")),
            low_s=low, high_s=high, cacheable=False)


class FallbackRouting:
    """Tries providers in order; the first that answers wins."""

    def __init__(self, providers: list[RoutingProvider]):
        self.providers = providers
        self.name = "+".join(p.name for p in providers) or "none"

    async def route(self, origin: Point, dest: Point, departure: datetime) -> RouteEstimate:
        if not self.providers:
            raise ProviderError("no routing provider configured (set TOMTOM_API_KEY / GOOGLE_MAPS_API_KEY)")
        errors = []
        for p in self.providers:
            try:
                return await p.route(origin, dest, departure)
            except ProviderError as e:
                log.warning("%s failed: %s", p.name, e)
                errors.append(f"{p.name}: {e}")
        raise ProviderError("; ".join(errors))
