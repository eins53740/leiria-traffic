"""Builds the object graph from Settings (shared by the web app and the CLI)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx

from .calendar import ContextBuilder, EventCalendar, PortugalHolidays, YamlSchoolCalendar
from .config import Settings
from .db import Store
from .engine import EngineConfig, TravelTimeEngine
from .routing import FallbackRouting, GoogleRoutesProvider, TomTomProvider
from .weather import OpenMeteoProvider

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    client: httpx.AsyncClient
    store: Store
    engine: TravelTimeEngine
    tomtom: TomTomProvider | None


def build(settings: Settings, client: httpx.AsyncClient) -> App:
    available = {}
    if settings.tomtom_key:
        available["tomtom"] = TomTomProvider(settings.tomtom_key, client, settings.tomtom_qps)
    if settings.google_key:
        available["google"] = GoogleRoutesProvider(settings.google_key, client, with_bounds=settings.google_bounds)
    chain = [available[p] for p in settings.providers if p in available]
    skipped = [p for p in settings.providers if p not in available]
    if skipped:
        log.warning("providers without an API key, skipped: %s", ", ".join(skipped))
    store = Store(settings.db_path)
    contexts = ContextBuilder(YamlSchoolCalendar(settings.data_dir / "school_calendar.yaml"),
                              PortugalHolidays(settings.holiday_subdiv),
                              EventCalendar.load(settings.data_dir / "events.yaml"))
    engine = TravelTimeEngine(FallbackRouting(chain), OpenMeteoProvider(client), contexts, store,
                              EngineConfig(cache_ttl=timedelta(days=settings.cache_ttl_days),
                                           rain_adjust_pct=settings.rain_adjust_pct))
    return App(settings, client, store, engine, available.get("tomtom"))
