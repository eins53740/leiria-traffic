from __future__ import annotations

from datetime import date, datetime, time, timedelta

import httpx
import pytest
import respx

from leiria_traffic import routing
from leiria_traffic.db import Store
from leiria_traffic.engine import TZ
from leiria_traffic.heatmap import grid, heatmap, LEIRIA_CENTRE
from leiria_traffic.models import Point
from leiria_traffic.routing import GoogleRoutesProvider, ProviderError, TomTomProvider
from leiria_traffic.weather import OpenMeteoProvider

from .conftest import A, B

KEY = "SECRET-TEST-KEY-123"
DEP = datetime(2026, 10, 13, 8, 10, tzinfo=TZ)
SUMMARY = {"lengthInMeters": 11530, "travelTimeInSeconds": 1064, "trafficDelayInSeconds": 0,
           "departureTime": "2026-10-13T08:10:00+01:00", "arrivalTime": "2026-10-13T08:27:44+01:00",
           "noTrafficTravelTimeInSeconds": 933, "historicTrafficTravelTimeInSeconds": 1064,
           "liveTrafficIncidentsTravelTimeInSeconds": 1064}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(routing.asyncio, "sleep", instant)


@respx.mock
async def test_tomtom_parses_all_three_times():
    route = respx.get(url__regex=r"https://api.tomtom.com/routing/1/calculateRoute/.*").mock(
        return_value=httpx.Response(200, json={"routes": [{"summary": SUMMARY}]}))
    async with httpx.AsyncClient() as c:
        e = await TomTomProvider(KEY, c, qps=1000).route(A, B, DEP)
    q = route.calls[0].request.url.params
    assert q["computeTravelTimeFor"] == "all" and q["departAt"] == "2026-10-13T08:10:00+01:00"
    assert (e.duration_s, e.typical_s, e.free_flow_s, e.live_s, e.distance_m) == (1064, 1064, 933, 1064, 11530)
    assert e.arrival.strftime("%H:%M:%S") == "08:27:44"


@respx.mock
async def test_tomtom_retries_429_then_succeeds():
    respx.get(url__regex=r".*calculateRoute.*").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"routes": [{"summary": SUMMARY}]})])
    async with httpx.AsyncClient() as c:
        e = await TomTomProvider(KEY, c, qps=1000).route(A, B, DEP)
    assert e.duration_s == 1064


@respx.mock
async def test_tomtom_error_never_leaks_the_key():
    respx.get(url__regex=r".*calculateRoute.*").mock(
        return_value=httpx.Response(403, text=f"Developer Inactive key={KEY}"))
    async with httpx.AsyncClient() as c:
        with pytest.raises(ProviderError) as ei:
            await TomTomProvider(KEY, c, qps=1000).route(A, B, DEP)
    assert KEY not in str(ei.value) and "403" in str(ei.value)



@respx.mock
async def test_google_parses_durations_and_bounds_and_is_not_cacheable():
    def reply(request):
        model = __import__("json").loads(request.content)["trafficModel"]
        d = {"BEST_GUESS": "1100s", "PESSIMISTIC": "1400s", "OPTIMISTIC": "950s"}[model]
        return httpx.Response(200, json={"routes": [{"duration": d, "staticDuration": "1000s", "distanceMeters": 11600}]})
    respx.post(GoogleRoutesProvider.URL).mock(side_effect=reply)
    async with httpx.AsyncClient() as c:
        e = await GoogleRoutesProvider(KEY, c, qps=1000, with_bounds=True).route(A, B, DEP)
    assert (e.duration_s, e.low_s, e.high_s, e.typical_s, e.cacheable) == (1100, 950, 1400, 1000, False)


@respx.mock
async def test_weather_forecast_vs_beyond_horizon():
    respx.get(OpenMeteoProvider.FORECAST).mock(return_value=httpx.Response(200, json={"hourly": {
        "time": [f"2026-10-13T{h:02d}:00" for h in range(24)], "precipitation": [0.0] * 8 + [3.1] + [0.0] * 15,
        "temperature_2m": [15.0] * 24}}))
    async with httpx.AsyncClient() as c:
        w = OpenMeteoProvider(c, today=date(2026, 10, 8))
        day = await w.day(A, date(2026, 10, 13))
        assert day[8].raining and day[8].intensity == "moderate" and not day[9].raining
        assert await w.day(A, date(2026, 12, 1)) == {}


def test_grid_shape():
    g = grid(LEIRIA_CENTRE, 6, 10)
    assert len(g) == 100
    lats = sorted({p.lat for p in g})
    assert len(lats) == 10 and lats[0] < LEIRIA_CENTRE.lat < lats[-1]



class CountingRouting:
    name = "tomtom"

    def __init__(self, unroutable=0):
        self.calls, self.unroutable = 0, unroutable

    async def route(self, origin, dest, departure):
        from leiria_traffic.models import RouteEstimate
        self.calls += 1
        if self.calls <= self.unroutable:
            raise ProviderError("NO_ROUTE_FOUND")
        return RouteEstimate(self.name, departure, 900, free_flow_s=600)


async def test_heatmap_ratio_uses_provider_free_flow_and_cache(tmp_path):
    store, r = Store(tmp_path / "h.db"), CountingRouting(unroutable=1)
    h = await heatmap(r, store, timedelta(days=7), date(2026, 10, 13), time(8, 5), mode="ratio", per_side=3,
                      concurrency=1)
    assert h["departure"] == "08:00" and h["requested"] == 8  # centre cell skipped
    assert len(h["cells"]) == 7 and {c["ratio"] for c in h["cells"]} == {1.5}
    assert h["source"].endswith("no-traffic time")
    again = await heatmap(r, store, timedelta(days=7), date(2026, 10, 13), time(8, 0), mode="ratio", per_side=3)
    assert again["provider_calls"] == 1  # only the previously unroutable cell is retried


def test_logging_setup_keeps_httpx_quiet():
    """httpx's INFO line prints the full URL, and the TomTom key is in the query string."""
    import logging

    from leiria_traffic.config import setup_logging

    setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
