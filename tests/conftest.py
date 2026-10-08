from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from leiria_traffic.calendar import ContextBuilder, EventCalendar, PortugalHolidays, YamlSchoolCalendar
from leiria_traffic.db import Store
from leiria_traffic.engine import EngineConfig, TravelTimeEngine
from leiria_traffic.models import Point, RouteEstimate, Weather
from leiria_traffic.routing import ProviderError

DATA = Path(__file__).resolve().parents[1] / "data"
A, B = Point(39.7436, -8.8071), Point(39.6930, -8.8960)


class FakeRouting:
    """Deterministic provider: 15 min free flow, +10 min peak at 08:00-09:00 and 18:00-19:00."""

    def __init__(self, name: str = "fake", fail: bool = False, cacheable: bool = True):
        self.name, self.fail, self.cacheable, self.calls = name, fail, cacheable, 0

    async def route(self, origin, dest, departure: datetime) -> RouteEstimate:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} down")
        peak = 600 if departure.hour in (8, 18) else 0
        return RouteEstimate(self.name, departure, 900 + peak, 11530, typical_s=900 + peak, free_flow_s=900,
                             cacheable=self.cacheable)


class FakeWeather:
    def __init__(self, rain_hours=()):
        self.rain_hours = set(rain_hours)

    async def day(self, where, day):
        return {h: Weather(3.0 if h in self.rain_hours else 0.0, 15.0, "fake") for h in range(24)}


@pytest.fixture
def contexts():
    return ContextBuilder(YamlSchoolCalendar(DATA / "school_calendar.yaml"), PortugalHolidays("10"),
                          EventCalendar.load(DATA / "events.yaml"))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


@pytest.fixture
def make_engine(contexts, store):
    def make(routing=None, weather=None, **cfg):
        return TravelTimeEngine(routing or FakeRouting(), weather or FakeWeather(), contexts, store,
                                EngineConfig(**cfg))
    return make


MONDAY_TERM = date(2026, 10, 12)
