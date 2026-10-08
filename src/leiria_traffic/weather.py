"""Hourly weather for a day at a point (Open-Meteo: forecast ≤16 days, archive for the past).

Free tier is for non-commercial use (<10 000 calls/day); data CC-BY 4.0.
https://open-meteo.com/en/docs · https://open-meteo.com/en/docs/historical-weather-api
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Protocol

import httpx

from .models import Point, Weather

log = logging.getLogger(__name__)
FORECAST_DAYS = 16


class WeatherProvider(Protocol):
    async def day(self, where: Point, day: date) -> dict[int, Weather]: ...


class OpenMeteoProvider:
    FORECAST = "https://api.open-meteo.com/v1/forecast"
    ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

    def __init__(self, client: httpx.AsyncClient, today: date | None = None):
        self._client = client
        self._today = today
        self._cache: dict[tuple[str, date], dict[int, Weather]] = {}

    async def day(self, where: Point, day: date) -> dict[int, Weather]:
        """{hour: Weather}. Empty dict when the date is beyond the forecast horizon
        or the service fails: weather is optional, a miss must not break a profile."""
        key = (where.key(), day)
        if key in self._cache:
            return self._cache[key]
        today = self._today or date.today()
        if day > today + timedelta(days=FORECAST_DAYS - 1):
            return {}
        url, source = (self.ARCHIVE, "open-meteo-archive") if day < today - timedelta(days=5) \
            else (self.FORECAST, "open-meteo-forecast")
        params = {"latitude": where.lat, "longitude": where.lon, "hourly": "precipitation,temperature_2m",
                  "timezone": "Europe/Lisbon", "start_date": day.isoformat(), "end_date": day.isoformat()}
        try:
            r = await self._client.get(url, params=params, timeout=15)
            r.raise_for_status()
            h = r.json()["hourly"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("weather unavailable for %s: %s", day, e)
            return {}
        out = {int(t[11:13]): Weather(p, temp, source)
               for t, p, temp in zip(h["time"], h["precipitation"], h["temperature_2m"])}
        self._cache[key] = out
        return out
