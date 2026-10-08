"""Leiria heatmap: travel time between the centre and a grid of points, for one slot.

mode "time"  = minutes from/to the centre at the chosen departure (provider prediction).
mode "ratio" = that prediction ÷ the provider's own no-traffic time for the same trip
               (TomTom noTrafficTravelTimeInSeconds). 1.0 = free flow.

One calculateRoute call per cell, not Matrix v2: the TomTom free tier has 20 000
routing requests/month but only 2 500 matrix cells, and only calculateRoute
returns the no-traffic time needed for the ratio.
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, time, timedelta

from .db import Store
from .engine import TZ
from .models import Point, RouteEstimate, snap_to_slot
from .routing import ProviderError, RoutingProvider

LEIRIA_CENTRE = Point(39.74362, -8.80705)  # Praça Rodrigues Lobo


def grid(centre: Point, radius_km: float, per_side: int) -> list[Point]:
    """per_side × per_side points covering a square of ±radius_km around centre."""
    if per_side < 2:
        raise ValueError("per_side must be >= 2")
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(centre.lat)))
    step = 2 / (per_side - 1)
    return [Point(round(centre.lat - dlat + i * step * dlat, 5), round(centre.lon - dlon + j * step * dlon, 5))
            for i in range(per_side) for j in range(per_side)]


async def heatmap(provider: RoutingProvider, store: Store, ttl: timedelta, day: date, at: time, *,
                  mode: str = "time", inbound: bool = True, centre: Point = LEIRIA_CENTRE,
                  radius_km: float = 6.0, per_side: int = 10, concurrency: int = 8) -> dict:
    if mode not in ("time", "ratio"):
        raise ValueError("mode must be 'time' or 'ratio'")
    departure = datetime.combine(day, snap_to_slot(at), TZ)
    points = [p for p in grid(centre, radius_km, per_side)
              if abs(p.lat - centre.lat) > 1e-4 or abs(p.lon - centre.lon) > 1e-4]
    sem = asyncio.Semaphore(concurrency)
    calls = 0

    async def cell(p: Point) -> RouteEstimate | None:
        nonlocal calls
        o, d = (p, centre) if inbound else (centre, p)
        hit = store.cached(provider.name, o, d, departure, ttl)
        if hit:
            return hit
        async with sem:
            try:
                e = await provider.route(o, d, departure)
            except ProviderError:
                return None  # unroutable point (field, river): leave the cell empty
            calls += 1
        store.put(o, d, departure, e)
        return e

    results = await asyncio.gather(*(cell(p) for p in points))
    cells = []
    for p, e in zip(points, results):
        if e is None:
            continue
        ratio = round(e.duration_s / e.free_flow_s, 2) if e.free_flow_s else None
        minutes = round(e.duration_s / 60, 1)
        cells.append({"lat": p.lat, "lon": p.lon, "minutes": minutes, "ratio": ratio,
                      "value": ratio if mode == "ratio" else minutes})
    return {"date": day.isoformat(), "departure": departure.strftime("%H:%M"), "mode": mode,
            "direction": "to centre" if inbound else "from centre", "centre": [centre.lat, centre.lon],
            "provider": provider.name, "source": "provider_prediction"
            + (" ÷ provider no-traffic time" if mode == "ratio" else ""),
            "cells": cells, "requested": len(points), "provider_calls": calls}
