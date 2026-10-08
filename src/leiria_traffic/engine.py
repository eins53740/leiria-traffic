"""Travel-time engine: provider prediction per 10-min slot + labelled extras.

Every number carries its Source. The provider's prediction is never altered in
place: our adjustments are listed separately and only applied to `duration_min`
when explicitly enabled (RAIN_ADJUST_PCT > 0).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .calendar import ContextBuilder
from .db import Store
from .models import DayContext, Point, RouteEstimate, SlotResult, Source, Weather, slot_times, snap_to_slot
from .routing import ProviderError, RoutingProvider
from .stats import summarize
from .weather import WeatherProvider

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Lisbon")


@dataclass
class EngineConfig:
    cache_ttl: timedelta = timedelta(days=7)
    rain_adjust_pct: float = 0.0  # 0 = off. FHWA range for arterials: +11..+50 % (see README)
    min_samples: int = 3  # measured stats shown only with at least this many observations
    concurrency: int = 8


def _min(s: int | None) -> float | None:
    return None if s is None else round(s / 60, 1)


class TravelTimeEngine:
    def __init__(self, routing: RoutingProvider, weather: WeatherProvider, contexts: ContextBuilder,
                 store: Store, config: EngineConfig | None = None):
        self.routing, self.weather, self.contexts, self.store = routing, weather, contexts, store
        self.cfg = config or EngineConfig()

    # ------------------------------------------------------------------ helpers
    def context(self, day: date) -> DayContext:
        c = self.contexts.build(day)
        self.store.save_context(c)
        return c

    async def _estimate(self, o: Point, d: Point, departure: datetime) -> RouteEstimate:
        for p in getattr(self.routing, "providers", [self.routing]):
            hit = self.store.cached(p.name, o, d, departure, self.cfg.cache_ttl)
            if hit:
                return hit
        e = await self.routing.route(o, d, departure)
        self.store.put(o, d, departure, e)
        return e

    @staticmethod
    def _weather_for(hourly: dict[int, Weather], hour: int, override: str) -> Weather | None:
        if override == "dry":
            return Weather(0.0, None, "override")
        if override == "rain":
            return Weather(3.0, None, "override")
        return hourly.get(hour)

    def _slot(self, e: RouteEstimate | None, departure: datetime, w: Weather | None, ctx: DayContext,
              route_id: int | None, error: str | None = None) -> SlotResult:
        hhmm = departure.strftime("%H:%M")
        weather_txt = None if w is None else (w.intensity or "unknown") + f" ({w.source})"
        if e is None:
            return SlotResult(hhmm, None, None, None, weather=weather_txt, error=error)
        duration = e.duration_s
        adjustments = []
        if self.cfg.rain_adjust_pct and w is not None and w.raining:
            extra = round(duration * self.cfg.rain_adjust_pct / 100)
            adjustments.append({"source": Source.WEATHER_ADJUSTMENT.value, "seconds": extra,
                                "reason": f"rain {w.precipitation_mm} mm/h, +{self.cfg.rain_adjust_pct:g} % (FHWA range)"})
            duration += extra
        measured = None
        if route_id is not None:
            vals = self.store.observed_minutes(route_id, hhmm, weekday=ctx.weekday,
                                               school_term=ctx.school_term,
                                               raining=None if w is None else w.raining)
            s = summarize(vals)
            if s and s["n"] >= self.cfg.min_samples:
                measured = {"source": Source.MEASURED_STATS.value, **{k: round(v, 1) for k, v in s.items()}}
        return SlotResult(
            departure=hhmm, duration_min=_min(duration),
            arrival=(departure + timedelta(seconds=duration)).strftime("%H:%M"),
            source=Source.PROVIDER_PREDICTION, provider=e.provider, typical_min=_min(e.typical_s),
            free_flow_min=_min(e.free_flow_s), low_min=_min(e.low_s), high_min=_min(e.high_s),
            distance_km=None if e.distance_m is None else round(e.distance_m / 1000, 2),
            weather=weather_txt, adjustments=adjustments, measured=measured)

    # ------------------------------------------------------------------ public
    async def estimate(self, o: Point, d: Point, day: date, at: time, weather: str = "auto") -> dict:
        """One departure ("how long if I leave at 08:10 on …")."""
        departure = datetime.combine(day, snap_to_slot(at), TZ)
        ctx = self.context(day)
        hourly = await self.weather.day(o, day) if weather == "auto" else {}
        w = self._weather_for(hourly, departure.hour, weather)
        try:
            slot = self._slot(await self._estimate(o, d, departure), departure, w, ctx, self.store.route_id_for(o, d))
        except ProviderError as err:
            slot = self._slot(None, departure, w, ctx, None, str(err))
        return {"context": ctx.label(), "slot": slot.to_dict()}

    async def profile(self, o: Point, d: Point, day: date, weather: str = "auto") -> dict:
        """All 144 slots of a day."""
        ctx = self.context(day)
        hourly = await self.weather.day(o, day) if weather == "auto" else {}
        route_id = self.store.route_id_for(o, d)
        sem = asyncio.Semaphore(self.cfg.concurrency)

        async def one(t: time) -> SlotResult:
            departure = datetime.combine(day, t, TZ)
            w = self._weather_for(hourly, t.hour, weather)
            async with sem:
                try:
                    return self._slot(await self._estimate(o, d, departure), departure, w, ctx, route_id)
                except ProviderError as err:
                    return self._slot(None, departure, w, ctx, route_id, str(err))

        slots = await asyncio.gather(*(one(t) for t in slot_times(day)))
        ok = [s for s in slots if s.duration_min is not None]
        best = min(ok, key=lambda s: s.duration_min) if ok else None
        worst = max(ok, key=lambda s: s.duration_min) if ok else None
        return {
            "date": day.isoformat(), "context": ctx.label(), "weather_mode": weather,
            "providers": sorted({s.provider for s in ok}),
            "summary": None if not ok else {
                "best": {"departure": best.departure, "minutes": best.duration_min},
                "worst": {"departure": worst.departure, "minutes": worst.duration_min},
                "free_flow_min": min((s.free_flow_min for s in ok if s.free_flow_min), default=None)},
            "errors": sorted({s.error for s in slots if s.error}),
            "slots": [s.to_dict() for s in slots],
        }

    def scenario_dates(self, weekday: int, start: date, horizon_days: int = 400) -> list[dict]:
        """The next future date for each scenario on the given weekday (0 = Monday).

        Providers predict for a real date, so "Monday in term vs Monday in a school
        break" is answered by asking for the next real date of each kind."""
        wanted = {"school term": lambda c: c.school_term,
                  "school break": lambda c: not c.school_term and c.school_break not in (None, "summer", "weekend")
                  and not c.public_holiday,
                  "summer holidays": lambda c: c.school_break == "summer" and not c.public_holiday,
                  "public holiday": lambda c: bool(c.public_holiday)}
        found: dict[str, dict] = {}
        day = start + timedelta(days=(weekday - start.weekday()) % 7)
        end = start + timedelta(days=horizon_days)
        while day <= end and len(found) < len(wanted):
            try:
                c = self.contexts.build(day)
            except LookupError:
                break
            for name, test in wanted.items():
                if name not in found and test(c):
                    found[name] = {"scenario": name, "date": day.isoformat(), "context": c.label()}
            day += timedelta(days=7)
        return list(found.values())
